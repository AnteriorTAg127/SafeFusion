"""SQLite 存储层：API Key / 词库 / 正则规则 / 图片白名单 / 审核记录五类表的 DAO。

设计要点（对齐 PRD §5 与 开发/v0.2/分工.md T17 任务卡）：
- 连接随实例持有（``sqlite3.Connection``），全程由单个 ``threading.Lock`` 互斥；
- ``PRAGMA journal_mode=WAL`` + ``synchronous=NORMAL``：读不阻塞写；
- 建表与辅助索引在构造时幂等执行（``IF NOT EXISTS``）；
- 全部写操作显式 ``commit``；时间戳统一为 UTC ISO 8601 字符串（毫秒精度）；
- 重复资源（Key / 词条 / 白名单 md5 / 规则）一律告警而不静默覆盖。
"""

import json
import re
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

_logger = get_logger("storage.database")

#: API Key 权限分组全集
_TIERS: tuple[str, str] = ("standard", "full")

#: 建表语句（列严格对齐 T2 任务卡）
_CREATE_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key        TEXT PRIMARY KEY,
        tier       TEXT NOT NULL DEFAULT 'standard' CHECK (tier IN ('standard', 'full')),
        enabled    INTEGER NOT NULL DEFAULT 1,
        note       TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keywords (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        word     TEXT NOT NULL,
        source   TEXT,
        UNIQUE (category, word)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whitelist_meta (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        md5        TEXT NOT NULL,
        phash_hex  TEXT NOT NULL,
        note       TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        request_id    TEXT PRIMARY KEY,
        ts            TEXT NOT NULL,
        text_hash     TEXT,
        has_violation INTEGER NOT NULL,
        confidence    REAL,
        category      TEXT,
        source        TEXT NOT NULL,
        detail_json   TEXT,
        key_tier      TEXT
    )
    """,
    # PRD v0.2 M4：正则消歧规则库（category 为空串表示不限定类别，
    # 作用于全部命中；同 (category, pattern, action) 判重，防重复导入）
    """
    CREATE TABLE IF NOT EXISTS rules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        category   TEXT,
        pattern    TEXT NOT NULL,
        action     TEXT NOT NULL CHECK (action IN ('exempt', 'violate')),
        note       TEXT,
        is_active  INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        UNIQUE (category, pattern, action)
    )
    """,
    # PRD v0.3.0 M4：配置 settings 表（取代 data/config_overrides.json）。
    # group 为配置分组名（对齐 AppConfig 分组白名单），key 为该分组内
    # **叶子字段的点分路径**（如 embedding 组的 "local.model_name"——
    # 与 config_override._leaf_paths 口径一致，便于逐叶子来源标识与
    # 环境变量「钉住」对比），value_json 为 JSON 序列化后的叶子值；
    # "group" 是 SQLite 保留字，全部 SQL 中一律加引号引用。
    """
    CREATE TABLE IF NOT EXISTS settings (
        "group"    TEXT NOT NULL,
        key        TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY ("group", key)
    )
    """,
)

#: 辅助索引：白名单 md5 唯一（防重复图入库）、审核日志按时间/来源查询提速、
#: 规则按类别过滤提速
_CREATE_INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_whitelist_md5 ON whitelist_meta (md5)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_ts ON audit_logs (ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_source ON audit_logs (source)",
    "CREATE INDEX IF NOT EXISTS idx_rules_category ON rules (category)",
)


def _utc_now() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（毫秒精度）。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """将 ``sqlite3.Row`` 转为普通 dict。"""

    # 不用 SIM118 建议的 `key in row`：sqlite3.Row 无 __contains__，那是值的成员判断，
    # 与 dict 语义不同（实测 Python 3.12：值 'x' 命中会让 `'x' in row` 为 True）
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


class Database:
    """SQLite DAO：五张业务表的增删查改。

    Args:
        db_path: SQLite 数据库文件路径（父目录不存在时自动创建）。

    Note:
        连接在构造时创建并随实例持有；关闭请调用 :meth:`close`。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in _CREATE_TABLES:
            self._conn.execute(statement)
        for statement in _CREATE_INDEXES:
            self._conn.execute(statement)
        self._conn.commit()

    def close(self) -> None:
        """关闭底层连接（进程退出或测试收尾时调用；关闭后实例不可再使用）。"""

        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ keys

    def create_key(
        self,
        key: str,
        tier: str = "standard",
        enabled: bool = True,
        note: str | None = None,
    ) -> None:
        """新增一个 API Key。

        Args:
            key: Key 明文。
            tier: 权限分组，``standard``（仅基本判定）或 ``full``（完整细节）。
            enabled: 是否启用；禁用 Key 鉴权时返回 401。
            note: 备注（用途 / 归属方）。

        Raises:
            ValueError: ``tier`` 非法，或 Key 已存在（不静默覆盖）。
        """

        if tier not in _TIERS:
            raise ValueError(f"tier 必须是 {'/'.join(_TIERS)}，收到 {tier!r}")
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM api_keys WHERE key = ?", (key,)).fetchone()
            if exists is not None:
                _logger.warning("API Key 重复创建被拒绝（不覆盖）")
                raise ValueError(f"API Key 已存在: {key}")
            self._conn.execute(
                "INSERT INTO api_keys (key, tier, enabled, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, tier, int(enabled), note, _utc_now()),
            )
            self._conn.commit()

    def list_keys(self) -> list[dict[str, Any]]:
        """列出全部 API Key（按创建时间升序）。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT key, tier, enabled, note, created_at FROM api_keys ORDER BY created_at, key"
            ).fetchall()
            return [_row_to_dict(row) for row in rows]

    def set_key_enabled(self, key: str, enabled: bool) -> bool:
        """启用 / 禁用指定 Key。

        Returns:
            True 表示确有该 Key 且状态已更新；False 表示 Key 不存在。
        """

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE api_keys SET enabled = ? WHERE key = ?", (int(enabled), key)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_key(self, key: str) -> bool:
        """删除指定 API Key（主模型集成补齐，2026-08-26，供 DELETE /admin/keys/{key}）。

        Returns:
            True 表示确有该 Key 且已删除；False 表示 Key 不存在。
        """

        with self._lock:
            cursor = self._conn.execute("DELETE FROM api_keys WHERE key = ?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    def update_key_note(self, key: str, note: str) -> bool:
        """更新指定 Key 的备注（主模型集成补齐，2026-08-26，供 PATCH /admin/keys/{key}）。

        Returns:
            True 表示确有该 Key 且备注已更新；False 表示 Key 不存在。
        """

        with self._lock:
            cursor = self._conn.execute("UPDATE api_keys SET note = ? WHERE key = ?", (note, key))
            self._conn.commit()
            return cursor.rowcount > 0

    def get_key(self, key: str) -> dict[str, Any] | None:
        """按 Key 明文查询（含 enabled / tier，供鉴权使用）。

        Returns:
            完整记录 dict（含 tier / enabled / note / created_at），不存在返回 None。
        """

        with self._lock:
            row = self._conn.execute(
                "SELECT key, tier, enabled, note, created_at FROM api_keys WHERE key = ?",
                (key,),
            ).fetchone()
            return _row_to_dict(row) if row is not None else None

    # -------------------------------------------------------------- keywords

    def add_keywords(self, items: Sequence[tuple[str, str, str | None]]) -> tuple[int, int]:
        """批量新增词条。

        Args:
            items: ``(category, word, source)`` 三元组序列；source 可为 None。
                重复词条（category + word 唯一冲突）自动跳过并告警，不覆盖。

        Returns:
            ``(新增条数, 跳过条数)``。
        """

        if not items:
            return (0, 0)
        with self._lock:
            cursor = self._conn.executemany(
                "INSERT OR IGNORE INTO keywords (category, word, source) VALUES (?, ?, ?)",
                items,
            )
            self._conn.commit()
            inserted = cursor.rowcount
            skipped = len(items) - inserted
            if skipped:
                _logger.warning("批量词条导入跳过 %d 条重复项（category+word 唯一）", skipped)
            return inserted, skipped

    def list_keywords(self, category: str | None = None) -> list[dict[str, Any]]:
        """列出词条，可按类别过滤（按类别、id 排序）。"""

        sql = "SELECT id, category, word, source FROM keywords"
        params: list[Any] = []
        if category is not None:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY category, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_dict(row) for row in rows]

    def delete_keyword(self, keyword_id: int) -> bool:
        """按主键删除词条。

        Returns:
            True 表示确有该词条且已删除；False 表示不存在。
        """

        with self._lock:
            cursor = self._conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def dedup_keywords(self) -> dict[str, int]:
        """按 (category, 归一化词) 一键去重：清理全半角/空白/标点等变体重复。

        ``keywords`` 表有 UNIQUE(category, word) 约束，精确重复不存在；去重
        针对历史脏数据/变体词条（如「敏感词」与「敏感词！」）：归一化键 =
        全角转半角 + 去空白 + 剥离常见标点，同类别同键保留最小 id（v0.3.0 G10）。

        Returns:
            ``{"before", "after", "removed"}`` 计数（after = before - removed）。
        """

        def _norm(word: str) -> str:
            out: list[str] = []
            for ch in word:
                code = ord(ch)
                if code == 0x3000:
                    code = 0x20
                elif 0xFF01 <= code <= 0xFF5E:  # 全角 → 半角
                    code -= 0xFEE0
                ch = chr(code)
                if ch.isspace() or ch in "，。！？：；、（）()【】[]《》〈〉“”\"''.,!?:;":
                    continue
                out.append(ch)
            return "".join(out)

        rows = self.list_keywords()
        removed_ids: list[int] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["category"] or "", _norm(row["word"] or ""))
            if key in seen:
                removed_ids.append(int(row["id"]))
            else:
                seen.add(key)
        before = len(rows)
        if not removed_ids:
            return {"before": before, "after": before, "removed": 0}
        with self._lock:
            placeholders = ",".join("?" * len(removed_ids))
            cursor = self._conn.execute(
                f"DELETE FROM keywords WHERE id IN ({placeholders})", removed_ids
            )
            self._conn.commit()
            removed = cursor.rowcount
            after = self._conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
            return {"before": before, "after": after, "removed": removed}

    # ------------------------------------------------------------------ rules

    def add_rules(
        self, items: Sequence[tuple[str | None, str, str, str | None]]
    ) -> tuple[int, int]:
        """批量新增正则消歧规则（PRD v0.2 M4）。

        Args:
            items: ``(category, pattern, action, note)`` 四元组序列。
                category 可为 None / 空串（规则不限定类别，作用于全部命中）；
                action 限 ``exempt``（豁免）/ ``violate``（追加强命中）；
                note 可为 None。规则以 ``(category, pattern, action)`` 判重，
                重复项自动跳过并告警，不静默覆盖（与词库导入同口径）。

        Returns:
            ``(新增条数, 跳过条数)``。

        Raises:
            ValueError: action 非法、pattern 为空或不是有效正则（写入前快速
                失败，保证 rules 表内不会留存引擎无法编译的规则）。
        """

        if not items:
            return (0, 0)
        for _category, pattern, action, _note in items:
            if action not in ("exempt", "violate"):
                raise ValueError(f"规则 action 必须为 exempt 或 violate，收到 {action!r}")
            if not pattern:
                raise ValueError("规则 pattern 不能为空")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"规则 pattern 不是有效正则: {pattern!r}（{exc}）") from exc
        with self._lock:
            cursor = self._conn.executemany(
                "INSERT OR IGNORE INTO rules "
                "(category, pattern, action, note, is_active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                [
                    (category or "", pattern, action, note, _utc_now())
                    for category, pattern, action, note in items
                ],
            )
            self._conn.commit()
            inserted = cursor.rowcount
            skipped = len(items) - inserted
            if skipped:
                _logger.warning(
                    "批量规则导入跳过 %d 条重复项（category+pattern+action 唯一）", skipped
                )
            return inserted, skipped

    def list_rules(
        self, category: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """列出正则消歧规则，可按类别/启用状态过滤（按 id 升序，插入序稳定）。

        Args:
            category: 按类别过滤；None 返回全部类别（无类别规则 category 为空串）。
            active_only: True（默认）仅返回 is_active=1 的规则；False 返回全部
                （含已停用），供管理端核对。

        Returns:
            规则字典列表（id / category / pattern / action / note / is_active
            （0/1） / created_at）。
        """

        sql = "SELECT id, category, pattern, action, note, is_active, created_at FROM rules"
        conditions: list[str] = []
        params: list[Any] = []
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if active_only:
            conditions.append("is_active = 1")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_dict(row) for row in rows]

    def delete_rule(self, rule_id: int) -> bool:
        """按主键删除规则。

        Returns:
            True 表示确有该规则且已删除；False 表示不存在。
        """

        with self._lock:
            cursor = self._conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def set_rule_active(self, rule_id: int, active: bool) -> bool:
        """启用 / 停用指定规则（停用后不参与消歧，但保留记录可再启用）。

        Returns:
            True 表示确有该规则且状态已更新；False 表示不存在。
        """

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE rules SET is_active = ? WHERE id = ?", (int(active), rule_id)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------ whitelist images

    def add_whitelist(self, md5: str, phash_hex: str, note: str | None = None) -> int:
        """新增白名单图片元数据（``add_whitelist_meta`` 的协议别名）。

        T4 ``WhitelistMatcher`` 以鸭子类型依赖 ``add_whitelist`` 方法名，
        本别名保持两个模块解耦（主模型集成时补，2026-08-26）。
        """

        return self.add_whitelist_meta(md5, phash_hex, note)

    def add_whitelist_meta(self, md5: str, phash_hex: str, note: str | None = None) -> int:
        """新增白名单图片元数据。

        Args:
            md5: 图片内容 MD5（唯一）。
            phash_hex: 感知哈希十六进制串（imagehash pHash）。
            note: 备注。

        Returns:
            条目主键 id。md5 已存在时告警并直接返回既有 id（不覆盖、不重复入库）。
        """

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM whitelist_meta WHERE md5 = ?", (md5,)
            ).fetchone()
            if existing is not None:
                _logger.warning("白名单图片 md5 已存在（id=%s），返回既有条目", existing["id"])
                return int(existing["id"])
            cursor = self._conn.execute(
                "INSERT INTO whitelist_meta (md5, phash_hex, note, created_at) VALUES (?, ?, ?, ?)",
                (md5, phash_hex, note, _utc_now()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_whitelist(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """列出白名单元数据（按 id 升序），支持分页（limit 为 None 时全量返回）。"""

        sql = "SELECT id, md5, phash_hex, note, created_at FROM whitelist_meta ORDER BY id"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_dict(row) for row in rows]

    def delete_whitelist(self, entry_id: int) -> bool:
        """按主键删除白名单条目。

        Returns:
            True 表示确有该条目且已删除；False 表示不存在。
        """

        with self._lock:
            cursor = self._conn.execute("DELETE FROM whitelist_meta WHERE id = ?", (entry_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- settings

    #: settings 页面按 (group, key) 排序的一致性（utf8_binary 序），保证
    #: list_settings 输出稳定（供来源标识 / 合并使用）。
    _SETTINGS_ORDER = ' ORDER BY "group", key'

    def list_settings(self, group: str | None = None) -> list[dict[str, Any]]:
        """列出 settings 行（PRD v0.3.0 M4）。

        Args:
            group: 分组名过滤；None 返回全部。返回行不含解析后的 value
                （原始 value_json 字符串保留，由配置层统一解析）。

        Returns:
            行字典列表（"group" / key / value_json / updated_at，按 (group, key) 升序）。
        """

        sql = 'SELECT "group", key, value_json, updated_at FROM settings'
        params: list[Any] = []
        if group is not None:
            sql += ' WHERE "group" = ?'
            params.append(group)
        sql += self._SETTINGS_ORDER
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_dict(row) for row in rows]

    def get_setting(self, group: str, key: str) -> dict[str, Any] | None:
        """读取单个设置项。

        Args:
            group: 分组名。
            key: 组内叶子点分路径（如 ``local.model_name``）。

        Returns:
            行字典（"group" / key / value_json / updated_at），不存在返回 None。
        """

        with self._lock:
            row = self._conn.execute(
                'SELECT "group", key, value_json, updated_at FROM settings '
                'WHERE "group" = ? AND key = ?',
                (group, key),
            ).fetchone()
            return _row_to_dict(row) if row is not None else None

    def set_settings(self, group: str, values: dict[str, Any]) -> None:
        """整组 upsert 配置项（PRD v0.3.0 M4）。

        Args:
            group: 分组名。
            values: ``{叶子点分路径: 值}`` 映射，值为任意 JSON 可序列化对象
                （建议由配置层先把嵌套分组载荷展平为叶子路径后再写入，
                与 :meth:`list_settings` 的 key 口径一致）。

        Note:
            逐键 ``INSERT … ON CONFLICT DO UPDATE``（值整体替换、更新
            updated_at）；已在组内的其他键保持不变——调用方按「整组替换」
            语义时需自行传入该组全部叶子（管理端 PUT 仅传负载字段，
            保持 v0.2.1 覆盖层「部分键覆盖」语义）。
        """

        with self._lock:
            for key, value in values.items():
                self._conn.execute(
                    'INSERT INTO settings ("group", key, value_json, updated_at) '
                    "VALUES (?, ?, ?, ?) "
                    'ON CONFLICT("group", key) DO UPDATE SET '
                    "value_json = excluded.value_json, updated_at = excluded.updated_at",
                    (group, str(key), json.dumps(value, ensure_ascii=False), _utc_now()),
                )
            self._conn.commit()

    def delete_settings(self, group: str) -> bool:
        """删除整个分组的设置项。

        Returns:
            True 表示确有该分组且已删除；False 表示分组不存在。
        """

        with self._lock:
            cursor = self._conn.execute('DELETE FROM settings WHERE "group" = ?', (group,))
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------ audit logs

    def insert_audit_log(
        self,
        request_id: str,
        has_violation: bool,
        source: str,
        ts: str | None = None,
        text_hash: str | None = None,
        confidence: float | None = None,
        category: str | None = None,
        detail: dict[str, Any] | None = None,
        key_tier: str | None = None,
    ) -> None:
        """写入一条审核记录。

        Args:
            request_id: 请求唯一标识（UUID）。
            has_violation: 是否判定违规。
            source: 判定来源（semantic / llm / basic_rules_pass / cache / permanent_list）。
            ts: 审核完成时间（ISO 8601），缺省取当前 UTC。
            text_hash: 文本规范化哈希。
            confidence: 综合置信度（0~1）。
            category: 判定类别（如 色情 / 赌博）。
            detail: 审核明细（full 组填充）；内部序列化为 JSON 存入 detail_json。
            key_tier: 请求所用 Key 的分组（standard / full）。

        Raises:
            ValueError: ``request_id`` 已存在（异常重试写入导致的重复）。
        """

        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO audit_logs "
                    "(request_id, ts, text_hash, has_violation, confidence, "
                    " category, source, detail_json, key_tier) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        ts or _utc_now(),
                        text_hash,
                        int(has_violation),
                        confidence,
                        category,
                        source,
                        detail_json,
                        key_tier,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"审核记录 request_id 已存在: {request_id}") from exc
            self._conn.commit()

    def _log_where(
        self,
        start_ts: str | None,
        end_ts: str | None,
        has_violation: bool | None,
        source: str | None,
        category: str | None,
        key_tier: str | None,
    ) -> tuple[str, list[Any]]:
        """按过滤条件拼装 WHERE 子句与参数（query_logs / count_logs 共用）。"""

        conditions: list[str] = []
        params: list[Any] = []
        if start_ts is not None:
            conditions.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            conditions.append("ts <= ?")
            params.append(end_ts)
        if has_violation is not None:
            conditions.append("has_violation = ?")
            params.append(int(has_violation))
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if key_tier is not None:
            conditions.append("key_tier = ?")
            params.append(key_tier)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    def query_logs(
        self,
        start_ts: str | None = None,
        end_ts: str | None = None,
        has_violation: bool | None = None,
        source: str | None = None,
        category: str | None = None,
        key_tier: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """分页查询审核记录（按 ts 倒序，最新在前）。

        Args:
            start_ts / end_ts: 时间段过滤（含端点），ISO 8601 字符串。
            has_violation: 按结论过滤（True=违规 / False=安全）。
            source: 按判定来源过滤。
            category: 按类别过滤。
            key_tier: 按 Key 分组过滤（standard / full）。
            limit: 每页条数（>= 1）。
            offset: 偏移（>= 0）。

        Raises:
            ValueError: ``limit`` 或 ``offset`` 非法。
        """

        if limit < 1 or offset < 0:
            raise ValueError(f"分页参数非法: limit={limit}, offset={offset}")
        where, params = self._log_where(start_ts, end_ts, has_violation, source, category, key_tier)
        sql = (
            "SELECT request_id, ts, text_hash, has_violation, confidence, "
            "category, source, detail_json, key_tier FROM audit_logs "
            f"{where} ORDER BY ts DESC, request_id DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit, offset]).fetchall()
            return [_row_to_dict(row) for row in rows]

    def count_logs(
        self,
        start_ts: str | None = None,
        end_ts: str | None = None,
        has_violation: bool | None = None,
        source: str | None = None,
        category: str | None = None,
        key_tier: str | None = None,
    ) -> int:
        """按与 :meth:`query_logs` 相同的过滤条件统计记录总数（用于分页）。"""

        where, params = self._log_where(start_ts, end_ts, has_violation, source, category, key_tier)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM audit_logs {where}", params
            ).fetchone()
            return int(row["n"])
