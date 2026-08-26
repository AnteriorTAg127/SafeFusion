"""管理 API（:8001，PRD §4.2）：Key / 词库 / 图片白名单 / 审核日志 / 向量重建。

设计要点：
- 认证：全部 ``/admin/*`` 经路由器级依赖要求 ``X-Admin-Token`` 头与启动时解析的
  管理令牌一致（``dependencies.require_admin_token``，常数时间比较）。
- 令牌解析顺序：config 携带的 ``admin_token``（或 config.admin.token）→ 环境变量
  ``ADMIN_PASSWORD`` → 两者皆缺时启动生成 ``secrets.token_urlsafe(16)`` 并以
  WARNING 打印一次。**自动生成场景是唯一允许日志打印令牌的例外**：管理员启动后
  需要借此获知令牌；配置/环境变量来源的令牌绝不写入日志（红线：密钥不落盘）。
- 存储解耦：只依赖 T2 ``Database`` 已实现的方法；keys 的删除与备注更新所需
  ``Database.delete_key`` / ``Database.update_key_note`` 当前缺失，采用鸭子类型
  探测，缺失时返回 501（Not Implemented）并给出中文错误，不静默失败。
- 图片白名单文件持久化于 ``config.data_dir/whitelist/``（缺省 ``./data/whitelist/``），
  以 ``{md5}.png`` 命名 —— md5 取 PNG 编码字节（与 ``compute_hashes`` 一致），
  故磁盘文件内容哈希与入库 md5 相等，删除时可按 md5 定位文件。
- 向量重建清单（manifest）由 ``scripts/normalize_assets.py`` 产出（PRD §5
  统一布局 ``data/vectors/``）；``rebuild_hook(manifest_path)`` 由集成方注入，
  支持同步/异步函数，未注入时端点返回 501。
- v0.1 管理端 QPS 低，DAO 同步调用直接放入 async 端点可接受；单条失败（如单张
  图片解码失败）记录错误并继续处理其余条目，不拖垮整批（rules.md 规则 4）。
"""

from __future__ import annotations

import base64
import contextlib
import csv
import inspect
import io
import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from safefusion import __version__
from safefusion.api.dependencies import Page, pagination, require_admin_token
from safefusion.engines.image_pipeline import WhitelistMatcher, compute_hashes, decode_images
from safefusion.logging_setup import get_logger
from safefusion.models.schemas import ImageInput
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


def _resolve_admin_token(config: Any) -> str:
    """解析管理令牌：config → 环境变量 ADMIN_PASSWORD → 启动生成（打印一次）。"""

    if config is not None:
        token = getattr(config, "admin_token", None) or getattr(
            getattr(config, "admin", None), "token", None
        )
        if isinstance(token, str) and token:
            return token
    token = os.environ.get("ADMIN_PASSWORD")
    if token:
        return token
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
    config: Any = None,
) -> FastAPI:
    """创建管理 API 应用（FastAPI，部署时挂载于 :8001）。

    Args:
        db: T2 SQLite DAO（``storage.database.Database`` 实例）。
        whitelist_matcher: T4 图片白名单匹配器（``WhitelistMatcher``，已在构造时注入 db）。
        rebuild_hook: 向量库重建回调，接收归一化清单路径字符串；可为同步或异步函数；
            未注入时 ``POST /admin/vectors/rebuild`` 返回 501。
        config: 应用配置（``AppConfig``，或提供 ``data_dir`` / ``admin_token`` 属性的
            鸭子类型）；为 None 时白名单目录与默认清单取 ``./data`` 下。

    Returns:
        已注册五组端点与全局异常处理器（脱敏 JSON）的 FastAPI 实例。
    """

    token = _resolve_admin_token(config)
    data_dir = Path(getattr(config, "data_dir", "./data"))
    whitelist_dir = data_dir / "whitelist"

    app = FastAPI(
        title="SafeFusion 管理 API",
        version=__version__,
        description="PRD §4.2：Key / 词库 / 白名单 / 日志 / 向量重建。端点需 X-Admin-Token。",
    )
    router = APIRouter(
        prefix="/admin",
        dependencies=[Depends(require_admin_token(token))],
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
        ``Database.update_key_note(key, note)``，缺失时返回 501。"""

        row = db.get_key(key)
        if row is None:
            raise HTTPException(status_code=404, detail=f"API Key 不存在: {_mask_key(key)}")
        if body.enabled is not None:
            db.set_key_enabled(key, body.enabled)
        if body.note is not None:
            updater = _storage_method(db, "update_key_note", "更新 Key 备注")
            if not updater(key, body.note):
                raise HTTPException(status_code=404, detail=f"API Key 不存在: {_mask_key(key)}")
        updated = db.get_key(key)
        return {**updated, "enabled": bool(updated["enabled"])}

    @router.delete("/keys/{key}")
    async def delete_key(key: str) -> dict[str, Any]:
        """删除 API Key：需存储层 ``Database.delete_key(key)``，缺失时返回 501。"""

        deleter = _storage_method(db, "delete_key", "删除 API Key")
        if not deleter(key):
            raise HTTPException(status_code=404, detail=f"API Key 不存在: {_mask_key(key)}")
        return {"deleted": key}

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
