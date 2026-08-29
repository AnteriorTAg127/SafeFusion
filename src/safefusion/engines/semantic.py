"""多模态语义检索引擎：黑白对抗检索 + 三/四信号置信度（PRD §3.1 / §3.3，T6 任务卡 + v0.2 M6）。

对单个审核输入（可选文本 + 零或多个帧）执行：
1. **向量化** —— 文本经 ``encode_texts`` 编码为单向量；多帧经 ``encode_images``
   编码后取平均（图片池化）得到单向量；图文并存时经 ``fuse_vectors(text_vec,
   [image_pool], mode)`` 融合为单一查询向量（默认 ``pool`` 平均融合）；
2. **对抗检索** —— 查询向量分别对黑库 / 白库各取 Top-K；
3. **置信度** —— 默认三信号（PRD §3.1，v0.1 无 Rerank）；``rerank_enabled``
   开启时（PRD v0.2 M6，决策 A）对黑库候选做 Rerank 二次打分，扩展为四信号。

本模块只依赖 T2/T5 的**契约**（见 开发/v0.1/分工.md「统一接口契约」），
不对未完成模块做硬 import：embedding 与 store 以鸭子类型使用
（``supports_mixed_input`` / ``encode_texts`` / ``encode_images`` /
``search(query, pool, top_k)``），T5 模块缺失时 ``fuse_vectors`` 回退到本模块
的等价实现（concat / weighted / pool 三种模式语义一致）。
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

if TYPE_CHECKING:
    from PIL import Image

try:  # T5 可能未合并：fuse_vectors 缺失时回退到本模块等价实现
    from safefusion.engines.embedding import fuse_vectors as _embedding_fuse_vectors
except ImportError:
    _embedding_fuse_vectors = None

from safefusion.engines.rerank import get_rerank_backend

_logger = logging.getLogger("safefusion.engines.semantic")

#: 降级原因码（供编排层区分「判定安全」与「语义层不可用」，避免误放行）
_REASON_EMPTY_INPUT = "empty_input"
_REASON_EMBEDDING_ERROR = "embedding_error"
_REASON_STORE_ERROR = "store_error"
_REASON_EMPTY_BLACK = "empty_black_pool"
_REASON_EMPTY_WHITE = "empty_white_pool"


class EmbeddingBackend(Protocol):
    """T5 契约的 Embedding 后端最小协议（仅静态标注，运行时不强制）。

    Attributes:
        supports_mixed_input: 后端是否支持文本+图片合并编码。

    Methods:
        encode_texts: 文本列表 → ``(n, d)`` 浮点数组（L2 归一化）。
        encode_images: 图片列表 → ``(n, d)`` 浮点数组（L2 归一化）。
    """

    supports_mixed_input: bool

    def encode_texts(self, texts: list[str]) -> np.ndarray: ...
    def encode_images(self, images: list[Any]) -> np.ndarray: ...


class VectorStoreBackend(Protocol):
    """T2 契约的向量库最小协议（仅静态标注，运行时不强制）。

    Methods:
        search: 余弦相似度 Top-K 查询，返回按分数降序的命中列表
            （元素带 ``id`` / ``score`` / ``metadata``，可属性或字典访问）。
    """

    def search(self, query: np.ndarray, pool: str, top_k: int) -> list[Any]: ...


class SemanticEngine:
    """黑白对抗语义检索引擎（T6）。

    三信号与置信度公式（PRD §3.1 三信号 v0.1 简化版，权重/阈值全部可配）：

    - 信号 1：``black_top_score``（s_bt）—— 黑库最高余弦相似度；
    - 信号 2：``black_avg``（s_ba）/ ``white_avg``（s_wa）—— 黑白库各自 Top-K 平均相似度；
    - 信号 3：``margin = s_ba − s_wa``，与 ``margin_w``（默认 0.05）比较；
    - 信号 4（PRD v0.2 M6，``rerank_enabled=True`` 时）：``rerank_black_max``
      —— 黑库候选经 Rerank 二次打分（查询-候选成对余弦）后的最高分。

    ``margin_signal = max(0, s_ba − s_wa − margin_w)``

    v0.1（Rerank 关闭）：
    ``confidence = clip(w_top·s_bt + w_margin·(margin_signal / margin_norm), 0, 1)``

    v0.2（Rerank 开启）：
    ``confidence = clip(w_top·s_bt + w_margin·(margin_signal / margin_norm)
    + w_rerank·rerank_black_max, 0, 1)``

    四信号权重取自 ``rerank_w_top / rerank_w_margin / rerank_w_rerank``
    （默认 0.5 / 0.3 / 0.2）；Rerank 失败时 ``rerank_black_max`` 记为 None
    并按 0 计入（回退三信号效果，不拖垮整次审核）。

    v0.1 默认权重：``w_top = 0.6``、``w_margin = 0.4``、``margin_norm = 0.3``、
    ``semantic_threshold = 0.67``。

    判定（PRD §3.1 + 防误判增强）：``triggered = (s_bt ≥ semantic_threshold
    且 s_bt − s_wt ≥ black_white_gap) or (margin_signal > 0)`` —— 单条黑库
    相似度再高，若与白库最高相似度差距不足（黑并未显著更接近），不判违规；
    ``category`` 取黑库最高分命中的 ``metadata.category``（缺失为 None）。

    降级政策（黑白库任一为空或输入无效时，**不做任何违规断言**）：
    返回 ``{"triggered": False, "confidence": 0.0, ...}`` 且 ``reason`` 注明原因，
    由编排层（T9）据此区分「语义层不可用」与「判定安全」。
    """

    _DEFAULT_THRESHOLDS: dict[str, Any] = {
        "semantic_threshold": 0.67,
        "margin_w": 0.05,
        "black_white_gap": 0.02,
        "top_k": 5,
        "margin_norm": 0.3,
        "weights": {"w_top": 0.6, "w_margin": 0.4},
        "fuse_mode": "pool",
        # v0.2 M6 Rerank 四信号：默认关闭（行为与 v0.1 完全一致）
        "rerank_enabled": False,
        "rerank_w_top": 0.5,
        "rerank_w_margin": 0.3,
        "rerank_w_rerank": 0.2,
        "rerank_top_k": 5,
    }

    def __init__(
        self,
        embedding: EmbeddingBackend,
        store: VectorStoreBackend,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        """初始化语义引擎。

        Args:
            embedding: T5 契约的 Embedding 后端（鸭子类型）。
            store: T2 契约的向量库（鸭子类型）。
            thresholds: 阈值字典（可与默认值部分合并），支持键：
                semantic_threshold / margin_w / top_k / margin_norm /
                weights{ w_top, w_margin } / fuse_mode，以及 v0.2 Rerank 键
                （键名对齐 config.semantic）：rerank_enabled /
                rerank_w_top / rerank_w_margin / rerank_w_rerank / rerank_top_k。
        """
        self.embedding = embedding
        self.store = store

        merged = dict(self._DEFAULT_THRESHOLDS)
        if thresholds:
            merged.update(thresholds)
        weights = dict(self._DEFAULT_THRESHOLDS["weights"])
        if isinstance(merged.get("weights"), dict):
            weights.update(merged["weights"])
        merged["weights"] = weights
        self.thresholds = merged

    def audit(
        self,
        text: str | None,
        frames: list[Image],
        ov: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对单个输入执行多模态语义检索与三信号置信度计算。

        Args:
            text: 待检文本；None 或去空白后为空视为「无文本」。
            frames: 已解码帧（PIL.Image 列表）；空列表视为「无图片」。
            ov: 请求级覆盖参数（dict），支持键 semantic_threshold / margin_w /
                top_k / margin_norm；未知键忽略。

        Returns:
            固定键字典：
            - ``triggered``: bool，是否产生违规语义信号；
            - ``confidence``: float，三信号加权置信度（clamp 到 [0,1]）；
            - ``category``: str | None，黑库最高分命中类别；
            - ``black_top``: dict | None，黑库最高分命中
              ``{"id", "score", "category", "metadata"}``；
            - ``black_avg`` / ``white_avg``: float，黑白库 Top-K 平均相似度；
            - ``rerank_black_max``: float | None，仅 ``rerank_enabled=True``
              时出现：黑库候选 Rerank 二次打分最高分；失败为 None（置信度
              中该信号按 0 计入，回退三信号效果）；关闭时无此键（与 v0.1 一致）；
            - ``reason``: str | None，正常为 None；降级时为降级原因码
              （empty_input / embedding_error / store_error /
              empty_black_pool / empty_white_pool）。
        """
        has_text = bool(text and text.strip())
        if not has_text and not frames:
            return self._degraded(_REASON_EMPTY_INPUT)

        query = self._build_query(text, frames)
        if query is None:
            return self._degraded(_REASON_EMBEDDING_ERROR)

        eff = self._effective_thresholds(ov)
        top_k = eff["top_k"]
        semantic_threshold = eff["semantic_threshold"]
        margin_w = eff["margin_w"]
        margin_norm = eff["margin_norm"]
        w_top = eff["weights"]["w_top"]
        w_margin = eff["weights"]["w_margin"]

        try:
            black_hits = self.store.search(query, "black", top_k)
            white_hits = self.store.search(query, "white", top_k)
        except Exception:
            _logger.exception("语义检索失败（store.search 异常），语义层降级")
            return self._degraded(_REASON_STORE_ERROR)

        if not black_hits:
            return self._degraded(_REASON_EMPTY_BLACK)
        if not white_hits:
            return self._degraded(_REASON_EMPTY_WHITE)

        top_hit = max(black_hits, key=self._hit_score)
        black_top_score = float(self._hit_score(top_hit))
        white_top_score = float(max(self._hit_score(h) for h in white_hits))
        black_avg = float(np.mean([self._hit_score(h) for h in black_hits]))
        white_avg = float(np.mean([self._hit_score(h) for h in white_hits]))

        margin_signal = max(0.0, black_avg - white_avg - margin_w)
        if margin_norm <= 0:
            _logger.warning("margin_norm=%s 非法（须>0），回退默认 0.3", margin_norm)
            margin_norm = 0.3

        # v0.2 M6 四信号：rerank_enabled 时对黑库候选做查询-候选成对重排，
        # rerank_black_max 参与置信度；失败返回 None（该信号按 0 计入）
        rerank_enabled = bool(eff.get("rerank_enabled", False))
        rerank_black_max: float | None = None
        if rerank_enabled:
            rerank_black_max = self._run_rerank(query, black_hits, eff)

        if rerank_enabled:
            w_top = float(eff.get("rerank_w_top", 0.5))
            w_margin = float(eff.get("rerank_w_margin", 0.3))
            w_rerank = float(eff.get("rerank_w_rerank", 0.2))
            rerank_term = w_rerank * (rerank_black_max if rerank_black_max is not None else 0.0)
            confidence_raw = (
                w_top * black_top_score + w_margin * (margin_signal / margin_norm) + rerank_term
            )
        else:
            confidence_raw = w_top * black_top_score + w_margin * (margin_signal / margin_norm)
        confidence = float(np.clip(confidence_raw, 0.0, 1.0))

        # 防误判（对齐 Node 原版）：黑库单条再相似，若与白库最高相似度差距
        # 不足 black_white_gap，说明「黑并未显著更接近」，不判违规。
        black_white_gap = float(eff.get("black_white_gap", 0.05))
        triggered = (
            black_top_score >= semantic_threshold
            and (black_top_score - white_top_score) >= black_white_gap
        ) or margin_signal > 0
        metadata = self._hit_metadata(top_hit)
        category = metadata.get("category") if isinstance(metadata, dict) else None

        result = {
            "triggered": triggered,
            "confidence": confidence,
            "category": category,
            "black_top": {
                "id": self._hit_id(top_hit),
                "score": black_top_score,
                "category": category,
                "metadata": metadata,
            },
            "black_avg": black_avg,
            "white_avg": white_avg,
            "reason": None,
        }
        # v0.2 M6：仅开启 Rerank 时暴露 rerank_black_max（关闭时与 v0.1 输出一致）
        if rerank_enabled:
            result["rerank_black_max"] = rerank_black_max
        return result

    def _build_query(self, text: str | None, frames: list[Image]) -> np.ndarray | None:
        """将文本与帧编码为单一查询向量；任一模态编码失败返回 None（上层降级）。

        多帧输出按平均池化为单向量；兼容后端返回 ``(n, d)`` 与单条 ``(d,)`` 两种形状。
        """
        try:
            image_vec = None
            if frames:
                image_arr = np.asarray(self.embedding.encode_images(frames))
                image_vec = image_arr if image_arr.ndim == 1 else image_arr.mean(axis=0)
        except Exception:
            _logger.exception("图片编码失败（encode_images 异常），语义层降级")
            return None

        try:
            text_vec = None
            if text and text.strip():
                text_arr = np.asarray(self.embedding.encode_texts([text]))
                text_vec = text_arr if text_arr.ndim == 1 else text_arr[0]
        except Exception:
            _logger.exception("文本编码失败（encode_texts 异常），语义层降级")
            return None

        if text_vec is not None and image_vec is not None:
            return self._fuse_vectors(text_vec, [image_vec])
        if text_vec is not None:
            return text_vec
        if image_vec is not None:
            return image_vec
        return None

    def _fuse_vectors(self, text_vec: np.ndarray, image_vecs: list[np.ndarray]) -> np.ndarray:
        """图文向量融合；优先调用 T5 的 ``fuse_vectors``，缺失时本地等价实现。

        多帧在进入本方法前已池化为单向量，故 ``image_vecs`` 至多一个元素；
        ``weighted`` 与 T5 实现的 ``weighted_avg`` 语义一致（加权平均），
        委托给 T5 时做模式名翻译；本地回退版本 ``weighted`` 固定 0.5/0.5 加权。
        """
        mode = self.thresholds["fuse_mode"]
        if _embedding_fuse_vectors is not None:
            delegate_mode = "weighted_avg" if mode == "weighted" else mode
            return _embedding_fuse_vectors(text_vec, image_vecs, delegate_mode)

        if not image_vecs:
            return text_vec
        image_pool = np.mean(np.asarray(image_vecs), axis=0)
        if mode == "concat":
            return np.concatenate([text_vec, image_pool])
        if mode == "weighted":
            return 0.5 * text_vec + 0.5 * image_pool
        # pool / 其他未知模式：取平均（维度不变，最安全）
        return (text_vec + image_pool) / 2.0

    def _run_rerank(
        self, query: np.ndarray, black_hits: list[Any], eff: dict[str, Any]
    ) -> float | None:
        """对黑库候选执行 Rerank 二次打分（PRD v0.2 M6，决策 A）。

        将命中转成候选字典（``{"id", "score", "metadata"}``）交给
        RerankBackend 重排，返回黑库候选最大 ``rerank_score``。
        全程异常隔离：后端 / 编码任何失败仅记录日志并返回 None，
        调用方回退三信号置信度，不拖垮整次审核（降级而不误判）。

        Args:
            query: 查询融合向量。
            black_hits: 黑库 Top-K 命中（契约 SearchHit 或等价鸭子类型）。
            eff: 生效阈值（含 rerank_top_k 候选数与 rerank 开关）。

        Returns:
            黑库候选最大 rerank_score；后端执行失败或结果为空时 None。
        """
        try:
            rerank_top_k = max(0, int(eff.get("rerank_top_k", 5)))
        except (TypeError, ValueError):
            rerank_top_k = 5
        candidates = [
            {
                "id": self._hit_id(hit),
                "score": self._hit_score(hit),
                "metadata": self._hit_metadata(hit),
            }
            for hit in black_hits
        ]
        if rerank_top_k >= 1:
            candidates = candidates[:rerank_top_k]
        try:
            backend = get_rerank_backend(eff, self.embedding)
            reranked = backend.rerank(query, candidates)
        except Exception:
            _logger.exception("Rerank 执行失败（回退三信号置信度）")
            return None
        if not reranked:
            return None
        return max(float(item.get("rerank_score", item.get("score", 0.0))) for item in reranked)

    def _effective_thresholds(self, ov: dict[str, Any] | None) -> dict[str, Any]:
        """合并默认阈值与请求级覆盖（仅接受有限数值，非法覆盖忽略并告警）。"""
        if not ov:
            return self.thresholds
        eff = dict(self.thresholds)
        for key in ("semantic_threshold", "margin_w", "margin_norm", "black_white_gap"):
            value = ov.get(key)
            if self._is_finite_number(value):
                eff[key] = float(value)
            elif key in ov:
                _logger.warning("忽略非法阈值覆盖 %s=%r", key, ov.get(key))
        if "top_k" in ov:
            try:
                eff["top_k"] = max(1, int(ov["top_k"]))
            except (TypeError, ValueError):
                _logger.warning("忽略非法 top_k 覆盖: %r", ov.get("top_k"))
        return eff

    @staticmethod
    def _is_finite_number(value: Any) -> bool:
        """是否有限数值（排除 bool；bool 是 int 的子类）。"""
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float)) and math.isfinite(value)

    @staticmethod
    def _hit_score(hit: Any) -> float:
        """兼容 NamedTuple 属性与字典两种命中表示。"""
        if isinstance(hit, dict):
            return float(hit["score"])
        return float(hit.score)

    @staticmethod
    def _hit_metadata(hit: Any) -> dict[str, Any]:
        if isinstance(hit, dict):
            return dict(hit.get("metadata") or {})
        metadata = getattr(hit, "metadata", None) or {}
        return dict(metadata)

    @staticmethod
    def _hit_id(hit: Any) -> str:
        value = hit.get("id") if isinstance(hit, dict) else getattr(hit, "id", None)
        return "" if value is None else str(value)

    @staticmethod
    def _degraded(reason: str) -> dict[str, Any]:
        """降级结果：不做违规断言，reason 注明原因（编排层据此路由）。"""
        return {
            "triggered": False,
            "confidence": 0.0,
            "category": None,
            "black_top": None,
            "black_avg": 0.0,
            "white_avg": 0.0,
            "reason": reason,
        }
