"""管理 API 配置端点与配置覆盖层测试（PRD v0.2.1 M2 / T22 任务卡）。

覆盖：
- **Key 遮蔽**（决策 F）：GET/PUT /admin/config 中 ``api_key`` 一律替换为
  ``{"api_key_env", "configured"}``，响应体绝不含真实密钥值；
- **合并优先级**（决策 D）：内置默认 < config.yaml < 覆盖层 < 环境变量，
  环境变量最高优先、不反向写回；（覆盖层文件原子读写 / 损坏容错 / 分组删除）；
- **PUT 校验**：未知分组 / 非法 backend / 必填缺失 / 数值越界 / ``api_key``
  写入被拒 / ``fuse_mode`` 维度一致性（backend=cloud 且 weighted_avg|pool
  → 422 建议 concat）均返回 422 中文可读错误；空对象 ``{}`` 删除覆盖层分组；
- **静态托管**：``web/dist`` 不存在时无影响；存在时 mount 成功且 /admin/* 路由
  不被 ``/`` 静态目录吞掉（Starlette 按注册顺序匹配）。

沿用 tests/test_api_admin.py 风格：TestClient + tmp_path 真实 SQLite + 鸭子配置。
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from safefusion.api.admin import create_admin_app
from safefusion.config import load_config
from safefusion.core import config_override as co
from safefusion.engines.image_pipeline import WhitelistMatcher
from safefusion.storage.database import Database

api_main = importlib.import_module("safefusion.api.__main__")

TOKEN = "admin-config-token"


class _DuckCfg:
    """带 data_dir 与 admin_token 的配置鸭子类型（与 test_api_admin 同款）。"""

    def __init__(self, data_dir: Path, token: str = TOKEN) -> None:
        self.data_dir = str(data_dir)
        self.admin_token = token


def _headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token} if token is not None else {}


def _put(client: TestClient, group: str, payload: dict) -> Any:
    """PUT /admin/config/{group} 便捷包装（缩短用例行宽）。"""

    return client.put(f"/admin/config/{group}", json=payload, headers=_headers())


def _make_client(tmp_path: Path) -> TestClient:
    db = Database(tmp_path / "audit.db")
    matcher = WhitelistMatcher(db)
    app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
    client = TestClient(app)
    return client


@pytest.fixture
def admin_client(tmp_path: Path) -> TestClient:
    client = _make_client(tmp_path)
    yield client
    client.close()


class TestConfigAuth:
    """配置端点鉴权：缺失 / 错误令牌 401。"""

    def test_get_config_requires_token(self, admin_client) -> None:
        assert admin_client.get("/admin/config").status_code == 401

    def test_wrong_token_401(self, admin_client) -> None:
        resp = admin_client.get("/admin/config", headers=_headers("wrong"))
        assert resp.status_code == 401


class TestGetConfigMasking:
    """GET /admin/config：全量分组 + Key 遮蔽（决策 F）。"""

    def test_groups_returned(self, admin_client) -> None:
        resp = admin_client.get("/admin/config", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        for group in ("embedding", "llm", "thresholds", "cache", "keyword", "semantic", "review"):
            assert group in body
        # embedding 按 {backend, local, cloud} 结构对齐 config.py
        assert body["embedding"]["backend"] == "local"
        assert set(body["embedding"].keys()) == {"backend", "local", "cloud"}
        # 语义组虚拟键 fuse_mode 默认 pool（对齐 SemanticEngine 阈值默认）
        assert body["semantic"]["fuse_mode"] == "pool"

    def test_llm_key_masked_env_set(self, admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_LLM_API_KEY", "sk-env-secret-123")
        resp = admin_client.get("/admin/config", headers=_headers())
        body = resp.json()
        assert body["llm"]["api_key"] == {
            "api_key_env": "OPENAI_API_KEY",
            "configured": True,
        }
        assert "sk-env-secret-123" not in resp.text

    def test_embedding_cloud_key_mask_env_unset(self, admin_client) -> None:
        body = admin_client.get("/admin/config", headers=_headers()).json()
        assert body["embedding"]["cloud"]["api_key"] == {
            "api_key_env": "SAFEFUSION_EMBEDDING_API_KEY",
            "configured": False,
        }

    def test_embedding_cloud_key_configured_env_set(
        self, admin_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAFEFUSION_EMBEDDING_API_KEY", "emb-secret-456")
        resp = admin_client.get("/admin/config", headers=_headers())
        body = resp.json()
        assert body["embedding"]["cloud"]["api_key"]["configured"] is True
        assert "emb-secret-456" not in resp.text

    def test_get_never_leaks_secret_values(
        self, admin_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAFEFUSION_LLM_API_KEY", "llm-secret-aaa")
        monkeypatch.setenv("SAFEFUSION_EMBEDDING_API_KEY", "emb-secret-bbb")
        resp = admin_client.get("/admin/config", headers=_headers())
        assert resp.status_code == 200
        assert "llm-secret-aaa" not in resp.text
        assert "emb-secret-bbb" not in resp.text
        assert "api_key" in resp.text  # 键名可见、值不可见


class TestOverrideMerge:
    """覆盖层合并优先级（决策 D）：默认 < YAML < 覆盖层 < 环境变量。"""

    def test_defaults_when_no_override(self, tmp_path: Path) -> None:
        eff = co.effective_config(load_config(None), tmp_path)
        assert eff.thresholds.semantic_threshold == 0.67

    def test_override_beats_default(self, tmp_path: Path) -> None:
        co.save_overrides(tmp_path, "thresholds", {"semantic_threshold": 0.5})
        eff = co.effective_config(load_config(None), tmp_path)
        assert eff.thresholds.semantic_threshold == 0.5
        assert eff.thresholds.margin_w == 0.05  # 未覆盖键仍取默认

    def test_env_beats_override_not_reverse_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co.save_overrides(tmp_path, "thresholds", {"semantic_threshold": 0.5})
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.9")
        eff = co.effective_config(load_config(None), tmp_path)
        assert eff.thresholds.semantic_threshold == 0.9
        # 不反向写回：覆盖层文件保持原值，环境变量只覆盖运行值
        raw = json.loads((tmp_path / "config_overrides.json").read_text(encoding="utf-8"))
        assert raw["thresholds"]["semantic_threshold"] == 0.5

    def test_yaml_below_override(self, tmp_path: Path) -> None:
        yaml_text = "thresholds:\n  semantic_threshold: 0.55\n"
        (tmp_path / "cfg.yaml").write_text(yaml_text, encoding="utf-8")
        co.save_overrides(tmp_path, "thresholds", {"semantic_threshold": 0.5})
        eff = co.effective_config(load_config(str(tmp_path / "cfg.yaml")), tmp_path)
        assert eff.thresholds.semantic_threshold == 0.5  # 覆盖层 > YAML

    def test_partial_override_keeps_others(self, tmp_path: Path) -> None:
        co.save_overrides(tmp_path, "llm", {"model": "gpt-test"})
        eff = co.effective_config(load_config(None), tmp_path)
        assert eff.llm.model == "gpt-test"
        assert eff.llm.base_url == "https://api.openai.com/v1"  # 未覆盖键沿用默认

    def test_unknown_group_dropped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        co.save_overrides(tmp_path, "bogus_group", {"x": 1})
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            eff = co.effective_config(load_config(None), tmp_path)
        assert "未知分组" in caplog.text
        assert eff.thresholds.semantic_threshold == 0.67  # 其余分组不受影响

    def test_corrupt_file_tolerated(self, tmp_path: Path) -> None:
        (tmp_path / "config_overrides.json").write_text("{broken", encoding="utf-8")
        assert co.load_overrides(tmp_path) == {}
        eff = co.effective_config(load_config(None), tmp_path)
        assert eff.thresholds.semantic_threshold == 0.67

    def test_merge_strips_hand_edited_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 红线防御：手工编辑的覆盖层若携带 api_key，加载合并时剥离并告警
        (tmp_path / "config_overrides.json").write_text(
            json.dumps({"llm": {"api_key": "hand-secret", "model": "gpt-x"}}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            eff = co.effective_config(load_config(None), tmp_path)
        assert "api_key" in caplog.text
        assert eff.llm.model == "gpt-x"  # 非密钥键仍生效
        assert eff.llm.api_key is None  # 密钥值未进入配置模型

    def test_save_update_delete_single_group(self, tmp_path: Path) -> None:
        path = co.save_overrides(tmp_path, "server", {"port": 9999})
        assert path == tmp_path / "config_overrides.json"
        merged = co.update_overrides(tmp_path, "server", {"admin_port": 9001})
        assert merged == {"port": 9999, "admin_port": 9001}
        assert co.delete_group_overrides(tmp_path, "server") is True
        assert co.load_overrides(tmp_path) == {}
        assert not path.exists()  # 无剩余分组时移除空壳文件
        assert co.delete_group_overrides(tmp_path, "server") is False
        assert list(tmp_path.glob("*.tmp")) == []  # 原子写不残留临时文件


class TestPutConfig:
    """PUT /admin/config/{group}：校验、落盘、恢复默认、Key 遮蔽。"""

    def test_put_valid_thresholds_writes_override(self, admin_client, tmp_path: Path) -> None:
        resp = admin_client.put(
            "/admin/config/thresholds",
            json={"semantic_threshold": 0.5, "margin_w": 0.1},
            headers=_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is True
        assert body["restart_required"] is True
        assert body["group"] == "thresholds"
        assert body["config"]["semantic_threshold"] == 0.5
        raw = json.loads((tmp_path / "config_overrides.json").read_text(encoding="utf-8"))
        assert raw["thresholds"] == {"semantic_threshold": 0.5, "margin_w": 0.1}
        got = admin_client.get("/admin/config", headers=_headers()).json()
        assert got["thresholds"]["semantic_threshold"] == 0.5

    def test_put_partial_keeps_unchanged_fields(self, admin_client) -> None:
        resp = admin_client.put("/admin/config/llm", json={"model": "gpt-x"}, headers=_headers())
        assert resp.status_code == 200
        got = admin_client.get("/admin/config", headers=_headers()).json()
        assert got["llm"]["model"] == "gpt-x"
        assert got["llm"]["base_url"] == "https://api.openai.com/v1"

    def test_put_unknown_group_422_readable(self, admin_client) -> None:
        resp = admin_client.put("/admin/config/nope", json={}, headers=_headers())
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert "未知配置分组" in error
        assert "thresholds" in error and "llm" in error  # 错误信息列出可选分组

    def test_put_invalid_backend_422(self, admin_client) -> None:
        resp = _put(admin_client, "embedding", {"backend": "banana"})
        assert resp.status_code == 422
        assert "unknown" not in resp.json()["error"]
        assert "可选 local / cloud" in resp.json()["error"]

    def test_put_invalid_cache_backend_422(self, admin_client) -> None:
        resp = _put(admin_client, "cache", {"backend": "banana"})
        assert resp.status_code == 422
        assert "可选 memory / redis" in resp.json()["error"]

    def test_put_out_of_range_422(self, admin_client) -> None:
        resp = admin_client.put(
            "/admin/config/thresholds", json={"semantic_threshold": 1.5}, headers=_headers()
        )
        assert resp.status_code == 422
        assert "超出范围" in resp.json()["error"]

    def test_put_unknown_field_422(self, admin_client) -> None:
        resp = admin_client.put(
            "/admin/config/thresholds", json={"bogus_field": 0.5}, headers=_headers()
        )
        assert resp.status_code == 422
        assert "未知配置键" in resp.json()["error"]

    def test_put_cloud_requires_base_url_and_model(self, admin_client) -> None:
        resp = _put(admin_client, "embedding", {"backend": "cloud"})
        assert resp.status_code == 422
        assert "必填字段缺失" in resp.json()["error"]
        assert "base_url" in resp.json()["error"]

    def test_put_cloud_with_pool_fuse_mode_422_suggests_concat(self, admin_client) -> None:
        resp = admin_client.put(
            "/admin/config/embedding",
            json={"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}},
            headers=_headers(),
        )
        assert resp.status_code == 422
        assert "concat" in resp.json()["error"]  # 可读提示建议改用 concat

    def test_put_fuse_mode_concat_then_cloud_ok(self, admin_client) -> None:
        first = admin_client.put(
            "/admin/config/semantic", json={"fuse_mode": "concat"}, headers=_headers()
        )
        assert first.status_code == 200
        assert first.json()["config"]["fuse_mode"] == "concat"
        second = admin_client.put(
            "/admin/config/embedding",
            json={"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}},
            headers=_headers(),
        )
        assert second.status_code == 200
        assert second.json()["config"]["backend"] == "cloud"
        # 遮蔽：在线 Key 仍未配置也不泄露
        assert second.json()["config"]["cloud"]["api_key"]["configured"] is False

    def test_put_semantic_bad_fuse_mode_422(self, admin_client) -> None:
        resp = _put(admin_client, "semantic", {"fuse_mode": "banana"})
        assert resp.status_code == 422
        assert "concat" in resp.json()["error"]

    def test_put_semantic_weighted_avg_with_cloud_backend_422(
        self, admin_client, tmp_path: Path
    ) -> None:
        # 先落盘：语义 fuse_mode=concat + embedding backend=cloud（绕过组合校验直写）
        assert (
            admin_client.put(
                "/admin/config/semantic", json={"fuse_mode": "concat"}, headers=_headers()
            ).status_code
            == 200
        )
        assert (
            admin_client.put(
                "/admin/config/embedding",
                json={"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}},
                headers=_headers(),
            ).status_code
            == 200
        )
        resp = admin_client.put(
            "/admin/config/semantic", json={"fuse_mode": "weighted_avg"}, headers=_headers()
        )
        assert resp.status_code == 422
        assert "改用 concat" in resp.json()["error"]
        # 校验失败不落盘
        raw = json.loads((tmp_path / "config_overrides.json").read_text(encoding="utf-8"))
        assert raw["semantic"]["fuse_mode"] == "concat"

    def test_put_api_key_rejected_422(self, admin_client, tmp_path: Path) -> None:
        resp = _put(admin_client, "llm", {"api_key": "sk-huge-secret"})
        assert resp.status_code == 422
        assert "禁止写入密钥字段" in resp.json()["error"]
        assert "sk-huge-secret" not in resp.text
        # api_key_env（变量名）仍可配置——不是密钥值
        ok = admin_client.put(
            "/admin/config/llm", json={"api_key_env": "MY_CUSTOM_KEY"}, headers=_headers()
        )
        assert ok.status_code == 200

    def test_put_empty_body_resets_group(self, admin_client, tmp_path: Path) -> None:
        _put(admin_client, "thresholds", {"semantic_threshold": 0.5})
        resp = _put(admin_client, "thresholds", {})
        assert resp.status_code == 200
        assert resp.json()["deleted_override"] is True
        assert resp.json()["config"]["semantic_threshold"] == 0.67  # 恢复默认
        # 删除后无剩余分组：覆盖层文件整体移除（不留空壳）
        assert co.load_overrides(tmp_path) == {}
        assert not (tmp_path / "config_overrides.json").exists()

    def test_put_required_field_not_empty(self, admin_client) -> None:
        resp = admin_client.put("/admin/config/llm", json={"model": ""}, headers=_headers())
        assert resp.status_code == 422
        assert "不能为空" in resp.json()["error"]


class TestStaticMount:
    """web/dist 静态托管（PRD 风险表：不存在时无影响；存在时挂载且不吞 /admin/*）。"""

    def test_no_dist_no_mount(self) -> None:
        app = FastAPI()
        assert api_main.maybe_mount_web_dist(app, "E:/no/such/web/dist") is False
        assert not any(isinstance(route, Mount) for route in app.routes)

    def test_mount_when_index_exists(self, tmp_path: Path) -> None:
        dist = tmp_path / "web" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html>sf</html>", encoding="utf-8")
        app = FastAPI()
        assert api_main.maybe_mount_web_dist(app, dist) is True
        assert any(isinstance(route, Mount) for route in app.routes)

    def test_admin_routes_not_shadowed_after_mount(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        admin_app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path))
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>sf-panel</html>", encoding="utf-8")
        assert api_main.maybe_mount_web_dist(admin_app, dist) is True
        client = TestClient(admin_app)
        # /admin/* 优先命中 API 路由（挂载在 include_router 之后）
        assert client.get("/admin/keys", headers=_headers()).status_code == 200
        assert client.get("/", headers=_headers()).status_code == 200
        assert "sf-panel" in client.get("/", headers=_headers()).text
        client.close()
        db.close()
