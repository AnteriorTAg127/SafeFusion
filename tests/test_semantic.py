"""语义引擎测试：三/四信号置信度公式 / triggered 判定 / ov 覆盖 / 降级原因码 / M6 Rerank。

公式（PRD §3.1 三信号 v0.1；PRD v0.2 M6 四信号）：
    margin_signal = max(0, black_avg - white_avg - margin_w)
    三信号: confidence = clip(w_top*black_top + w_margin*(margin_signal/margin_norm), 0, 1)
    四信号: confidence = clip(w_top*black_top + w_margin*(margin_signal/margin_norm)
                              + w_rerank*rerank_black_max, 0, 1)
    triggered = (black_top >= semantic_threshold) or (margin_signal > 0)
场景素材来自 T6 自检脚本（开发/v0.1/tmp/t6_check.py）与 T19 任务卡
（开发/v0.2/分工.md：RerankBackend 协议 + LocalClipRerank 二次编码重排）。
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from safefusion.config import SemanticConfig
from safefusion.engines.rerank import LocalClipRerank, NoneRerank, get_rerank_backend
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
        # 黑白同分（gap=0）→ 黑未显著更接近，不触发（防误判增强）
        eng = make_engine(
            black_cos=[0.8, 0.8],
            white_cos=[0.8, 0.8],  # margin=0 且 gap=0
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng.audit("内容", [])["triggered"] is False  # 黑白同分不判违规
        eng2 = make_engine(
            black_cos=[0.79, 0.79],
            white_cos=[0.79, 0.79],
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng2.audit("内容", [])["triggered"] is False

    def test_triggered_requires_black_white_gap(self) -> None:
        # 黑顶分超阈值但白顶分几乎相同（gap 不足）→ 不触发
        eng = make_engine(
            black_cos=[0.82, 0.8],
            white_cos=[0.81, 0.8],  # black_top=0.82, white_top=0.81, gap=0.01 < 0.02
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng.audit("内容", [])["triggered"] is False
        # 黑顶分显著高于白顶分（gap 足够）→ 触发
        eng2 = make_engine(
            black_cos=[0.82, 0.8],
            white_cos=[0.6, 0.6],  # gap=0.22 >= 0.02
            thresholds={"semantic_threshold": 0.8, "weights": {"w_top": 1.0, "w_margin": 0.0}},
        )
        assert eng2.audit("内容", [])["triggered"] is True

    def test_triggered_gap_configurable(self) -> None:
        # black_white_gap 可配置：调大后原触发场景翻转
        eng = make_engine(
            black_cos=[0.85, 0.85],
            white_cos=[0.8, 0.8],  # gap=0.05，默认阈值 0.67 超
            thresholds={
                "semantic_threshold": 0.67,
                "black_white_gap": 0.1,
                "weights": {"w_top": 1.0, "w_margin": 0.0},
            },
        )
        # gap=0.05 < 0.1 → 不触发
        assert eng.audit("内容", [])["triggered"] is False
        eng2 = make_engine(
            black_cos=[0.95, 0.85],
            white_cos=[0.8, 0.8],  # gap=0.15 >= 0.1
            thresholds={
                "semantic_threshold": 0.67,
                "black_white_gap": 0.1,
                "weights": {"w_top": 1.0, "w_margin": 0.0},
            },
        )
        assert eng2.audit("内容", [])["triggered"] is True


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


class TestRerankBackend:
    """Rerank 后端（T19）：二次编码方向正确 / 无内容保底 / 开关路由。"""

    def test_rerank_score_direction_and_order(self) -> None:
        # 原 score 与二次编码相似度方向相反：b0 原分低但重编码后更接近查询
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(5.0), "远": unit(80.0)},
            images=np.zeros((0, 2)),
        )
        backend = LocalClipRerank(emb)
        candidates = [
            {"id": "b1", "score": 0.9, "metadata": {"category": "违规", "text": "远"}},
            {"id": "b0", "score": 0.6, "metadata": {"category": "色情", "text": "近"}},
        ]
        out = backend.rerank(unit(0.0), candidates)
        # 查询=unit(0°)，「近」=unit(5°) 余弦 ≈ cos5° 高于「远」=unit(80°) cos80°
        assert [c["id"] for c in out] == ["b0", "b1"]
        assert out[0]["rerank_score"] == pytest.approx(np.cos(np.radians(5.0)), abs=1e-6)
        assert out[1]["rerank_score"] == pytest.approx(np.cos(np.radians(80.0)), abs=1e-6)
        # 输出是浅拷贝，不污染调用方候选
        assert "rerank_score" not in candidates[0]

    def test_candidate_without_content_falls_back_to_original_score(self) -> None:
        emb = FakeEmbedding(texts={"近": unit(0.0)}, images=np.zeros((0, 2)))
        backend = LocalClipRerank(emb)
        out = backend.rerank(
            unit(0.0),
            [
                {"id": "b0", "score": 0.4, "metadata": {"category": "色情", "text": "近"}},
                {"id": "b1", "score": 0.8, "metadata": {"category": "违规"}},
            ],
        )
        scores = {c["id"]: c["rerank_score"] for c in out}
        # 「近」与查询同向 → cos=1.0，排在无内容保底（原 score 0.8）之前
        assert scores["b0"] == pytest.approx(1.0, abs=1e-6)
        assert scores["b1"] == pytest.approx(0.8)
        assert out[0]["id"] == "b0"

    def test_image_candidate_uses_encode_images(self) -> None:
        emb = FakeEmbedding(texts={"近": unit(0.0)}, images=np.stack([unit(10.0)]))
        backend = LocalClipRerank(emb)
        out = backend.rerank(
            unit(0.0),
            [{"id": "b0", "score": 0.5, "metadata": {"image": Image.new("RGB", (8, 8))}}],
        )
        # 图像候选走 encode_images 二次编码（fake 返回 unit(10°)) → cos10°
        assert emb.image_calls == 1
        assert out[0]["rerank_score"] == pytest.approx(np.cos(np.radians(10.0)), abs=1e-6)

    def test_missing_image_path_falls_back(self) -> None:
        emb = FakeEmbedding(texts={}, images=np.zeros((0, 2)))
        backend = LocalClipRerank(emb)
        out = backend.rerank(
            unit(0.0),
            [{"id": "b0", "score": 0.7, "metadata": {"image_path": "no_such_dir/x.jpg"}}],
        )
        assert out[0]["rerank_score"] == pytest.approx(0.7)  # 保底原 score

    def test_empty_candidates_noop(self) -> None:
        emb = FakeEmbedding(texts={}, images=np.zeros((0, 2)))
        assert LocalClipRerank(emb).rerank(unit(0.0), []) == []

    def test_factory_routing(self) -> None:
        emb = FakeEmbedding(texts={}, images=np.zeros((0, 2)))
        assert isinstance(get_rerank_backend({"rerank_enabled": False}, emb), NoneRerank)
        assert isinstance(get_rerank_backend({}, emb), NoneRerank)  # 键缺省=关闭
        # pydantic 配置模型（config.semantic）同样可路由
        assert isinstance(
            get_rerank_backend(SemanticConfig(rerank_enabled=True), emb), LocalClipRerank
        )
        assert isinstance(get_rerank_backend(SemanticConfig(rerank_enabled=False), emb), NoneRerank)


class TestRerankFourSignal:
    """四信号置信度（T19，PRD v0.2 M6）：开关两侧行为 / 权重 / 失败回退。"""

    def test_enabled_rerank_black_max_in_confidence(self) -> None:
        # 黑库命中 metadata 携带文本供二次编码；查询与「近」同向 → rerank≈cos5°
        store = FakeStore(
            black=[
                Hit("b0", 0.5, {"category": "色情", "text": "近"}),
                Hit("b1", 0.4, {"category": "违规", "text": "远"}),
            ],
            white=[Hit("w0", 0.2, {"category": "安全"})],
        )
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(5.0), "远": unit(80.0)},
            images=np.zeros((0, 2)),
        )
        eng = SemanticEngine(
            emb,
            store,
            {
                "rerank_enabled": True,
                "rerank_w_top": 0.5,
                "rerank_w_margin": 0.3,
                "rerank_w_rerank": 0.2,
            },
        )
        out = eng.audit("内容", [])
        rerank_max = float(np.cos(np.radians(5.0)))
        margin = max(0.0, 0.45 - 0.2 - 0.05)  # black_avg=0.45, white_avg=0.2
        expected = 0.5 * 0.5 + 0.3 * (margin / 0.3) + 0.2 * rerank_max
        assert out["rerank_black_max"] == pytest.approx(rerank_max, abs=1e-6)
        assert out["confidence"] == pytest.approx(min(1.0, expected), abs=1e-6)

    def test_w_rerank_one_confidence_equals_rerank_black_max(self) -> None:
        store = FakeStore(
            black=[Hit("b0", 0.9, {"category": "色情", "text": "近"})],
            white=[Hit("w0", 0.2, {"category": "安全"})],
        )
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(0.0)},
            images=np.zeros((0, 2)),
        )
        eng = SemanticEngine(
            emb,
            store,
            {
                "rerank_enabled": True,
                "rerank_w_top": 0.0,
                "rerank_w_margin": 0.0,
                "rerank_w_rerank": 1.0,
            },
        )
        out = eng.audit("内容", [])
        # 查询与「近」同向 → rerank_black_max = cos0° = 1.0，confidence = 1·1.0
        assert out["rerank_black_max"] == pytest.approx(1.0, abs=1e-6)
        assert out["confidence"] == pytest.approx(out["rerank_black_max"], abs=1e-6)

    def test_w_rerank_zero_falls_back_to_three_signal(self) -> None:
        store = FakeStore(
            black=[Hit("b0", 0.5, {"category": "色情", "text": "近"})],
            white=[Hit("w0", 0.2, {"category": "安全"})],
        )
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(5.0)},
            images=np.zeros((0, 2)),
        )
        eng = SemanticEngine(
            emb,
            store,
            {
                "rerank_enabled": True,
                "rerank_w_top": 0.5,
                "rerank_w_margin": 0.3,
                "rerank_w_rerank": 0.0,
            },
        )
        out = eng.audit("内容", [])
        margin = max(0.0, 0.5 - 0.2 - 0.05)
        # rerank 项为 0：confidence = 0.5·black_top + 0.3·(margin/0.3)
        assert out["confidence"] == pytest.approx(0.5 * 0.5 + 0.3 * (margin / 0.3), abs=1e-6)

    def test_rerank_top_k_caps_candidates(self) -> None:
        # 黑库按原 score 降序：b0(0.9, 文本「远」) 先入，rerank_top_k=1 只重排它
        store = FakeStore(
            black=[
                Hit("b0", 0.9, {"category": "色情", "text": "远"}),
                Hit("b1", 0.5, {"category": "违规", "text": "近"}),
            ],
            white=[Hit("w0", 0.2, {"category": "安全"})],
        )
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(5.0), "远": unit(80.0)},
            images=np.zeros((0, 2)),
        )
        eng = SemanticEngine(
            emb,
            store,
            {
                "rerank_enabled": True,
                "rerank_w_top": 0.0,
                "rerank_w_margin": 0.0,
                "rerank_w_rerank": 1.0,
                "rerank_top_k": 1,
            },
        )
        out = eng.audit("内容", [])
        rerank_max = float(np.cos(np.radians(80.0)))
        assert out["rerank_black_max"] == pytest.approx(rerank_max, abs=1e-6)
        assert out["confidence"] == pytest.approx(rerank_max, abs=1e-6)

    def test_disabled_output_identical_to_v01(self) -> None:
        eng = make_engine(black_cos=[0.9, 0.7], white_cos=[0.2, 0.2])
        out = eng.audit("内容", [])
        # 与 v0.1 键集完全一致：无 rerank_black_max 键
        assert set(out) == {
            "triggered",
            "confidence",
            "category",
            "black_top",
            "black_avg",
            "white_avg",
            "reason",
        }
        black_avg = 0.8
        margin = max(0.0, black_avg - 0.2 - 0.05)
        # v0.1 权重 0.6/0.4
        assert out["confidence"] == pytest.approx(min(1.0, 0.6 * 0.9 + 0.4 * (margin / 0.3)))

    def test_rerank_failure_falls_back_three_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FailingBackend:
            def rerank(self, query_vec: np.ndarray, candidates: list[dict]) -> list[dict]:
                raise RuntimeError("fake rerank failure")

        monkeypatch.setattr(
            "safefusion.engines.semantic.get_rerank_backend",
            lambda cfg, embedding: _FailingBackend(),
        )
        store = FakeStore(
            black=[Hit("b0", 0.5, {"category": "色情", "text": "近"})],
            white=[Hit("w0", 0.2, {"category": "安全"})],
        )
        emb = FakeEmbedding(
            texts={"内容": unit(0.0), "近": unit(5.0)},
            images=np.zeros((0, 2)),
        )
        eng = SemanticEngine(
            emb,
            store,
            {
                "rerank_enabled": True,
                "rerank_w_top": 0.5,
                "rerank_w_margin": 0.3,
                "rerank_w_rerank": 0.2,
            },
        )
        out = eng.audit("内容", [])
        # Rerank 失败：rerank_black_max=None（键仍存在），置信度按信号 0 计
        assert out["rerank_black_max"] is None
        margin = max(0.0, 0.5 - 0.2 - 0.05)
        expected = 0.5 * 0.5 + 0.3 * (margin / 0.3) + 0.2 * 0.0
        assert out["confidence"] == pytest.approx(expected, abs=1e-6)
