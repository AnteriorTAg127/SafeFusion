"""管理 API（:8001，PRD §4.2）：Key / 词库 / 正则规则 / 图片白名单 / 审核日志 /
向量重建 / 配置读写（v0.2.1 M2 + v0.3.0 M4 DB 化与全量热应用）。

设计要点：
- 认证：全部 ``/admin/*`` 经路由器级依赖要求 ``X-Admin-Token`` 头与当前
  管理令牌一致（``dependencies.require_admin_token``，常数时间比较）。
  令牌由 :class:`~safefusion.api.dependencies.AdminToken` 容器承载并暴露于
  ``app.state.admin_token``，供改密端点热切换（旧令牌立即失效）。
- 令牌解析顺序：config 携带的 ``admin_token``（或 config.admin.token）→
  环境变量 ``ADMIN_PASSWORD`` → 两者皆缺时启动生成 ``secrets.token_urlsafe(16)``
  并以 WARNING 打印一次。**自动生成场景是唯一允许日志打印令牌的例外**：
  管理员启动后需要借此获知令牌；配置/环境变量来源的令牌绝不写入日志。
- 存储解耦：只依赖 ``Database`` 已实现的方法；keys 的删除与备注更新所需
  ``Database.delete_key`` / ``Database.update_key_note`` 当前缺失，采用鸭子
  类型探测，缺失时返回 501（Not Implemented）并给出中文错误，不静默失败。
- 正则消歧规则（PRD v0.2 M4）：``POST /admin/rules`` 接受 JSON 数组或
  multipart CSV，写库后调用注入的 ``reload_hook``（通常为
  ``AppContext.reload_rules``）热重载即时生效；未注入时端点正常返回但响应含
  ``"reload": "skipped"``。
- 图片白名单文件持久化于 ``config.data_dir/whitelist/``（缺省
  ``./data/whitelist/``），以 ``{md5}.png`` 命名 —— md5 取 PNG 编码字节
  （与 ``compute_hashes`` 一致），故磁盘文件内容哈希与入库 md5 相等。
- 向量重建清单（manifest）由 ``scripts/normalize_assets.py`` 产出（PRD §5
  统一布局 ``data/vectors/``）；``rebuild_hook(manifest_path)`` 由集成方注入。
- **配置端点（v0.3.0 M4）**：存储迁至 SQLite settings 表（优先级 内置默认 <
  config.yaml < DB settings < 环境变量；env 只读内存生效、绝不写 DB）；
  ``PUT /admin/config/{group}`` 写库后**立即热应用**（参数类直接改实例 /
  组件重建类原子替换 / 失败回滚，见 ``core.hot_apply``），不再「重启生效」；
  ``GET /admin/config/sources`` 返回字段级来源映射（T39 前端契约）。
- **追加端点区（v0.3.0 M2/M3/M5/M6，T30B）**：``GET /admin/models``（chinese-clip
  / fasttext / 向量库 / 语义引擎状态含懒装配原因码）、``POST /admin/models/
  download`` + ``GET /admin/models/download/{task_id}``（后台下载任务与进度轮询，
  同模型并发互斥）、``POST /admin/models/load``（显式装配语义层，同步完成）、
  ``GET /admin/health``（组件就绪 + 数据概况 + 缓存统计，degraded 与 :8000 同
  口径）、``GET /admin/test-examples`` 与 ``POST /admin/test-audit``（现场试运行，
  完整 detail 供证据面板）、``POST /admin/config/test-connection``（embedding /
  llm / fasttext 渠道冒烟）、``POST /admin/config/password``（改密：hmac 校验 +
  长度 ≥10 + AdminToken 热切 + settings 表 admin.token 持久化）。
- v0.1 管理端 QPS 低，DAO 同步调用直接放入 async 端点可接受；单条失败（如
  单张图片解码失败）记录错误并继续处理其余条目，不拖垮整批（rules.md 规则 4）。
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hmac
import inspect
import io
import json
import os
import random
import secrets
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from safefusion import __version__
from safefusion.api.dependencies import AdminToken, Page, pagination, require_admin_token
from safefusion.config import AppConfig, load_config
from safefusion.core import hot_apply
from safefusion.core.config_override import (
    candidate_overrides,
    config_sources,
    effective_config,
    flatten_group,
    get_config_groups,
    group_to_dict,
    mask_secret_fields,
    merge_overrides,
    validate_group_update,
)
from safefusion.core.context import AppContext
from safefusion.core.orchestrator import AuditOrchestrator
from safefusion.core.review import ReviewScheduler
from safefusion.engines.embedding import get_embedding_backend
from safefusion.engines.image_pipeline import WhitelistMatcher, compute_hashes, decode_images
from safefusion.engines.light_model import LightTextModel
from safefusion.engines.llm_client import LLMClient
from safefusion.engines.model_repo import (
    DEFAULT_MODEL_NAME,
    DownloadManager,
    probe_hf_model,
    resolve_hf_cache_dir,
)
from safefusion.logging_setup import get_logger
from safefusion.models.schemas import AuditRequest, ImageInput
from safefusion.storage.database import Database

logger = get_logger("api.admin")

__all__ = ["create_admin_app"]

#: 日志导出 CSV 列名（与 audit_logs 表列对齐，detail 为 detail_json 原文）
_EXPORT_HEADERS = (
    "request_id",
    "ts",
    "text_hash",
    "has_violation",
    "confidence",
    "category",
    "source",
    "key_tier",
    "detail",
)

#: 白名单文件扩展名固定为 PNG（与 compute_hashes 的 md5 口径一致）
_WHITELIST_EXT = ".png"

#: 归一化清单默认路径：data/vectors/manifest.jsonl（PRD §5，由 normalize_assets 产出）
_DEFAULT_MANIFEST = "vectors/manifest.jsonl"

#: 日志查询参数描述（query_logs / export_logs 共用，控制行长）
_Q_START_T = "起始时间（ISO 8601，含端点）"
_Q_END_T = "结束时间（ISO 8601，含端点）"
_Q_HAS_VIOLATION_T = "按结论过滤：true=违规 / false=安全"
_Q_SOURCE_T = "按判定来源过滤（semantic / llm / basic_rules_pass 等）"
_Q_CATEGORY_T = "按类别过滤（如 色情 / 赌博）"
_Q_KEY_TIER_T = "按 Key 分组过滤（standard / full）"


class KeyCreate(BaseModel):
    """POST /admin/keys 请求体。"""

    tier: Literal["standard", "full"] = Field(
        default="standard", description="Key 权限分组：standard（基本判定）| full（完整细节）"
    )
    note: str | None = Field(default=None, description="备注（用途 / 归属方）")


class KeyPatch(BaseModel):
    """PATCH /admin/keys/{key} 请求体（enabled 与 note 至少其一）。"""

    enabled: bool | None = Field(default=None, description="启用 / 禁用")
    note: str | None = Field(default=None, description="更新备注")


class RebuildBody(BaseModel):
    """POST /admin/vectors/rebuild 请求体（均可缺省）。"""

    manifest_path: str | None = Field(
        default=None,
        description=(
            "归一化清单路径（scripts/normalize_assets.py 产出）；缺省取 data/vectors/manifest.jsonl"
        ),
    )


class RuleActivePatch(BaseModel):
    """PATCH /admin/rules/{rule_id}/active 请求体。"""

    active: bool = Field(description="True=启用 / False=停用（停用后规则不参与消歧）")


class ModelDownloadBody(BaseModel):
    """POST /admin/models/download 请求体。"""

    model_name: str | None = Field(
        default=None, description="HF 模型名；缺省取当前配置的 embedding.local.model_name"
    )


class TestConnectionBody(BaseModel):
    """POST /admin/config/test-connection 请求体（各渠道冒烟，临时参数不落库）。"""

    channel: Literal["embedding", "llm", "fasttext"] = Field(description="待测试渠道")
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "可选临时参数覆盖（不落库；如 embedding cloud 的 base_url/model、"
            "llm 的 base_url/model）"
        ),
    )


class PasswordBody(BaseModel):
    """POST /admin/config/password 请求体（改密 C5）。"""

    current_password: str = Field(description="当前管理密码")
    new_password: str = Field(description="新管理密码（长度 ≥ 10）")


#: 试运行示例抽取：默认条数 / 单条长度上限 / 每池头部扫描上限（读文件头即可）
_EXAMPLE_COUNT = 20
_EXAMPLE_MAX_LEN = 200
_EXAMPLE_HEAD_LIMIT = 400

#: 云端 Embedding Key 兜底环境变量规范名（与 embedding.py 一致）
_EMBED_KEY_ENV_FALLBACK = "SAFEFUSION_EMBEDDING_API_KEY"


def _deep_merge_dict(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """递归深度合并两个字典（``update`` 优先；供渠道测试的临时参数覆盖）。"""

    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _strip_secret_keys_from_payload(node: Any) -> Any:
    """递归移除请求体中的 ``api_key`` 键（密钥红线：不参与临时参数合并/落库）。"""

    if not isinstance(node, dict):
        return node
    return {
        key: _strip_secret_keys_from_payload(value) if isinstance(value, dict) else value
        for key, value in node.items()
        if key != "api_key"
    }


def _err_text(exc: Exception) -> str:
    """脱敏的失败原因摘要（截断 120 字符，防异常消息携带密钥值）。"""

    message = str(exc).strip() or type(exc).__name__
    return message[:120]


def _channel_result(
    channel: str, ok: bool, message: str, detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    """渠道冒烟统一响应形态（前端内联结果卡契约）。"""

    return {"channel": channel, "ok": ok, "message": message, "detail": detail or {}}


def _sample_corpus(
    corpus_dir: Path,
    limit: int = _EXAMPLE_COUNT,
    max_len: int = _EXAMPLE_MAX_LEN,
) -> list[dict[str, str]]:
    """从黑白语料随机抽样去重文本（PRD v0.3.0 M2，对齐旧版 admin.js 示例抽取）。

    语料列结构：第 1 列为文本（``text,label,category,source``），跳过表头行；
    文本 ≤ ``max_len`` 字符、跨池去重（黑 / 白同文本取先读到的池）、带
    ``pool`` 标注；**只读文件头部**（每池至多 ``_EXAMPLE_HEAD_LIMIT`` 个有效
    候选）后随机抽样，不整载语料。文件缺失 / 读取异常 → 该池忽略（不报错）。

    Args:
        corpus_dir: ``{data_dir}/corpus`` 目录（含 black.csv / white.csv）。
        limit: 抽样条数上限。
        max_len: 单条文本长度上限（字符）。

    Returns:
        ``[{"text": ..., "pool": "black"|"white"}, ...]``；无任何语料时为空列表。
    """

    texts: dict[str, str] = {}
    for pool, filename in (("black", "black.csv"), ("white", "white.csv")):
        path = corpus_dir / filename
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
                reader = csv.reader(fh)
                next(reader, None)  # 表头行
                pool_count = 0
                for row in reader:
                    if not row or not row[0].strip():
                        continue
                    text = row[0].strip()
                    if len(text) > max_len:
                        continue
                    if text not in texts:
                        texts[text] = pool
                        pool_count += 1
                    if pool_count >= _EXAMPLE_HEAD_LIMIT:
                        break
        except (OSError, csv.Error):
            continue
    items = [{"text": text, "pool": pool} for text, pool in texts.items()]
    if len(items) > limit:
        return random.sample(items, limit)
    return items


def _fasttext_smoke_loadable(model_path: str, config_path: str) -> bool:
    """fasttext 轻量可加载性：文件存在 + config.json 可解析且含必需超参键。

    不装载完整模型（避免每次 /admin/models 全量加载）；真实冒烟由
    ``POST /admin/config/test-connection``（channel=fasttext）做。
    """

    required = ("nbuckets", "emb_dim", "classes", "class_to_idx", "violation_class")
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        return isinstance(payload, dict) and all(key in payload for key in required)
    except Exception:
        return False


async def _test_embedding_channel(
    ctx: AppContext, override: dict[str, Any] | None
) -> dict[str, Any]:
    """embedding 渠道冒烟：cloud 真编码「连接测试」（返回耗时+维度）；local 最小校验。

    - cloud：无 base_url / model / Key 时给出明确错误文案（不含密钥值）；
    - local：**不做全量模型加载**（避免触发下载），仅校验权重目录 / HF 缓存，
      提示实际装配经 POST /admin/models/load 验证。
    """

    cfg = ctx.config
    merged = _deep_merge_dict(
        cfg.embedding.model_dump(), _strip_secret_keys_from_payload(override or {})
    )
    backend_kind = str(merged.get("backend") or "local")
    if backend_kind == "cloud":
        cloud = merged.get("cloud") or {}
        base_url = str(cloud.get("base_url") or "").strip()
        model = str(cloud.get("model") or "").strip()
        if not base_url:
            return _channel_result(
                "embedding", False, "云端 Embedding 未配置 base_url（embedding.cloud.base_url）"
            )
        if not model:
            return _channel_result(
                "embedding", False, "云端 Embedding 未配置 model（embedding.cloud.model）"
            )
        env_name = str(cloud.get("api_key_env") or _EMBED_KEY_ENV_FALLBACK)
        key = os.environ.get(env_name) or os.environ.get(_EMBED_KEY_ENV_FALLBACK)
        if not key:
            return _channel_result(
                "embedding",
                False,
                f"未配置密钥（环境变量 {env_name}；密钥只允许来自环境变量）",
                {"base_url": base_url, "model": model},
            )
        try:
            backend = get_embedding_backend({"backend": "cloud", "cloud": cloud})
            t0 = time.monotonic()
            vectors = await run_in_threadpool(backend.encode_texts, ["连接测试"])
            cost_ms = round((time.monotonic() - t0) * 1000, 1)
            dim = int(vectors.shape[1]) if getattr(vectors, "ndim", 0) >= 2 else None
            closer = getattr(backend, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()
            return _channel_result(
                "embedding",
                True,
                "连接成功",
                {"duration_ms": cost_ms, "dimension": dim, "model": model, "base_url": base_url},
            )
        except Exception as exc:
            return _channel_result("embedding", False, f"连接失败：{_err_text(exc)}")
    # local：最小装载校验（不做全量模型加载）
    local = merged.get("local") or {}
    weights_path = local.get("weights_path")
    model_name = str(local.get("model_name") or DEFAULT_MODEL_NAME)
    cache_dir = resolve_hf_cache_dir(cfg.data_dir)
    if weights_path:
        weights_dir = Path(weights_path)
        if not weights_dir.is_dir():
            return _channel_result("embedding", False, f"本地权重目录不存在：{weights_path}")
        return _channel_result(
            "embedding",
            True,
            "本地权重目录已配置且存在：实际装配请经 POST /admin/models/load 验证"
            "（local 渠道不做全量模型加载，避免触发下载）",
            {"weights_path": weights_path, "note": "local 不做全量加载验证"},
        )
    probe = probe_hf_model(cache_dir, model_name)
    if probe["exists"] and probe["complete"]:
        return _channel_result(
            "embedding",
            True,
            "本地 Chinese-CLIP 权重已缓存（HF 缓存命中）：实际装配请经 "
            "POST /admin/models/load 验证（缓存命中装配秒级）",
            {"cache_dir": str(cache_dir), "size_bytes": probe["size_bytes"]},
        )
    return _channel_result(
        "embedding",
        False,
        f"本地 Chinese-CLIP 权重未缓存（{cache_dir}）；请先经 POST /admin/models/download "
        "下载。本地模型不做全量加载验证，实际装配见 /admin/models/load",
        {"cache_dir": str(cache_dir)},
    )


async def _test_llm_channel(ctx: AppContext, override: dict[str, Any] | None) -> dict[str, Any]:
    """llm 渠道冒烟：用当前/临时配置发最小请求，返回耗时 + 字符数；无 Key 明确报错。"""

    cfg = ctx.config
    merged = _deep_merge_dict(
        cfg.llm.model_dump(exclude={"api_key"}), _strip_secret_keys_from_payload(override or {})
    )
    env_name = str(merged.get("api_key_env") or "OPENAI_API_KEY")
    key = os.environ.get("SAFEFUSION_LLM_API_KEY") or os.environ.get(env_name)
    if not key:
        return _channel_result(
            "llm",
            False,
            f"未配置密钥（环境变量 {env_name}；密钥只允许来自环境变量）",
            {"api_key_env": env_name},
        )
    base_url = merged.get("base_url")
    model = merged.get("model")
    if not base_url or not model:
        return _channel_result("llm", False, "未配置 llm.base_url 或 llm.model")
    client = LLMClient(merged)
    if not client.available:
        return _channel_result("llm", False, "LLM 客户端配置不完整（不可用）")
    try:
        t0 = time.monotonic()
        verdict = await client.judge("连接测试", [], None)
        cost_ms = round((time.monotonic() - t0) * 1000, 1)
        if verdict is None:
            return _channel_result(
                "llm",
                False,
                "LLM 冒烟请求失败（服务不可达或未返回有效判定）",
                {"duration_ms": cost_ms, "model": model},
            )
        return _channel_result(
            "llm",
            True,
            "连接成功",
            {"duration_ms": cost_ms, "chars": len("连接测试"), "model": model},
        )
    except Exception as exc:
        return _channel_result("llm", False, f"连接失败：{_err_text(exc)}")


def _test_fasttext_channel(ctx: AppContext, override: dict[str, Any] | None) -> dict[str, Any]:
    """fasttext 渠道冒烟：文件存在 + LightTextModel 可加载（构造即冒烟，绝不抛）。"""

    cfg = ctx.config
    merged = _deep_merge_dict(
        {
            "model_path": cfg.light_model.model_path,
            "config_path": cfg.light_model.config_path,
        },
        _strip_secret_keys_from_payload(override or {}),
    )
    model_path = merged.get("model_path")
    config_path = merged.get("config_path")
    if not model_path or not config_path:
        return _channel_result(
            "fasttext", False, "未配置 fasttext 模型（light_model.model_path / config_path）"
        )
    if not Path(model_path).is_file():
        return _channel_result("fasttext", False, f"模型文件不存在：{model_path}")
    if not Path(config_path).is_file():
        return _channel_result("fasttext", False, f"配置文件不存在：{config_path}")
    try:
        t0 = time.monotonic()
        model = LightTextModel(model_path, config_path)
        cost_ms = round((time.monotonic() - t0) * 1000, 1)
        if model.disabled:
            return _channel_result(
                "fasttext",
                False,
                "模型不可加载（torch 缺失 / 权重或 config.json 非法，组件 disabled）",
                {"duration_ms": cost_ms},
            )
        prediction = model.predict("连接测试")
        return _channel_result(
            "fasttext",
            True,
            "模型加载与推理冒烟通过",
            {
                "duration_ms": cost_ms,
                "label": prediction.get("label") if prediction else None,
                "score": prediction.get("score") if prediction else None,
            },
        )
    except Exception as exc:
        return _channel_result("fasttext", False, f"加载失败：{_err_text(exc)}")


def _resolve_admin_token(config: Any, db: Any = None) -> str:
    """解析管理令牌：config（YAML）→ 环境变量 → DB settings（admin.token）→ 启动生成。

    优先级（对齐 M4 决策 B「默认 < YAML < DB < env」）：环境变量
    ``ADMIN_PASSWORD`` 覆盖 DB（env 只覆盖内存不写 DB，文档语义同改密端点）：
    1. config 携带的 ``admin_token``（或 config.admin.token）；
    2. 环境变量 ``ADMIN_PASSWORD``（内存最高优先，**不写 DB**）；
    3. settings 表 ``admin`` 分组 ``token`` 键（改密端点
       ``POST /admin/config/password`` 持久化；重启后且未设 env 时生效）；
    4. 三者皆缺时启动生成 ``secrets.token_urlsafe(16)`` 并以 WARNING 打印一次。
    **自动生成场景是唯一允许日志打印令牌的例外**：管理员启动后需要借此获知
    令牌；配置 / 环境变量 / DB 来源的令牌绝不写入日志。
    """

    if config is not None:
        token = getattr(config, "admin_token", None) or getattr(
            getattr(config, "admin", None), "token", None
        )
        if isinstance(token, str) and token:
            return token
    token = os.environ.get("ADMIN_PASSWORD")
    if token:
        return token
    if db is not None:
        try:
            row = db.get_setting("admin", "token")
            if row is not None:
                value = json.loads(str(row["value_json"]))
                if isinstance(value, str) and value:
                    return value
        except Exception:
            logger.warning("读取 settings 表 admin.token 失败（回退自动生成）")
    token = secrets.token_urlsafe(16)
    # 唯一允许打印令牌的场景：自动生成后管理员需借此获知令牌；仅此一次。
    logger.warning(
        "未配置 ADMIN_PASSWORD 环境变量，本次启动自动生成管理令牌（仅此一次输出）：%s", token
    )
    return token


def _storage_method(db: Any, name: str, description: str) -> Callable[..., Any]:
    """探测存储层方法，缺失时抛 501（不静默失败、不访问私有成员）。"""

    method = getattr(db, name, None)
    if method is None:
        raise HTTPException(
            status_code=501,
            detail=f"{description}暂不可用：存储层未提供 Database.{name}()，请联系集成方补全",
        )
    return method


def _mask_key(key: str) -> str:
    """Key 脱敏：仅显示前 8 位（明文只在创建成功时返回一次）。"""

    return key[:8] + "…"


def _resolve_key_ref(db: Any, ref: str) -> str:
    """Key 定位：支持完整明文或列表脱敏前缀（``key[:8]+…``）。

    精确匹配优先；未命中时清理尾部非字母数字字符（如 "…"）后做前缀
    唯一匹配。前缀命中多条返回 400（请提供完整 Key），零命中返回 404。
    供 PATCH/DELETE ``/keys/{key}`` 使用，解决「列表只回显前缀无法删改」
    的前端管理缺口（v0.3.0 T40 发现）。
    """

    if db.get_key(ref) is not None:
        return ref
    prefix = "".join(ch for ch in ref if ch.isalnum() or ch in ("_", "-"))
    matches = [str(row["key"]) for row in db.list_keys() if str(row["key"]).startswith(prefix)]
    if len(matches) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"Key 前缀 {_mask_key(prefix)} 不唯一（{len(matches)} 条），请提供完整 Key",
        )
    if not matches:
        raise HTTPException(status_code=404, detail=f"API Key 不存在: {_mask_key(ref)}")
    return matches[0]


def _backup_keywords_zip(data_dir: Path, db: Any) -> str:
    """词库一键去重前的自动备份：全量词条导出 CSV 压入 ``data/backups/keywords_dedup_<ts>.zip``。

    Returns:
        备份文件名（basename，含 .zip）；异常向上抛（调用方转 500 中止去重）。
    """

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"keywords_dedup_{ts}.zip"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(("category", "word", "source"))
    for row in db.list_keywords():
        writer.writerow((row.get("category") or "", row.get("word") or "", row.get("source") or ""))
    with zipfile.ZipFile(backups_dir / filename, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("keywords.csv", buf.getvalue().encode("utf-8"))
    return filename


def _parse_keywords_csv(text: str) -> list[tuple[str, str, str]]:
    """解析 CSV（类别,词 两列）：跳过空行与表头行，返回 (category, word, source) 三元组。"""

    items: list[tuple[str, str, str]] = []
    head = True
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        category, word = row[0].strip(), row[1].strip()
        if not category or not word:
            continue
        if head and category == "类别" and word == "词":
            head = False
            continue
        head = False
        items.append((category, word, "admin_import"))
    return items


def _parse_keywords_txt(text: str, category: str) -> list[tuple[str, str, str]]:
    """解析 TXT（每行一词）：跳过空行与 ``#`` 注释行，统一挂到指定类别下。"""

    items: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        items.append((category, word, "admin_import"))
    return items


#: 规则 CSV 表头行（中英文均可，逐行判定跳过）
_RULES_CSV_HEADERS: tuple[tuple[str, str], ...] = (
    ("category", "pattern"),
    ("类别", "规则"),
    ("类别", "pattern"),
)


def _parse_rules_csv(text: str) -> list[dict[str, Any]]:
    """解析规则 CSV（category,pattern,action 三列）。

    跳过空行 / 表头行 / 空 pattern 行；action 列缺省或为空时默认 ``exempt``；
    category 可为空（规则不限定类别，作用于全部命中）。

    Args:
        text: CSV 原文（UTF-8，可带 BOM）。

    Returns:
        规范化后的规则字典列表（category / pattern / action / note）。
    """

    items: list[dict[str, Any]] = []
    head = True
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        category, pattern = row[0].strip(), row[1].strip()
        if not pattern:
            continue
        if head and (category, pattern) in _RULES_CSV_HEADERS:
            head = False
            continue
        head = False
        action = row[2].strip() if len(row) > 2 else ""
        items.append(
            {
                "category": category,
                "pattern": pattern,
                "action": action or "exempt",
                "note": None,
            }
        )
    return items


def _normalize_rule_items(payload: list[Any]) -> list[dict[str, Any]]:
    """JSON 规则数组规范化：缺失 / 空 action 默认 ``exempt``，空 pattern 剔除。

    Args:
        payload: JSON 数组（元素为规则对象字典）。

    Returns:
        规范化后的规则字典列表（category / pattern / action / note）。

    Raises:
        HTTPException(400): 数组元素不是对象字典（API 误用，不静默跳过）。
    """

    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="JSON 规则数组元素必须是对象字典")
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        action = str(item.get("action") or "").strip() or "exempt"
        items.append(
            {
                "category": str(item.get("category") or "").strip(),
                "pattern": pattern,
                "action": action,
                "note": item.get("note"),
            }
        )
    return items


async def _run_reload_hook(hook: Callable[[], Any] | None) -> str:
    """调用注入的热重载钩子并归类结果：``ok`` / ``failed`` / ``skipped``（未注入）。

    同步钩子在线程池执行（避免阻塞事件循环），异步钩子直接 await；
    钩子抛异常或显式返回 False（热重载失败回退旧实例）时归类为 ``failed``，
    但端点本身仍正常返回（不因规则层刷新失败而回绝已落库的写操作）。

    Args:
        hook: 注入的热重载回调（通常为 ``AppContext.reload_rules``）。

    Returns:
        ``ok`` / ``failed`` / ``skipped``。
    """

    if hook is None:
        return "skipped"
    try:
        if inspect.iscoroutinefunction(hook):
            result = await hook()
        else:
            result = await run_in_threadpool(hook)
        if inspect.isawaitable(result):
            result = await result
        return "ok" if result is not False else "failed"
    except Exception as exc:
        logger.warning("规则热重载钩子执行失败（响应仍正常返回）: %r", exc)
        return "failed"


def _normalize_log(row: dict[str, Any]) -> dict[str, Any]:
    """审核记录行转 API 形态：has_violation 转 bool、detail_json 解析为 detail。"""

    out = dict(row)
    out["has_violation"] = bool(row.get("has_violation"))
    raw = out.pop("detail_json", None)
    if raw:
        try:
            out["detail"] = json.loads(raw)
        except (TypeError, ValueError):
            out["detail"] = None
    else:
        out["detail"] = None
    return out


def create_admin_app(
    db: Database,
    whitelist_matcher: WhitelistMatcher,
    rebuild_hook: Callable[[str], Any] | None = None,
    reload_hook: Callable[[], Any] | None = None,
    config: Any = None,
    reviewer: ReviewScheduler | None = None,
    container: Any = None,
) -> FastAPI:
    """创建管理 API 应用（FastAPI，部署时挂载于 :8001）。

    Args:
        db: SQLite DAO（``storage.database.Database`` 实例）。
        whitelist_matcher: 图片白名单匹配器（``WhitelistMatcher``，已在构造时注入 db）。
        rebuild_hook: 向量库重建回调，接收归一化清单路径字符串；可为同步或异步函数；
            未注入时 ``POST /admin/vectors/rebuild`` 返回 501。
        reload_hook: 词库/规则热重载回调（PRD v0.2 M4），通常注入
            ``AppContext.reload_rules``；可为同步或异步函数；未注入时规则写
            入端点正常返回但响应含 ``"reload": "skipped"``。
        config: 应用**基础配置**（``AppConfig``：内置默认 + YAML + 环境变量，
            **不含 DB settings**——DB 层由 ``db`` 提供，见
            ``config_override.effective_config``）；或提供 ``data_dir`` /
            ``admin_token`` 属性的鸭子类型；为 None 时按 ``load_config(None)``。
        reviewer: 定时复核调度器（PRD v0.2 M7，``core.review.ReviewScheduler``）；
            未注入时 ``POST /admin/review/run`` 与 ``GET /admin/review/status``
            返回 501；llm 分组热应用时经其 ``reload_llm`` 替换专用客户端。
        container: 共享 ``AppContext``（PRD v0.3.0 M4 热应用目标）；为 None 时
            配置端点只落库不热应用（响应 ``"applied": false`` 并注明）。

    Returns:
        已注册各端点与全局异常处理器（脱敏 JSON）的 FastAPI 实例；管理令牌
        容器暴露于 ``app.state.admin_token``（改密热切换入口）。
    """

    token = _resolve_admin_token(config, db)
    token_store = AdminToken(token)
    data_dir = Path(getattr(config, "data_dir", "./data"))
    whitelist_dir = data_dir / "whitelist"
    # 基配置私有深拷贝：热应用会**就地**修改容器持有的配置对象（保持编排器
    # 引用身份不变），绝不能把注入的 ``config`` 引用污染掉（否则来源标识 /
    # data_dir 等会随热应用失真）。
    if isinstance(config, AppConfig):
        config = config.model_copy(deep=True)

    app = FastAPI(
        title="SafeFusion 管理 API",
        version=__version__,
        description=(
            "PRD §4.2：Key / 词库 / 规则 / 白名单 / 日志 / 向量重建 / 配置（需 X-Admin-Token）"
        ),
    )
    # v0.3.0 M4：令牌热切换入口（改密端点 / 测试经 set 热应用，旧令牌立即失效）
    app.state.admin_token = token_store
    # v0.3.0 M3：管理侧健康端点的启动时间戳（与审核端 /health 的 uptime_s 同口径）
    app.state.startup_ts = time.monotonic()
    # v0.3.0 M6：模型下载任务注册表（进程内后台线程 + 同模型互斥）
    downloads = DownloadManager()
    router = APIRouter(
        prefix="/admin",
        dependencies=[Depends(require_admin_token(token_store))],
    )

    # ------------------------------------------------------------- keys CRUD
    @router.post("/keys", status_code=201)
    async def create_key(body: KeyCreate) -> dict[str, Any]:
        """生成一个 API Key：sf_ 前缀 + 随机熵；明文仅本次创建响应返回一次。"""

        key = "sf_" + secrets.token_urlsafe(24)
        try:
            db.create_key(key, tier=body.tier, enabled=True, note=body.note)
        except ValueError as exc:  # tier 非法 / 极罕见碰撞，均不静默
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"key": key, "tier": body.tier, "enabled": True, "note": body.note}

    @router.get("/keys")
    async def list_keys() -> list[dict[str, Any]]:
        """列出全部 API Key：key 字段脱敏（仅前 8 位），供核对不泄露明文。"""

        return [
            {
                "key": _mask_key(str(row["key"])),
                "tier": row["tier"],
                "enabled": bool(row["enabled"]),
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in db.list_keys()
        ]

    @router.patch("/keys/{key}")
    async def patch_key(key: str, body: KeyPatch) -> dict[str, Any]:
        """更新 API Key：enabled 启停（已支持）；note 备注更新需存储层
        ``Database.update_key_note(key, note)``，缺失时返回 501。
        key 支持完整明文或列表脱敏前缀（见 _resolve_key_ref）。"""

        resolved = _resolve_key_ref(db, key)
        if body.enabled is not None:
            db.set_key_enabled(resolved, body.enabled)
        if body.note is not None:
            updater = _storage_method(db, "update_key_note", "更新 Key 备注")
            if not updater(resolved, body.note):
                raise HTTPException(
                    status_code=404, detail=f"API Key 不存在: {_mask_key(resolved)}"
                )
        updated = db.get_key(resolved)
        return {
            **updated,
            "key": _mask_key(str(updated["key"])),
            "enabled": bool(updated["enabled"]),
        }

    @router.delete("/keys/{key}")
    async def delete_key(key: str) -> dict[str, Any]:
        """删除 API Key：需存储层 ``Database.delete_key(key)``，缺失时返回 501。
        key 支持完整明文或列表脱敏前缀（见 _resolve_key_ref）。"""

        resolved = _resolve_key_ref(db, key)
        deleter = _storage_method(db, "delete_key", "删除 API Key")
        if not deleter(resolved):
            raise HTTPException(status_code=404, detail=f"API Key 不存在: {_mask_key(resolved)}")
        return {"deleted": _mask_key(resolved)}

    # -------------------------------------------------------------- keywords
    @router.post("/keywords/import")
    async def import_keywords(
        file: Annotated[UploadFile, File(description="CSV（类别,词 两列）或 TXT（每行一词）")],
        category: Annotated[str | None, Query(description="TXT 导入必填的类别（每行一词）")] = None,
    ) -> dict[str, Any]:
        """批量导入词条：``.csv`` 按「类别,词」两列解析（首行表头自动跳过），
        其他扩展名按 TXT 解析（每行一词 + 必填 ``category`` 参数）；重复词条
        （category+word 唯一）跳过并计数，不静默覆盖。"""

        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="文件编码不支持：仅接受 UTF-8（可带 BOM）",
            ) from exc
        filename = (file.filename or "").lower()
        if filename.endswith(".csv"):
            items = _parse_keywords_csv(text)
        else:
            if not category:
                raise HTTPException(status_code=400, detail="TXT 导入必须提供 category 查询参数")
            items = _parse_keywords_txt(text, category)
        inserted, skipped = db.add_keywords(items)
        return {"inserted": inserted, "skipped": skipped, "total": inserted + skipped}

    @router.get("/keywords")
    async def list_keywords(
        category: Annotated[str | None, Query(description="按类别过滤，缺省返回全部")] = None,
        page: Annotated[Page, Depends(pagination)] = None,
    ) -> dict[str, Any]:
        """词库分页查询（可按类别过滤）：返回 total 与当前页词条（含 id / source）。"""

        all_rows = db.list_keywords(category)
        return {
            "total": len(all_rows),
            "page": page.page,
            "page_size": page.page_size,
            "items": all_rows[page.offset : page.offset + page.limit],
        }

    @router.delete("/keywords/{keyword_id}")
    async def delete_keyword(keyword_id: int) -> dict[str, Any]:
        """按主键删除单个词条；不存在返回 404。"""

        if not db.delete_keyword(keyword_id):
            raise HTTPException(status_code=404, detail=f"词条不存在: id={keyword_id}")
        return {"deleted": keyword_id}

    @router.post("/keywords/dedup")
    async def dedup_keywords() -> dict[str, Any]:
        """一键去重（PRD v0.3.0 G10）：先自动备份 zip 至 ``data/backups/``，
        再按 (category, word) 去重（保留最小 id），完成后触发关键词引擎热重载
        （container.reload_keywords；未注入容器 → reload=skipped）。

        响应契约（前端 T41 消费）：``{status, before, after, removed, failed,
        backup_file, reload}``（reload ∈ ok | skipped | failed）。
        """

        try:
            backup_file = _backup_keywords_zip(data_dir, db)
        except Exception as exc:  # noqa: BLE001 - 备份失败必须中止，防止无兜底去重
            raise HTTPException(status_code=500, detail=f"去重前备份失败，已中止：{exc}") from exc
        result = db.dedup_keywords()
        reload = "skipped"
        if container is not None:
            try:
                reload = "ok" if container.reload_keywords() else "failed"
            except Exception:  # noqa: BLE001 - 重载失败不影响去重结果
                reload = "failed"
        return {"status": "ok", **result, "failed": 0, "backup_file": backup_file, "reload": reload}

    # ------------------------------------------------------------- rules
    @router.get("/rules")
    async def list_rules(
        category: Annotated[
            str | None, Query(description="按类别过滤；缺省返回全部（含无类别规则）")
        ] = None,
        active_only: Annotated[
            bool, Query(description="仅返回启用规则；false 返回全部（含已停用）")
        ] = True,
    ) -> dict[str, Any]:
        """列出正则消歧规则（PRD v0.2 M4）：可按类别 / 启用状态过滤。"""

        rows = db.list_rules(category, active_only=active_only)
        return {
            "total": len(rows),
            "items": [{**row, "is_active": bool(row["is_active"])} for row in rows],
        }

    @router.post("/rules")
    async def add_rules(request: Request) -> dict[str, Any]:
        """批量新增正则消歧规则（PRD v0.2 M4）。

        支持两种请求体：JSON 数组 ``[{category, pattern, action, note}]``
        （action 缺省为 exempt）；multipart ``file`` 字段上传 CSV
        （``category,pattern,action`` 三列，action 空默认 exempt，表头行自动
        跳过）。重复规则（category+pattern+action 唯一）跳过并计数；写库成功后
        调用注入的 reload 钩子（未注入时响应含 ``"reload": "skipped"``）。
        """

        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="CSV 导入需提供 file 字段（multipart）")
            raw = await upload.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail="文件编码不支持：仅接受 UTF-8（可带 BOM）"
                ) from exc
            items = _parse_rules_csv(text)
        else:
            try:
                payload = json.loads(await request.body())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(
                    status_code=400, detail="请求体必须是 JSON 数组或 multipart CSV"
                ) from exc
            if not isinstance(payload, list):
                raise HTTPException(status_code=400, detail="JSON 请求体必须是规则对象数组")
            items = _normalize_rule_items(payload)
        try:
            inserted, skipped = db.add_rules(
                [
                    (item["category"], item["pattern"], item["action"], item["note"])
                    for item in items
                ]
            )
        except ValueError as exc:  # action 非法 / 无效正则，批量整体拒绝
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "inserted": inserted,
            "skipped": skipped,
            "total": inserted + skipped,
            "reload": await _run_reload_hook(reload_hook),
        }

    @router.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: int) -> dict[str, Any]:
        """按主键删除规则；不存在返回 404。删除成功后调用热重载钩子即时生效。"""

        if not db.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail=f"规则不存在: id={rule_id}")
        return {"deleted": rule_id, "reload": await _run_reload_hook(reload_hook)}

    @router.patch("/rules/{rule_id}/active")
    async def set_rule_active(rule_id: int, body: RuleActivePatch) -> dict[str, Any]:
        """启用 / 停用规则；不存在返回 404。状态变更后调用热重载钩子即时生效。"""

        if not db.set_rule_active(rule_id, body.active):
            raise HTTPException(status_code=404, detail=f"规则不存在: id={rule_id}")
        return {
            "id": rule_id,
            "active": body.active,
            "reload": await _run_reload_hook(reload_hook),
        }

    # ------------------------------------------------------------- review
    @router.post("/review/run")
    async def run_review() -> dict[str, Any]:
        """手动触发一轮定时复核（PRD v0.2 M7）。

        - 未注入 reviewer（集成方未装配调度器）→ 501；
        - 已有复核在执行（自动轮次或并发触发）→ 202 Accepted（本次触发忽略，
          可轮询 ``/admin/review/status`` 获取最新报告）；
        - 正常执行 → 200 + 报告摘要（含 ``skipped_reason``，如
          ``llm_unavailable`` / ``text_unavailable`` / ``no_samples``）。
        """

        if reviewer is None:
            raise HTTPException(
                status_code=501,
                detail="定时复核暂不可用：未注入 reviewer（缺少数据库或 LLM 组件）",
            )
        report = await reviewer.trigger()
        if report is None:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "running",
                    "message": "复核已在执行中，本次手动触发已忽略",
                    "status_detail": reviewer.status(),
                },
            )
        return {"status": "ok", "summary": report.as_dict()}

    @router.get("/review/status")
    async def review_status() -> dict[str, Any]:
        """查询定时复核状态（PRD v0.2 M7）：启用 / 间隔 / 运行中 / 上次运行时间 /
        最近报告 / 报告目录。未注入 reviewer 时返回 501。"""

        if reviewer is None:
            raise HTTPException(
                status_code=501,
                detail="定时复核暂不可用：未注入 reviewer（缺少数据库或 LLM 组件）",
            )
        return reviewer.status()

    # ------------------------------------------------------ whitelist images
    @router.post("/whitelist/images")
    async def upload_whitelist_images(
        files: Annotated[list[UploadFile], File(description="多张白名单图片（PNG/JPEG/GIF 等）")],
    ) -> dict[str, Any]:
        """批量上传白名单图片：逐个计算 md5 + pHash，原图存 ``data/whitelist/{md5}.png``，
        元数据入库（md5 唯一，重复上传幂等返回既有 id）；单张解码失败记录错误不中断整批。"""

        results: list[dict[str, Any]] = []
        whitelist_dir.mkdir(parents=True, exist_ok=True)
        for upload in files:
            data = await upload.read()
            if not data:
                results.append({"file": upload.filename, "error": "空文件"})
                continue
            try:
                b64_text = base64.b64encode(data).decode("ascii")
                img = (await decode_images([ImageInput(base64=b64_text)]))[0]
                md5_hex, phash = compute_hashes(img)
                png_buf = io.BytesIO()
                img.save(png_buf, format="PNG")  # 与 compute_hashes 的 md5 口径一致
                target = whitelist_dir / f"{md5_hex}{_WHITELIST_EXT}"
                target.write_bytes(png_buf.getvalue())
                entry_id = db.add_whitelist(md5_hex, str(phash), note=upload.filename)
                results.append(
                    {
                        "id": entry_id,
                        "md5": md5_hex,
                        "phash_hex": str(phash),
                        "note": upload.filename,
                        "file": str(target),
                    }
                )
            except ValueError as exc:
                results.append({"file": upload.filename, "error": f"图片解码失败：{exc}"})
        uploaded = sum(1 for item in results if "id" in item)
        return {"uploaded": uploaded, "failed": len(results) - uploaded, "items": results}

    @router.get("/whitelist/images")
    async def list_whitelist_images(
        page: Annotated[Page, Depends(pagination)] = None,
    ) -> dict[str, Any]:
        """白名单图片元数据分页查询（id / md5 / phash_hex / note / created_at）。"""

        all_rows = db.list_whitelist()
        return {
            "total": len(all_rows),
            "page": page.page,
            "page_size": page.page_size,
            "items": db.list_whitelist(limit=page.limit, offset=page.offset),
        }

    @router.delete("/whitelist/images/{entry_id}")
    async def delete_whitelist_image(entry_id: int) -> dict[str, Any]:
        """删除白名单条目：删 DB 记录 + 尽力而为删除磁盘原图（{md5}.png，缺失忽略）。"""

        rows = db.list_whitelist()
        row = next((r for r in rows if r["id"] == entry_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail=f"白名单条目不存在: id={entry_id}")
        db.delete_whitelist(entry_id)
        target = whitelist_dir / f"{row['md5']}{_WHITELIST_EXT}"
        file_deleted = target.exists()
        with contextlib.suppress(OSError):
            target.unlink()
        return {"deleted": entry_id, "file_deleted": file_deleted, "file": str(target)}

    # ------------------------------------------------------------ audit logs
    @router.get("/logs")
    async def query_logs(
        start: Annotated[str | None, Query(description=_Q_START_T)] = None,
        end: Annotated[str | None, Query(description=_Q_END_T)] = None,
        has_violation: Annotated[bool | None, Query(description=_Q_HAS_VIOLATION_T)] = None,
        source: Annotated[str | None, Query(description=_Q_SOURCE_T)] = None,
        category: Annotated[str | None, Query(description=_Q_CATEGORY_T)] = None,
        key_tier: Annotated[str | None, Query(description=_Q_KEY_TIER_T)] = None,
        page: Annotated[Page, Depends(pagination)] = None,
    ) -> dict[str, Any]:
        """审核记录分页查询（时间倒序），支持时间 / 结论 / 来源 / 类别 / Key 分组过滤。"""

        filters = {
            "start_ts": start,
            "end_ts": end,
            "has_violation": has_violation,
            "source": source,
            "category": category,
            "key_tier": key_tier,
        }
        total = db.count_logs(**filters)
        rows = db.query_logs(limit=page.limit, offset=page.offset, **filters)
        return {
            "total": total,
            "page": page.page,
            "page_size": page.page_size,
            "items": [_normalize_log(r) for r in rows],
        }

    @router.get("/logs/export")
    async def export_logs(
        start: Annotated[str | None, Query(description=_Q_START_T)] = None,
        end: Annotated[str | None, Query(description=_Q_END_T)] = None,
        has_violation: Annotated[bool | None, Query(description=_Q_HAS_VIOLATION_T)] = None,
        source: Annotated[str | None, Query(description=_Q_SOURCE_T)] = None,
        category: Annotated[str | None, Query(description=_Q_CATEGORY_T)] = None,
        key_tier: Annotated[str | None, Query(description=_Q_KEY_TIER_T)] = None,
    ) -> StreamingResponse:
        """导出审核记录为 CSV（StreamingResponse，带 utf-8-sig BOM 兼容 Excel）；
        过滤条件与 ``/admin/logs`` 一致，按页（每页 1000 条）流式拉取。"""

        filters = {
            "start_ts": start,
            "end_ts": end,
            "has_violation": has_violation,
            "source": source,
            "category": category,
            "key_tier": key_tier,
        }

        def _generate() -> Any:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(_EXPORT_HEADERS)
            yield "\ufeff" + buffer.getvalue()  # BOM 只出现一次
            buffer.seek(0)
            buffer.truncate(0)
            offset = 0
            while True:
                rows = db.query_logs(limit=1000, offset=offset, **filters)
                for row in rows:
                    writer.writerow(
                        [
                            row["request_id"],
                            row["ts"],
                            row.get("text_hash") or "",
                            str(bool(row["has_violation"])).lower(),
                            row.get("confidence") if row.get("confidence") is not None else "",
                            row.get("category") or "",
                            row["source"],
                            row.get("key_tier") or "",
                            row.get("detail_json") or "",
                        ]
                    )
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate(0)
                if len(rows) < 1000:
                    break
                offset += len(rows)

        return StreamingResponse(
            _generate(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit_logs.csv"'},
        )

    # ----------------------------------------------------- vectors/rebuild
    @router.post("/vectors/rebuild")
    async def rebuild_vectors(body: RebuildBody | None = None) -> dict[str, Any]:
        """向量库重建 / 增量导入：调用注入的 ``rebuild_hook(manifest_path)``。

        manifest（归一化清单）由 ``scripts/normalize_assets.py`` 产出
        （PRD §5 统一布局 ``data/vectors/``）；未注入 rebuild_hook 时返回 501。
        同步钩子在线程池执行，异步钩子直接 await。
        """

        if rebuild_hook is None:
            raise HTTPException(
                status_code=501,
                detail="向量库重建暂不可用：未注入 rebuild_hook",
            )
        manifest = (
            body.manifest_path
            if body is not None and body.manifest_path
            else str(data_dir / _DEFAULT_MANIFEST)
        )
        try:
            if inspect.iscoroutinefunction(rebuild_hook):
                result = await rebuild_hook(manifest)
            else:
                result = await run_in_threadpool(rebuild_hook, manifest)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # 重建失败：记录日志、响应脱敏（不回栈）
            logger.exception("向量库重建失败: manifest=%s", manifest)
            raise HTTPException(status_code=500, detail=f"向量库重建失败：{exc}") from exc
        return {"status": "ok", "manifest": manifest, "result": result}

    # -------------------------------------------- config 读写（v0.3.0 M4：DB 化 + 全量热应用）
    def _config_base() -> AppConfig:
        """基配置：内置默认 + YAML + 环境变量（**不含 DB settings**，DB 层由 db 提供）。

        融合 YAML / 环境变量已在 ``load_config`` 完成；DB settings 在
        :func:`_effective_cfg` 之上合并（默认 < YAML < DB < 环境变量）。
        """

        return config if isinstance(config, AppConfig) else load_config(None)

    def _effective_cfg() -> AppConfig:
        """当前有效配置：基配置 + DB settings（环境变量保持最高优先、不反向写回）。"""

        return effective_config(_config_base(), db=db)

    def _group_response(cfg: AppConfig, group: str) -> dict[str, Any]:
        """单个分组的响应形态：序列化 + Key 遮蔽（不返回密钥值）。"""

        return mask_secret_fields(group, group_to_dict(cfg)[group], cfg)

    def _group_sources(group: str) -> dict[str, str]:
        """该组叶子字段级来源映射（契约见 ``config_override.config_sources``）。"""

        return config_sources(_config_base(), db=db)[group]

    def _restore_group_rows(group: str, rows: list[dict[str, Any]]) -> None:
        """热应用失败后按旧行快照恢复该组 DB（原样 upsert 或全删）。"""

        old_rows = [row for row in rows if row["group"] == group]
        if not old_rows:
            db.delete_settings(group)
            return
        values: dict[str, Any] = {}
        for row in old_rows:
            try:
                values[str(row["key"])] = json.loads(str(row["value_json"]))
            except (TypeError, ValueError):
                continue
        if values:
            db.set_settings(group, values)
        else:
            db.delete_settings(group)

    def _brief_exc(exc: Exception) -> str:
        """脱敏的失败原因摘要（截断 120 字符，防异常消息携带密钥值）。"""

        message = str(exc).strip() or type(exc).__name__
        return message[:120]

    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        """返回全量有效配置（按分组）。

        由「内置默认 < config.yaml < DB settings < 环境变量」合并而成；密钥类
        字段（``llm.api_key`` / ``embedding.cloud.api_key`` 等）只返回
        ``{"api_key_env": <变量名或 null>, "configured": <bool>}``，绝不返回值；
        语义组 ``fuse_mode`` 为真实叶子一并返回。**字段级来源**见
        ``GET /admin/config/sources``（各分组旁路元数据，不混入本响应）。
        """

        cfg = _effective_cfg()
        groups = group_to_dict(cfg)
        return {name: mask_secret_fields(name, value, cfg) for name, value in groups.items()}

    @router.get("/config/sources")
    async def get_config_sources() -> dict[str, dict[str, str]]:
        """返回每个分组内叶子字段的来源映射（PRD v0.3.0 M4 决策 B）。

        **T39 前端契约**：::

            {
              "thresholds": {"semantic_threshold": "db", "margin_w": "default", ...},
              "llm":        {"base_url": "yaml", "api_key": "env", ...},
              ...
            }

        - 顶层键 = 配置分组名（``get_config_groups()`` 白名单）；
        - 组内键 = **叶子字段点分路径**（嵌套子模型用点连接，
          如 ``embedding.cloud.base_url``）；
        - 值 = ``effective_source`` ∈ default / yaml / db / env——「当前生效值
          来自哪一层」：env（被 ``SAFEFUSION_*`` 环境变量钉住，只覆盖内存
          不写 DB）→ db（settings 表行）→ yaml（config.yaml 使叶子偏离内置
          默认）→ default；密钥叶子只可能为 env 或 default。

        前端据此渲染每字段来源徽标；本响应不混入配置值（避免重复携带）。
        """

        return config_sources(_config_base(), db=db)

    @router.put("/config/{group}")
    async def update_config(group: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新单个配置分组：写 DB settings 并**立即热应用**（不重启）。

        - 请求体为该分组的 JSON 对象（允许部分键，未给出的键沿用当前有效值）；
        - 空对象 ``{}`` 表示删除该分组 DB settings（恢复默认并热应用回退，
          T25「恢复默认」按钮语义）；
        - 流程：校验（失败 422 中文可读）→ 重建类**试建造**（失败 500、
          DB 不写、旧实例继续生效）→ 落库 → 锁内原子替换 / 参数类叶子同步
          （失败回滚 DB 旧组值并 500）；
        - 成功返回更新后的分组（同样遮蔽 Key）+ 字段级 ``sources`` +
          ``"applied": true``（含 ``apply_scope``：runtime 即时生效 /
          config 仅配置叶子——server/logging 绑定类变更下次启动生效）；
          未注入共享容器时 ``"applied": false``（仅落库，注明原因，重启后生效）。
        """

        if group not in get_config_groups():
            raise HTTPException(
                status_code=422,
                detail=f"未知配置分组: {group}，可选分组: {', '.join(get_config_groups())}",
            )
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="请求体必须是分组配置对象（JSON 对象字典）")
        is_delete = payload == {}
        if not is_delete:
            try:
                validate_group_update(group, payload, _config_base(), db)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        # 候选有效配置：当前 DB 行叠加本次负载（删除 = 移除该组）；env 仍最高优先
        rows = db.list_settings()
        candidate = merge_overrides(
            _config_base(),
            candidate_overrides(rows, group, None if is_delete else payload),
        )

        # ① 重建类试建造（锁外，失败 → 500 且不落库，旧实例继续生效）
        staged: hot_apply.StagedSwap | None = None
        if container is not None and group in hot_apply.REBUILD_GROUPS:
            try:
                staged = await run_in_threadpool(hot_apply.stage_apply, container, group, candidate)
            except Exception as exc:
                logger.exception("配置试建造失败: group=%s", group)
                raise HTTPException(
                    status_code=500,
                    detail="配置应用失败（已回滚，未落库，旧配置继续生效）: " + _brief_exc(exc),
                ) from exc

        # ② 落库（部分键覆盖语义；空对象删除整组）
        try:
            if is_delete:
                db.delete_settings(group)
            else:
                db.set_settings(group, flatten_group(payload))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"配置保存失败: {exc}") from exc

        # ③ 热应用（锁内原子替换 / 参数类叶子同步；失败回滚 DB 旧组值）
        scope = "none"
        if container is not None:
            try:
                scope = await run_in_threadpool(
                    hot_apply.apply_hot, container, group, candidate, staged, reviewer
                )
            except Exception as exc:
                logger.exception("配置热应用失败: group=%s（回滚 DB 旧组值）", group)
                try:
                    _restore_group_rows(group, rows)
                except Exception:
                    logger.exception("热应用回滚后 DB 恢复失败（重启后以 DB 现状为准）")
                raise HTTPException(
                    status_code=500,
                    detail="配置应用失败（已回滚，旧配置继续生效）: " + _brief_exc(exc),
                ) from exc

        return {
            "group": group,
            "config": _group_response(_effective_cfg(), group),
            "saved": True,
            "applied": container is not None,
            "apply_scope": scope,
            "sources": _group_sources(group),
            "deleted_db_group": is_delete,
        }

    # ------------------------------------------ 运行时聚合（PRD v0.3.0 M2/M3/M5/M6，追加端点区）
    def _require_container() -> AppContext:
        """共享容器守卫：未注入容器时新端返回 503（不静默降级）。"""

        if container is None:
            raise HTTPException(
                status_code=503,
                detail="未注入共享 AppContext（集成方未装配容器），无法提供运行时状态",
            )
        return container

    @router.get("/models")
    async def list_models() -> dict[str, Any]:
        """模型清单（PRD v0.3.0 M6）：chinese-clip / fasttext / 向量库 / 语义引擎。

        - chinese-clip：backend 与状态（未配置 / 未下载 / 下载中 / 已就绪 /
          error / cloud）+ HF 缓存路径 / blobs 数 / 缓存字节；
        - fasttext：配置状态 + 文件存在性 + 轻量可加载性（config.json 冒烟）；
        - vector_store：黑白池条数与维度；
        - semantic：装配状态与降级原因码（lazy_pending / embedding_error 等）。
        """

        ctx = _require_container()
        cfg = ctx.config
        emb_cfg = cfg.embedding
        cache_dir = resolve_hf_cache_dir(cfg.data_dir)
        emb_status = ctx.embedding_status()
        clip: dict[str, Any] = {
            "backend": emb_cfg.backend,
            "model_name": emb_cfg.local.model_name,
            "weights_path": emb_cfg.local.weights_path,
            "cache_dir": str(cache_dir),
            "loaded": ctx.embedding is not None,
            "load_status": emb_status["status"],
            "load_reason": emb_status["reason"],
            "cached_files": 0,
            "cache_size_bytes": 0,
            "cache_partial": False,
        }
        if emb_cfg.backend == "cloud":
            clip["status"] = "cloud"
            clip["message"] = "云端 Embedding 后端：装配/使用由 /admin/config/test-connection 冒烟"
        elif emb_cfg.local.weights_path:
            weights_dir = Path(emb_cfg.local.weights_path)
            clip["cached_files"] = None
            clip["cache_size_bytes"] = None
            if ctx.embedding is not None:
                clip["status"] = "ready"
            elif weights_dir.is_dir():
                clip["status"] = "ready"
                clip["message"] = "本地权重目录已存在（weights_path），可经 /admin/models/load 装配"
            else:
                clip["status"] = "error"
                clip["message"] = f"本地权重目录不存在：{emb_cfg.local.weights_path}"
        else:
            probe = probe_hf_model(cache_dir, emb_cfg.local.model_name)
            clip["cached_files"] = probe["blobs"]
            clip["cache_size_bytes"] = probe["size_bytes"]
            clip["cache_partial"] = probe["exists"] and not probe["complete"]
            running = downloads.running_for(emb_cfg.local.model_name)
            if ctx.embedding is not None:
                clip["status"] = "ready"
            elif running is not None:
                clip["status"] = "downloading"
            elif probe["exists"] and probe["complete"]:
                clip["status"] = "ready"
                clip["message"] = "HF 权重已缓存，可经 /admin/models/load 装配"
            else:
                clip["status"] = "not_downloaded"
                clip["message"] = (
                    "权重未下载（或有未完成的部分缓存），请先 POST /admin/models/download"
                )

        lm_cfg = cfg.light_model
        ft_configured = bool(lm_cfg.model_path and lm_cfg.config_path)
        ft_model_exists = bool(lm_cfg.model_path and Path(lm_cfg.model_path).is_file())
        ft_config_exists = bool(lm_cfg.config_path and Path(lm_cfg.config_path).is_file())
        ft_loadable = bool(
            ft_configured
            and ft_model_exists
            and ft_config_exists
            and _fasttext_smoke_loadable(lm_cfg.model_path, lm_cfg.config_path)
        )
        fasttext: dict[str, Any] = {
            "configured": ft_configured,
            "model_path": lm_cfg.model_path,
            "config_path": lm_cfg.config_path,
            "model_file_exists": ft_model_exists,
            "config_file_exists": ft_config_exists,
            "loadable": ft_loadable,
            "status": (
                "ready"
                if ft_loadable
                else "error"
                if (ft_model_exists and ft_config_exists)
                else "missing"
                if ft_configured
                else "not_configured"
            ),
        }

        store = ctx.store

        def _pool_stat(pool: str) -> dict[str, Any]:
            if store is None:
                return {"count": 0, "dim": None}
            count = store.count(pool)
            dim = None
            dimmer = getattr(store, "dim", None)
            if callable(dimmer):
                with contextlib.suppress(Exception):
                    dim = dimmer(pool)
            return {"count": count, "dim": dim}

        return {
            "hf_cache_dir": str(cache_dir),
            "chinese_clip": clip,
            "fasttext": fasttext,
            "vector_store": {
                "black": _pool_stat("black"),
                "white": _pool_stat("white"),
            },
            "semantic": {
                "ready": ctx.semantic is not None,
                "status": emb_status["status"],
                "reason": ctx.semantic_degraded_reason(),
                "backend": emb_status["backend"],
            },
        }

    @router.post("/models/download", status_code=202)
    async def start_model_download(body: ModelDownloadBody | None = None) -> dict[str, Any]:
        """后台下载 Chinese-CLIP 权重（PRD v0.3.0 M6 D2）。

        - 下载目的缓存目录 = ``resolve_hf_cache_dir(data_dir)``（HF_HOME 环境
          变量优先，缺省 ``data/models/hf``）；
        - 同模型并发下载**互斥**：进行中任务被复用（``reused=true`` 返回既有
          task_id，不重复起线程）；
        - 返回 ``task_id``，进度经 ``GET /admin/models/download/{task_id}`` 轮询。
        """

        ctx = _require_container()
        cfg = ctx.config
        model_name = (
            body.model_name
            if body is not None and body.model_name
            else cfg.embedding.local.model_name
        )
        if not model_name:
            raise HTTPException(
                status_code=422,
                detail="无法确定下载模型：请提供 model_name 或配置 embedding.local.model_name",
            )
        cache_dir = resolve_hf_cache_dir(cfg.data_dir)
        task, reused = await run_in_threadpool(downloads.start, model_name, str(cache_dir))
        return {
            "task_id": task.task_id,
            "model_name": model_name,
            "status": task.status,
            "reused": reused,
            "cache_dir": str(cache_dir),
            "message": (
                "复用进行中的下载任务（同模型互斥）"
                if reused
                else "下载任务已启动（后台执行，可轮询进度）"
            ),
        }

    @router.get("/models/download/{task_id}")
    async def get_model_download(task_id: str) -> dict[str, Any]:
        """查询模型下载任务进度（运行中 / 完成 / 失败：阶段 / 百分比 / 已下载字节）。"""

        task = downloads.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"下载任务不存在: {task_id}")
        return task.snapshot()

    @router.post("/models/load")
    async def load_models() -> dict[str, Any]:
        """显式装配语义层（PRD v0.3.0 M6 D1）。

        同步完成（线程池内等待）：本机缓存命中时秒级；权重缺失（local_files_only）
        快速失败返回原因码。装配成功后 embedding / semantic 退出 degraded
        （原因码 lazy_pending → 清除），旧必在 /admin/models /admin/health 可见。
        """

        ctx = _require_container()
        result = await run_in_threadpool(ctx.load_semantic, 300.0)
        return {
            "status": result["status"],
            "reason": result["reason"],
            "message": result["message"],
            "semantic_ready": result["semantic_ready"],
            "duration_s": result.get("duration_s"),
            "summary": ctx.embedding_status(),
        }

    @router.get("/health")
    async def admin_health(request: Request) -> dict[str, Any]:
        """管理侧健康聚合（PRD v0.3.0 M3）：组件就绪清单 + 降级原因码 + 数据概况 + 缓存统计。

        ``degraded`` 与 :8000 ``GET /health`` 同口径（组件名清单）；字段级
        ``components`` 展开各组件就绪状态与降级原因码（含懒装配 lazy_pending），
        ``data`` 为数据概况，``cache`` 复用 :class:`CacheLayer.stats`。
        """

        ctx = _require_container()
        cfg = ctx.config
        db = ctx.database
        store = ctx.store
        light_ready = ctx.light_model is not None and not ctx.light_model.disabled
        llm_ready = ctx.llm is not None and ctx.llm.available
        keywords_n = len(db.list_keywords()) if db is not None else 0
        rules_n = len(db.list_rules(active_only=False)) if db is not None else 0
        whitelist_n = len(db.list_whitelist()) if db is not None else 0
        emb_status = ctx.embedding_status()

        def _pool_stat(pool: str) -> dict[str, Any]:
            if store is None:
                return {"count": 0, "dim": None}
            count = store.count(pool)
            dim = None
            dimmer = getattr(store, "dim", None)
            if callable(dimmer):
                with contextlib.suppress(Exception):
                    dim = dimmer(pool)
            return {"count": count, "dim": dim}

        black = _pool_stat("black")
        white = _pool_stat("white")
        components: dict[str, Any] = {
            "light_model": {
                "ready": light_ready,
                "reason": None if light_ready else "light_model_disabled",
                "model_path": cfg.light_model.model_path,
                "config_path": cfg.light_model.config_path,
            },
            "embedding": {
                "ready": ctx.embedding is not None,
                "backend": emb_status["backend"],
                "status": emb_status["status"],
                "reason": emb_status["reason"],
            },
            "semantic": {
                "ready": ctx.semantic is not None,
                "status": emb_status["status"],
                "reason": ctx.semantic_degraded_reason(),
            },
            "llm": {
                "ready": llm_ready,
                "reason": None if llm_ready else "llm_unavailable",
                "base_url": cfg.llm.base_url,
                "model": cfg.llm.model,
            },
            "keyword_engine": {
                "ready": ctx.keyword_engine is not None,
                "keywords": keywords_n,
                "reason": None if ctx.keyword_engine is not None else "keyword_engine_unavailable",
            },
            "rules": {
                "ready": bool(cfg.keyword.regex_rules_enabled),
                "enabled": bool(cfg.keyword.regex_rules_enabled),
                "count": rules_n,
                "reason": None if cfg.keyword.regex_rules_enabled else "regex_rules_disabled",
            },
            "vector_black": {
                "ready": black["count"] > 0,
                "count": black["count"],
                "dim": black["dim"],
            },
            "vector_white": {
                "ready": white["count"] > 0,
                "count": white["count"],
                "dim": white["dim"],
            },
        }
        return {
            "status": "ok",
            "version": __version__,
            "components": components,
            "degraded": list(ctx.degraded),
            "data": {
                "keywords": keywords_n,
                "vector_black": black["count"],
                "vector_white": white["count"],
                "whitelist_images": whitelist_n,
                "rules": rules_n,
            },
            "cache": ctx.cache_layer.stats() if ctx.cache_layer is not None else None,
            "uptime_s": round(time.monotonic() - request.app.state.startup_ts, 3),
        }

    @router.get("/test-examples")
    async def test_examples() -> dict[str, Any]:
        """随机抽取黑白语料示例（PRD v0.3.0 M2）：≤200 字符去重文本 + pool 标注。

        只读 ``{data_dir}/corpus/{black,white}.csv`` 文件头部（每池至多 400 个
        有效候选）后随机抽 20 条；文件缺失 / 语料为空 → 空列表（不报错）。
        """

        ctx = _require_container()
        items = _sample_corpus(Path(ctx.config.data_dir) / "corpus")
        return {"items": items, "total": len(items)}

    @router.post("/test-audit")
    async def test_audit(body: AuditRequest) -> dict[str, Any]:
        """管理端试运行审核（PRD v0.3.0 M2）：与 ``POST /v1/audit`` 契约一致。

        差异仅是鉴权与 detail 完整性：走管理令牌（X-Admin-Token），编排器以
        ``tier=full`` 执行 → 返回**完整 detail**（关键词 / 正则 / 语义 Top5 /
        白名单 / LLM / 黑白三值等），供前端证据面板展示；同样复用缓存与审计
        日志写入（与审核 API 同管线）。
        """

        ctx = _require_container()
        orchestrator = AuditOrchestrator(ctx)
        result = await orchestrator.process_audit(body, "full")
        return result.model_dump()

    @router.post("/config/test-connection")
    async def test_connection(body: TestConnectionBody) -> dict[str, Any]:
        """配置渠道冒烟（PRD v0.3.0 M5）：embedding / llm / fasttext。

        ``config`` 为可选临时参数（如 embedding cloud 的 base_url/model），
        仅本次冒烟生效、不落库；全部失败不崩，错误信息面向用户可读
        （密钥值绝不返回 / 记录）。
        """

        ctx = _require_container()
        if body.channel == "embedding":
            return await _test_embedding_channel(ctx, body.config)
        if body.channel == "llm":
            return await _test_llm_channel(ctx, body.config)
        return _test_fasttext_channel(ctx, body.config)

    @router.post("/config/password")
    async def change_password(body: PasswordBody, request: Request) -> dict[str, Any]:
        """修改管理密码（PRD v0.3.0 C5 / M4 C，T30A 遗留）。

        - 校验 current == 当前令牌（hmac 常数时间比较）、新密码长度 ≥ 10、
          且不同于当前密码；
        - 通过后更新 ``app.state.admin_token``（``AdminToken.set``，旧令牌
          **立即失效**）并持久化到 settings 表 ``admin.token``（DB 层生效；
          重启后仍有效；环境变量 ``ADMIN_PASSWORD`` 若已设置则在下次启动时
          保持最高优先——env 只覆盖内存不写 DB，文档语义同 M4 决策 B）。
        """

        store = request.app.state.admin_token
        if not isinstance(store, AdminToken):
            raise HTTPException(status_code=500, detail="管理令牌容器缺失（app.state.admin_token）")
        if not hmac.compare_digest(body.current_password, store.value):
            raise HTTPException(status_code=400, detail="当前密码不正确")
        if len(body.new_password) < 10:
            raise HTTPException(status_code=400, detail="新密码长度必须 ≥ 10 位")
        if hmac.compare_digest(body.new_password, store.value):
            raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
        try:
            db.set_settings("admin", {"token": body.new_password})
        except Exception as exc:
            detail = f"新密码保存失败：{_err_text(exc)}"
            raise HTTPException(status_code=500, detail=detail) from exc
        store.set(body.new_password)  # 旧令牌立即失效
        return {
            "ok": True,
            "message": "管理密码已更新：旧令牌立即失效；已持久化到 settings 表 admin.token"
            "（若设置了 ADMIN_PASSWORD 环境变量，重启后以环境变量为准）",
            "persisted": True,
        }

    # ------------------------------------------ 全局异常处理（脱敏 JSON）
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """HTTPException → 统一 JSON（error 字段；detail 非字符串时脱敏为通用文案）。"""

        detail = exc.detail if isinstance(exc.detail, str) else "请求被拒绝"
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求参数校验失败 → 422 脱敏 JSON（不回字段细节，防内部结构泄露）。"""

        return JSONResponse(status_code=422, content={"error": "请求参数校验失败"})

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """未预期异常 → 500 脱敏 JSON：完整堆栈只进日志（logger.exception），不回响应。"""

        logger.exception("管理 API 内部异常: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"error": "服务器内部错误"})

    app.include_router(router)
    return app
