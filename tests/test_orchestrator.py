"""编排器测试：PRD §3.1 主流程五路径 + 降级策略 + overrides 权限。

覆盖（T9 任务卡验收）：
① 快速放行（全帧白名单命中 且 文本零风险 → basic_rules_pass）；
② 关键词强信号不短路（白名单全命中仍进语义层）；
③ 缓存命中（含 standard/full tier 隔离）；
④ LLM 成功与回退（skip_llm / judge 失败 → 回退语义层）；
⑤ overrides 权限（非 full 组 → PermissionError）。

组件以真实类 + 受控假组件（fakes）组合注入 AppContext，全链路无网络/无模型。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from safefusion.cache.caches import CacheLayer
from safefusion.config import AppConfig
from safefusion.core.context import AppContext
from safefusion.core.orchestrator import AuditOrchestrator
from safefusion.engines.image_pipeline import WhitelistMatcher
from safefusion.engines.keyword_engine import KeywordEngine
from safefusion.engines.light_model import LightTextModel
from safefusion.models.schemas import AuditRequest, ImageInput, Overrides
from safefusion.storage.database import Database

from .conftest import png_bytes
from .fakes import FakeKeywordEngine, FakeLLM, FakeSemantic

#: 编排器测试统一使用默认阈值配置（data_dir 与容器无关，不落盘）
_DEFAULT_CFG = AppConfig.model_validate({})


def _make_container(
    db: Database | None,
    *,
    keyword: KeywordEngine | FakeKeywordEngine | None = None,
    whitelist: WhitelistMatcher | None = None,
    semantic: FakeSemantic | None = None,
    llm: FakeLLM | None = None,
    cache: CacheLayer | None = None,
) -> AppContext:
    return AppContext(
        config=_DEFAULT_CFG,
        database=db,
        store=None,
        embedding=None,
        keyword_engine=keyword,
        light_model=LightTextModel(None, None),  # disabled：predict 不参与
        whitelist=whitelist,
        semantic=semantic,
        llm=llm,
        cache_layer=cache,
        degraded=[],
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _empty_semantic() -> FakeSemantic:
    return FakeSemantic(
        {
            "triggered": False,
            "confidence": 0.2,
            "category": None,
            "black_top": {"id": "b0", "score": 0.2, "category": None, "metadata": {}},
            "black_avg": 0.2,
            "white_avg": 0.1,
            "reason": None,
        }
    )


class TestFastPass:
    """唯一快速放行通道：文本零风险（纯文本请求）直通 basic_rules_pass。

    注：全帧白名单命中 + 文本零风险的端到端用例受 src 缺陷 S2 阻塞
    （WhitelistMatcher 命中距离为 np.int64 → _persist JSON 序列化失败），
    见本文件 TestSrcDefectS2::test_whitelist_hit_persist（xfail 标记）。
    """

    async def test_basic_rules_pass_text_only(self) -> None:
        kw = KeywordEngine()
        kw.load_categories({})  # 空词库 → scan 返回 []
        cache = CacheLayer({})
        orch = AuditOrchestrator(
            _make_container(None, keyword=kw, semantic=_empty_semantic(), cache=cache)
        )
        req = AuditRequest(text="正常文本")
        result = await orch.process_audit(req, "full")
        assert result.has_violation is False
        assert result.source == "basic_rules_pass"
        assert result.cache_hit is False
        # full 组 detail 含三层基础规则明细
        assert result.detail is not None
        assert result.detail.keyword is not None
        assert result.detail.image_whitelist == []

    async def test_basic_rules_pass_with_db_and_log(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            kw = KeywordEngine()
            kw.load_categories({})
            orch = AuditOrchestrator(
                _make_container(db, keyword=kw, semantic=_empty_semantic(), cache=CacheLayer({}))
            )
            result = await orch.process_audit(AuditRequest(text="正常文本"), "standard")
            assert result.source == "basic_rules_pass"
            assert result.detail is None  # standard 裁剪
            assert len(db.query_logs()) == 1  # 审计日志已落库
        finally:
            db.close()

    async def test_text_risk_blocks_pass(self) -> None:
        # 文本有关键词 → 不进快速放行（即使无图片）
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})
        semantic = _empty_semantic()
        orch = AuditOrchestrator(_make_container(None, keyword=kw, semantic=semantic))
        result = await orch.process_audit(AuditRequest(text="裸聊"), "full")
        assert result.source != "basic_rules_pass"
        assert semantic.audit_calls == 1  # 确实走了语义层


class TestKeywordNoShortCircuit:
    """关键词强信号不短路：关键词命中（即使帧白名单未命中）仍进入语义层汇总决策。"""

    async def test_keyword_signal_still_runs_semantic(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            whitelist = WhitelistMatcher(db)  # 空白名单：帧不命中 → 帧也产生风险信号
            kw = KeywordEngine()
            kw.load_categories({"色情": ["裸聊"]})
            semantic = _empty_semantic()
            orch = AuditOrchestrator(
                _make_container(
                    db,
                    keyword=kw,
                    whitelist=whitelist,
                    semantic=semantic,
                    cache=CacheLayer({}),
                )
            )
            result = await orch.process_audit(
                AuditRequest(text="裸聊", images=[ImageInput(base64=_b64(png_bytes()))]), "full"
            )
            # 未被快速放行，进入语义层
            assert result.source == "semantic"
            assert semantic.audit_calls == 1
            assert result.detail is not None
            assert len(result.detail.keyword.hits) == 1
            # detail 元素为 ImageWhitelistHit 模型（属性访问）
            assert [f.hit for f in result.detail.image_whitelist] == [False]
        finally:
            db.close()


class TestSrcDefectS2:
    """src 缺陷 S2 回归用例（已修复）：白名单命中距离曾为 np.int64 导致持久化 JSON 失败。

    2026-08-26 主模型在 image_pipeline.match 出口执行 int() 归一化后，
    移除 xfail 恢复正式断言（缺陷详情见 开发/v0.1/debug/debug_0.md）。
    """

    async def test_whitelist_hit_persist(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            whitelist = WhitelistMatcher(db)
            img_bytes = png_bytes()
            whitelist.add_image(img_bytes, note="样板")
            kw = KeywordEngine()
            kw.load_categories({})
            orch = AuditOrchestrator(
                _make_container(
                    db,
                    keyword=kw,
                    whitelist=whitelist,
                    semantic=_empty_semantic(),
                    cache=CacheLayer({}),
                )
            )
            result = await orch.process_audit(
                AuditRequest(text="正常文本", images=[ImageInput(base64=_b64(img_bytes))]), "full"
            )
            # S2 修复后预期：保持 basic_rules_pass 而非退化为兜底 semantic
            assert result.source == "basic_rules_pass"
            assert result.has_violation is False
        finally:
            db.close()


class TestCacheTierIsolation:
    """缓存命中与 standard/full tier 隔离（T10 报告缺陷②修复验证）。

    文本带关键词强信号：首次计算经过语义层并写缓存，第二次同 tier 命中；
    standard 档键含 tier 不同 → 未命中 full 档缓存。
    """

    async def test_same_tier_hit_and_cross_tier_miss(self) -> None:
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})
        semantic = _empty_semantic()
        cache = CacheLayer({})
        llm = FakeLLM(verdict=None)  # 不会走到 LLM 档（confidence 0.2 → safe）
        orch = AuditOrchestrator(
            _make_container(None, keyword=kw, semantic=semantic, llm=llm, cache=cache)
        )

        req = AuditRequest(text="裸聊", overrides=Overrides(margin_w=0.01))
        first = await orch.process_audit(req, "full")
        assert first.cache_hit is False
        assert semantic.audit_calls == 1  # 首次真实计算

        second = await orch.process_audit(req, "full")
        assert second.cache_hit is True
        assert second.source == "cache"
        assert second.detail is not None  # full 档缓存保留 detail
        assert semantic.audit_calls == 1  # 第二次未再触发语义

        third = await orch.process_audit(AuditRequest(text="裸聊"), "standard")
        # tier 不同 → 键不同 → 未命中 full 档写入的缓存（standard 无 overrides）
        assert third.cache_hit is False
        assert third.detail is None  # standard 档裁剪
        assert semantic.audit_calls == 2


class TestLLMPaths:
    """LLM 兜底档：成功采用、失败回退语义层、skip_llm 直回退。

    带关键词强信号进入语义层（否则会走快速放行，T9 自检同款约束）。
    """

    def _llm_tier_semantic(self) -> FakeSemantic:
        return FakeSemantic(
            {
                "triggered": True,
                "confidence": 0.5,  # 落在 [0.35, 0.75] → LLM 档
                "category": "色情",
                "black_top": {"id": "b0", "score": 0.7, "category": "色情", "metadata": {}},
                "black_avg": 0.7,
                "white_avg": 0.3,
                "reason": None,
            }
        )

    def _keyword_signal(self) -> KeywordEngine:
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})
        return kw

    async def test_llm_success(self) -> None:
        semantic = self._llm_tier_semantic()
        verdict = {"is_violation": False, "category": None, "confidence": 0.6, "reason": "正常"}
        llm = FakeLLM(verdict=verdict)
        orch = AuditOrchestrator(
            _make_container(None, keyword=self._keyword_signal(), semantic=semantic, llm=llm)
        )
        result = await orch.process_audit(AuditRequest(text="裸聊"), "full")
        assert llm.judge_calls == 1
        assert result.source == "llm"
        assert result.has_violation is False
        # merge_final：0.6*0.6 + 0.4*0.5
        assert result.confidence == pytest.approx(0.56)
        assert result.detail is not None and result.detail.llm is not None
        assert result.detail.llm.is_violation is False

    async def test_llm_failure_falls_back(self) -> None:
        semantic = self._llm_tier_semantic()
        llm = FakeLLM(verdict=None)  # 失败 → 回退语义层
        orch = AuditOrchestrator(
            _make_container(None, keyword=self._keyword_signal(), semantic=semantic, llm=llm)
        )
        result = await orch.process_audit(AuditRequest(text="裸聊"), "full")
        assert llm.judge_calls == 1
        assert result.source == "semantic"
        assert result.has_violation is True  # 回退语义层 triggered=True
        assert result.confidence == pytest.approx(0.5)

    async def test_skip_llm_no_call(self) -> None:
        semantic = self._llm_tier_semantic()
        verdict = {"is_violation": True, "category": "色情", "confidence": 0.9, "reason": "x"}
        llm = FakeLLM(verdict=verdict)
        orch = AuditOrchestrator(
            _make_container(None, keyword=self._keyword_signal(), semantic=semantic, llm=llm)
        )
        result = await orch.process_audit(AuditRequest(text="裸聊", skip_llm=True), "full")
        assert llm.judge_calls == 0  # skip_llm 不调用
        assert result.source == "semantic"

    async def test_llm_unavailable_falls_back(self) -> None:
        semantic = self._llm_tier_semantic()
        llm = FakeLLM(verdict=None, available=False)
        orch = AuditOrchestrator(
            _make_container(None, keyword=self._keyword_signal(), semantic=semantic, llm=llm)
        )
        result = await orch.process_audit(AuditRequest(text="裸聊"), "standard")
        assert result.source == "semantic"
        assert llm.judge_calls == 0


class TestSemanticDegradation:
    """语义层降级 ≠ 安全：有强信号保守判违规，无强信号判安全。

    ``degraded`` 标记写入审计日志 detail_json（AuditDetail 无此字段，
    响应 detail 不携带——见 test_0.md 疑似缺陷记录）。
    """

    def _degraded(self, reason: str = "semantic_disabled") -> FakeSemantic:
        return FakeSemantic(
            {
                "triggered": False,
                "confidence": 0.0,
                "category": None,
                "black_top": None,
                "black_avg": 0.0,
                "white_avg": 0.0,
                "reason": reason,
            }
        )

    async def test_degraded_with_strong_signal_violates(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            kw = KeywordEngine()
            kw.load_categories({"色情": ["裸聊"]})
            orch = AuditOrchestrator(_make_container(db, keyword=kw, semantic=self._degraded()))
            result = await orch.process_audit(AuditRequest(text="裸聊"), "full")
            assert result.has_violation is True  # 保守取向
            assert result.source == "semantic"
            assert result.category == "色情"  # 关键词类别兜底
            # 降级标记进入审计日志
            log = db.query_logs()[0]
            assert "semantic:semantic_disabled" in log["detail_json"]
        finally:
            db.close()

    async def test_degraded_without_signal_safe(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            kw = KeywordEngine()
            kw.load_categories({})
            # 非白名单图片产生帧风险信号 → 进入语义层；无强信号 → 判安全
            orch = AuditOrchestrator(_make_container(db, keyword=kw, semantic=self._degraded()))
            result = await orch.process_audit(
                AuditRequest(text="普通文本", images=[ImageInput(base64=_b64(png_bytes()))]), "full"
            )
            assert result.has_violation is False
            assert result.source == "semantic"
            assert "semantic:semantic_disabled" in db.query_logs()[0]["detail_json"]
        finally:
            db.close()

    async def test_degraded_pinyin_only_hit_is_safe(self, tmp_path: Path) -> None:
        """语义降级 + 仅拼音弱命中（同音/错音）→ 不判违规（弱信号降权）。"""
        db = Database(tmp_path / "audit.db")
        try:
            kw = KeywordEngine()
            kw.load_categories({"测试": ["捡闻"]})  # 仅同音通道，无原文命中
            orch = AuditOrchestrator(_make_container(db, keyword=kw, semantic=self._degraded()))
            result = await orch.process_audit(AuditRequest(text="见闻很多"), "full")
            # 弱命中阻止快速放行（进入语义层），但语义降级时不做违规证据
            assert result.has_violation is False
            assert result.source == "semantic"
        finally:
            db.close()


class TestOverridesPermission:
    """overrides 仅 full 组可用；full 组覆盖透传语义层。"""

    async def test_standard_tier_with_overrides_forbidden(self) -> None:
        orch = AuditOrchestrator(_make_container(None, semantic=_empty_semantic()))
        with pytest.raises(PermissionError, match="overrides 仅 full 组可用"):
            await orch.process_audit(
                AuditRequest(text="t", overrides=Overrides(semantic_threshold=0.9)), "standard"
            )

    async def test_full_tier_overrides_passed_to_semantic(self) -> None:
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})
        semantic = _empty_semantic()
        orch = AuditOrchestrator(_make_container(None, keyword=kw, semantic=semantic))
        await orch.process_audit(
            AuditRequest(text="裸聊", overrides=Overrides(margin_w=0.02)), "full"
        )
        assert semantic.last_ov == {"margin_w": 0.02}  # 仅语义相关键透传


class TestErrorFallback:
    """最外层兜底：组件异常不崩，返回安全结果并落错误审计。"""

    async def test_semantic_exception_fallback(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            kw = KeywordEngine()
            kw.load_categories({"色情": ["裸聊"]})
            semantic = FakeSemantic(exc=RuntimeError("boom"))
            orch = AuditOrchestrator(_make_container(db, keyword=kw, semantic=semantic))
            result = await orch.process_audit(AuditRequest(text="裸聊"), "full")
            # 语义层异常 → 降级；强信号存在 → 保守判违规
            assert result.has_violation is True
            assert result.source == "semantic"
            # 兜底降级标记写入审计日志
            assert "semantic_exception" in db.query_logs()[0]["detail_json"]
            assert len(db.query_logs()) == 1
        finally:
            db.close()

    async def test_image_decode_error_fallback(self) -> None:
        orphan = AuditOrchestrator(_make_container(None, semantic=_empty_semantic()))
        result = await orphan.process_audit(
            AuditRequest(text="文本", images=[ImageInput(base64="!!!bad!!!")]), "full"
        )
        assert result.has_violation is False  # 兜底安全结果，不抛异常


class TestAggregator:
    """纯函数决策层（core/aggregator.py）：汇总 / 三档分档 / 最终合并。"""

    def test_summarize_basic_no_signals(self) -> None:
        from safefusion.core.aggregator import summarize_basic

        assert summarize_basic({"hits": []}, None, []) == {"risk_signals": False, "all_safe": True}
        assert summarize_basic(None, None, None)["all_safe"] is True

    def test_summarize_basic_keyword_signal(self) -> None:
        from safefusion.core.aggregator import summarize_basic

        assert summarize_basic({"hits": [{"keyword": "x"}]}, None, [])["risk_signals"] is True

    def test_summarize_basic_light_violation(self) -> None:
        from safefusion.core.aggregator import summarize_basic

        assert summarize_basic({"hits": []}, {"violation": True}, [])["risk_signals"] is True
        assert summarize_basic({"hits": []}, {"violation": False}, [])["all_safe"] is True

    def test_summarize_basic_frame_signal(self) -> None:
        from safefusion.core.aggregator import summarize_basic

        mixed = [{"hit": True}, {"hit": False}]
        assert summarize_basic({"hits": []}, None, mixed)["risk_signals"] is True
        assert summarize_basic({"hits": []}, None, [{"hit": True}])["all_safe"] is True

    @pytest.mark.parametrize(
        ("conf", "low", "high", "expected"),
        [
            (0.2, 0.35, 0.75, "safe"),  # 低档
            (0.34, 0.35, 0.75, "safe"),
            (0.35, 0.35, 0.75, "llm"),  # 等于 low → LLM 档（端点含）
            (0.5, 0.35, 0.75, "llm"),
            (0.75, 0.35, 0.75, "llm"),  # 等于 high → LLM 档
            (0.76, 0.35, 0.75, "violation"),
            (1.0, 0.35, 0.75, "violation"),
        ],
    )
    def test_decide_tier_boundaries(
        self, conf: float, low: float, high: float, expected: str
    ) -> None:
        from safefusion.core.aggregator import decide_tier

        assert decide_tier(conf, low, high) == expected

    def test_merge_final_llm_success(self) -> None:
        from safefusion.core.aggregator import merge_final

        verdict = {"is_violation": True, "category": "色情", "confidence": 0.6, "reason": "r"}
        sem = {"triggered": False, "confidence": 0.4, "category": "x"}
        has_violation, confidence, category, source = merge_final(verdict, sem)
        assert (has_violation, source) == (True, "llm")
        assert confidence == pytest.approx(0.6 * 0.6 + 0.4 * 0.4)
        assert category == "色情"  # LLM 类别优先

    def test_merge_final_fallback(self) -> None:
        from safefusion.core.aggregator import merge_final

        sem = {"triggered": True, "confidence": 0.3, "category": "赌博"}
        assert merge_final(None, sem) == (True, 0.3, "赌博", "semantic")

    def test_merge_final_llm_without_confidence(self) -> None:
        from safefusion.core.aggregator import merge_final

        verdict = {"is_violation": False, "confidence": None}
        sem = {"triggered": True, "confidence": 0.5, "category": "a"}
        _, confidence, _, source = merge_final(verdict, sem)
        # LLM 无 confidence → 用语义置信度代入，权重仍按 0.6/0.4
        assert confidence == pytest.approx(0.6 * 0.5 + 0.4 * 0.5)
        assert source == "llm"

    def test_merge_final_weight_clipped(self) -> None:
        from safefusion.core.aggregator import merge_final

        verdict = {"is_violation": True, "confidence": 1.0}
        sem = {"triggered": False, "confidence": 0.0}
        _, confidence, _, source = merge_final(verdict, sem, llm_weight=5.0)
        assert confidence == pytest.approx(1.0)  # 权重裁剪到 1 → 全取 LLM 置信度
        assert source == "llm"
