"""管理 API 契约测试（FastAPI TestClient）：五组端点 + 鉴权 + 脱敏。

对应 T11 任务卡验收：TestClient 覆盖每组端点主路径与鉴权失败路径。
全部走 tmp_path 真实 SQLite + 真实 WhitelistMatcher。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from safefusion.api.admin import create_admin_app
from safefusion.engines.image_pipeline import WhitelistMatcher, compute_hashes
from safefusion.storage.database import Database

from .conftest import png_bytes

TOKEN = "admin-test-token"


class _DuckCfg:
    """带 data_dir 与 admin_token 的配置鸭子类型。"""

    def __init__(self, data_dir: Path, token: str) -> None:
        self.data_dir = str(data_dir)
        self.admin_token = token


def _headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token} if token is not None else {}


def _parse_png(data: bytes):
    """将 PNG 字节解码为 PIL 图并计算哈希（供白名单命中断言）。"""
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(data))
    img.load()
    return img


@pytest.fixture
def admin_env(tmp_path: Path) -> tuple[Database, TestClient]:
    db = Database(tmp_path / "audit.db")
    matcher = WhitelistMatcher(db)
    app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
    client = TestClient(app)
    yield db, client
    db.close()


class TestAdminAuth:
    """鉴权：缺失 / 错误令牌 → 401。"""

    def test_no_token_401(self, admin_env) -> None:
        _, client = admin_env
        resp = client.get("/admin/keys")
        assert resp.status_code == 401

    def test_wrong_token_401(self, admin_env) -> None:
        _, client = admin_env
        resp = client.get("/admin/keys", headers=_headers("wrong"))
        assert resp.status_code == 401

    def test_valid_token_ok(self, admin_env) -> None:
        _, client = admin_env
        resp = client.get("/admin/keys", headers=_headers())
        assert resp.status_code == 200


class TestKeysEndpoints:
    """POST/GET/PATCH/DELETE /admin/keys；列表脱敏。"""

    def test_create_key(self, admin_env) -> None:
        db, client = admin_env
        resp = client.post("/admin/keys", json={"tier": "full", "note": "联调"}, headers=_headers())
        assert resp.status_code == 201
        body = resp.json()
        assert body["key"].startswith("sf_")
        assert body["tier"] == "full"
        assert db.get_key(body["key"]) is not None

    def test_create_invalid_tier_400(self, admin_env) -> None:
        _, client = admin_env
        resp = client.post("/admin/keys", json={"tier": "admin"}, headers=_headers())
        # KeyCreate 用 Literal 拦截非法 tier → 走 422 脱敏 JSON（而非 400）
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_list_keys_masked(self, admin_env) -> None:
        db, client = admin_env
        db.create_key("sf_abcdefghijklmnopqrstuvwx", tier="standard")
        resp = client.get("/admin/keys", headers=_headers())
        rows = resp.json()
        assert len(rows) == 1
        # 明文不回显：仅前 8 位 + 省略号（key[:8] = "sf_abcde"）
        assert rows[0]["key"] == "sf_abcde…"
        assert "abcdefghijklmnopqrstuvwx" not in resp.text

    def test_patch_enabled_and_note(self, admin_env) -> None:
        db, client = admin_env
        db.create_key("sf_patch_me", tier="full", note="old")
        resp = client.patch(
            "/admin/keys/sf_patch_me",
            json={"enabled": False, "note": "new"},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False  # 响应层转 bool
        row = db.get_key("sf_patch_me")
        assert row["enabled"] == 0  # 存储层为 0/1
        assert row["note"] == "new"

    def test_patch_missing_404(self, admin_env) -> None:
        _, client = admin_env
        resp = client.patch("/admin/keys/sf_nope", json={"enabled": True}, headers=_headers())
        assert resp.status_code == 404

    def test_delete_key(self, admin_env) -> None:
        db, client = admin_env
        db.create_key("sf_del_me")
        resp = client.delete("/admin/keys/sf_del_me", headers=_headers())
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "sf_del_me"
        assert db.get_key("sf_del_me") is None
        assert client.delete("/admin/keys/sf_del_me", headers=_headers()).status_code == 404

    def test_patch_validation_422_masked(self, admin_env) -> None:
        _, client = admin_env
        # "yes" 等词会被 pydantic 宽松 bool 解析为 True → 需用真正非法值触发 422
        resp = client.patch(
            "/admin/keys/sf_x", json={"enabled": "not-a-bool-word"}, headers=_headers()
        )
        assert resp.status_code == 422
        assert "error" in resp.json()


class TestKeywordsEndpoints:
    """词库导入（CSV/TXT）/ 查看 / 删除。"""

    def test_import_csv(self, admin_env) -> None:
        db, client = admin_env
        resp = client.post(
            "/admin/keywords/import",
            files={"file": ("words.csv", "类别,词\n色情,裸聊\n广告,加我", "text/csv")},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json() == {"inserted": 2, "skipped": 0, "total": 2}
        assert len(db.list_keywords()) == 2

    def test_import_csv_duplicate_skipped(self, admin_env) -> None:
        _, client = admin_env
        payload = {"file": ("w.csv", "类别,词\n色情,裸聊\n广告,加我", "text/csv")}
        client.post("/admin/keywords/import", files=payload, headers=_headers())
        resp = client.post("/admin/keywords/import", files=payload, headers=_headers())
        assert resp.json() == {"inserted": 0, "skipped": 2, "total": 2}

    def test_import_txt_requires_category(self, admin_env) -> None:
        _, client = admin_env
        resp = client.post(
            "/admin/keywords/import",
            files={"file": ("words.txt", "裸聊\n加我", "text/plain")},
            headers=_headers(),
        )
        assert resp.status_code == 400

    def test_import_txt_with_category(self, admin_env) -> None:
        db, client = admin_env
        resp = client.post(
            "/admin/keywords/import?category=色情",
            files={"file": ("words.txt", "# 注释\n裸聊\n\n加我", "text/plain")},
            headers=_headers(),
        )
        assert resp.json() == {"inserted": 2, "skipped": 0, "total": 2}
        assert all(r["category"] == "色情" for r in db.list_keywords())

    def test_list_keywords_paginated(self, admin_env) -> None:
        db, client = admin_env
        db.add_keywords([("色情", f"w{i}", None) for i in range(5)])
        resp = client.get("/admin/keywords?page=2&page_size=2", headers=_headers())
        body = resp.json()
        assert body["total"] == 5
        assert body["page"] == 2
        assert len(body["items"]) == 2

    def test_delete_keyword(self, admin_env) -> None:
        db, client = admin_env
        db.add_keywords([("色情", "裸聊", None)])
        keyword_id = db.list_keywords()[0]["id"]
        assert client.delete(f"/admin/keywords/{keyword_id}", headers=_headers()).status_code == 200
        assert client.delete(f"/admin/keywords/{keyword_id}", headers=_headers()).status_code == 404


class TestRulesEndpoints:
    """规则 CRUD（PRD v0.2 M4）：JSON 批量 / CSV 导入 / 列表 / 删除 / 启停 / reload 钩子。"""

    def test_auth_required(self, admin_env) -> None:
        _, client = admin_env
        assert client.get("/admin/rules").status_code == 401

    def test_post_json_and_hook_called(self, tmp_path: Path) -> None:
        calls: list[str] = []
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(
            db,
            matcher,
            reload_hook=lambda: calls.append("reload") or True,
            config=_DuckCfg(tmp_path, TOKEN),
        )
        client = TestClient(app)
        resp = client.post(
            "/admin/rules",
            json=[{"category": "广告", "pattern": "加我好友", "action": "exempt", "note": "天气"}],
            headers=_headers(),
        )
        body = resp.json()
        assert body["inserted"] == 1
        assert body["skipped"] == 0
        assert body["reload"] == "ok"
        assert calls == ["reload"]
        assert len(db.list_rules()) == 1
        db.close()

    def test_post_json_action_default_exempt(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        resp = client.post(
            "/admin/rules",
            json=[{"category": "广告", "pattern": "加我好友"}],
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["reload"] == "skipped"  # 未注入 hook
        assert db.list_rules()[0]["action"] == "exempt"
        db.close()

    def test_post_json_invalid_action_400(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        resp = client.post(
            "/admin/rules",
            json=[{"category": "广告", "pattern": "x", "action": "ban"}],
            headers=_headers(),
        )
        assert resp.status_code == 400
        assert db.list_rules() == []  # 批量整体拒绝，未写入
        db.close()

    def test_post_csv_import_empty_action_exempt(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        csv_text = "category,pattern,action\n广告,加我好友,\n色情,裸聊,violate\n"
        resp = client.post(
            "/admin/rules",
            files={"file": ("rules.csv", csv_text, "text/csv")},
            headers=_headers(),
        )
        body = resp.json()
        assert body["inserted"] == 2
        assert body["skipped"] == 0
        rows = db.list_rules()
        assert {r["action"] for r in rows} == {"exempt", "violate"}
        assert next(r for r in rows if r["pattern"] == "加我好友")["action"] == "exempt"
        db.close()

    def test_get_list_filter_and_active(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        db.add_rules([("色情", "裸聊", "violate", None), (None, "天气", "exempt", None)])
        resp = client.get("/admin/rules", headers=_headers())
        body = resp.json()
        assert body["total"] == 2
        assert all(item["is_active"] is True for item in body["items"])
        filtered = client.get("/admin/rules?category=色情", headers=_headers()).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["pattern"] == "裸聊"
        db.close()

    def test_patch_active_disables(self, tmp_path: Path) -> None:
        calls: list[str] = []
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(
            db,
            matcher,
            reload_hook=lambda: calls.append("reload") or True,
            config=_DuckCfg(tmp_path, TOKEN),
        )
        client = TestClient(app)
        db.add_rules([("色情", "裸聊", "violate", None)])
        rule_id = db.list_rules()[0]["id"]
        resp = client.patch(
            f"/admin/rules/{rule_id}/active", json={"active": False}, headers=_headers()
        )
        assert resp.status_code == 200
        assert resp.json() == {"id": rule_id, "active": False, "reload": "ok"}
        assert db.list_rules(active_only=True) == []
        missing = client.patch("/admin/rules/999/active", json={"active": True}, headers=_headers())
        assert missing.status_code == 404
        db.close()

    def test_delete_rule_and_hook(self, tmp_path: Path) -> None:
        calls: list[str] = []
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(
            db,
            matcher,
            reload_hook=lambda: calls.append("reload") or True,
            config=_DuckCfg(tmp_path, TOKEN),
        )
        client = TestClient(app)
        db.add_rules([("色情", "裸聊", "violate", None)])
        rule_id = db.list_rules()[0]["id"]
        resp = client.delete(f"/admin/rules/{rule_id}", headers=_headers())
        assert resp.status_code == 200
        assert resp.json() == {"deleted": rule_id, "reload": "ok"}
        assert db.list_rules() == []
        assert calls == ["reload"]  # 仅删除触发一次（规则经 DAO 直插，不走端点）
        assert client.delete(f"/admin/rules/{rule_id}", headers=_headers()).status_code == 404
        db.close()

    def test_reload_hook_failure_reported(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(
            db,
            matcher,
            reload_hook=lambda: False,
            config=_DuckCfg(tmp_path, TOKEN),
        )
        client = TestClient(app)
        resp = client.post(
            "/admin/rules",
            json=[{"pattern": "x", "action": "exempt"}],
            headers=_headers(),
        )
        # 写入成功但热重载失败 → 响应标注 failed
        assert resp.json()["inserted"] == 1
        assert resp.json()["reload"] == "failed"
        db.close()


class TestWhitelistEndpoints:
    """白名单上传（multipart）/ 列表 / 删除；上传后命中距离 0。"""

    def test_upload_and_match_distance_zero(self, admin_env, tmp_path: Path) -> None:
        db, client = admin_env
        data = png_bytes(color=(5, 120, 200))
        resp = client.post(
            "/admin/whitelist/images",
            files={"files": ("sample.png", data, "image/png")},
            headers=_headers(),
        )
        body = resp.json()
        assert body["uploaded"] == 1
        assert body["failed"] == 0
        item = body["items"][0]
        assert item["md5"] and item["phash_hex"]
        assert item["file"] == str(tmp_path / "whitelist" / f"{item['md5']}.png")  # 原图按 md5 落盘
        # 上传后：同一图片经 matcher.match 距离 0
        matcher = WhitelistMatcher(db)
        img = _parse_png(data)
        _, phash = compute_hashes(img)
        row = matcher.match(phash, max_distance=0)
        assert row is not None
        assert row["distance"] == 0

    def test_upload_partial_failure(self, admin_env) -> None:
        _, client = admin_env
        resp = client.post(
            "/admin/whitelist/images",
            files=[
                ("files", ("ok.png", png_bytes(), "image/png")),
                ("files", ("bad.png", b"not-an-image", "image/png")),
            ],
            headers=_headers(),
        )
        body = resp.json()
        assert body["uploaded"] == 1
        assert body["failed"] == 1
        assert any("error" in item for item in body["items"])

    def test_list_and_delete(self, admin_env, tmp_path: Path) -> None:
        _, client = admin_env
        data = png_bytes(color=(1, 2, 3))
        client.post(
            "/admin/whitelist/images",
            files={"files": ("a.png", data, "image/png")},
            headers=_headers(),
        )
        listing = client.get("/admin/whitelist/images", headers=_headers()).json()
        assert listing["total"] == 1
        entry_id = listing["items"][0]["id"]
        resp = client.delete(f"/admin/whitelist/images/{entry_id}", headers=_headers())
        body = resp.json()
        assert body["deleted"] == entry_id
        assert body["file_deleted"] is True
        again = client.delete(f"/admin/whitelist/images/{entry_id}", headers=_headers())
        assert again.status_code == 404


class TestLogsEndpoints:
    """审核日志分页查询与 CSV 导出。"""

    def _seed_logs(self, db: Database) -> None:
        db.insert_audit_log(
            "r1",
            True,
            "semantic",
            ts="2026-02-01T00:00:00.000+00:00",
            text_hash="h1",
            confidence=0.8,
            category="色情",
            key_tier="full",
        )
        db.insert_audit_log(
            "r2",
            False,
            "basic_rules_pass",
            ts="2026-02-02T00:00:00.000+00:00",
            text_hash="h2",
            confidence=0.0,
            key_tier="standard",
        )

    def test_query_logs_filtered(self, admin_env) -> None:
        db, client = admin_env
        self._seed_logs(db)
        body = client.get("/admin/logs?source=semantic", headers=_headers()).json()
        assert body["total"] == 1
        assert body["items"][0]["request_id"] == "r1"
        assert body["items"][0]["detail"] is None  # detail_json 解析为 detail

    def test_query_logs_has_violation_filter(self, admin_env) -> None:
        db, client = admin_env
        self._seed_logs(db)
        body = client.get("/admin/logs?has_violation=true", headers=_headers()).json()
        assert body["total"] == 1

    def test_export_csv(self, admin_env) -> None:
        db, client = admin_env
        self._seed_logs(db)
        resp = client.get("/admin/logs/export", headers=_headers())
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "\ufeff" in resp.text  # BOM 兼容 Excel
        assert "request_id" in resp.text
        assert "r1" in resp.text and "r2" in resp.text


class TestRebuildEndpoints:
    """向量库重建：未注入 501 / 同步 / 异步 / 失败 500。"""

    def test_no_hook_501(self, admin_env) -> None:
        _, client = admin_env
        resp = client.post("/admin/vectors/rebuild", json={}, headers=_headers())
        assert resp.status_code == 501

    def test_sync_hook(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(
            db,
            matcher,
            rebuild_hook=lambda manifest: {"rows": 100},
            config=_DuckCfg(tmp_path, TOKEN),
        )
        client = TestClient(app)
        resp = client.post(
            "/admin/vectors/rebuild", json={"manifest_path": "m.jsonl"}, headers=_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"rows": 100}
        db.close()

    def test_async_hook(self, tmp_path: Path) -> None:
        async def _hook(manifest: str) -> dict[str, Any]:
            return {"manifest": manifest, "ok": True}

        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, rebuild_hook=_hook, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        resp = client.post("/admin/vectors/rebuild", json={}, headers=_headers())
        body = resp.json()
        assert body["status"] == "ok"
        # Windows 路径分隔符为 \，做归一化后断言清单尾缀
        assert body["manifest"].replace("\\", "/").endswith("vectors/manifest.jsonl")
        db.close()

    def test_hook_error_500_masked(self, tmp_path: Path) -> None:
        def _bad(manifest: str) -> None:
            raise RuntimeError("重建内部失败")

        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        app = create_admin_app(db, matcher, rebuild_hook=_bad, config=_DuckCfg(tmp_path, TOKEN))
        client = TestClient(app)
        resp = client.post("/admin/vectors/rebuild", json={}, headers=_headers())
        assert resp.status_code == 500
        assert "error" in resp.json()
        db.close()
