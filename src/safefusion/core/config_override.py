"""配置覆盖层（PRD v0.2.1 M2，决策 C/D/E/F）：``data/config_overrides.json`` 读写与合并。

职责：
- **持久化覆盖层**：``{data_dir}/config_overrides.json``（gitignored），按分组存储
  ``{"group": {...}}``；提供原子写（临时文件 + ``os.replace``）的加载 / 保存 /
  合并 / 删除单组接口，多线程下读写同一文件以进程内锁串行化（防 SELECT-then-WRITE
  竞态，rules.md 代码质量清单）；
- **合并优先级**：内置默认 < config.yaml < **覆盖层** < 环境变量。环境变量保持
  最高优先：被 ``SAFEFUSION_<路径>_<键>`` 环境变量钉住的叶子在合并时跳过覆盖层
  （不反向写回）；密钥（``api_key``）只从环境变量解析，覆盖层一律拒绝写入；
- **分组对齐**：分组白名单取自 :class:`~safefusion.config.AppConfig` 当前结构
  （凡注解为 pydantic 子模型的分组字段），配置结构漂移时未知分组「丢弃并告警」
  （PRD 风险表）；``fuse_mode`` 是语义组的**虚拟键**（``SemanticEngine`` 阈值键，
  非 AppConfig 字段），仅在 GET/PUT 序列化与校验层可见，不进入 pydantic 校验；
- **Key 遮蔽**（决策 F）：``mask_secret_fields`` 把任意 ``api_key`` 叶子替换为
  ``{"api_key_env": <变量名或 null>, "configured": <bool>}``，绝不返回密钥值。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from safefusion.config import AppConfig

_logger = logging.getLogger("safefusion.config_override")

#: 覆盖层文件名（位于 ``data_dir`` 下，gitignored）
OVERRIDES_FILENAME = "config_overrides.json"

#: 环境变量前缀（对齐 config.py 的三层加载约定）
_ENV_PREFIX = "SAFEFUSION_"

#: 语义组虚拟键允许取值（对齐 ``engines.embedding.fuse_vectors`` 模式参数）
_FUSE_MODES: tuple[str, ...] = ("concat", "weighted_avg", "pool")
#: 语义组虚拟键默认值（对齐 ``engines.semantic.SemanticEngine._DEFAULT_THRESHOLDS``）
_FUSE_MODE_DEFAULT = "pool"

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

#: 覆盖层文件读写串行化锁（管理端并发读写不致互相覆盖）
_WRITE_LOCK = threading.Lock()

__all__ = [
    "OVERRIDES_FILENAME",
    "delete_group_overrides",
    "effective_config",
    "get_config_groups",
    "group_to_dict",
    "load_overrides",
    "mask_secret_fields",
    "merge_overrides",
    "save_overrides",
    "update_overrides",
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
    """覆盖层文件路径：``{data_dir}/config_overrides.json``。"""

    return Path(data_dir) / OVERRIDES_FILENAME


def load_overrides(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """读取覆盖层文件，返回按分组存储的字典。

    文件不存在 → ``{}``；JSON 损坏或顶层不是分组映射 → 告警并返回 ``{}``
    （绝不因覆盖层损坏拖垮启动 / 管理 API）。
    """

    path = overrides_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.warning("覆盖层文件读取失败（按空覆盖层继续）: %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("覆盖层文件顶层必须是分组映射，已忽略: %s", path)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_overrides(data_dir: str | Path, group: str, payload: dict[str, Any]) -> Path:
    """写入单个分组到覆盖层（整体替换该组），原子写：临时文件 + ``os.replace``。

    Args:
        data_dir: 运行时数据目录（覆盖层文件所在目录）。
        group: 分组名（确保为 ``get_config_groups()`` 白名单成员）。
        payload: 该组配置字典（允许部分键，加载时与默认值合并）。

    Returns:
        写入后的覆盖层文件路径。
    """

    with _WRITE_LOCK:
        overrides = load_overrides(data_dir)
        overrides[group] = payload
        return _atomic_write_json(overrides_path(data_dir), overrides)


def update_overrides(data_dir: str | Path, group: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把 ``payload`` 深度合并进已有分组并落盘；返回合并后的分组字典。"""

    with _WRITE_LOCK:
        overrides = load_overrides(data_dir)
        merged = _deep_merge(overrides.get(group, {}), payload)
        overrides[group] = merged
        _atomic_write_json(overrides_path(data_dir), overrides)
        return merged


def delete_group_overrides(data_dir: str | Path, group: str) -> bool:
    """删除覆盖层中的分组；返回该组原先是否存在。

    删除后无剩余分组则移除整个覆盖层文件（磁盘不留空壳）。
    """

    with _WRITE_LOCK:
        overrides = load_overrides(data_dir)
        if group not in overrides:
            return False
        del overrides[group]
        path = overrides_path(data_dir)
        if overrides:
            _atomic_write_json(path, overrides)
        else:
            with contextlib.suppress(OSError):
                path.unlink()
        return True


def effective_config(config: AppConfig, data_dir: str | Path) -> AppConfig:
    """在已加载配置（默认值 + YAML + 环境变量）之上合并覆盖层，返回新实例。

    Args:
        config: ``load_config`` 产出的配置（已含环境变量层，环境变量保持最高优先）。
        data_dir: 覆盖层文件所在目录（通常即 ``config.data_dir``）。

    Returns:
        合并覆盖层后的新 ``AppConfig``（原实例不被修改）。
    """

    return merge_overrides(config, load_overrides(data_dir))


def merge_overrides(config: AppConfig, overrides: dict[str, dict[str, Any]]) -> AppConfig:
    """把覆盖层按「默认 < YAML < 覆盖层 < 环境变量」合并进配置副本。

    环境变量保持最高优先：被 ``SAFEFUSION_<路径>_<键>`` 钉住的叶子跳过覆盖层
    （不反向写回）；未知分组、组内非法键整组丢弃并告警（PRD 风险表）。

    Args:
        config: 已加载配置（默认值 + YAML + 环境变量）。
        overrides: ``load_overrides`` 产出的分组字典。

    Returns:
        合并后的新 ``AppConfig`` 实例。
    """

    merged = config.model_copy(deep=True)
    pinned = _env_pinned_paths(merged)
    for group, payload in overrides.items():
        model = _group_model(group)
        if model is None:
            _logger.warning("覆盖层包含未知分组 %s（与 AppConfig 不对齐），已丢弃", group)
            continue
        if not isinstance(payload, dict):
            _logger.warning("覆盖层分组 %s 不是对象字典，已丢弃", group)
            continue
        current = getattr(merged, group).model_dump()
        candidate = _deep_merge(current, _strip_secret_keys(payload))
        if group == "semantic":
            candidate.pop("fuse_mode", None)  # 虚拟键：不进入 pydantic 校验
        try:
            validated = model.model_validate(candidate).model_dump()
        except ValidationError as exc:
            _logger.warning("覆盖层分组 %s 校验失败，整组丢弃: %s", group, _strip_pydantic(exc))
            continue
        _apply_leaf_values(getattr(merged, group), validated, f"{group}.", pinned)
    return merged


def group_to_dict(config: AppConfig, data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """把有效配置按分组序列化为字典（供 ``GET /admin/config`` 使用）。

    语义组额外携带虚拟键 ``fuse_mode``（默认 ``pool``，可用覆盖层修改），
    与 ``SemanticEngine`` 阈值结构对齐；本函数输出未遮蔽的原值，
    对外输出须经 :func:`mask_secret_fields`。
    """

    overrides = load_overrides(data_dir)
    out: dict[str, dict[str, Any]] = {}
    for group in get_config_groups():
        out[group] = getattr(config, group).model_dump()
        if group == "semantic":
            fuse = overrides.get("semantic", {}).get("fuse_mode")
            out[group]["fuse_mode"] = fuse if fuse in _FUSE_MODES else _FUSE_MODE_DEFAULT
    return out


def mask_secret_fields(group: str, group_dict: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    """递归遮蔽密钥叶子：任何 ``api_key`` 键 → ``{"api_key_env", "configured"}``。

    只返回环境变量**名**与「当前是否已设置」布尔（决策 F），绝不读取 / 返回密钥值；
    ``configured`` 依当前进程环境变量判定（若配置是环境变量机制，则该布尔表示
    对应环境变量已设置）。

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


def validate_group_update(
    group: str,
    payload: dict[str, Any],
    config: AppConfig,
    data_dir: str | Path,
) -> dict[str, Any]:
    """校验一次 ``PUT /admin/config/{group}`` 的分组更新负载。

    校验顺序（任一失败抛 :class:`ValueError`，消息面向用户可读）：
    1. 分组白名单；
    2. 负载必须包含密钥键（``api_key`` 禁止写入覆盖层，红线）；
    3. 字符串字段非空（必填项）、pydantic 类型 / 未知键校验；
    4. ``backend`` 白名单（embedding: local|cloud；cache: memory|redis）；
    5. 数值范围（阈值 / 权重 / 采样带 ∈ [0,1]）；
    6. ``fuse_mode`` 维度一致性：backend=cloud 且 fuse_mode ∈ weighted_avg/pool
       （在线维度 ≠ 本地 CLIP 512）→ 422 并建议改用 concat。

    Args:
        group: 目标分组名（未知分组直接报错）。
        payload: 请求体分组对象（允许部分键，未给出的键沿用当前有效值）。
        config: 基配置（默认值 + YAML + 环境变量，用于构造当前有效分组）。
        data_dir: 覆盖层文件所在目录。

    Returns:
        校验通过后的合并分组字典（含虚拟键 ``fuse_mode``），供落盘 / 返回使用。
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
            + "；密钥仅可通过环境变量配置（如 SAFEFUSION_LLM_API_KEY），不写入覆盖层"
        )
    for path, value in _iter_string_leaves(payload):
        if value.strip() == "":
            raise ValueError(f"字段 {group}.{path} 不能为空（必填项）")

    effective = effective_config(config, data_dir)
    current = getattr(effective, group).model_dump()
    candidate = _deep_merge(current, payload)

    # 虚拟键 fuse_mode：语义组可配，取值白名单；其余分组沿用当前有效值
    if group == "semantic" and "fuse_mode" in candidate:
        fuse_mode = candidate["fuse_mode"]
        if fuse_mode not in _FUSE_MODES:
            raise ValueError(f"未知 fuse_mode: {fuse_mode!r}，可选: {' / '.join(_FUSE_MODES)}")
        body = {k: v for k, v in candidate.items() if k != "fuse_mode"}
    else:
        fuse_mode = group_to_dict(effective, data_dir)["semantic"]["fuse_mode"]
        body = candidate

    try:
        validated_model = _group_model(group).model_validate(body)  # type: ignore[union-attr]
    except ValidationError as exc:
        raise ValueError(_readable_validation_error(exc, group)) from exc
    validated = validated_model.model_dump()

    _validate_business_rules(group, validated, fuse_mode, effective, data_dir)

    candidate["fuse_mode"] = fuse_mode
    return candidate


def _validate_business_rules(
    group: str,
    validated: dict[str, Any],
    fuse_mode: str,
    effective: AppConfig,
    data_dir: str | Path,
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

    环境变量后缀是下划线连接（``thresholds_semantic_threshold``），而覆盖层
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


def _strip_secret_keys(node: dict[str, Any]) -> dict[str, Any]:
    """递归删除覆盖层负载中的 ``api_key`` 键（密钥只从环境变量解析，红线）。

    对**手工编辑**的覆盖层文件兜底（管理端点 PUT 已前置拒绝）；剥离时告警，
    保证合并路径永远不把密钥值写进配置模型。
    """

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "api_key":
            if value is not None:
                _logger.warning("覆盖层禁止携带密钥字段 api_key，已剥离（密钥只从环境变量读取）")
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
            continue  # 环境变量最高优先，覆盖层不反向写回
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


def _atomic_write_json(path: Path, data: dict[str, Any]) -> Path:
    """原子写 JSON：临时文件写入 + ``os.replace`` 替换（同目录跨平台安全）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path
