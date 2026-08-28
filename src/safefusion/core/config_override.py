"""配置合并层（PRD v0.2.1 M2 + v0.3.0 M4）：settings 表（DB）读取、合并与来源标识。

职责（v0.3.0 起，配置存储由 ``data/config_overrides.json`` 迁移至 SQLite
settings 表，见 :func:`migrate_overrides_file`）：

- **持久化层**：settings 表（``storage.database.Database`` 的
  ``list_settings/set_settings/delete_settings``）按 ``(group, 叶子点分路径)``
  存 JSON 值；本模块只负责**读取合并**，写入一律经管理端 PUT 落库
  （见 ``api.admin`` 与 :func:`~safefusion.core.hot_apply`）；
- **合并优先级（决策 B）**：内置默认 < config.yaml < **DB settings** <
  环境变量。被 ``SAFEFUSION_<路径>_<键>`` 环境变量钉住的叶子在合并时跳过
  DB 值（env 只读内存生效、绝不反向写回 DB）；密钥（``api_key``）只从
  环境变量解析，settings 中出现的手工 ``api_key`` 行在合并时剥离并告警；
- **来源标识（决策 B）**：:func:`config_sources` 对每个分组产出叶子字段级
  ``effective_source``（default/yaml/db/env），供管理端
  ``GET /admin/config/sources`` 使用（T39 前端契约）；
- **分组对齐**：分组白名单取自 :class:`~safefusion.config.AppConfig` 当前
  结构（凡注解为 pydantic 子模型的分组字段），配置结构漂移时未知分组
  「丢弃并告警」（PRD 风险表）；``fuse_mode`` 是语义组的真实叶子
  （``SemanticConfig.fuse_mode``），v0.3.0 起随普通叶子一并合并生效；
- **迁移**：启动时检测旧 ``data/config_overrides.json`` → 一次性导入
  settings 表（先展平为叶子点分路径再逐组 upsert）→ 原文件改名
  ``config_overrides.json.migrated`` 归档；迁移失败仅告警不阻止启动；
- **Key 遮蔽**（决策 F）：``mask_secret_fields`` 把任意 ``api_key`` 叶子
  替换为 ``{"api_key_env": <变量名或 null>, "configured": <bool>}``，
  绝不返回密钥值。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from safefusion.config import AppConfig

_logger = logging.getLogger("safefusion.config_override")

#: 旧覆盖层文件名（位于 ``data_dir`` 下，v0.3.0 前使用；迁移后归档）
OVERRIDES_FILENAME = "config_overrides.json"

#: 迁移归档后缀：旧文件改名 ``config_overrides.json.migrated``
MIGRATED_SUFFIX = ".migrated"

#: 环境变量前缀（对齐 config.py 的三层加载约定）
_ENV_PREFIX = "SAFEFUSION_"

#: 语义组虚拟键（fuse_mode）允许取值（对齐 config.py SemanticConfig 语义，
#: 用于 PUT 白名单校验；合并路径按普通叶子处理）
_FUSE_MODES: tuple[str, ...] = ("concat", "weighted_avg", "pool")

#: 管理侧专用组（**不参与配置合并**）：settings 表 ``admin.token`` 由
#: 改密端点（POST /admin/config/password）直写、管理令牌解析直读，不属于
#: AppConfig 配置分组；合并时静默跳过（不告警），避免每次 effective_config
#: 刷屏「未知分组」日志。
SPECIAL_GROUPS: frozenset[str] = frozenset({"admin"})

#: 数值范围校验规则：{分组: {字段: (下限, 上限)}}（阈值 / 权重 / 采样带 ∈ [0,1]）
_RANGE_RULES: dict[str, dict[str, tuple[float, float]]] = {
    "thresholds": {
        "semantic_threshold": (0.0, 1.0),
        "margin_w": (0.0, 1.0),
        "confidence_low": (0.0, 1.0),
        "confidence_high": (0.0, 1.0),
    },
    "semantic": {
        "rerank_w_top": (0.0, 1.0),
        "rerank_w_margin": (0.0, 1.0),
        "rerank_w_rerank": (0.0, 1.0),
    },
    "review": {
        "band_low": (0.0, 1.0),
        "band_high": (0.0, 1.0),
    },
}

__all__ = [
    "MIGRATED_SUFFIX",
    "OVERRIDES_FILENAME",
    "candidate_overrides",
    "config_sources",
    "effective_config",
    "flatten_group",
    "get_config_groups",
    "group_to_dict",
    "mask_secret_fields",
    "merge_overrides",
    "migrate_overrides_file",
    "validate_group_update",
]


def get_config_groups() -> list[str]:
    """返回当前 ``AppConfig`` 的分组白名单（凡注解为 pydantic 子模型的分组字段）。

    对齐 PRD §2.2：``embedding / llm / thresholds / cache / keyword / semantic /
    review`` 等全部配置分组开放；``data_dir`` 等标量字段不构成分组。
    """

    groups: list[str] = []
    for name, field in AppConfig.model_fields.items():
        ann = field.annotation
        if ann is not None and isinstance(ann, type) and issubclass(ann, BaseModel):
            groups.append(name)
    return groups


def _group_model(group: str) -> type[BaseModel] | None:
    """返回分组的 pydantic 模型类；未知分组返回 None。"""

    field = AppConfig.model_fields.get(group)
    if field is None:
        return None
    ann = field.annotation
    if ann is None or not (isinstance(ann, type) and issubclass(ann, BaseModel)):
        return None
    return ann


def overrides_path(data_dir: str | Path) -> Path:
    """旧覆盖层文件路径（仅供迁移探测）：``{data_dir}/config_overrides.json``。"""

    return Path(data_dir) / OVERRIDES_FILENAME


# ------------------------------------------------------------------ 合并


def effective_config(config: AppConfig, db: Any = None) -> AppConfig:
    """在已加载配置（默认值 + YAML + 环境变量）之上合并 DB settings，返回新实例。

    Args:
        config: ``load_config`` 产出的配置（已含环境变量层，环境变量保持最高优先）。
        db: ``Database`` 实例（提供 ``list_settings()``）；为 None 时按无 DB
            配置处理（返回深拷贝，行为与 v0.2.1 无覆盖层时一致）。

    Returns:
        合并 DB settings 后的新 ``AppConfig``（原实例不被修改）。
    """

    if db is None:
        return config.model_copy(deep=True)
    try:
        rows = db.list_settings()
    except Exception as exc:
        _logger.warning("读取 settings 失败（按无 DB 配置继续）: %s", exc)
        return config.model_copy(deep=True)
    return merge_overrides(config, _db_rows_to_overrides(rows))


def merge_overrides(config: AppConfig, overrides: dict[str, dict[str, Any]]) -> AppConfig:
    """把各组叠加值按「默认 < YAML < DB < 环境变量」合并进配置副本。

    环境变量保持最高优先：被 ``SAFEFUSION_<路径>_<键>`` 钉住的叶子跳过叠加层
    （不反向写回）；未知分组、组内非法键整组丢弃并告警（PRD 风险表）。

    Args:
        config: 已加载配置（默认值 + YAML + 环境变量）。
        overrides: 分组字典（组内为浅层字典，嵌套子模型按嵌套 dict 展开；
            ``_db_rows_to_overrides`` / 迁移 / 校验链路产出该形态）。

    Returns:
        合并后的新 ``AppConfig`` 实例。
    """

    merged = config.model_copy(deep=True)
    pinned = _env_pinned_paths(merged)
    for group, payload in overrides.items():
        if group in SPECIAL_GROUPS:
            continue  # 管理侧专用组（admin.token 等）：直读直写，不参与合并
        model = _group_model(group)
        if model is None:
            _logger.warning("配置叠加包含未知分组 %s（与 AppConfig 不对齐），已丢弃", group)
            continue
        if not isinstance(payload, dict):
            _logger.warning("配置叠加分组 %s 不是对象字典，已丢弃", group)
            continue
        current = getattr(merged, group).model_dump()
        candidate = _deep_merge(current, _strip_secret_keys(payload))
        try:
            validated = model.model_validate(candidate).model_dump()
        except ValidationError as exc:
            _logger.warning("配置叠加分组 %s 校验失败，整组丢弃: %s", group, _strip_pydantic(exc))
            continue
        _apply_leaf_values(getattr(merged, group), validated, f"{group}.", pinned)
    return merged


def group_to_dict(config: AppConfig) -> dict[str, dict[str, Any]]:
    """把有效配置按分组序列化为字典（供 ``GET /admin/config`` 使用）。

    语义组 ``fuse_mode`` 为 ``SemanticConfig`` 真实叶子，随 ``model_dump``
    一并输出；本函数输出未遮蔽的原值，对外输出须经 :func:`mask_secret_fields`。
    """

    out: dict[str, dict[str, Any]] = {}
    for group in get_config_groups():
        out[group] = getattr(config, group).model_dump()
    return out


# ------------------------------------------------------------------ 来源标识


def config_sources(base_config: AppConfig, db: Any = None) -> dict[str, dict[str, str]]:
    """产出每个分组内叶子字段的来源映射（PRD v0.3.0 M4 决策 B）。

    **T39 前端契约**（``GET /admin/config/sources`` 响应结构）：::

        {
          "thresholds": {"semantic_threshold": "db", "margin_w": "default", ...},
          "llm":        {"base_url": "yaml", "api_key": "env", ...},
          ...
        }

    - 顶层键 = 配置分组名（``get_config_groups()`` 白名单）；
    - 组内键 = **叶子字段点分路径**（嵌套子模型用点连接，如
      ``embedding.local.model_name`` 的组内键 ``local.model_name``）；
    - 值 = ``effective_source`` ∈ default / yaml / db / env，语义为
      「当前生效值来自哪一层」：env（被 ``SAFEFUSION_*`` 环境变量钉住，
      对环境变量名始终有效，与取值无关）→ db（settings 表存在该叶子行）→
      yaml（``load_config`` 的 YAML 层使叶子偏离内置默认）→ default。
      密钥叶子（``api_key``）只可能为 env 或 default（yaml 被剥离、PUT
      拒绝写库，合并时手工 DB 行亦被剥离）。

    Args:
        base_config: **未合并 DB** 的基础配置（内置默认 + YAML + 环境变量）；
            传入已含 DB 的配置会使 yaml 判定失真。
        db: ``Database`` 实例；None 视为空 settings。

    Returns:
        分组 → {叶子点分路径: 来源}。
    """

    defaults = AppConfig()
    pinned = _env_pinned_paths(base_config)
    rows = db.list_settings() if db is not None else []
    db_keys = {(str(row["group"]), str(row["key"])) for row in rows}

    out: dict[str, dict[str, str]] = {}
    for group in get_config_groups():
        model = getattr(base_config, group)
        mapping: dict[str, str] = {}
        for leaf_path, _suffix in _leaf_paths(model):
            full = f"{group}.{leaf_path}"
            if _is_secret_leaf(full):
                mapping[leaf_path] = _secret_source(full, base_config)
            elif full in pinned:
                mapping[leaf_path] = "env"
            elif (group, leaf_path) in db_keys:
                mapping[leaf_path] = "db"
            elif _get_leaf(getattr(defaults, group), leaf_path) != _get_leaf(model, leaf_path):
                mapping[leaf_path] = "yaml"
            else:
                mapping[leaf_path] = "default"
        out[group] = mapping
    return out


# ------------------------------------------------------------------ 遮蔽


def mask_secret_fields(group: str, group_dict: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    """递归遮蔽密钥叶子：任何 ``api_key`` 键 → ``{"api_key_env", "configured"}``。

    只返回环境变量**名**与「当前是否已设置」布尔（决策 F），绝不读取 / 返回
    密钥值；``configured`` 依当前进程环境变量判定。

    Args:
        group: 分组名（用于定位密钥的环境变量规范名，如 ``llm.api_key``）。
        group_dict: 该组原始字典（``group_to_dict`` 输出）。
        config: 有效配置（提供 ``llm.api_key_env`` 等变量名）。
    """

    def _walk(node: dict[str, Any], prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if key == "api_key":
                out[key] = _secret_status(path, config)
            elif isinstance(value, dict):
                out[key] = _walk(value, path)
            else:
                out[key] = value
        return out

    return _walk(group_dict, group)


# ------------------------------------------------------------------ 校验


def validate_group_update(
    group: str,
    payload: dict[str, Any],
    config: AppConfig,
    db: Any = None,
) -> dict[str, Any]:
    """校验一次 ``PUT /admin/config/{group}`` 的分组更新负载。

    校验顺序（任一失败抛 :class:`ValueError`，消息面向用户可读）：
    1. 分组白名单；
    2. 负载必须包含密钥键（``api_key`` 禁止写入配置存储，红线）；
    3. 字符串字段非空（必填项）、pydantic 类型 / 未知键校验；
    4. ``backend`` 白名单（embedding: local|cloud；cache: memory|redis）；
    5. 数值范围（阈值 / 权重 / 采样带 ∈ [0,1]）；
    6. ``fuse_mode`` 维度一致性：backend=cloud 且 fuse_mode ∈ weighted_avg/pool
       （在线维度 ≠ 本地 CLIP 512）→ 422 并建议改用 concat。

    Args:
        group: 目标分组名（未知分组直接报错）。
        payload: 请求体分组对象（允许部分键，未给出的键沿用当前有效值）。
        config: 基配置（默认值 + YAML + 环境变量，用于构造当前有效分组）。
        db: ``Database`` 实例（提供当前 settings，None 视为空）。

    Returns:
        校验通过后的合并分组字典（含虚拟键 ``fuse_mode``），供响应 / 试建造使用。
    """

    if group not in get_config_groups():
        raise ValueError(f"未知配置分组: {group}，可选分组: {', '.join(get_config_groups())}")
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是分组配置对象（JSON 对象字典）")

    secret_paths = _find_secret_keys(payload)
    if secret_paths:
        raise ValueError(
            "禁止写入密钥字段: "
            + ", ".join(secret_paths)
            + "；密钥仅可通过环境变量配置（如 SAFEFUSION_LLM_API_KEY），不写入配置存储"
        )
    for path, value in _iter_string_leaves(payload):
        if value.strip() == "":
            raise ValueError(f"字段 {group}.{path} 不能为空（必填项）")

    effective = effective_config(config, db)
    current = getattr(effective, group).model_dump()
    candidate = _deep_merge(current, payload)

    # 虚拟键 fuse_mode：语义组可配，取值白名单；其余分组沿用当前有效值
    if group == "semantic" and "fuse_mode" in candidate:
        fuse_mode = candidate["fuse_mode"]
        if fuse_mode not in _FUSE_MODES:
            raise ValueError(f"未知 fuse_mode: {fuse_mode!r}，可选: {' / '.join(_FUSE_MODES)}")
        body = {k: v for k, v in candidate.items() if k != "fuse_mode"}
    else:
        fuse_mode = effective.semantic.fuse_mode
        body = candidate

    try:
        validated_model = _group_model(group).model_validate(body)  # type: ignore[union-attr]
    except ValidationError as exc:
        raise ValueError(_readable_validation_error(exc, group)) from exc
    validated = validated_model.model_dump()

    _validate_business_rules(group, validated, fuse_mode, effective, db)

    candidate["fuse_mode"] = fuse_mode
    return candidate


def _validate_business_rules(
    group: str,
    validated: dict[str, Any],
    fuse_mode: str,
    effective: AppConfig,
    db: Any = None,
) -> None:
    """分组业务规则（backend 白名单 / 必填 / 数值范围 / fuse_mode 维度一致性）。"""

    if group == "embedding":
        backend = validated["backend"]
        if backend not in ("local", "cloud"):
            raise ValueError(f"未知 embedding.backend: {backend!r}（可选 local / cloud）")
        if backend == "cloud":
            cloud = validated.get("cloud") or {}
            missing = [k for k in ("base_url", "model") if not str(cloud.get(k) or "").strip()]
            if missing:
                raise ValueError(
                    "backend=cloud 时 embedding.cloud 必填字段缺失: " + ", ".join(missing)
                )
            if fuse_mode in ("weighted_avg", "pool"):
                raise ValueError(
                    "fuse_mode="
                    + fuse_mode
                    + " 要求文本与图像向量同维，但 backend=cloud（在线 embedding 维度"
                    " 与本地 Chinese-CLIP 512 不一致，图文并存融合会报错）；"
                    "请将 semantic.fuse_mode 改为 concat（推荐），或保持 backend=local"
                )
        else:
            local = validated.get("local") or {}
            if not str(local.get("model_name") or "").strip():
                raise ValueError("embedding.local.model_name 不能为空")
    elif group == "cache":
        backend = validated["backend"]
        if backend not in ("memory", "redis"):
            raise ValueError(f"未知 cache.backend: {backend!r}（可选 memory / redis）")
    elif group == "semantic":
        embedding_backend = effective.embedding.backend
        if embedding_backend == "cloud" and fuse_mode in ("weighted_avg", "pool"):
            raise ValueError(
                "fuse_mode=" + fuse_mode + " 要求文本与图像向量同维，但当前 embedding.backend=cloud"
                "（在线维度 ≠ 本地 CLIP 512）；请改用 concat"
            )

    for field, (low, high) in _RANGE_RULES.get(group, {}).items():
        value = validated.get(field)
        if value is not None and not (low <= value <= high):
            raise ValueError(f"字段 {group}.{field} 取值 {value} 超出范围 [{low}, {high}]")


# ------------------------------------------------------------------ 迁移


def migrate_overrides_file(data_dir: str | Path, db: Any) -> bool:
    """把旧覆盖层文件一次性迁移进 settings 表并归档（PRD v0.3.0 M4 决策 B）。

    - 文件不存在 → False（无待迁移）；
    - 逐组先展平为叶子点分路径再 ``set_settings``（键级 upsert，幂等）；
      单组写库失败仅告警并跳过该组；
    - 全部导入完成后原文件改名 ``config_overrides.json.migrated``（已存在该
      归档名时追加 UTC 时间戳防覆盖）；
    - **失败仅告警不阻止启动**：任何异常在此函数内被吞掉并记录，返回值供
      调用方观测；启动流程不因迁移失败中断（DB 空时按默认配置继续）。

    Args:
        data_dir: 运行时数据目录（旧覆盖层文件所在目录）。
        db: ``Database`` 实例（settings 表写入目标）。

    Returns:
        True 表示发现旧文件且已尝试迁移（含只归档无内容 / 部分失败）；
        False 表示无旧文件可迁移。
    """

    path = Path(data_dir) / OVERRIDES_FILENAME
    if not path.is_file():
        return False
    try:
        overrides = _read_legacy_overrides(path)
    except Exception as exc:
        _logger.warning("迁移失败：旧覆盖层读取异常，跳过导入（原文件保留）: %s", exc)
        return False
    if not overrides:
        _logger.info("旧覆盖层为空或不可解析，直接归档（无内容可迁移）: %s", path.name)
    for group, payload in overrides.items():
        try:
            db.set_settings(str(group), flatten_group(payload))
        except Exception as exc:
            _logger.warning("迁移 settings 写库失败（跳过该组 %s）: %s", group, exc)
    target = path.with_name(OVERRIDES_FILENAME + MIGRATED_SUFFIX)
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        target = path.with_name(f"{OVERRIDES_FILENAME}.migrated.{stamp}")
    try:
        path.rename(target)
    except OSError as exc:
        _logger.warning("旧覆盖层归档改名失败（settings 已导入，原文件保留待手工处理）: %s", exc)
        return True
    _logger.info("配置覆盖层已一次性导入 settings 表并归档为 %s", target.name)
    return True


def _read_legacy_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """读取旧覆盖层文件（v0.3.0 前格式），返回按分组存储的字典。

    文件不存在 → ``{}``；JSON 损坏或顶层不是分组映射 → 告警并返回 ``{}``
    （迁移绝不因旧文件损坏中断启动）。
    """

    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.warning("旧覆盖层文件读取失败（按空覆盖层继续）: %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("旧覆盖层文件顶层必须是分组映射，已忽略: %s", path)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


# ------------------------------------------------------------------ 展平 / 展开


def flatten_group(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """把分组载荷展平为 ``{叶子点分路径: 值}``（settings 表 key 口径）。

    嵌套 dict 逐层下钻：``{"local": {"model_name": "x"}}`` →
    ``{"local.model_name": "x"}``。仅 leaf 值（非 dict）落键。
    """

    flat: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten_group(value, path))
        else:
            flat[path] = value
    return flat


def candidate_overrides(
    rows: list[dict[str, Any]], group: str, payload: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """从 settings 行构造叠加字典，并按本次负载替换 / 移除指定分组。

    供管理端 PUT 计算「候选有效配置」使用（写库前先试建造）：

    - ``payload=None``：移除该分组（空对象 ``{}`` 恢复默认的删除语义）；
    - ``payload`` 非 None：**部分键覆盖**——深度合并进该组既有叠加
      （与 v0.2.1 覆盖层「部分键覆盖」语义一致，未提及叶子沿用
      YAML / 默认 / 其余 DB 行）。

    Args:
        rows: ``db.list_settings()`` 原始行。
        group: 本次目标分组。
        payload: 本次负载（已通过校验、已剥离密钥）或 None（删除）。

    Returns:
        ``{group: 组内嵌套 dict}`` 叠加字典（merge_overrides 输入形态）。
    """

    overrides = _db_rows_to_overrides(list(rows))
    if payload is None:
        overrides.pop(group, None)
    else:
        overrides.setdefault(group, {})
        overrides[group] = _deep_merge(overrides.get(group, {}), payload)
    return overrides


def _expand_dotted(path: str, value: Any) -> dict[str, Any]:
    """把单条叶子点分路径展开为嵌套 dict（与 :func:`_flatten_group` 互逆）。"""

    parts = path.split(".")
    node: dict[str, Any] = {}
    cursor = node
    for part in parts[:-1]:
        nxt: dict[str, Any] = {}
        cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
    return node


def _db_rows_to_overrides(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把 settings 行列表转为 ``{group: 组内嵌套 dict}``（merge 输入形态）。

    同一组多条叶子经深度合并拼成嵌套结构（``local.model_name`` +
    ``local.device`` → ``{"local": {...}}``）；某行 value_json 损坏时跳过
    该行并告警，不影响其余行。
    """

    overrides: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = str(row["group"])
        key = str(row["key"])
        try:
            value = json.loads(str(row["value_json"]))
        except (TypeError, ValueError) as exc:
            _logger.warning("settings 行值 JSON 解析失败，跳过: %s.%s（%s）", group, key, exc)
            continue
        nested = _expand_dotted(key, value)
        if group not in overrides:
            overrides[group] = {}
        overrides[group] = _deep_merge(overrides[group], nested)
    return overrides


# ------------------------------------------------------------------ 内部工具


def _iter_string_leaves(node: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """收集负载中的字符串叶子（路径, 值），供必填非空校验。"""

    found: list[tuple[str, str]] = []
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            found.extend(_iter_string_leaves(value, path))
        elif isinstance(value, str):
            found.append((path, value))
    return found


def _find_secret_keys(node: dict[str, Any], prefix: str = "") -> list[str]:
    """递归查找 ``api_key`` 键的完整路径列表。"""

    found: list[str] = []
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if key == "api_key":
            found.append(path)
        elif isinstance(value, dict):
            found.extend(_find_secret_keys(value, path))
    return found


def _env_pinned_paths(config: AppConfig) -> frozenset[str]:
    """当前进程内由 ``SAFEFUSION_<路径>_<键>`` 环境变量钉住的叶子点分路径集合。

    环境变量后缀是下划线连接（``thresholds_semantic_threshold``），而配置
    叶子路径是点分（``thresholds.semantic_threshold``），且叶子名本身可含
    下划线（如 ``phash_whitelist_distance``）——故经配置模型的标量叶子映射
    （对齐 config.py 的 ``_iter_scalar_targets``）把后缀解析回点分路径。
    密钥类（``_api_key`` 结尾）由 config.py 单独解析，不参与“钉住”判定。
    """

    suffix_to_path = {suffix: path for path, suffix in _leaf_paths(config)}
    pinned: set[str] = set()
    for name in os.environ:
        if not name.startswith(_ENV_PREFIX):
            continue
        if name.lower().endswith("_api_key"):
            continue
        path = suffix_to_path.get(name[len(_ENV_PREFIX) :].lower())
        if path is not None:
            pinned.add(path)
    return frozenset(pinned)


def _leaf_paths(model: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """遍历配置模型的全部标量叶子，产出 (点分路径, 环境变量后缀) 二元组。"""

    pairs: list[tuple[str, str]] = []
    for name, field in type(model).model_fields.items():
        ann = field.annotation
        if ann is not None and isinstance(ann, type) and issubclass(ann, BaseModel):
            pairs.extend(_leaf_paths(getattr(model, name), prefix + (name,)))
        else:
            pairs.append((".".join((*prefix, name)), "_".join((*prefix, name)).lower()))
    return pairs


def _get_leaf(model: Any, path: str) -> Any:
    """按点分路径读取模型叶子的当前值。"""

    holder: Any = model
    for part in path.split("."):
        holder = getattr(holder, part)
    return holder


def _is_secret_leaf(full_path: str) -> bool:
    """是否为密钥叶子（``llm.api_key`` / ``embedding.cloud.api_key``）。"""

    return full_path.endswith(".api_key")


def _secret_source(full_path: str, config: AppConfig) -> str:
    """密钥叶子的来源：env（对应环境变量已设置）或 default（未设置）。"""

    if full_path == "llm.api_key":
        env = (
            os.environ.get("SAFEFUSION_LLM_API_KEY") is not None
            or os.environ.get(config.llm.api_key_env) is not None
        )
    elif full_path == "embedding.cloud.api_key":
        env = os.environ.get("SAFEFUSION_EMBEDDING_API_KEY") is not None
        if config.embedding.cloud.api_key_env:
            env = env or (os.environ.get(config.embedding.cloud.api_key_env) is not None)
    else:
        return "default"
    return "env" if env else "default"


def _strip_secret_keys(node: dict[str, Any]) -> dict[str, Any]:
    """递归删除叠加层负载中的 ``api_key`` 键（密钥只从环境变量解析，红线）。

    对**手工编辑**的 settings 行兜底（管理端点 PUT 已前置拒绝）；剥离时告警，
    保证合并路径永远不把密钥值写进配置模型。
    """

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "api_key":
            if value is not None:
                _logger.warning("配置存储禁止携带密钥字段 api_key，已剥离（密钥只从环境变量读取）")
            continue
        if isinstance(value, dict):
            out[key] = _strip_secret_keys(value)
        else:
            out[key] = value
    return out


def _apply_leaf_values(
    holder: Any, values: dict[str, Any], prefix: str, pinned: frozenset[str]
) -> None:
    """把分组合并结果逐叶子写回模型实例；被环境变量钉住的叶子跳过。"""

    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            child = getattr(holder, key)
            if isinstance(child, BaseModel):
                _apply_leaf_values(child, value, path + ".", pinned)
        elif path in pinned:
            continue  # 环境变量最高优先，DB 不反向写回
        else:
            setattr(holder, key, value)


def _secret_status(path: str, config: AppConfig) -> dict[str, Any]:
    """返回某密钥叶子的遮蔽信息：环境变量名 + 是否已设置（不读真实值）。"""

    if path == "llm.api_key":
        env_var = config.llm.api_key_env or "OPENAI_API_KEY"
        configured = os.environ.get("SAFEFUSION_LLM_API_KEY") is not None or (
            os.environ.get(env_var) is not None
        )
        return {"api_key_env": env_var, "configured": configured}
    if path == "embedding.cloud.api_key":
        env_var = config.embedding.cloud.api_key_env or "SAFEFUSION_EMBEDDING_API_KEY"
        configured = os.environ.get("SAFEFUSION_EMBEDDING_API_KEY") is not None
        if config.embedding.cloud.api_key_env:
            configured = configured or (
                os.environ.get(config.embedding.cloud.api_key_env) is not None
            )
        return {"api_key_env": env_var, "configured": configured}
    return {"api_key_env": None, "configured": False}


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """递归深度合并两个字典（``update`` 优先，嵌套字典逐层合并）。"""

    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _readable_validation_error(exc: ValidationError, group: str) -> str:
    """把 pydantic 校验错误转成面向用户的中文可读消息（取第一条，字段带路径）。"""

    first = exc.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    etype = str(first.get("type", ""))
    if etype == "extra_forbidden":
        return f"未知配置键: {group}.{loc}（该分组不含此字段）"
    if etype == "missing":
        return f"必要字段缺失: {group}.{loc}"
    msg = str(first.get("msg", "")).replace("Value error, ", "")
    return f"字段 {group}.{loc} 配置非法: {msg}"


def _strip_pydantic(exc: ValidationError) -> str:
    """日志用的 pydantic 错误摘要（不携带任何密钥值）。"""

    first = exc.errors()[0]
    return f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg', '')}"
