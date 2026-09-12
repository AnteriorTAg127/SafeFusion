"""M6 Rerank 重排后端模块（PRD v0.2 §2 M6，决策 A：复用 CLIP 向量成对重排）。

对语义检索返回的黑库候选做**查询-候选成对重排**：复用既有 Embedding 后端
（默认本地 Chinese-CLIP）对候选内容做**独立二次编码**（区别于入库时的一次
编码），再与查询融合向量成对余弦相似度生成 ``rerank_score``，供语义引擎
组成四信号置信度（PRD v0.2 M6）。

本模块提供三层接口：

- :class:`RerankBackend` —— 重排后端协议；docstring 注明可替换为专用
  重排模型（如 BGE-reranker、云端 Rerank API），只需实现相同 ``rerank``
  签名即可接入，无需改动语义引擎；
- :class:`NoneRerank` —— 空后端（默认禁用路径），原样返回候选；
- :class:`LocalClipRerank` —— 决策 A 的本地实现（零新模型下载）；
- :func:`get_rerank_backend` —— 按 ``rerank_enabled`` 开关路由的工厂。

候选契约（对齐 :class:`~safefusion.storage.vector_store.SearchHit`）：
``{"id", "score", "metadata"}``，其中 ``metadata`` 携带候选原始内容：

- ``text``：文本候选，重排时经 ``encode_texts`` 二次编码；
- 图像内容键（``image`` / ``image_path`` / ``images``，值为 PIL.Image /
  路径字符串）：图像候选，经 ``encode_images`` 二次编码；路径字符串经
  Pillow 打开，文件缺失 / 损坏时保底原 score；
- 两者皆无的候选跳过二次编码，``rerank_score`` 保底取原 ``score``。

失败隔离：单次批量重编码异常仅记录日志并保底原 score，不拖垮整批候选。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import httpx
import numpy as np
from PIL import Image

from safefusion.engines.embedding import BaseEmbedding, l2_normalize
from safefusion.engines.failover import (
    AllProvidersFailed,
    FailoverCircuit,
    call_with_failover,
)

_logger = logging.getLogger("safefusion.engines.rerank")

#: 远程 Rerank Key 兜底环境变量名（对齐 config.py 约定）
_STANDARD_RERANK_KEY_ENV = "SAFEFUSION_RERANK_API_KEY"

#: metadata 中表示图像内容的键（值为 PIL.Image / 路径字符串 / bytes）
_IMAGE_META_KEYS: tuple[str, ...] = ("image", "image_path", "images")

#: 路径文件不存在 / 不可读的兜底提示（路径可能为 URL 场景由云端后端处理）
_IMAGE_PATH_ERROR_HINT = "（若路径不可读且确实需要重排，可改用云端 Embedding 后端）"


class RerankBackend(Protocol):
    """Rerank 重排后端协议（M6）。

    Args:
        query_vec: 查询融合向量（一维数组）。
        candidates: 黑库候选列表，元素为 ``{"id", "score", "metadata"}``。

    Returns:
        含 ``rerank_score`` 的候选列表，按 ``rerank_score`` 降序。
        实现可原地或返回新列表；调用方只读结果。

    Note:
        本协议刻意保持最小签名，便于**替换专用重排模型**（如 BGE-reranker /
        云端 Rerank API）：专用模型只需实现同样的 ``rerank`` 即可无缝接入，
        语义引擎无需改动。实现可为同步 ``def`` 或 ``async def``（鸭子类型，
        当前语义引擎按同步结果消费；异步后端由调用方自行 await）。
    """

    def rerank(
        self, query_vec: np.ndarray, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


class NoneRerank:
    """空重排后端（默认禁用路径）：不做任何重排，原样返回候选。"""

    name = "none"

    def rerank(
        self, query_vec: np.ndarray, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """不重排：返回候选副本（与 v0.1 检索结果一致）。"""
        return list(candidates)


class LocalClipRerank:
    """本地 CLIP 向量重排后端（决策 A，零新模型下载）。

    不使用入库时的预计算向量，而是对每个候选做**独立二次编码**（与查询
    编码同一 Embedding 后端），再与查询融合向量成对余弦相似度作为
    ``rerank_score``：

    - 候选 metadata 含非空 ``text`` → 走 ``encode_texts``（文本类型）；
    - 候选 metadata 含图像内容键（``image`` / ``image_path`` / ``images``）
      → 走 ``encode_images``（图像类型）；
    - 两者皆无 → 跳过二次编码，``rerank_score`` 保底取原 ``score``；
    - 图像路径文件缺失 / 损坏 → 单条保底原 score，不拖垮整批。

    同类型候选合并为单次批量编码（batch，PRD v0.2 M6 性能约定）；
    成对相似度计算前对查询与候选向量做 L2 归一化（防御；Embedding 后端
    契约已要求输出归一化，此处叠加无损）。

    Args:
        embedding: 与查询向量同一 Embedding 后端（``encode_texts`` /
            ``encode_images``），用于候选二次编码。
    """

    name = "local_clip"

    def __init__(self, embedding: BaseEmbedding) -> None:
        self.embedding = embedding

    def rerank(
        self, query_vec: np.ndarray, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """对候选二次编码并成对余弦重排，返回按 ``rerank_score`` 降序的新列表。

        Args:
            query_vec: 查询融合向量（一维数组）。
            candidates: 黑库候选 ``{id, score, metadata}``（不会原地修改）。

        Returns:
            含 ``rerank_score`` 的候选列表（浅拷贝），降序排列。
        """
        if not candidates:
            return []
        query = l2_normalize(np.asarray(query_vec, dtype=np.float32))

        # 先按类型分组：文本 / 图像各合并为一次批量编码
        text_items: list[tuple[int, str]] = []
        image_items: list[tuple[int, Any]] = []
        for idx, cand in enumerate(candidates):
            metadata = cand.get("metadata") or {}
            text = metadata.get("text")
            if isinstance(text, str) and text.strip():
                text_items.append((idx, text))
                continue
            image = self._resolve_image(metadata)
            if image is not None:
                image_items.append((idx, image))

        # 二次编码：批次内单条失败整体保底原 score（记录日志，不中断整批）
        new_vectors: dict[int, np.ndarray] = {}
        self._encode_text_batch(text_items, new_vectors)
        self._encode_image_batch(image_items, new_vectors)

        out: list[dict[str, Any]] = []
        for idx, cand in enumerate(candidates):
            item = dict(cand)  # 浅拷贝，避免污染调用方候选列表
            vec = new_vectors.get(idx)
            if vec is not None:
                item["rerank_score"] = float(
                    np.dot(query, l2_normalize(np.asarray(vec, dtype=np.float32)))
                )
            else:
                item["rerank_score"] = float(item.get("score", 0.0))
            out.append(item)
        out.sort(key=lambda item: item["rerank_score"], reverse=True)
        return out

    def _encode_text_batch(
        self, text_items: list[tuple[int, str]], new_vectors: dict[int, np.ndarray]
    ) -> None:
        """批量二次编码文本候选；异常整体保底原 score。"""
        if not text_items:
            return
        try:
            arr = np.asarray(self.embedding.encode_texts([t for _, t in text_items]))
        except Exception:
            _logger.exception("Rerank 文本二次编码失败（候选保底原 score）")
            return
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        for (idx, _), vec in zip(text_items, arr, strict=True):
            new_vectors[idx] = vec

    def _encode_image_batch(
        self, image_items: list[tuple[int, Any]], new_vectors: dict[int, np.ndarray]
    ) -> None:
        """批量二次编码图像候选；异常整体保底原 score。"""
        if not image_items:
            return
        try:
            arr = np.asarray(self.embedding.encode_images([img for _, img in image_items]))
        except Exception:
            _logger.exception("Rerank 图像二次编码失败（候选保底原 score）")
            return
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        for (idx, _), vec in zip(image_items, arr, strict=True):
            new_vectors[idx] = vec

    @staticmethod
    def _resolve_image(metadata: dict[str, Any]) -> Any | None:
        """从 metadata 解析图像内容；无法解码返回 None（该候选保底原 score）。

        路径字符串经 Pillow 打开；PIL.Image / 其它对象原样透传给
        ``encode_images``（后端按自身契约处理，如云端 data URI）。
        """
        for key in _IMAGE_META_KEYS:
            value = metadata.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                # 仅当路径文件存在且可打开才视为可编码；否则保底原 score
                if not os.path.isfile(value):
                    _logger.warning(
                        "Rerank 候选图像路径不存在（key=%s, path=%s）%s",
                        key,
                        value,
                        _IMAGE_PATH_ERROR_HINT,
                    )
                    return None
                try:
                    return Image.open(value)
                except Exception:
                    _logger.warning(
                        "Rerank 候选图像路径不可读（key=%s, path=%s），保底原 score",
                        key,
                        value,
                    )
                    return None
            return value
        return None


class CloudRerank:
    """远程 ReRanker 后端（PRD v0.4.0 M2）。

    ``POST {base_url}/rerank``，请求体：
    ``{"model": ..., "query": <原始文本>, "documents": [候选文本...], "top_n": N}``。
    响应兼容两种形态：
    ``{"results":[{"index":0,"relevance_score":0.9}]}`` 或
    ``{"data":[{"index":0,"score":0.9}]}``。

    候选无文本（如图片候选）不发送远程 API，保留原 ``score`` 保底。
    密钥只从环境变量读取（``api_key_env`` 指定，兜底
    ``SAFEFUSION_RERANK_API_KEY``）。
    """

    name = "cloud"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        self.base_url: str = str(cfg.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("云端 ReRanker 需要配置 rerank.providers[].base_url")
        self.model: str = str(cfg.get("model") or "")
        if not self.model:
            raise ValueError("云端 ReRanker 需要配置 rerank.providers[].model")
        api_key_env = cfg.get("api_key_env") or _STANDARD_RERANK_KEY_ENV
        self._api_key: str | None = os.environ.get(str(api_key_env)) or os.environ.get(
            _STANDARD_RERANK_KEY_ENV
        )
        if not self._api_key and not cfg.get("allow_no_key", False):
            raise RuntimeError(
                "云端 ReRanker 未配置 API Key（密钥只允许来自环境变量）。"
                f"请设置环境变量 {api_key_env}（或 SAFEFUSION_RERANK_API_KEY）"
            )
        timeout = float(cfg.get("timeout", 10.0))
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def rerank(
        self, query_vec: np.ndarray, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """远程重排：只发送含文本的候选，其余保底原 score。"""
        if not candidates:
            return []
        documents: list[str] = []
        indices: list[int] = []
        for idx, cand in enumerate(candidates):
            metadata = cand.get("metadata") or {}
            text = metadata.get("text")
            if isinstance(text, str) and text.strip():
                documents.append(text)
                indices.append(idx)
        out: list[dict[str, Any]] = []
        if not documents:
            for cand in candidates:
                item = dict(cand)
                item["rerank_score"] = float(item.get("score", 0.0))
                out.append(item)
            out.sort(key=lambda item: item["rerank_score"], reverse=True)
            return out

        query_text = ""
        if candidates and isinstance(candidates[0].get("metadata"), dict):
            query_text = str(candidates[0]["metadata"].get("_query_text") or "")
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query_text or self._query_text_from_vector(query_vec),
            "documents": documents,
            "top_n": len(documents),
        }
        resp = self._client.post(f"{self.base_url}/rerank", json=payload)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or data.get("data") or []
        score_by_index: dict[int, float] = {}
        for item in results:
            idx = int(item.get("index", -1))
            score = item.get("relevance_score", item.get("score"))
            if idx >= 0 and score is not None:
                score_by_index[idx] = float(score)
        for idx, cand in enumerate(candidates):
            item = dict(cand)
            if idx in indices and idx in score_by_index:
                item["rerank_score"] = score_by_index[idx]
            else:
                item["rerank_score"] = float(item.get("score", 0.0))
            out.append(item)
        out.sort(key=lambda item: item["rerank_score"], reverse=True)
        return out

    @staticmethod
    def _query_text_from_vector(query_vec: np.ndarray) -> str:
        """查询向量无法直接还原为文本；远程协议需要 query 文本。

        当前由 MultiRerank 在调用前把原始 query 文本注入候选元数据
        ``{"_query_text": ...}``；此方法尝试从候选元数据读取。
        若无法获取则返回空串（服务端可能报错，由上层降级）。
        """
        return ""

    def close(self) -> None:
        """关闭底层 httpx 客户端连接。"""
        self._client.close()


class MultiRerank:
    """多提供者 Rerank 后端（PRD v0.4.0 M3）。

    按优先级顺序尝试多个 Rerank 提供者；单个提供者抛异常时切换下一个；
    全部失败返回「候选原 score 保底」列表（不拖垮语义层）。

    Args:
        providers: ``(name, priority, backend, failover)`` 提供者列表。
        active_provider: 手动指定首选提供者名称。
        query_text: 原始查询文本（远程 ReRanker 需要）；为 None 时本地 CLIP
            仍可正常工作。
    """

    name = "multi"

    def __init__(
        self,
        providers: list[tuple[str, int, RerankBackend, FailoverCircuit]],
        active_provider: str | None = None,
        query_text: str | None = None,
    ) -> None:
        self._providers = providers
        self._active = active_provider
        self._query_text = query_text

    def rerank(
        self, query_vec: np.ndarray, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """依次尝试各提供者；全部失败保底原 score。"""
        # 远程协议需要 query 文本：把原始文本注入候选元数据，CloudRerank 读取
        prepared: list[dict[str, Any]] = []
        for cand in candidates:
            item = dict(cand)
            metadata = dict(item.get("metadata") or {})
            if self._query_text:
                metadata["_query_text"] = self._query_text
            item["metadata"] = metadata
            prepared.append(item)

        try:
            return call_with_failover(
                self._providers,
                lambda backend: backend.rerank(query_vec, prepared),
                active=self._active,
                logger=_logger,
                label="Rerank 提供者",
            )
        except AllProvidersFailed as exc:
            # 全部失败：保底原 score，不让重排失败拖垮语义层
            _logger.warning(
                "全部 Rerank 提供者失败，候选保底原 score（最后错误: %s）", exc.__cause__
            )
            fallback: list[dict[str, Any]] = []
            for cand in prepared:
                item = dict(cand)
                item["rerank_score"] = float(item.get("score", 0.0))
                fallback.append(item)
            fallback.sort(key=lambda item: item["rerank_score"], reverse=True)
            return fallback


def get_rerank_backend(config: Any, embedding: BaseEmbedding) -> RerankBackend:
    """Rerank 后端工厂。

    Args:
        config: semantic 分组配置（含 ``rerank_enabled`` 开关；接受 dict 或
            pydantic 模型，如 ``SemanticConfig``）。``rerank_enabled=True``
            返回 :class:`LocalClipRerank`（决策 A 默认），否则（含缺省）返回
            :class:`NoneRerank`（禁用路径，与 v0.1 行为一致）。
        embedding: Embedding 后端（LocalClipRerank 二次编码用）。

    Returns:
        与开关匹配的 RerankBackend 实例。
    """
    cfg = _as_dict(config)
    if cfg.get("rerank_enabled", False):
        return LocalClipRerank(embedding)
    return NoneRerank()


def _as_dict(value: Any) -> dict[str, Any]:
    """将 dict 或 pydantic BaseModel 归一为普通 dict（与 embedding._as_dict 同语义）。"""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"期望 dict 或 pydantic 配置模型，实际为 {type(value).__name__}")


def build_rerank_backend(
    rerank_cfg: Any,
    semantic_cfg: Any,
    embedding: BaseEmbedding,
) -> RerankBackend:
    """按完整 Rerank 配置构造后端（PRD v0.4.0 M2/M3）。

    Args:
        rerank_cfg: ``RerankConfig`` pydantic 模型或同构 dict（含 providers /
            active_provider / failover）。
        semantic_cfg: ``SemanticConfig`` 或含 ``rerank_enabled`` 的 dict。
        embedding: Embedding 后端（本地重排候选二次编码用）。

    Returns:
        - ``semantic.rerank_enabled=False`` → :class:`NoneRerank`；
        - ``rerank.providers`` 为空 → 回退 :class:`LocalClipRerank`；
        - providers 非空 → :class:`MultiRerank`（本地/云端按列表路由）。
    """
    rerank_dict = _as_dict(rerank_cfg)
    semantic_dict = _as_dict(semantic_cfg)
    if not semantic_dict.get("rerank_enabled", False):
        return NoneRerank()

    providers_cfg = rerank_dict.get("providers") or []
    if not providers_cfg:
        return LocalClipRerank(embedding)

    failover_cfg = rerank_dict.get("failover") or {}
    active = rerank_dict.get("active_provider")
    built: list[tuple[str, int, RerankBackend, FailoverCircuit]] = []
    for provider in providers_cfg:
        p = dict(provider)
        name = str(p.get("name") or f"provider-{len(built) + 1}")
        priority = int(p.get("priority", 1))
        provider_type = str(p.get("type") or "local").lower()
        if provider_type == "cloud":
            backend: RerankBackend = CloudRerank(p)
        else:
            backend = LocalClipRerank(embedding)
        built.append((name, priority, backend, FailoverCircuit(**failover_cfg)))
    return MultiRerank(built, active_provider=active)
