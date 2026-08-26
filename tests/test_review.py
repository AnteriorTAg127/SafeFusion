"""定时复核（PRD v0.2 M7）单元与契约测试。

覆盖：
- ``Reviewer.review_once``：逐条复核一致率计算、违规一致率→阈值建议方向、
  带外样本过滤、LLM 失败计数、无密钥跳过、原文不可取降级统计模式；
- ``ReviewScheduler``：trigger 落盘与状态、两次并发触发只执行一次、自动调度
  启停（interval_seconds 测试钩子加速）；
- 管理端 ``/admin/review/run``（501/200/202）与 ``/admin/review/status``（501/200）。

原文可取得性：当前 audit_logs 不落原文全文，仅当 ``detail_json`` 内嵌
``text`` 键时可逐条复核（本文用该方式构造全文模式数据）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from safefusion.api.admin import create_admin_app
from safefusion.config import AppConfig, ReviewConfig, ThresholdsConfig
from safefusion.core.review import Reviewer, ReviewScheduler
from safefusion.engines.image_pipeline import WhitelistMatcher
from safefusion.storage.database import Database

from .conftest import build_config

TOKEN = "admin-test-token"


class _DictLLM:
    """按原文查表返回判定的测试 LLM：text → verdict dict 或 None（LLM 失败）。"""

    def __init__(
        self,
        mapping: dict[str, dict[str, Any] | None],
        *,
        available: bool = True,
        delay: float = 0.0,
    ) -> None:
        self._mapping = mapping
        self.available = available
        self.delay = delay
        self.judge_calls = 0

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
        if self.delay:
            await asyncio.sleep(self.delay)
        verdict = self._mapping.get(text)
        return dict(verdict) if verdict is not None else None


class _DuckAdminCfg:
    """管理端配置鸭子类型（data_dir + admin_token；review 配置由调度器真实 config 承担）。"""

    def __init__(self, data_dir: Path, token: str) -> None:
        self.data_dir = str(data_dir)
        self.admin_token = token


def _add_log(
    db: Database,
    request_id: str,
    has_violation: bool,
    confidence: float,
    *,
    detail: dict[str, Any] | None = None,
    source: str = "semantic",
) -> None:
    """写入一条审核日志（带原文时经 detail 内嵌 ``text`` 键）。"""

    db.insert_audit_log(
        request_id=request_id,
        has_violation=has_violation,
        source=source,
        text_hash=f"h-{request_id}",
        confidence=confidence,
        category="色情" if has_violation else None,
        detail=detail,
    )


def _verdict(violation: bool) -> dict[str, Any]:
    """构造完整判定 dict（LLM 复核输出形态）。"""

    return {
        "is_violation": violation,
        "category": "色情" if violation else None,
        "confidence": 0.9 if violation else 0.1,
        "reason": "复核理由",
    }


def _review_cfg(**overrides: Any) -> ReviewConfig:
    """复核配置（默认带 0.35~0.75、sample_size=10、auto_tune=False）。"""

    return ReviewConfig(**overrides)


def _thresholds(**overrides: Any) -> ThresholdsConfig:
    """阈值配置（默认 0.35 / 0.75）。"""

    return ThresholdsConfig(**overrides)


# ------------------------------------------------------------------ Reviewer


class TestReviewerFullMode:
    """逐条 LLM 复核（detail_json 内嵌 text 时启用）：一致率与建议方向。"""

    @pytest.mark.asyncio
    async def test_consistent_rate_and_raise_suggestion(self, tmp_db: Database) -> None:
        _add_log(tmp_db, "v1", True, 0.50, detail={"text": "违规样例一", "kw": ["x"]})
        _add_log(tmp_db, "v2", True, 0.70, detail={"text": "违规样例二"})
        _add_log(tmp_db, "s1", False, 0.40, detail={"text": "安全样例一"})
        _add_log(tmp_db, "s2", False, 0.60, detail={"text": "安全样例二"})
        # 带外样本不应被采样
        _add_log(tmp_db, "hi", True, 0.90, detail={"text": "高档外"})
        _add_log(tmp_db, "lo", False, 0.10, detail={"text": "低档外"})

        llm = _DictLLM(
            {
                "违规样例一": _verdict(True),
                "违规样例二": _verdict(True),
                "安全样例一": _verdict(False),
                "安全样例二": _verdict(False),
            }
        )
        report = await Reviewer().review_once(
            tmp_db, llm, _review_cfg(sample_size=10), thresholds=_thresholds()
        )

        assert report.mode == "full"
        assert report.skipped_reason is None
        assert report.sampled == 4  # 带外 2 条被过滤
        assert report.reviewed == 4
        assert report.consistent == 4
        assert report.consistent_rate == 1.0
        assert report.disagreements == {"missed": 0, "false_alarm": 0}
        # 违规一致率 2/2 → 建议上调 confidence_high（+0.05）
        assert report.suggestions
        sug = report.suggestions[0]
        assert sug["action"] == "raise"
        assert sug["key"] == "confidence_high"
        assert sug["current"] == 0.75
        assert sug["suggested"] == 0.80
        assert sug["delta"] == 0.05
        assert sug["auto_tune"] is False
        assert sug["alternative"]["action"] == "lower"
        assert sug["alternative"]["key"] == "confidence_low"
        assert sug["alternative"]["suggested"] == 0.30

    @pytest.mark.asyncio
    async def test_low_violation_agree_lowers_threshold(self, tmp_db: Database) -> None:
        # 两条管线判违规、LLM 复核判安全 → 违规一致率 0 → 建议下调 confidence_high
        _add_log(tmp_db, "v1", True, 0.60, detail={"text": "文本A"})
        _add_log(tmp_db, "v2", True, 0.65, detail={"text": "文本B"})

        llm = _DictLLM({"文本A": _verdict(False), "文本B": _verdict(False)})
        report = await Reviewer().review_once(tmp_db, llm, _review_cfg(), thresholds=_thresholds())

        assert report.consistent == 0
        assert report.consistent_rate == 0.0
        assert report.disagreements == {"missed": 0, "false_alarm": 2}
        sug = report.suggestions[0]
        assert sug["action"] == "lower"
        assert sug["key"] == "confidence_high"
        assert sug["suggested"] == 0.70
        assert sug["alternative"]["action"] == "raise"
        assert sug["alternative"]["suggested"] == 0.40

    @pytest.mark.asyncio
    async def test_judge_failure_excluded_from_consistency(self, tmp_db: Database) -> None:
        _add_log(tmp_db, "v1", True, 0.55, detail={"text": "违A"})
        _add_log(tmp_db, "v2", True, 0.58, detail={"text": "违B"})
        _add_log(tmp_db, "s1", False, 0.45, detail={"text": "安A"})

        llm = _DictLLM({"违A": _verdict(True), "违B": None, "安A": _verdict(False)})
        report = await Reviewer().review_once(tmp_db, llm, _review_cfg(), thresholds=_thresholds())

        assert report.sampled == 3
        assert report.reviewed == 2  # 违B 复核失败不计入
        assert report.consistent == 2
        assert report.consistent_rate == 1.0
        assert report.stats["judge_failed"] == 1

    @pytest.mark.asyncio
    async def test_band_sampling_respects_edges(self, tmp_db: Database) -> None:
        # 边界样本（等于 band_low / band_high）应被包含
        _add_log(tmp_db, "low-edge", True, 0.35, detail={"text": "下界"})
        _add_log(tmp_db, "high-edge", False, 0.75, detail={"text": "上界"})
        _add_log(tmp_db, "out", True, 0.80, detail={"text": "界外"})

        llm = _DictLLM({"下界": _verdict(True), "上界": _verdict(False), "界外": _verdict(True)})
        report = await Reviewer().review_once(tmp_db, llm, _review_cfg(), thresholds=_thresholds())
        assert report.sampled == 2
        assert report.reviewed == 2
        assert llm.judge_calls == 2


class TestReviewerSkipPaths:
    """无密钥 / 原文不可取 / 无样本：不抛异常，报告标记原因。"""

    @pytest.mark.asyncio
    async def test_llm_unavailable_skips(self, tmp_db: Database) -> None:
        _add_log(tmp_db, "v1", True, 0.50, detail={"text": "文本"})
        bloated = _DictLLM({}, available=False)
        report = await Reviewer().review_once(tmp_db, bloated, _review_cfg())
        assert report.skipped_reason == "llm_unavailable"
        assert report.sampled == 0
        assert report.reviewed == 0

        report_none = await Reviewer().review_once(tmp_db, None, _review_cfg())
        assert report_none.skipped_reason == "llm_unavailable"

    @pytest.mark.asyncio
    async def test_text_unavailable_statistical_mode(self, tmp_db: Database) -> None:
        # detail_json 不含 text/content 等键 → 无法取回原文 → 统计模式
        _add_log(tmp_db, "v1", True, 0.50, detail={"llm": {"is_violation": True}})
        _add_log(tmp_db, "v2", True, 0.65)
        _add_log(tmp_db, "s1", False, 0.45, detail={"semantic": {"confidence": 0.45}})
        _add_log(tmp_db, "s2", False, 0.60)

        report = await Reviewer().review_once(
            tmp_db, _DictLLM({}), _review_cfg(), thresholds=_thresholds()
        )
        assert report.mode == "statistical"
        assert report.skipped_reason == "text_unavailable"
        assert report.sampled == 4
        assert report.reviewed == 0
        assert report.consistent_rate is None
        assert report.stats["text_missing"] == 4
        assert report.stats["band"] == {"low": 0.35, "high": 0.75}
        assert report.stats["original_violations"] == 2
        # 违规占比 0.5 → 无初步建议（watch 仅极端占比时给出）
        assert report.suggestions == []

    @pytest.mark.asyncio
    async def test_statistical_watch_suggestion_on_skewed_band(self, tmp_db: Database) -> None:
        # 违规占比 ≥ 0.7 → 统计模式给出 watch 建议（高边界）
        for i in range(7):
            _add_log(tmp_db, f"v{i}", True, 0.40 + i * 0.02)
        _add_log(tmp_db, "s0", False, 0.42)

        report = await Reviewer().review_once(
            tmp_db, _DictLLM({}), _review_cfg(), thresholds=_thresholds()
        )
        assert report.skipped_reason == "text_unavailable"
        assert report.suggestions
        assert report.suggestions[0]["action"] == "watch"
        assert report.suggestions[0]["key"] == "confidence_high"

    @pytest.mark.asyncio
    async def test_no_samples(self, tmp_db: Database) -> None:
        report = await Reviewer().review_once(tmp_db, _DictLLM({}), _review_cfg())
        assert report.skipped_reason == "no_samples"
        assert report.sampled == 0


# -------------------------------------------------------------- ReviewScheduler


class TestReviewScheduler:
    """触发 / 并发安全 / 落盘 / 自动调度。"""

    def _scheduler(
        self,
        tmp_db: Database,
        tmp_path: Path,
        llm: _DictLLM,
        *,
        interval_seconds: float | None = None,
        interval_min: int = 0,
    ) -> tuple[AppConfig, ReviewScheduler]:
        config = build_config(
            tmp_path, review={"interval_min": interval_min, "band_low": 0.35, "band_high": 0.75}
        )
        scheduler = ReviewScheduler(
            tmp_db, llm, config, data_dir=tmp_path, interval_seconds=interval_seconds
        )
        return config, scheduler

    @pytest.mark.asyncio
    async def test_trigger_writes_report_and_status(self, tmp_db: Database, tmp_path: Path) -> None:
        _add_log(tmp_db, "v1", True, 0.50, detail={"text": "违T"})
        llm = _DictLLM({"违T": _verdict(True)})
        _, scheduler = self._scheduler(tmp_db, tmp_path, llm)

        report = await scheduler.trigger()
        assert report is not None
        assert report.sampled == 1
        assert scheduler.status()["last_run_ts"] == report.ts
        assert scheduler.status()["enabled"] is False  # interval_min=0 不自动调度
        assert scheduler.status()["running"] is False

        files = list((tmp_path / "review_reports").glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["sampled"] == 1
        assert payload["skipped_reason"] is None

    @pytest.mark.asyncio
    async def test_trigger_concurrent_runs_once(self, tmp_db: Database, tmp_path: Path) -> None:
        _add_log(tmp_db, "v1", True, 0.50, detail={"text": "违C"})
        llm = _DictLLM({"违C": _verdict(True)}, delay=0.1)
        _, scheduler = self._scheduler(tmp_db, tmp_path, llm)

        first, second = await asyncio.gather(scheduler.trigger(), scheduler.trigger())
        # 并发两发：其一执行并返回报告，其二被互斥守卫忽略（None）
        assert (first is None) != (second is None)
        ran = first if first is not None else second
        assert ran.sampled == 1
        assert scheduler.status()["running"] is False  # 结束后复位
        files = list((tmp_path / "review_reports").glob("*.json"))
        assert len(files) == 1  # 只落盘一次

    def test_auto_schedule_start_stop(self, tmp_db: Database, tmp_path: Path) -> None:
        _add_log(tmp_db, "v1", True, 0.50, detail={"text": "违A"})
        llm = _DictLLM({"违A": _verdict(True)})
        _, scheduler = self._scheduler(tmp_db, tmp_path, llm, interval_min=1, interval_seconds=0.1)
        scheduler.start()
        assert scheduler.status()["enabled"] is True
        try:
            deadline = time.monotonic() + 5.0
            while scheduler.status()["last_run_ts"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert scheduler.status()["last_run_ts"] is not None  # 自动轮次已执行
        finally:
            scheduler.stop()
        assert list((tmp_path / "review_reports").glob("*.json"))

    def test_start_disabled_when_interval_zero(self, tmp_db: Database, tmp_path: Path) -> None:
        _, scheduler = self._scheduler(tmp_db, tmp_path, _DictLLM({}), interval_min=0)
        scheduler.start()
        assert scheduler._thread is None  # interval_min=0 不启动自动调度

    @pytest.mark.asyncio
    async def test_trigger_after_failed_run_restores_running(self, tmp_path: Path) -> None:
        # 一轮复核整体抛异常（如采样查询失败）也须复位 running 并产出 error 报告
        class _BoomDB:
            """采样即抛异常：触发复核执行失败路径。"""

            def query_logs(self, **kwargs: Any) -> list[dict[str, Any]]:
                raise RuntimeError("db-boom")

        config = build_config(tmp_path, review={"interval_min": 0})
        scheduler = ReviewScheduler(_BoomDB(), _DictLLM({}), config, data_dir=tmp_path)
        report = await scheduler.trigger()
        assert report is not None
        assert report.skipped_reason == "error:RuntimeError"
        assert report.stats["error"] == "db-boom"
        assert scheduler.status()["running"] is False


# ------------------------------------------------------------- admin endpoints


@pytest.fixture
def admin_review_env(tmp_path: Path) -> tuple[Database, TestClient, _DictLLM]:
    """带 reviewer 注入的管理端测试环境（真实 Database + WhitelistMatcher）。"""

    db = Database(tmp_path / "audit.db")
    matcher = WhitelistMatcher(db)
    config = build_config(tmp_path, review={"interval_min": 0, "band_low": 0.35, "band_high": 0.75})
    llm = _DictLLM({})
    scheduler = ReviewScheduler(db, llm, config, data_dir=tmp_path)
    app = create_admin_app(db, matcher, config=_DuckAdminCfg(tmp_path, TOKEN), reviewer=scheduler)
    client = TestClient(app)
    yield db, client, scheduler
    db.close()


def _headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token} if token is not None else {}


class TestReviewEndpoints:
    """/admin/review/run（501/200/202）与 /admin/review/status（501/200）。"""

    def test_run_without_reviewer_501(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckAdminCfg(tmp_path, TOKEN))
        try:
            client = TestClient(app)
            resp = client.post("/admin/review/run", headers=_headers())
            assert resp.status_code == 501
            assert "error" in resp.json()
            status = client.get("/admin/review/status", headers=_headers())
            assert status.status_code == 501
        finally:
            db.close()

    def test_status_without_reviewer_401(self, tmp_path: Path) -> None:
        # 无令牌仍是 401（鉴权先于 501）
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckAdminCfg(tmp_path, TOKEN))
        try:
            client = TestClient(app)
            resp = client.get("/admin/review/status")
            assert resp.status_code == 401
        finally:
            db.close()

    def test_run_and_status_200(self, admin_review_env) -> None:
        db, client, scheduler = admin_review_env
        _add_log(db, "v1", True, 0.50, detail={"text": "违X"})
        db.create_key("sf_review_key", tier="full")

        resp = client.post("/admin/review/run", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        summary = body["summary"]
        assert summary["sampled"] == 1
        assert summary["skipped_reason"] is None
        assert summary["mode"] == "full"

        status = client.get("/admin/review/status", headers=_headers())
        assert status.status_code == 200
        state = status.json()
        assert state["running"] is False
        assert state["last_run_ts"] is not None
        assert state["last_report"] is not None
        assert state["reports_dir"].endswith("review_reports")

    def test_run_202_when_already_running(self, admin_review_env) -> None:
        db, client, scheduler = admin_review_env
        scheduler._running = True  # 模拟已有复核在执行（互斥守卫路径）
        try:
            resp = client.post("/admin/review/run", headers=_headers())
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "running"
        finally:
            scheduler._running = False
