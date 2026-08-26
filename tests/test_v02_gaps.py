"""v0.2 补缺断言（T21 集成测试专项）：覆盖分析发现的未覆盖分支。

依据 ``开发/v0.2/test/test_0.md`` 范围分析，下列分支在 T14~T20 各自单测中
无直接覆盖，本文件补齐最小用例（只允许修改 tests/，不触碰 src/）：

- v0.2 配置键（image.animated / cache.backend+redis / keyword.regex_rules_enabled
  / semantic.rerank_* / review.*）默认值与 env 覆盖（T16/T17/T18/T19/T20 配置面）；
- ``AppContext.build`` 遇规则表存在引擎无法编译的非法行（绕过 DAO 校验直接入库）
  → 词库照常加载、规则层降级关闭（T17 重载失败回退的 build 期分支）；
- ``LocalClipRerank`` 批量二次编码异常 → 候选保底原 score（T19 失败隔离分支）；
- 编排器端到端：exempt 规则豁免 → ``basic_rules_pass``（T17 验收「豁免后正常放行」）；
- review 原文键变体（content / normalized / 原文）与混合可取得性（T20）；
- review 采样后续页失败保留已采集部分（T20 分页降级分支）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from safefusion.cache.caches import CacheLayer
from safefusion.config import AppConfig, load_config
from safefusion.core.context import AppContext
from safefusion.core.orchestrator import AuditOrchestrator
from safefusion.core.review import Reviewer, _sample_band
from safefusion.engines.keyword_engine import KeywordEngine
from safefusion.engines.light_model import LightTextModel
from safefusion.engines.rerank import LocalClipRerank
from safefusion.models.schemas import AuditRequest
from safefusion.storage.database import Database

from .conftest import build_config
from .fakes import FakeEmbedding, FakeSemantic
from .test_review import _add_log, _DictLLM, _review_cfg, _thresholds, _verdict


class TestV02ConfigDefaults:
    """T21 补缺：v0.2 新增配置键（T14~T20 各自改动未在 test_config 固化默认值）。"""

    def test_v02_key_defaults(self) -> None:
        cfg = load_config(None)
        # M5 Redis 后端（T18）
        assert cfg.cache.backend == "memory"
        assert cfg.cache.redis.url == "redis://127.0.0.1:6379/0"
        assert cfg.cache.redis.prefix == "sf:"
        # M3 动图抽帧（T16）
        assert cfg.image.animated.enabled is True
        assert cfg.image.animated.frames == 5
        assert cfg.image.animated.mode == "uniform"
        # M4 正则规则开关（T17）
        assert cfg.keyword.regex_rules_enabled is True
        # M6 Rerank 四信号（T19，默认关 = 与 v0.1 一致）
        assert cfg.semantic.rerank_enabled is False
        assert cfg.semantic.rerank_w_top == 0.5
        assert cfg.semantic.rerank_w_margin == 0.3
        assert cfg.semantic.rerank_w_rerank == 0.2
        assert cfg.semantic.rerank_top_k == 5
        # M7 定时复核（T20）
        assert cfg.review.interval_min == 240
        assert cfg.review.band_low == 0.35
        assert cfg.review.band_high == 0.75
        assert cfg.review.sample_size == 50
        assert cfg.review.auto_tune is False

    def test_v02_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_CACHE_BACKEND", "redis")
        monkeypatch.setenv("SAFEFUSION_CACHE_REDIS_PREFIX", "p:")
        monkeypatch.setenv("SAFEFUSION_IMAGE_ANIMATED_FRAMES", "3")
        monkeypatch.setenv("SAFEFUSION_KEYWORD_REGEX_RULES_ENABLED", "false")
        monkeypatch.setenv("SAFEFUSION_SEMANTIC_RERANK_ENABLED", "true")
        monkeypatch.setenv("SAFEFUSION_SEMANTIC_RERANK_W_RERANK", "0.3")
        monkeypatch.setenv("SAFEFUSION_REVIEW_INTERVAL_MIN", "30")
        cfg = load_config(None)
        assert cfg.cache.backend == "redis"
        assert cfg.cache.redis.prefix == "p:"
        assert cfg.image.animated.frames == 3
        assert cfg.keyword.regex_rules_enabled is False
        assert cfg.semantic.rerank_enabled is True
        assert cfg.semantic.rerank_w_rerank == 0.3
        assert cfg.review.interval_min == 30

    def test_v02_groups_reject_unknown_keys(self) -> None:
        # v0.2 新增配置分组同样 strict（extra=forbid），防配置拼写错误静默吞掉
        for group in ("cache", "image", "keyword", "semantic", "review"):
            with pytest.raises(ValidationError):
                AppConfig.model_validate({group: {"surprise_key": 1}})


class TestContextBuildInvalidRuleFallback:
    """T17 补缺：build 期遇规则表含引擎无法编译的非法行时的回退（词库照常、规则层关闭）。"""

    def test_build_invalid_rule_row_keeps_words_disables_rules(self, tmp_path: Path) -> None:
        # 绕过 add_rules 的写前校验，直接向 rules 表插入非法正则行（模拟脏库 / 手工 SQL）
        db_path = tmp_path / "audit.db"
        db = Database(db_path)
        db.add_keywords([("广告", "加我", None)])
        db._conn.execute(
            "INSERT INTO rules (category, pattern, action, note, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            ("广告", "(unclosed", "exempt", None, "2026-08-27T00:00:00+00:00"),
        )
        db._conn.commit()
        db.close()

        ctx = AppContext.build(build_config(tmp_path))  # 默认 regex_rules_enabled=True
        assert ctx.keyword_engine is not None
        assert ctx.keyword_engine.loaded is True
        assert ctx.keyword_engine.rules_enabled is False  # 规则层降级关闭
        assert "keyword_engine" not in ctx.degraded
        assert any(hit.keyword == "加我" for hit in ctx.keyword_engine.scan("加我"))


class TestRerankEncodeIsolation:
    """T19 补缺：LocalClipRerank 批量二次编码异常 → 候选保底原 score（失败隔离不拖垮整批）。"""

    def test_text_batch_encode_failure_keeps_original_scores(self) -> None:
        class _BrokenEmb:
            supports_mixed_input = True

            def encode_texts(self, texts: list[str]) -> np.ndarray:
                raise RuntimeError("encode down")

            def encode_images(self, images: list[Any]) -> np.ndarray:
                raise RuntimeError("encode down")

        backend = LocalClipRerank(_BrokenEmb())
        out = backend.rerank(
            np.array([1.0, 0.0], dtype=np.float32),
            [
                {"id": "b1", "score": 0.6, "metadata": {"category": "违规", "text": "远"}},
                {"id": "b0", "score": 0.9, "metadata": {"category": "色情", "text": "近"}},
            ],
        )
        # 编码失败不抛异常：全部保底原 score，按 rerank_score 降序
        assert [c["id"] for c in out] == ["b0", "b1"]
        assert out[0]["rerank_score"] == pytest.approx(0.9)
        assert out[1]["rerank_score"] == pytest.approx(0.6)

    def test_image_batch_encode_failure_keeps_original_scores(self) -> None:
        class _BrokenEmb:
            supports_mixed_input = True

            def encode_texts(self, texts: list[str]) -> np.ndarray:
                raise RuntimeError("encode down")

            def encode_images(self, images: list[Any]) -> np.ndarray:
                raise RuntimeError("encode down")

        backend = LocalClipRerank(_BrokenEmb())
        out = backend.rerank(
            np.array([1.0, 0.0], dtype=np.float32),
            [{"id": "b0", "score": 0.7, "metadata": {"image": Image.new("RGB", (8, 8))}}],
        )
        assert out[0]["rerank_score"] == pytest.approx(0.7)

    def test_text_meta_priority_over_image(self) -> None:
        # 候选同时携带 text 与 image：文本路径优先（encode_texts 生效，不误走图像）
        emb = FakeEmbedding(
            texts={"近": np.array([1.0, 0.0], dtype=np.float32)},
            images=np.stack([np.array([1.0, 0.0], dtype=np.float32)]),
        )
        backend = LocalClipRerank(emb)
        img = Image.new("RGB", (8, 8))
        out = backend.rerank(
            np.array([1.0, 0.0], dtype=np.float32),
            [{"id": "b0", "score": 0.5, "metadata": {"text": "近", "image": img}}],
        )
        assert emb.text_calls == 1
        assert emb.image_calls == 0
        assert out[0]["rerank_score"] == pytest.approx(1.0, abs=1e-6)


class TestOrchestratorRegexExemptFastPass:
    """T17 验收补缺：exempt 规则豁免命中 → basic_rules_pass（真实 KeywordEngine 端到端）。"""

    def _container(self, keyword: KeywordEngine, semantic: FakeSemantic, cache: CacheLayer):
        return AppContext(
            config=AppConfig.model_validate({}),
            database=None,
            store=None,
            embedding=None,
            keyword_engine=keyword,
            light_model=LightTextModel(None, None),  # disabled：predict 不参与
            whitelist=None,
            semantic=semantic,
            llm=None,
            cache_layer=cache,
            degraded=[],
        )

    @pytest.mark.asyncio
    async def test_exempt_rule_fast_pass_with_regex_detail(self) -> None:
        kw = KeywordEngine()
        kw.reload(
            {"广告": ["加我"]},
            [{"category": "广告", "pattern": "加我好友", "action": "exempt"}],
        )
        semantic = FakeSemantic()
        orch = AuditOrchestrator(self._container(kw, semantic, CacheLayer({})))
        result = await orch.process_audit(AuditRequest(text="加我好友一起玩"), "full")
        assert result.source == "basic_rules_pass"
        assert result.has_violation is False
        assert semantic.audit_calls == 0  # 豁免后不再进语义层
        assert result.detail is not None
        assert result.detail.keyword is not None
        assert len(result.detail.keyword.regex_filtered) == 1  # 豁免明细落 detail

    @pytest.mark.asyncio
    async def test_without_rule_keeps_hit_and_runs_semantic(self) -> None:
        kw = KeywordEngine()
        kw.reload({"广告": ["加我"]}, None)  # 规则层关闭
        semantic = FakeSemantic()  # 默认 = 语义降级结果（有强信号 → 保守判违规）
        orch = AuditOrchestrator(self._container(kw, semantic, CacheLayer({})))
        result = await orch.process_audit(AuditRequest(text="加我好友一起玩"), "full")
        assert result.source != "basic_rules_pass"
        assert semantic.audit_calls == 1  # 命中未被豁免 → 进入语义层


class TestReviewRecoverTextVariants:
    """T20 补缺：detail_json 内嵌原文的候选键（content / normalized / 原文）均可启用逐条复核。"""

    @pytest.mark.parametrize("key", ["content", "normalized", "原文"])
    @pytest.mark.asyncio
    async def test_text_key_variant_enables_full_mode(
        self, tmp_db: Database, key: str
    ) -> None:
        _add_log(tmp_db, "r1", True, 0.50, detail={key: "待复核原文样例"})
        llm = _DictLLM({"待复核原文样例": _verdict(True)})
        report = await Reviewer().review_once(
            tmp_db, llm, _review_cfg(), thresholds=_thresholds()
        )
        assert report.mode == "full"
        assert report.skipped_reason is None
        assert report.reviewed == 1
        assert report.consistent == 1
        assert llm.judge_calls == 1


class TestReviewMixedTextAvailability:
    """T20 补缺：部分样本可恢复原文 → 全量模式复核可恢复子集，text_missing 计入统计。"""

    @pytest.mark.asyncio
    async def test_mixed_text_availability_full_mode_with_missing(self, tmp_db: Database) -> None:
        _add_log(tmp_db, "a", True, 0.55, detail={"text": "甲"})
        _add_log(tmp_db, "b", False, 0.45, detail={"text": "乙"})
        _add_log(tmp_db, "c", True, 0.60)  # 无原文 → 不可复查
        llm = _DictLLM({"甲": _verdict(True), "乙": _verdict(False)})
        report = await Reviewer().review_once(
            tmp_db, llm, _review_cfg(), thresholds=_thresholds()
        )
        assert report.mode == "full"
        assert report.sampled == 3
        assert report.reviewed == 2  # 仅可恢复原文的子集参与 LLM 复核
        assert report.stats["text_missing"] == 1
        assert report.consistent_rate == 1.0
        assert report.suggestions[0]["action"] == "raise"  # 违规一致率 1/1


class TestReviewPartialSamplingPageFailure:
    """T20 补缺：采样后续页失败 → 保留已采集部分（分页降级分支，首页失败才整体失败）。"""

    @pytest.mark.asyncio
    async def test_second_page_failure_keeps_collected_partial(self) -> None:
        rows = [
            {"request_id": f"r{i}", "confidence": 0.5 if i in (0, 999) else 0.9}
            for i in range(1000)
        ]

        class _PageDB:
            def query_logs(self, **kwargs: Any) -> list[dict[str, Any]]:
                offset = int(kwargs.get("offset", 0))
                if offset >= 1000:
                    raise RuntimeError("storage down on later page")
                return rows

        sampled = _sample_band(_PageDB(), 0.35, 0.75, 50)
        assert [row["request_id"] for row in sampled] == ["r0", "r999"]
