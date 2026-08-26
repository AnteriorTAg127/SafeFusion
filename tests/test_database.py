"""SQLite 存储层（Database）测试：四表 CRUD / 过滤分页 / 重复告警 / 别名。

对应 T2 任务卡验收：Key / 词库 / 白名单 / 审核日志。全部走 tmp_db 真实 SQLite。
"""

from __future__ import annotations

import pytest

from safefusion.storage.database import Database


class TestApiKeys:
    """api_keys 表：创建 / 列出 / 启停 / 删除 / 备注更新 / 重复与非法 tier。"""

    def test_create_and_get(self, tmp_db: Database) -> None:
        tmp_db.create_key("k1", tier="full", note="n1")
        row = tmp_db.get_key("k1")
        assert row is not None
        assert row["tier"] == "full"
        # SQLite 布尔列以 0/1 存储（API 层负责转 bool）
        assert row["enabled"] == 1
        assert row["note"] == "n1"
        assert row["created_at"]

    def test_list_sorted(self, tmp_db: Database) -> None:
        tmp_db.create_key("k2", note="later")
        rows = tmp_db.list_keys()
        assert [r["key"] for r in rows] == ["k2"]

    def test_duplicate_key_rejected(self, tmp_db: Database) -> None:
        tmp_db.create_key("dup")
        with pytest.raises(ValueError, match="已存在"):
            tmp_db.create_key("dup")

    def test_invalid_tier_rejected(self, tmp_db: Database) -> None:
        with pytest.raises(ValueError, match="tier"):
            tmp_db.create_key("bad", tier="admin")

    def test_set_enabled(self, tmp_db: Database) -> None:
        tmp_db.create_key("k")
        assert tmp_db.set_key_enabled("k", False) is True
        assert tmp_db.get_key("k")["enabled"] == 0
        assert tmp_db.set_key_enabled("missing", True) is False

    def test_delete_key(self, tmp_db: Database) -> None:
        tmp_db.create_key("k")
        assert tmp_db.delete_key("k") is True
        assert tmp_db.get_key("k") is None
        assert tmp_db.delete_key("k") is False

    def test_update_note(self, tmp_db: Database) -> None:
        tmp_db.create_key("k")
        assert tmp_db.update_key_note("k", "新备注") is True
        assert tmp_db.get_key("k")["note"] == "新备注"
        assert tmp_db.update_key_note("missing", "x") is False


class TestKeywords:
    """keywords 表：批量导入（重复跳过）/ 过滤 / 删除 / 空导入。"""

    def test_add_and_list(self, tmp_db: Database) -> None:
        inserted, skipped = tmp_db.add_keywords([("色情", "裸聊", "s1"), ("广告", "加我", None)])
        assert (inserted, skipped) == (2, 0)
        rows = tmp_db.list_keywords()
        assert {(r["category"], r["word"]) for r in rows} == {("色情", "裸聊"), ("广告", "加我")}

    def test_list_filter_by_category(self, tmp_db: Database) -> None:
        tmp_db.add_keywords([("色情", "裸聊", None), ("广告", "加我", None)])
        assert [r["word"] for r in tmp_db.list_keywords("广告")] == ["加我"]

    def test_duplicate_skipped_not_overwrite(self, tmp_db: Database) -> None:
        tmp_db.add_keywords([("色情", "裸聊", "old")])
        inserted, skipped = tmp_db.add_keywords([("色情", "裸聊", "new"), ("色情", "新词", None)])
        assert inserted == 1
        assert skipped == 1
        rows = tmp_db.list_keywords("色情")
        assert len(rows) == 2
        # 旧词条 source 未被覆盖
        assert {r["source"] for r in rows} == {"old", None}

    def test_empty_add(self, tmp_db: Database) -> None:
        assert tmp_db.add_keywords([]) == (0, 0)

    def test_delete_keyword(self, tmp_db: Database) -> None:
        inserted, _ = tmp_db.add_keywords([("色情", "裸聊", None)])
        assert inserted == 1
        keyword_id = tmp_db.list_keywords()[0]["id"]
        assert tmp_db.delete_keyword(keyword_id) is True
        assert tmp_db.delete_keyword(keyword_id) is False
        assert tmp_db.list_keywords() == []


class TestWhitelist:
    """whitelist_meta 表：add_whitelist 别名 / md5 唯一幂等 / 分页 / 删除。"""

    def test_alias_matches_normal(self, tmp_db: Database) -> None:
        id_a = tmp_db.add_whitelist("m1", "0123456789abcdef0123456789abcdef", "a")
        rows = tmp_db.list_whitelist()
        assert len(rows) == 1
        assert rows[0]["id"] == id_a
        assert rows[0]["phash_hex"] == "0123456789abcdef0123456789abcdef"

    def test_duplicate_md5_returns_same_id(self, tmp_db: Database) -> None:
        id1 = tmp_db.add_whitelist_meta("m1", "aaaa", "first")
        id2 = tmp_db.add_whitelist_meta("m1", "bbbb", "second")
        assert id1 == id2
        assert len(tmp_db.list_whitelist()) == 1  # 不重复入库

    def test_pagination(self, tmp_db: Database) -> None:
        for i in range(5):
            tmp_db.add_whitelist(f"m{i}", f"{i:064x}", None)
        page = tmp_db.list_whitelist(limit=2, offset=1)
        assert len(page) == 2
        assert page[0]["md5"] == "m1"

    def test_delete_whitelist(self, tmp_db: Database) -> None:
        entry_id = tmp_db.add_whitelist("m1", "aaaa", None)
        assert tmp_db.delete_whitelist(entry_id) is True
        assert tmp_db.list_whitelist() == []
        assert tmp_db.delete_whitelist(entry_id) is False


class TestAuditLogs:
    """audit_logs 表：写入 / 组合过滤 / 分页 / 统计 / 重复 request_id。"""

    def _seed(self, tmp_db: Database) -> None:
        tmp_db.insert_audit_log(
            "r1",
            True,
            "semantic",
            ts="2026-01-01T00:00:00.000+00:00",
            text_hash="h1",
            confidence=0.8,
            category="色情",
            key_tier="full",
        )
        tmp_db.insert_audit_log(
            "r2",
            False,
            "basic_rules_pass",
            ts="2026-01-02T00:00:00.000+00:00",
            text_hash="h2",
            confidence=0.0,
            key_tier="standard",
        )
        tmp_db.insert_audit_log(
            "r3",
            True,
            "llm",
            ts="2026-01-03T00:00:00.000+00:00",
            text_hash="h3",
            confidence=0.9,
            category="赌博",
            key_tier="full",
        )

    def test_insert_and_query_all(self, tmp_db: Database) -> None:
        self._seed(tmp_db)
        rows = tmp_db.query_logs()
        assert len(rows) == 3
        # 按 ts 倒序：r3 最新在前
        assert rows[0]["request_id"] == "r3"

    def test_filters(self, tmp_db: Database) -> None:
        self._seed(tmp_db)
        assert len(tmp_db.query_logs(has_violation=True)) == 2
        assert len(tmp_db.query_logs(source="llm")) == 1
        assert len(tmp_db.query_logs(category="色情")) == 1
        assert len(tmp_db.query_logs(key_tier="standard")) == 1
        assert len(tmp_db.query_logs(start_ts="2026-01-02T00:00:00.000+00:00")) == 2
        assert len(tmp_db.query_logs(end_ts="2026-01-02T00:00:00.000+00:00")) == 2
        # ISO 时间串需提供完整精度才落入含端点比较（"2026-01-02" 无时分秒）
        assert (
            len(
                tmp_db.query_logs(
                    start_ts="2026-01-02T00:00:00.000+00:00",
                    end_ts="2026-01-02T23:59:59.999+00:00",
                )
            )
            == 1
        )

    def test_count_matches_filter(self, tmp_db: Database) -> None:
        self._seed(tmp_db)
        assert tmp_db.count_logs() == 3
        assert tmp_db.count_logs(has_violation=True) == 2
        assert tmp_db.count_logs(source="basic_rules_pass") == 1

    def test_pagination(self, tmp_db: Database) -> None:
        self._seed(tmp_db)
        page = tmp_db.query_logs(limit=3, offset=0)
        assert len(page) == 3
        assert page[0]["request_id"] == "r3"
        # 三条记录均未携带 detail → detail_json 为 None
        assert page[2]["detail_json"] is None

    def test_invalid_pagination_raises(self, tmp_db: Database) -> None:
        with pytest.raises(ValueError, match="分页参数非法"):
            tmp_db.query_logs(limit=0)
        with pytest.raises(ValueError, match="分页参数非法"):
            tmp_db.query_logs(offset=-1)

    def test_duplicate_request_id_raises(self, tmp_db: Database) -> None:
        tmp_db.insert_audit_log("r1", False, "semantic")
        with pytest.raises(ValueError, match="已存在"):
            tmp_db.insert_audit_log("r1", True, "llm")

    def test_detail_json_roundtrip(self, tmp_db: Database) -> None:
        tmp_db.insert_audit_log("r1", True, "semantic", detail={"keyword": {"hits": [1]}})
        row = tmp_db.query_logs()[0]
        assert '"hits"' in row["detail_json"]
        assert row["has_violation"] == 1  # SQLite 布尔以 0/1 存储


class TestDatabaseLifecycle:
    """数据库生命周期：关闭后重新打开数据仍在（WAL 持久化）。"""

    def test_close_then_reopen(self, tmp_path) -> None:
        db = Database(tmp_path / "audit.db")
        db.create_key("k")
        db.close()
        db2 = Database(tmp_path / "audit.db")
        try:
            assert db2.get_key("k") is not None
        finally:
            db2.close()

    def test_wal_mode(self, tmp_path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            journal = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert journal == "wal"
        finally:
            db.close()
