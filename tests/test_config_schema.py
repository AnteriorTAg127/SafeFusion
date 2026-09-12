"""配置 schema 单一来源与前后端漂移守卫（Brooks-Lint P8）。

覆盖：
- 后端 ``config_schema()`` 的分组与 ``get_config_groups()`` 一致；
- 字段元数据齐备（``path`` / ``type`` / ``default`` / ``description`` / ``secret``）；
- 密钥叶子被正确标记（``llm.api_key`` 等 → ``secret=True``）；
- **漂移守卫**：前端 ``configFields.ts`` 声明的分组与字段 key 必须与后端 schema
  **完全一致**——防止「后端新增字段而设置面板改不了」的静默漂移（P8 即由此发现
  ``black_white_gap`` / ``cloud.allow_no_key`` 两处真实遗漏）。
"""

from __future__ import annotations

import re
from pathlib import Path

from safefusion.core.config_override import config_schema, get_config_groups

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_FIELDS_TS = _REPO_ROOT / "web" / "src" / "views" / "settings" / "configFields.ts"


def _frontend_groups_and_keys() -> tuple[set[str], set[str]]:
    """从前端 ``GROUP_META`` 抽取声明的分组名与字段 key（路径为 ``group`` 内相对）。"""
    text = _CONFIG_FIELDS_TS.read_text(encoding="utf-8")
    groups = set(re.findall(r"group:\s*'([^']+)'", text))
    keys = set(re.findall(r"key:\s*'([^']+)'", text))
    return groups, keys


def test_schema_groups_match_config_groups() -> None:
    """schema 分组集合与配置分组白名单一致。"""
    names = {g["name"] for g in config_schema()["groups"]}
    assert names == set(get_config_groups())


def test_field_metadata_complete() -> None:
    """每个分组的每个字段都携带完整元数据（无空路径 / 空类型）。"""
    for group in config_schema()["groups"]:
        assert group["fields"], f"分组 {group['name']} 未反射出任何字段"
        for field in group["fields"]:
            assert set(field) == {"path", "type", "default", "description", "secret"}
            assert field["path"], f"{group['name']} 存在空字段路径"
            assert field["type"], f"{group['name']}.{field['path']} 类型为空"


def test_secret_leaves_marked() -> None:
    """密钥叶子（api_key）以 secret=True 标记，前端只读展示。"""
    llm = next(g for g in config_schema()["groups"] if g["name"] == "llm")
    api_key = next(f for f in llm["fields"] if f["path"] == "api_key")
    assert api_key["secret"] is True
    thresholds = next(g for g in config_schema()["groups"] if g["name"] == "thresholds")
    assert all(f["secret"] is False for f in thresholds["fields"])


def test_no_frontend_backend_drift() -> None:
    """漂移守卫：前端设置页字段清单必须与后端 schema 完全对齐（双向）。"""
    be_groups = {g["name"] for g in config_schema()["groups"]}
    be_paths = {f["path"] for g in config_schema()["groups"] for f in g["fields"]}
    fe_groups, fe_keys = _frontend_groups_and_keys()
    assert be_groups == fe_groups, (
        f"配置分组漂移：后端独有 {sorted(be_groups - fe_groups)}；"
        f"前端独有 {sorted(fe_groups - be_groups)}"
    )
    assert be_paths == fe_keys, (
        f"配置字段漂移：后端未在面板声明 {sorted(be_paths - fe_keys)}；"
        f"前端多余声明 {sorted(fe_keys - be_paths)}"
    )
