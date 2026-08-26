"""审核 API 契约测试（FastAPI TestClient）：鉴权 / 分级裁剪 / 限流 / health / 脱敏。

对应 T10 任务卡验收：401/403/429、standard/full 分级裁剪 detail、
overrides 权限、health 免认证。容器为真实降级装配（无 torch → 无 embedding/semantic）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from safefusion.api.app import create_app
from safefusion.core.context import AppContext
from safefusion.storage.database import Database

from .conftest import build_config, image_input_b64, png_bytes

FULL_KEY = "sf_full_key_0001"
STD_KEY = "sf_std_key_0001"
DISABLED_KEY = "sf_disabled_key_0001"


def _build_app(tmp_path: Path, keywords: list[tuple[str, str, str | None]] | None = None):
    """装配：预置 KeywordEngine 词库 + 三个测试 Key 后 build 容器。"""
    cfg = build_config(tmp_path)
    db = Database(Path(cfg.data_dir) / "audit.db")
    if keywords:
        db.add_keywords(keywords)
    db.create_key(FULL_KEY, tier="full", note="full")
    db.create_key(STD_KEY, tier="standard", note="standard")
    db.create_key(DISABLED_KEY, tier="standard", enabled=False)
    db.close()
    ctx = AppContext.build(cfg)
    return create_app(config=cfg, container=ctx)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"text": "普通文本"}
    body.update(overrides)
    return body


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(_build_app(tmp_path))


class TestAuth:
    """鉴权：缺失 / 无效 / 禁用 Key → 401；X-Api-Key 可用。"""

    def test_no_key_401(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json=_payload())
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid api key"}

    def test_bad_key_401(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json=_payload(), headers=_auth("sf_wrong_key"))
        assert resp.status_code == 401

    def test_disabled_key_401(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json=_payload(), headers=_auth(DISABLED_KEY))
        assert resp.status_code == 401

    def test_x_api_key_header_ok(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json=_payload(), headers={"X-Api-Key": STD_KEY})
        assert resp.status_code == 200

    def test_bearer_invalid_scheme_401(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/audit", json=_payload(), headers={"Authorization": f"Basic {STD_KEY}"}
        )
        assert resp.status_code == 401


class TestTierTrimming:
    """standard 裁剪 detail；full 保留明细。"""

    def test_standard_detail_none(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json=_payload(), headers=_auth(STD_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_violation"] is False
        assert body["source"] == "basic_rules_pass"
        assert body["detail"] is None
        assert body["cache_hit"] is False
        assert body["request_id"]

    def test_full_detail(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path, keywords=[("色情", "裸聊", "测试")])
        with TestClient(app) as c:
            resp = c.post("/v1/audit", json=_payload(text="裸聊"), headers=_auth(FULL_KEY))
        body = resp.json()
        assert resp.status_code == 200
        assert body["has_violation"] is True  # 语义层不可用 + 关键词强信号 → 保守违规
        assert body["source"] == "semantic"
        assert body["category"] == "色情"
        assert body["detail"]["keyword"]["hits"][0]["keyword"] == "裸聊"
        assert body["detail"]["semantic"]["black_avg"] == 0.0


class TestOverridesPermission:
    """overrides 仅 full 组：standard → 403。"""

    def test_standard_overrides_403(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/audit",
            json=_payload(overrides={"margin_w": 0.01}),
            headers=_auth(STD_KEY),
        )
        assert resp.status_code == 403
        assert resp.json() == {"error": "overrides 仅 full 组可用"}

    def test_full_overrides_ok(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/audit",
            json=_payload(overrides={"margin_w": 0.01}),
            headers=_auth(FULL_KEY),
        )
        assert resp.status_code == 200


class TestRateLimit:
    """进程内每 Key 限流：超限 429。"""

    def test_rate_limit_429(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_RATE_LIMIT", "2")
        app = _build_app(tmp_path)
        with TestClient(app) as c:
            for _ in range(2):
                r = c.post("/v1/audit", json=_payload(), headers=_auth(STD_KEY))
                assert r.status_code == 200
            third = c.post("/v1/audit", json=_payload(), headers=_auth(STD_KEY))
        assert third.status_code == 429
        assert "频繁" in third.json()["error"]


class TestHealth:
    """GET /health 免认证：状态 / 版本 / 降级清单 / 缓存统计。"""

    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.0"
        assert "embedding" in body["degraded"]  # 本环境无 torch → 降级
        assert "semantic" in body["degraded"]
        assert body["cache"]["audit_cache"]["enabled"] is True
        assert body["uptime_s"] >= 0


class TestValidationAndMasking:
    """422 脱敏 JSON；未预期异常 500 脱敏。"""

    def test_validation_error_422(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json={"text": 123}, headers=_auth(STD_KEY))
        assert resp.status_code == 422
        assert resp.json() == {"error": "请求参数校验失败"}

    def test_image_input_both_missing_422(self, client: TestClient) -> None:
        resp = client.post("/v1/audit", json={"images": [{}]}, headers=_auth(STD_KEY))
        assert resp.status_code == 422

    def test_internal_error_masked_500(self, client: TestClient) -> None:
        class _BoomOrchestrator:
            async def process_audit(self, req: Any, tier: str) -> Any:
                raise RuntimeError("内部细节不应泄露")

        client.app.state.orchestrator = _BoomOrchestrator()  # type: ignore[attr-defined]
        # 默认 TestClient 会 re-raise 服务器异常；关闭该行为以观测脱敏响应
        quiet = TestClient(client.app, raise_server_exceptions=False)
        resp = quiet.post("/v1/audit", json=_payload(), headers=_auth(STD_KEY))
        assert resp.status_code == 500
        assert resp.json() == {"error": "internal error"}


class TestCacheRoundtrip:
    """完整链路 + 缓存命中：相同请求二次命中（cache_hit=True）。"""

    def test_second_request_cache_hit(self, client: TestClient) -> None:
        headers = _auth(STD_KEY)
        body = _payload()
        first = client.post("/v1/audit", json=body, headers=headers).json()
        second = client.post("/v1/audit", json=body, headers=headers).json()
        assert first["cache_hit"] is False
        assert second["cache_hit"] is True
        assert second["source"] == "cache"
        assert second["detail"] is None  # standard 缓存也裁剪


class TestImageAudit:
    """图片请求全链路（真实解码+哈希，无网络）。"""

    def test_image_only_request_full(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path)  # 无关键词、白名单为空 → 帧未命中 → 语义层
        with TestClient(app) as c:
            resp = c.post(
                "/v1/audit",
                json={"images": [image_input_b64(png_bytes()).model_dump()]},
                headers=_auth(FULL_KEY),
            )
        body = resp.json()
        assert resp.status_code == 200
        # 非白名单图片 → 风险信号 → 语义层（本环境降级）→ 无强信号 → 安全
        assert body["has_violation"] is False
        assert body["source"] == "semantic"
        assert body["detail"]["image_whitelist"][0]["hit"] is False


class TestAppContextBuild:
    """AppContext.build 组件级降级装配（core/context.py）。"""

    def test_degraded_assembly(self, tmp_path: Path) -> None:
        from safefusion.core.context import AppContext

        cfg = build_config(tmp_path)
        ctx = AppContext.build(cfg)
        # 存储与基础组件真实装配
        assert ctx.database is not None
        assert ctx.cache_layer is not None
        assert ctx.keyword_engine is not None and ctx.keyword_engine.loaded
        assert ctx.store is not None
        assert ctx.whitelist is not None
        assert ctx.light_model is not None and ctx.light_model.disabled is True
        # 无 torch / 无 Key → 多模态组件降级为 None 且计入 degraded
        assert ctx.embedding is None
        assert ctx.semantic is None
        assert ctx.llm is None or ctx.llm.available is False
        for name in ("embedding", "semantic", "llm", "light_model"):
            assert name in ctx.degraded

    def test_keywords_loaded_into_engine(self, tmp_path: Path) -> None:
        from safefusion.core.context import AppContext

        cfg = build_config(tmp_path)
        seed = Database(Path(cfg.data_dir) / "audit.db")
        seed.add_keywords([("色情", "裸聊", "test")])
        seed.close()
        ctx = AppContext.build(cfg)
        assert ctx.keyword_engine is not None
        assert [h.keyword for h in ctx.keyword_engine.scan("裸聊")] == ["裸聊"]

    def test_empty_lexicon_builds_fine(self, tmp_path: Path) -> None:
        from safefusion.core.context import AppContext

        cfg = build_config(tmp_path)
        ctx = AppContext.build(cfg)
        assert ctx.keyword_engine is not None
        assert ctx.keyword_engine.scan("任意文本") == []
