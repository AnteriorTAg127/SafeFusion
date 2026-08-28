"""配置 DB 化 + 全量热应用 + 迁移 + 来源标识测试（PRD v0.3.0 M4 / T30A 任务卡）。

覆盖（完成标准 1）：
- **settings DAO**：list/get/set/delete_group、JSON 往返、upsert 覆盖、排序、
  缺失读取 None；
- **优先级**：内置默认 < config.yaml < DB settings < 环境变量（env 只读内存
  生效、绝不写 DB、不反向写回）；嵌套叶子（点分路径）与 fuse_mode 真叶子合并；
- **来源标识**：``config_sources`` 与 ``GET /admin/config/sources`` 的字段级
  default/yaml/db/env 映射（T39 契约）；
- **三类热应用**：参数类（thresholds 直接改实例 + 语义阈值原子替换）、
  后端切换（embedding 重建 + SemanticEngine 重绑 store）、密码（AdminToken
  热切换，旧令牌立即 401）；
- **失败回滚**：假 backend 试建造抛异常 → 500、DB 不写、旧实例仍可用；
- **迁移幂等**：旧 ``config_overrides.json`` 一次性导入 + ``.migrated`` 归档、
  二次迁移无副作用、损坏文件容错；
- **env 不写 DB**：环境变量配置不产生 settings 行。

测试环境约定：容器一律用 ``embedding.backend="cloud"``（无 Key 时装配即
降级，**避免触发真实 Chinese-CLIP 模型加载**）；热重建路径统一
``monkeypatch`` ``safefusion.core.hot_apply.build_embedding`` 注入假后端，
不依赖 ML 环境。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from safefusion.api.admin import create_admin_app
from safefusion.config import load_config
from safefusion.core import config_override as co
from safefusion.core import hot_apply
from safefusion.core.context import AppContext
from safefusion.engines.image_pipeline import WhitelistMatcher
from safefusion.storage.database import Database

from .conftest import build_config
from .fakes import FakeEmbedding

TOKEN = "sf-db-token"


# ------------------------------------------------------------------ 夹具工具


def _cloud_config(tmp_path: Path) -> Any:
    """最小配置：data_dir=tmp + embedding backend=cloud（无 Key → 免真实模型加载）。"""

    return build_config(tmp_path, embedding={"backend": "cloud"})


class _AdminDuckCfg:
    """带 data_dir 与 admin_token 的配置鸭子类型（管理令牌确定化）。"""

    def __init__(self, tmp_path: Path, token: str) -> None:
        self.data_dir = str(tmp_path)
        self.admin_token = token


def _make_admin(tmp_path: Path, token: str = TOKEN, reviewer: Any = None) -> TestClient:
    """带共享容器的管理应用（PUT 全量热应用目标）。"""

    db = Database(tmp_path / "audit.db")
    container = AppContext.build(_cloud_config(tmp_path), database=db)
    app = create_admin_app(
        db,
        WhitelistMatcher(db),
        config=_AdminDuckCfg(tmp_path, token),
        reviewer=reviewer,
        container=container,
    )
    client = TestClient(app)
    return client


def _headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token} if token is not None else {}


@pytest.fixture
def admin_client(tmp_path: Path) -> TestClient:
    client = _make_admin(tmp_path)
    yield client
    client.close()


# ------------------------------------------------------------- TestSettingsDao


class TestSettingsDao:
    """settings 表 DAO：list/get/set/delete_group + JSON 往返 + upsert。"""

    def test_set_list_roundtrip_and_ordering(self, tmp_db: Database) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5, "margin_w": 0.1})
        tmp_db.set_settings("llm", {"model": "gpt-x"})
        rows = tmp_db.list_settings()
        # 按 (group, key) 排序
        assert [(r["group"], r["key"]) for r in rows] == [
            ("llm", "model"),
            ("thresholds", "margin_w"),
            ("thresholds", "semantic_threshold"),
        ]
        # JSON 值字符串化（浮点 0.5 → "0.5"）
        row = tmp_db.get_setting("thresholds", "semantic_threshold")
        assert row is not None
        assert json.loads(row["value_json"]) == 0.5
        assert row["updated_at"]

    def test_upsert_overwrites_value(self, tmp_db: Database) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.8, "margin_w": 0.2})
        rows = {r["key"]: json.loads(r["value_json"]) for r in tmp_db.list_settings("thresholds")}
        assert rows == {"semantic_threshold": 0.8, "margin_w": 0.2}

    def test_get_missing_returns_none(self, tmp_db: Database) -> None:
        assert tmp_db.get_setting("thresholds", "nope") is None

    def test_delete_group(self, tmp_db: Database) -> None:
        tmp_db.set_settings("llm", {"model": "gpt-x"})
        assert tmp_db.delete_settings("llm") is True
        assert tmp_db.list_settings("llm") == []
        assert tmp_db.delete_settings("llm") is False

    def test_list_group_filter(self, tmp_db: Database) -> None:
        tmp_db.set_settings("llm", {"model": "a"})
        tmp_db.set_settings("thresholds", {"margin_w": 0.1})
        assert [r["key"] for r in tmp_db.list_settings("llm")] == ["model"]

    def test_nested_dotted_keys_roundtrip(self, tmp_db: Database) -> None:
        tmp_db.set_settings("embedding", {"local.model_name": "x", "backend": "cloud"})
        rows = {r["key"]: r["key"] for r in tmp_db.list_settings("embedding")}
        assert set(rows) == {"local.model_name", "backend"}


# ---------------------------------------------------------- TestPriorityMerge


class TestPriorityMerge:
    """优先级：内置默认 < config.yaml < DB settings < 环境变量（决策 B）。"""

    def test_defaults_when_no_settings(self, tmp_db: Database) -> None:
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.67

    def test_db_beats_default(self, tmp_db: Database) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.5
        assert eff.thresholds.margin_w == 0.05  # 未覆盖键仍取默认

    def test_env_beats_db_not_reverse_written(
        self, tmp_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.9")
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.9
        # env 只读内存生效：DB settings 行保持原值（env 绝不写 DB、不反向写回）
        row = tmp_db.get_setting("thresholds", "semantic_threshold")
        assert json.loads(row["value_json"]) == 0.5

    def test_yaml_below_db(self, tmp_path: Path, tmp_db: Database) -> None:
        yaml_text = "thresholds:\n  semantic_threshold: 0.55\n"
        (tmp_path / "cfg.yaml").write_text(yaml_text, encoding="utf-8")
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        eff = co.effective_config(load_config(str(tmp_path / "cfg.yaml")), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.5  # DB > YAML

    def test_partial_db_keeps_others(self, tmp_db: Database) -> None:
        tmp_db.set_settings("llm", {"model": "gpt-test"})
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.llm.model == "gpt-test"
        assert eff.llm.base_url == "https://api.openai.com/v1"  # 未覆盖键沿用默认

    def test_nested_dotted_leaf_applied(self, tmp_db: Database) -> None:
        tmp_db.set_settings("embedding", {"local.model_name": "custom-model"})
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.embedding.local.model_name == "custom-model"

    def test_fuse_mode_is_real_leaf(self, tmp_db: Database) -> None:
        tmp_db.set_settings("semantic", {"fuse_mode": "concat"})
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.semantic.fuse_mode == "concat"

    def test_unknown_group_dropped_with_warning(
        self, tmp_db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        tmp_db.set_settings("bogus_group", {"x": 1})
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            eff = co.effective_config(load_config(None), tmp_db)
        assert "未知分组" in caplog.text
        assert eff.thresholds.semantic_threshold == 0.67

    def test_merge_strips_hand_edited_secret(
        self, tmp_db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 红线防御：手工写入 DB 的密钥行，合并时剥离并告警
        tmp_db.set_settings("llm", {"api_key": "hand-secret", "model": "gpt-x"})
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            eff = co.effective_config(load_config(None), tmp_db)
        assert "api_key" in caplog.text
        assert eff.llm.model == "gpt-x"  # 非密钥键仍生效
        assert eff.llm.api_key is None  # 密钥值未进入配置模型

    def test_corrupt_value_row_skipped(
        self, tmp_path: Path, tmp_db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        # 旁路直写损坏 JSON 行（绕过 DAO 的序列化保护）
        conn = sqlite3.connect(str(tmp_path / "audit.db"))
        conn.execute(
            'INSERT INTO settings ("group", key, value_json, updated_at) VALUES (?, ?, ?, ?)',
            ("thresholds", "margin_w", "{broken", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            eff = co.effective_config(load_config(None), tmp_db)
        assert "JSON 解析失败" in caplog.text
        assert eff.thresholds.semantic_threshold == 0.5  # 完好行仍生效
        assert eff.thresholds.margin_w == 0.05  # 损坏行跳过 → 默认值


# --------------------------------------------------------- TestEffectiveSources


class TestEffectiveSources:
    """来源标识：default / yaml / db / env 字段级映射（决策 B / T39 契约）。"""

    def test_sources_via_config_sources(
        self, tmp_path: Path, tmp_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yaml_text = "thresholds:\n  margin_w: 0.09\n"
        (tmp_path / "cfg.yaml").write_text(yaml_text, encoding="utf-8")
        base = load_config(str(tmp_path / "cfg.yaml"))
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_CONFIDENCE_LOW", "0.4")
        sources = co.config_sources(base, tmp_db)["thresholds"]
        # semantic_threshold 来自 DB；margin_w 由 YAML 偏离默认
        assert sources["semantic_threshold"] == "db"
        assert sources["margin_w"] == "yaml"
        # confidence_low 被环境变量钉住 → env
        assert sources["confidence_low"] == "env"
        # 其余未设置 → default
        assert sources["confidence_high"] == "default"
        assert sources["phash_whitelist_distance"] == "default"

    def test_secret_leaf_source(self, tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        base = load_config(None)
        sources = co.config_sources(base, tmp_db)
        assert sources["llm"]["api_key"] == "default"  # 密钥未配 → default
        monkeypatch.setenv("SAFEFUSION_LLM_API_KEY", "sk-x")
        sources = co.config_sources(load_config(None), tmp_db)
        assert sources["llm"]["api_key"] == "env"  # 密钥从环境变量 → env

    def test_nested_leaf_source_key(self, tmp_db: Database) -> None:
        tmp_db.set_settings("embedding", {"local.model_name": "m"})
        sources = co.config_sources(load_config(None), tmp_db)
        assert sources["embedding"]["local.model_name"] == "db"
        assert sources["embedding"]["local.device"] == "default"

    def test_sources_endpoint_contract(self, admin_client: TestClient, tmp_db: Database) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5})
        resp = admin_client.get("/admin/config/sources", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        # 顶层键 = 分组白名单
        for group in ("embedding", "llm", "thresholds", "cache", "keyword", "semantic", "review"):
            assert group in body
        # 组内键 = 叶子点分路径；值 ∈ 四层来源
        th = body["thresholds"]
        assert th["semantic_threshold"] == "db"
        assert th["margin_w"] == "default"
        assert all(v in ("default", "yaml", "db", "env") for v in th.values())
        emb = body["embedding"]
        assert "local.model_name" in emb  # 嵌套子模型用点连接
        assert body["llm"]["api_key"] in ("env", "default")  # 密钥只可能这两者

    def test_env_never_writes_db(
        self, admin_client: TestClient, tmp_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.77")
        resp = admin_client.get("/admin/config", headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["thresholds"]["semantic_threshold"] == 0.77
        # env 只读内存生效：settings 表零行（env 绝不写 DB）
        assert tmp_db.list_settings() == []


# ---------------------------------------------------------- TestHotApplyParam


class TestHotApplyParam:
    """参数类热应用：写 DB 后立即改运行中实例（阈值 / 语义权重）。"""

    def test_thresholds_applied_immediately(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        # 不依赖 ML 环境：语义桩记录阈值字典（reload_semantic_thresholds 的目标）
        container.semantic = types.SimpleNamespace(thresholds={})  # type: ignore[assignment]
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        resp = client.put(
            "/admin/config/thresholds",
            json={"semantic_threshold": 0.45},
            headers=_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is True
        assert body["apply_scope"] == "runtime"
        assert body["saved"] is True
        assert body["deleted_db_group"] is False
        assert body["config"]["semantic_threshold"] == 0.45
        assert body["sources"]["semantic_threshold"] == "db"
        # 运行中实例就地生效（编排器引用同一对象）
        assert container.config.thresholds.semantic_threshold == 0.45
        # 语义引擎阈值字典原子替换（含阈值 + 语义分组键）
        assert container.semantic.thresholds["semantic_threshold"] == 0.45
        assert container.semantic.thresholds["fuse_mode"] == "pool"
        # DB 落库（部分键覆盖语义：只写负载键）
        rows = db.list_settings("thresholds")
        assert {r["key"] for r in rows} == {"semantic_threshold"}
        client.close()
        db.close()

    def test_semantic_group_applied(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        container.semantic = types.SimpleNamespace(thresholds={})  # type: ignore[assignment]
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        resp = client.put(
            "/admin/config/semantic", json={"rerank_enabled": True}, headers=_headers()
        )
        assert resp.status_code == 200
        assert container.config.semantic.rerank_enabled is True
        assert container.semantic.thresholds["rerank_enabled"] is True
        client.close()
        db.close()

    def test_keyword_rules_switch_reloads(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        # 记录 reload 调用（reload_hook 语义在热应用后置同步中触发）
        calls: list[str] = []
        original = container.reload_keywords

        def _tracked() -> bool:
            calls.append("reload")
            return original()

        container.reload_keywords = _tracked  # type: ignore[method-assign]
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        resp = client.put(
            "/admin/config/keyword", json={"regex_rules_enabled": False}, headers=_headers()
        )
        assert resp.status_code == 200
        assert container.config.keyword.regex_rules_enabled is False
        assert calls == ["reload"]  # 后置同步触发词库/规则重载（开关即时生效）
        client.close()
        db.close()


# --------------------------------------------------------- TestHotApplyRebuild


class TestHotApplyRebuild:
    """组件重建类热应用：后端切换（embedding → SemanticEngine 重绑 store，原子替换）。"""

    def test_embedding_backend_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        old_embedding = container.embedding  # cloud 无 Key → None（降级）
        old_semantic = container.semantic

        fake = FakeEmbedding(texts={"x": np.ones(4, dtype="float32")})
        monkeypatch.setattr(hot_apply, "build_embedding", lambda _cfg: fake)
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        # 语义 fuse_mode 先切 concat（cloud 校验要求；参数类 → 立即生效）
        assert (
            client.put(
                "/admin/config/semantic", json={"fuse_mode": "concat"}, headers=_headers()
            ).status_code
            == 200
        )
        resp = client.put(
            "/admin/config/embedding",
            json={"backend": "local", "local": {"model_name": "fake-model"}},
            headers=_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is True
        assert body["config"]["backend"] == "local"
        # 原子替换：新 embedding 生效、语义引擎重建并重绑新 embedding + 复用 store
        assert container.embedding is fake
        assert container.semantic is not None
        assert container.semantic.embedding is fake
        assert container.semantic.store is container.store
        assert old_embedding != container.embedding
        assert old_semantic != container.semantic
        # 旧 embedding 未受影响（本用例为 None；替换失败路径见回滚测试）
        row = db.get_setting("embedding", "backend")
        assert row is not None and json.loads(row["value_json"]) == "local"
        # degraded 清单刷新：embedding/semantic 不再降级
        assert "embedding" not in container.degraded
        assert "semantic" not in container.degraded
        client.close()
        db.close()

    def test_llm_rebuild_and_reviewer_reload(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        old_llm = container.llm

        class _ReviewerStub:
            def __init__(self) -> None:
                self.reloaded: list[Any] = []

            def reload_llm(self, llm: Any) -> None:
                self.reloaded.append(llm)

        reviewer = _ReviewerStub()
        app = create_admin_app(
            db,
            WhitelistMatcher(db),
            config=_AdminDuckCfg(tmp_path, TOKEN),
            container=container,
            reviewer=reviewer,
        )
        client = TestClient(app)
        resp = client.put(
            "/admin/config/llm",
            json={"base_url": "http://llm.local/v1", "model": "gpt-test"},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] is True
        assert container.llm is not None
        assert container.llm is not old_llm  # 新客户端实例
        assert reviewer.reloaded == [container.llm]  # 调度器专用客户端同源热切
        row = db.get_setting("llm", "base_url")
        assert json.loads(row["value_json"]) == "http://llm.local/v1"
        client.close()
        db.close()

    def test_cache_rebuild(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        old_cache = container.cache_layer
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        resp = client.put(
            "/admin/config/cache",
            json={"audit_cache": {"capacity": 123}},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert container.cache_layer is not None and container.cache_layer is not old_cache
        assert (
            client.get("/admin/config", headers=_headers()).json()["cache"]["audit_cache"][
                "capacity"
            ]
            == 123
        )
        client.close()
        db.close()


# --------------------------------------------------------- TestHotApplyRollback


class TestHotApplyRollback:
    """失败回滚：试建造抛异常 → 500、DB 不写、旧实例继续生效且可用。"""

    def test_failed_rebuild_keeps_old_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = Database(tmp_path / "audit.db")
        # 先注入可用假 embedding（旧实例）
        old_fake = FakeEmbedding(texts={"ok": np.ones(4, dtype="float32")})
        monkeypatch.setattr(hot_apply, "build_embedding", lambda _cfg: old_fake)
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        # 第一次 PUT：成功切换（旧实例 = old_fake）
        first = client.put(
            "/admin/config/embedding",
            json={"backend": "local", "local": {"model_name": "fake-a"}},
            headers=_headers(),
        )
        assert first.status_code == 200
        assert container.embedding is old_fake

        # 第二次 PUT：假 backend 构造抛异常 → 试建造失败
        def _boom(_cfg: Any) -> Any:
            raise RuntimeError("fake backend 构造失败（模拟不可用后端）")

        monkeypatch.setattr(hot_apply, "build_embedding", _boom)
        resp = client.put(
            "/admin/config/embedding",
            json={"backend": "local", "local": {"model_name": "fake-b"}},
            headers=_headers(),
        )
        assert resp.status_code == 500
        error = resp.json()["error"]
        assert "已回滚" in error and "未落库" in error
        assert "fake backend" in error
        # DB 不写：本次负载未落库，旧值不变
        rows = {r["key"]: json.loads(r["value_json"]) for r in db.list_settings("embedding")}
        assert rows.get("local.model_name") == "fake-a"
        # 旧实例仍生效且可用（替换失败不破坏运行中组件）
        assert container.embedding is old_fake
        out = container.embedding.encode_texts(["ok"])
        assert out.shape == (1, 4)
        # 管理端配置视图仍为旧值
        got = client.get("/admin/config", headers=_headers()).json()
        assert got["embedding"]["local"]["model_name"] == "fake-a"
        client.close()
        db.close()

    def test_delete_group_reverts_and_reapplies(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        container = AppContext.build(_cloud_config(tmp_path), database=db)
        container.semantic = types.SimpleNamespace(thresholds={})  # type: ignore[assignment]
        app = create_admin_app(
            db, WhitelistMatcher(db), config=_AdminDuckCfg(tmp_path, TOKEN), container=container
        )
        client = TestClient(app)
        client.put(
            "/admin/config/thresholds",
            json={"semantic_threshold": 0.45},
            headers=_headers(),
        )
        resp = client.put("/admin/config/thresholds", json={}, headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_db_group"] is True
        assert body["config"]["semantic_threshold"] == 0.67  # 恢复默认并热应用
        assert container.config.thresholds.semantic_threshold == 0.67
        assert container.semantic.thresholds["semantic_threshold"] == 0.67
        assert db.list_settings("thresholds") == []
        client.close()
        db.close()


# ---------------------------------------------------------- TestHotApplyPassword


class TestHotApplyPassword:
    """密码热应用：AdminToken 热切换，旧令牌立即失效（决策 M4 C）。"""

    def test_token_hot_swap_via_app_state(self, admin_client: TestClient) -> None:
        app = admin_client.app
        assert admin_client.get("/admin/config", headers=_headers(TOKEN)).status_code == 200
        # 热切换：设置新令牌（旧令牌立即 401，新令牌 200）
        hot_apply.apply_admin_token(app.state.admin_token, "new-admin-token-abc")
        assert admin_client.get("/admin/config", headers=_headers(TOKEN)).status_code == 401
        assert (
            admin_client.get("/admin/config", headers=_headers("new-admin-token-abc")).status_code
            == 200
        )

    def test_apply_admin_token_requires_set(self) -> None:
        with pytest.raises(TypeError):
            hot_apply.apply_admin_token(object(), "x")  # type: ignore[arg-type]


# ----------------------------------------------------------------- TestMigration


class TestMigration:
    """启动迁移：旧覆盖层文件 → settings 表 + .migrated 归档（幂等）。"""

    def test_migrate_once_and_archive(
        self, tmp_path: Path, tmp_db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        legacy = {
            "thresholds": {"semantic_threshold": 0.5, "margin_w": 0.1},
            "embedding": {"backend": "cloud", "cloud": {"base_url": "http://x", "model": "m"}},
            "semantic": {"fuse_mode": "concat"},
        }
        (tmp_path / co.OVERRIDES_FILENAME).write_text(json.dumps(legacy), encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="safefusion.config_override"):
            migrated = co.migrate_overrides_file(tmp_path, tmp_db)
        assert migrated is True
        # 旧文件已归档改名（内容不丢）
        assert not (tmp_path / co.OVERRIDES_FILENAME).exists()
        assert (tmp_path / (co.OVERRIDES_FILENAME + co.MIGRATED_SUFFIX)).is_file()
        # settings 落库（展平为叶子点分路径）
        rows = {(r["group"], r["key"]): json.loads(r["value_json"]) for r in tmp_db.list_settings()}
        assert rows[("thresholds", "semantic_threshold")] == 0.5
        assert rows[("embedding", "cloud.base_url")] == "http://x"
        assert rows[("semantic", "fuse_mode")] == "concat"
        # 迁移后有效配置立即生效
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.5
        assert eff.semantic.fuse_mode == "concat"

    def test_migration_idempotent(self, tmp_path: Path, tmp_db: Database) -> None:
        legacy = {"thresholds": {"semantic_threshold": 0.5}}
        path = tmp_path / co.OVERRIDES_FILENAME
        path.write_text(json.dumps(legacy), encoding="utf-8")
        assert co.migrate_overrides_file(tmp_path, tmp_db) is True
        # 二次调用：文件已归档不存在 → False；行数不变（无重复导入）
        assert co.migrate_overrides_file(tmp_path, tmp_db) is False
        rows = tmp_db.list_settings("thresholds")
        assert len(rows) == 1 and json.loads(rows[0]["value_json"]) == 0.5

    def test_no_file_returns_false(self, tmp_path: Path, tmp_db: Database) -> None:
        assert co.migrate_overrides_file(tmp_path, tmp_db) is False

    def test_corrupt_legacy_archived_without_crash(
        self, tmp_path: Path, tmp_db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / co.OVERRIDES_FILENAME).write_text("{broken", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="safefusion.config_override"):
            migrated = co.migrate_overrides_file(tmp_path, tmp_db)
        # 损坏容错：仍归档、不阻止（调用方继续启动）、settings 零行
        assert migrated is True
        assert tmp_db.list_settings() == []
        assert (tmp_path / (co.OVERRIDES_FILENAME + co.MIGRATED_SUFFIX)).is_file()
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.67  # 按默认继续

    def test_startup_flow_migrate_then_effective(self, tmp_path: Path) -> None:
        """模拟启动流程：迁移 → effective（default<YAML<DB<env）→ 容器复用连接。"""
        legacy = {"thresholds": {"semantic_threshold": 0.42}}
        (tmp_path / co.OVERRIDES_FILENAME).write_text(json.dumps(legacy), encoding="utf-8")
        base = load_config(None)
        db = Database(tmp_path / "audit.db")
        co.migrate_overrides_file(tmp_path, db)
        config = co.effective_config(base, db=db)
        assert config.thresholds.semantic_threshold == 0.42
        container = AppContext.build(config, database=db)
        assert container.database is db  # 复用启动迁移连接
        assert container.config.thresholds.semantic_threshold == 0.42
        container.database.close()
        db.close()

    def test_env_layer_still_wins_after_migration(
        self, tmp_path: Path, tmp_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = {"thresholds": {"semantic_threshold": 0.5}}
        (tmp_path / co.OVERRIDES_FILENAME).write_text(json.dumps(legacy), encoding="utf-8")
        co.migrate_overrides_file(tmp_path, tmp_db)
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.9")
        eff = co.effective_config(load_config(None), tmp_db)
        assert eff.thresholds.semantic_threshold == 0.9  # env 仍最高
        # 迁移后 env 依旧不写 DB
        row = tmp_db.get_setting("thresholds", "semantic_threshold")
        assert json.loads(row["value_json"]) == 0.5


# ---------------------------------------------------------- TestValidateHelpers


class TestValidateHelpers:
    """工具函数形态（flatten_group / candidate_overrides）。"""

    def test_flatten_group_nested(self) -> None:
        flat = co.flatten_group({"backend": "cloud", "local": {"model_name": "m"}, "n": None})
        assert flat == {"backend": "cloud", "local.model_name": "m", "n": None}

    def test_candidate_overrides_delete_and_merge(self, tmp_db: Database) -> None:
        tmp_db.set_settings("thresholds", {"semantic_threshold": 0.5, "margin_w": 0.1})
        rows = tmp_db.list_settings()
        merged = co.candidate_overrides(rows, "thresholds", {"margin_w": 0.9})
        assert merged["thresholds"] == {"semantic_threshold": 0.5, "margin_w": 0.9}
        removed = co.candidate_overrides(rows, "thresholds", None)
        assert "thresholds" not in removed
