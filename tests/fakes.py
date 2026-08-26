"""测试用假组件（T13 测试专项）。

集中提供受控的 Embedding / 向量库 / LLM / 关键词 / 语义引擎桩，供各测试模块
按组件契约注入。全部为纯桩：无网络、无模型、无 torch/transformers 依赖。
"""

from __future__ import annotations

from typing import Any, NamedTuple

import numpy as np


class Hit(NamedTuple):
    """模拟 storage.vector_store.SearchHit：id / score / metadata。"""

    id: str
    score: float
    metadata: dict[str, Any]


class FakeEmbedding:
    """受控 Embedding 后端：文本按查表返回、图片返回给定矩阵；可注入编码异常。

    Attributes:
        supports_mixed_input: 固定 True（供语义引擎探测契约）。
        raise_encode: 置 True 后 encode_* 抛 RuntimeError（编码失败降级路径）。
    """

    supports_mixed_input = True

    def __init__(
        self,
        texts: dict[str, np.ndarray] | None = None,
        images: np.ndarray | None = None,
        *,
        raise_encode: bool = False,
    ) -> None:
        self._texts: dict[str, np.ndarray] = texts or {}
        self._images = images
        self.raise_encode = raise_encode
        self.text_calls = 0
        self.image_calls = 0

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        self.text_calls += 1
        if self.raise_encode:
            raise RuntimeError("fake embedding encode_texts 失败")
        return np.stack([self._texts[t] for t in texts])

    def encode_images(self, images: list[Any]) -> np.ndarray:
        self.image_calls += 1
        if self.raise_encode:
            raise RuntimeError("fake embedding encode_images 失败")
        return np.asarray(self._images)


class FakeStore:
    """受控向量库：search 返回预先配置的命中（按得分降序取 top_k）。

    命中元素为 ``Hit(id, score, metadata)``；可选 ``fail_on_search`` 注入
    store.search 异常（语义层 store_error 降级路径），并记录每次检索的
    (pool, top_k) 供断言。

    Attributes:
        fail_on_search: 置 True 后 search 抛 RuntimeError。
        search_calls: 每次检索追加 ``(pool, top_k)``。
    """

    def __init__(
        self,
        black: list[Hit] | None = None,
        white: list[Hit] | None = None,
        *,
        fail_on_search: bool = False,
    ) -> None:
        self._items: dict[str, list[Hit]] = {
            "black": list(black or []),
            "white": list(white or []),
        }
        self.fail_on_search = fail_on_search
        self.search_calls: list[tuple[str, int]] = []

    def search(self, query: np.ndarray, pool: str, top_k: int) -> list[Hit]:
        self.search_calls.append((pool, top_k))
        if self.fail_on_search:
            raise RuntimeError("fake store search 失败")
        hits = sorted(self._items.get(pool, []), key=lambda h: -h.score)[: max(top_k, 0)]
        return [Hit(h.id, h.score, dict(h.metadata)) for h in hits]

    @classmethod
    def from_cosines(
        cls,
        black_cos: list[float],
        white_cos: list[float],
        black_categories: list[str] | None = None,
    ) -> FakeStore:
        """按目标余弦值构造黑白库命中（查询视为与条目相似度恰为目标值）。

        简化构造：直接以 ``score=cos`` 生成命中，避免在测试里摆弄真实向量与
        点积；语义引擎只用 ``hit.score`` / ``hit.metadata`` / ``hit.id``。
        """
        black = [
            Hit(
                id=f"b{i}",
                score=c,
                metadata={"category": (black_categories or ["" for _ in black_cos])[i]},
            )
            for i, c in enumerate(black_cos)
        ]
        white = [
            Hit(id=f"w{i}", score=c, metadata={"category": "安全"}) for i, c in enumerate(white_cos)
        ]
        return cls(black, white)


class FakeLLM:
    """受控 LLM 客户端：judge 按预置 verdict 返回或抛指定异常。

    Attributes:
        verdict: judge 返回的判定 dict（None 表示「失败/回退」）。
        exc: 若非 None，judge 每次调用抛该异常（调用异常路径）。
        available: 模拟 LLMClient.available（False 时编排层直接跳过）。
        judge_calls: judge 调用计数。
        last_animated / last_images / last_text: 最近一次 judge 的动图标记与
            入参（供编排层多帧断言，v0.2 M3）。
    """

    def __init__(
        self,
        verdict: dict[str, Any] | None = None,
        exc: Exception | None = None,
        available: bool = True,
    ) -> None:
        self.verdict = verdict
        self.exc = exc
        self.available = available
        self.judge_calls = 0
        self.last_animated: bool = False
        self.last_images: list[Any] = []
        self.last_text: str | None = None

    async def judge(
        self,
        text: str | None,
        images: list[Any],
        context: str | None,
        *,
        cache_hint: str | None = None,
        animated: bool = False,
    ) -> dict[str, Any] | None:
        self.judge_calls += 1
        self.last_text = text
        self.last_images = list(images)
        self.last_animated = animated
        if self.exc is not None:
            raise self.exc
        return self.verdict


class FakeKeywordEngine:
    """受控关键词引擎：scan 恒返回预置命中列表（默认空列表）。"""

    def __init__(self, hits: list[Any] | None = None, loaded: bool = True) -> None:
        self._hits = list(hits or [])
        self.loaded = loaded
        self.scan_calls = 0

    def scan(self, text: str) -> list[Any]:
        self.scan_calls += 1
        return list(self._hits)


class FakeSemantic:
    """受控语义引擎：audit 恒返回预置结果（默认降级结果）；可注入异常。

    Attributes:
        last_text / last_frames / last_ov: 最近一次 audit 的入参（便于断言 ov 透传）。
    """

    def __init__(self, result: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.result = result or {
            "triggered": False,
            "confidence": 0.0,
            "category": None,
            "black_top": None,
            "black_avg": 0.0,
            "white_avg": 0.0,
            "reason": "semantic_disabled",
        }
        self.exc = exc
        self.audit_calls = 0
        self.last_text: str | None = None
        self.last_frames: list[Any] = []
        self.last_ov: dict[str, Any] | None = None

    def audit(
        self,
        text: str | None,
        frames: list[Any],
        ov: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.audit_calls += 1
        self.last_text = text
        self.last_frames = frames
        self.last_ov = ov
        if self.exc is not None:
            raise self.exc
        return dict(self.result)
