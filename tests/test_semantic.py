"""语义引擎测试：三信号置信度公式 / triggered 判定 / ov 覆盖 / 降级原因码。

公式（PRD §3.1 三信号 v0.1）：
    margin_signal = max(0, black_avg - white_avg - margin_w)
    confidence = clip(w_top*black_top + w_margin*(margin_signal/margin_norm), 0, 1)
    triggered = (black_top >= semantic_threshold) or (margin_signal > 0)
场景素材来自 T6 自检脚本（开发/v0.1/tmp/t6_check.py）。
"""

from __future__ import annotations

import numpy as np
import pytest

from safefusion.engines.semantic import SemanticEngine

from .fakes import FakeEmbedding, FakeStore, Hit


def unit(angle_deg: float) -> np.ndarray:
    a = np.radians(angle_deg)
    return np.array([np.cos(a), np.sin(a)], dtype=np.float32)


def make_engine(
    black_cos: list[float],
    white_cos: list[float],
    thresholds: dict | None = None,
    black_categories: list[str] | None = None,
    raise_encode: bool = False,
) -> SemanticEngine:
    store = FakeStore.from_cosines(black_cos, white_cos, black_categories)
    emb = FakeEmbedding(
        texts={"内容": unit(0.0)}, images=np.zeros((0, 2)), raise_encode=raise_encode
    )
    return SemanticEngine(emb, store, thresholds)


class TestConfidenceFormula:
    """置信度按公式独立计算比对（而非硬编码快照）。"""

    def test_black_dominant_high_confidence(self) -> None:
        eng = make_engine(
            black_cos=[0.9, 0.775, 0.775, 0.775, 0.775],
            white_cos=[0.2, 0.2, 0.2, 0.2, 0.2],
            black_categories=["色情", "违规", "违规", "违规", "违规"],
        )
        out = eng.audit("内容", [])
        black_avg = 0.8
        margin = max(0.0, black_avg - 0.2 - 0.05)  # 0.55
        expected = min(1.0, 0.6 * 0.9 + 0.4 * (margin / 0.3))
        assert out["triggered"] is True
        assert out["confidence"] == pytest.approx(expected)
        assert out["category"] == "色情"
        assert out["black_top"]["score"] == pytest.approx(0.9)
        assert out["black_avg"] == pytest.approx(black_avg)
        assert out["white_avg"] == pytest.approx(0.2)
        assert out["reason"] is None

    def test_white_dominant_low_confidence(self) -> None:
        eng = make_engine(
            black_cos=[0.3, 0.3, 0.3],
            white_cos=[0.9, 0.9, 0.9],
            black_categories=["违规", "违规", "违规"],
        )
        out = eng.audit("内容", [])
        assert out["triggered"] is False
        assert out["confidence"] == pytest.approx(0.6 * 0.3)
        # category 取自黑库最高分命中（与 triggered 无关）
        assert out["category"] == "违规"

    def test_margin_below_margin_w_no_signal(self) -> None:
        eng = make_engine(black_cos=[0.5, 0.5], white_cos=[0.5, 0.5])
        out = eng.audit("内容", [])
        # 黑均-白均=0 < margin_w=0.05 → margin_signal=0
        assert out["triggered"] is False
        assert out["confidence"] == pytest.approx(0.6 * 0.5)

    def test_confidence_clipped_to_one(self) -> None:
        eng = make_engine(black_cos=[0.99, 0.99, 0.99], white_cos=[0.0, 0.0, 0.0])
        out = eng.audit("内容", [])
        assert out["confidence"] == 1.0

    def test_triggered_boundary_at_threshold(self) -> None:
        eng = make_engine(
            black_cos=[0.8, 0.8],
            white_cos=[0.8, 0.8],  # margin=0 → 只看 black_top
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng.audit("内容", [])["triggered"] is True  # black_top == threshold
        eng2 = make_engine(
            black_cos=[0.79, 0.79],
            white_cos=[0.79, 0.79],
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng2.audit("内容", [])["triggered"] is False


class TestOverrideCoverage:
    """请求级 ov 覆盖阈值（非法值忽略并告警）。"""

    def test_ov_raises_threshold_flips_triggered(self) -> None:
        eng = make_engine(black_cos=[0.9, 0.9], white_cos=[0.2, 0.2])
        # 默认阈值下 triggered=True，但 ov 拉高 threshold + margin_w 后翻转
        ov = {"semantic_threshold": 0.95, "margin_w": 0.9}
        out = eng.audit("内容", [], ov)
        assert out["triggered"] is False

    def test_ov_margin_w_lowers_signal(self) -> None:
        eng = make_engine(black_cos=[0.9, 0.7, 0.7, 0.7, 0.7], white_cos=[0.2, 0.2, 0.2, 0.2, 0.2])
        out = eng.audit("内容", [], {"margin_w": 0.5})
        # black_avg=0.74, white_avg=0.2 → margin=0.54-0.5=0.04 > 0 → margin_signal>0
        margin = max(0.0, 0.74 - 0.2 - 0.5)
        expected = min(1.0, 0.6 * 0.9 + 0.4 * (margin / 0.3))
        assert out["confidence"] == pytest.approx(expected)

    def test_illegal_ov_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        eng = make_engine(black_cos=[0.9, 0.9], white_cos=[0.2, 0.2])
        with caplog.at_level(logging.WARNING, logger="safefusion.engines.semantic"):
            out = eng.audit("内容", [], {"semantic_threshold": "high", "top_k": "x"})
        assert out["triggered"] is True  # 非法覆盖被忽略，回到默认阈值
        assert "忽略非法" in caplog.text

    def test_ov_top_k_bounds(self) -> None:
        eng = make_engine(black_cos=[0.9, 0.5], white_cos=[0.2, 0.2])
        eng.audit("内容", [], {"top_k": 0})  # 拉到 1，不崩
        assert eng.store.search_calls[-1][1] == 1


class TestMultiModalInputs:
    """文本 / 单图 / 多帧池化 / 图文混合均产出合法结果。"""

    def test_text_only(self) -> None:
        eng = make_engine(black_cos=[0.7], white_cos=[0.3])
        out = eng.audit("内容", [])
        assert out["reason"] is None

    def test_image_only(self) -> None:
        store = FakeStore(
            black=[Hit("b0", 0.7, {"category": "违规"})],
            white=[Hit("w0", 0.2, {})],
        )
        emb = FakeEmbedding(texts={}, images=np.array([[1.0, 0.0]], dtype=np.float32))
        eng = SemanticEngine(emb, store, {})
        out = eng.audit(None, [object()])
        assert out["reason"] is None
        assert out["black_top"]["id"] == "b0"

    def test_multi_frame_pooling_and_mixed(self) -> None:
        store = FakeStore(
            black=[Hit("b0", 0.8, {"category": "违规"})],
            white=[Hit("w0", 0.2, {})],
        )
        emb = FakeEmbedding(
            texts={"标题": unit(0.0)},
            images=np.stack([unit(20.0), unit(40.0)]),
        )
        eng = SemanticEngine(emb, store, {})
        # 混合 path 走 fuse（text vec + 池化图 vec）
        out = eng.audit("标题", [object(), object()])
        assert out["reason"] is None
        assert out["black_top"]["score"] == pytest.approx(0.8)


class TestDegradation:
    """降级原因码：不可用 ≠ 安全（编排层据此路由）。"""

    def test_empty_input(self) -> None:
        eng = make_engine(black_cos=[0.9], white_cos=[0.9])
        out = eng.audit(None, [])
        assert out["triggered"] is False
        assert out["confidence"] == 0.0
        assert out["reason"] == "empty_input"

    def test_embedding_error(self) -> None:
        eng = make_engine(black_cos=[0.9], white_cos=[0.2], raise_encode=True)
        out = eng.audit("内容", [])
        assert out["reason"] == "embedding_error"

    def test_store_error(self) -> None:
        store = FakeStore(black=[("b0", 0.9, {})], white=[("w0", 0.2, {})], fail_on_search=True)
        emb = FakeEmbedding(texts={"内容": unit(0.0)}, images=np.zeros((0, 2)))
        eng = SemanticEngine(emb, store, {})
        out = eng.audit("内容", [])
        assert out["reason"] == "store_error"

    def test_empty_black_pool(self) -> None:
        store = FakeStore(black=[], white=[Hit("w0", 0.5, {})])
        emb = FakeEmbedding(texts={"内容": unit(0.0)}, images=np.zeros((0, 2)))
        eng = SemanticEngine(emb, store, {})
        out = eng.audit("内容", [])
        assert out["reason"] == "empty_black_pool"

    def test_empty_white_pool(self) -> None:
        store = FakeStore(black=[Hit("b0", 0.5, {})], white=[])
        emb = FakeEmbedding(texts={"内容": unit(0.0)}, images=np.zeros((0, 2)))
        eng = SemanticEngine(emb, store, {})
        out = eng.audit("内容", [])
        assert out["reason"] == "empty_white_pool"
