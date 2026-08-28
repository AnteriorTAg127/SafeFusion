"""审核 API（:8000，PRD §4.1）：认证 / 审核 / 健康检查 / 限流 / 异常脱敏。

设计要点：

- **鉴权**：``Authorization: Bearer <key>`` 或 ``X-Api-Key`` 请求头，经
  ``Database.get_key`` 校验（不存在 / 禁用 → 401 ``{"error": "invalid api key"}``）；
  Key 分组（standard / full）决定响应 detail 裁剪与 overrides 权限。
  依赖工厂模式仿照 ``api.dependencies.require_admin_token``（T11 风格基准）；
- **overrides 权限**：仅 full 组可用。非 full 组携带时由编排器
  （:class:`~safefusion.core.orchestrator.AuditOrchestrator`）抛 ``PermissionError``，
  本层捕获并映射 403 ``{"error": "overrides 仅 full 组可用"}``（PRD §4.1 / 分工 T10）；
- **分级裁剪**：``standard`` 组响应 ``detail=None``（编排器已保证 non-full 不填 detail，
  本层在出口再裁剪一次，防 stub / 未来编排路径漏裁）；
- **限流**：每 API Key 进程内滑动窗口（``collections.deque`` + ``time.monotonic``），
  默认 60 次 / 60 秒；可用环境变量 ``SAFEFUSION_RATE_LIMIT`` 覆盖（正整数，次/窗口）。
  刻意不写入 ``config.py``——限流属部署时运行时策略，改动配置契约需主模型评审
  （PRD §6「简单速率限制（进程内）」）；
- **异常脱敏**：HTTPException 原样 JSON、请求体校验失败 422 通用文案、未预期异常
  500 通用文案 ``{"error": "internal error"}``（完整堆栈仅进日志，密钥/内部结构
  不回响应。PRD §6 安全「错误响应脱敏」）；
- **共享容器**：:func:`build_container` 装配共享 ``AppContext`` 并向 :class:`FastAPI`
  的 ``app.state`` 注入（container / orchestrator / rate_limiter / startup_ts），
  ``api/__main__.py`` 复用同一容器拉起双服务，避免同进程内重复装配。

部署：``uvicorn safefusion.api.app:create_app`` 监听 ``server.port``（默认 8000）；
或使用统一入口 ``python -m safefusion.api``（同时拉起 :8000 审核与 :8001 管理）。

注意：本模块**不使用** ``from __future__ import annotations``——端点依赖参数
``Annotated[..., Depends(require_key)]`` 中的 ``require_key`` 是工厂局部变量，
字符串化注解会让 FastAPI 无法求值 `Depends` 而把参数误判为 query（T11 的
``Depends(pagination)`` 是模块级名字才不受影响）；Python ≥ 3.10 原生支持
``str | None`` 注解，无需延迟求值。
"""

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from safefusion import __version__
from safefusion.config import AppConfig, load_config
from safefusion.core.context import AppContext
from safefusion.core.orchestrator import AuditOrchestrator
from safefusion.logging_setup import get_logger
from safefusion.models.schemas import AuditRequest, AuditResult

logger = get_logger("api.app")

__all__ = ["create_app", "build_container", "KeyRateLimiter"]

#: 每 API Key 限流默认值：60 次 / 60 秒滑动窗口（PRD §6「简单速率限制（进程内）」）。
#: 速率属部署时运行时策略，不写入 config.py；可用环境变量 SAFEFUSION_RATE_LIMIT
#: 覆盖（正整数，含义为「次 / 60 秒窗口」，缺省 60）。
_RATE_LIMIT_DEFAULT = 60
_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT_ENV = "SAFEFUSION_RATE_LIMIT"


def _resolve_rate_limit() -> int:
    """解析限流上限：环境变量 ``SAFEFUSION_RATE_LIMIT`` 覆盖，非法值回退默认。"""

    raw = os.environ.get(_RATE_LIMIT_ENV)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("忽略非法限流配置 %s=%r（应为正整数）", _RATE_LIMIT_ENV, raw)
    return _RATE_LIMIT_DEFAULT


class KeyRateLimiter:
    """每 API Key 的进程内滑动窗口限流器（``deque`` + ``time.monotonic``）。

    同一 Key 的请求时间戳追加进该 Key 对应的队列，窗口（``window_sec``）内请求数
    达到 ``limit`` 即拒绝；过期时间戳惰性弹出。线程安全（独立锁，窗口 dict 防竞态）。
    """

    def __init__(self, limit: int, window_sec: float) -> None:
        """初始化限流器。

        Args:
            limit: 单个窗口内允许的最大请求次数（> 0）。
            window_sec: 滑动窗口时长（秒）。
        """

        self._limit = limit
        self._window_sec = window_sec
        self._lock = threading.Lock()
        self._window: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """记录一次请求：窗口内未超限返回 True 并计数；超限返回 False（不计数）。"""

        now = time.monotonic()
        with self._lock:
            stamps = self._window.setdefault(key, deque())
            while stamps and now - stamps[0] > self._window_sec:
                stamps.popleft()
            if len(stamps) >= self._limit:
                return False
            stamps.append(now)
            return True


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """从请求头解析 API Key：优先 ``X-Api-Key``，其次 ``Authorization: Bearer <key>``。"""

    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        scheme, _, rest = authorization.partition(" ")
        if scheme.lower() == "bearer" and rest.strip():
            return rest.strip()
    return None


def _require_api_key(db: Any) -> Callable[..., dict[str, Any]]:
    """构造「API Key 有效且启用」的 FastAPI 依赖工厂（仿 T11 ``require_admin_token`` 模式）。

    Args:
        db: ``Database`` 实例；为 None（数据库降级）时一律 401——无 Key 可验证，
            避免把存储故障泄露为 500。

    Returns:
        依赖函数：返回该 Key 的数据库记录（含 ``tier`` / ``enabled``），供端点取
        权限分组；Key 缺失 / 禁用 → ``HTTPException(401)``（``{"error": "invalid api key"}``）。
    """

    def _dependency(
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    ) -> dict[str, Any]:
        api_key = _extract_api_key(authorization, x_api_key)
        if api_key is None or db is None:
            raise HTTPException(status_code=401, detail="invalid api key")
        row = db.get_key(api_key)
        if row is None or not bool(row["enabled"]):
            raise HTTPException(status_code=401, detail="invalid api key")
        return row

    return _dependency


def build_container(config: AppConfig, database: Any = None) -> AppContext:
    """装配共享组件容器（``AppContext.build``）并对降级组件清单打 warning。

    供 :func:`create_app` 与 ``api/__main__.py`` 复用——同一进程内两个应用
    （审核 / 管理）共享同一份存储、缓存与模型实例，避免重复装配。

    Args:
        config: 已加载的应用总配置。
        database: 可选已建 ``Database`` 实例（v0.3.0 启动迁移复用连接）；
            None 时由 ``AppContext.build`` 自行创建。

    Returns:
        装配完成的 ``AppContext``（``degraded`` 字段列出未成功装配的组件名）。
    """

    container = AppContext.build(config, database=database)
    if container.degraded:
        logger.warning("组件降级清单: %s", ", ".join(container.degraded))
    return container


def create_app(config: AppConfig | None = None, container: AppContext | None = None) -> FastAPI:
    """创建审核 API 应用（FastAPI，部署时监听 :8000，PRD §4.1）。

    创建即装配（等价于进程 startup）：``AppContext.build(config or load_config(None))``
    存入 ``app.state.container``，并组装 ``AuditOrchestrator`` / 限流器 / 启动时间戳。

    Args:
        config: 应用配置；为 None 时 ``load_config(None)``（内置默认值 + 环境变量）。
        container: 已装配的共享 ``AppContext``；为 None 时按 ``config`` 现场装配。
            ``__main__.py`` 传入已建容器以避免同进程双份装配（build 只执行一次）。

    Returns:
        已注册 ``POST /v1/audit`` / ``GET /health`` 与全局异常处理器（脱敏 JSON）的实例。
    """

    cfg = config if config is not None else load_config(None)
    ctx = container if container is not None else build_container(cfg)

    app = FastAPI(
        title="SafeFusion 审核 API",
        version=__version__,
        description=(
            "PRD §4.1：POST /v1/audit（Authorization: Bearer 或 X-Api-Key 认证，"
            "standard/full 分级响应）与 GET /health（免认证）。"
        ),
    )
    app.state.container = ctx
    app.state.orchestrator = AuditOrchestrator(ctx)
    app.state.rate_limiter = KeyRateLimiter(_resolve_rate_limit(), _RATE_WINDOW_SECONDS)
    app.state.startup_ts = time.monotonic()

    require_key = _require_api_key(ctx.database)

    # ------------------------------------------------------------- POST /v1/audit
    @app.post("/v1/audit")
    async def audit(
        body: AuditRequest,
        key_row: Annotated[dict[str, Any], Depends(require_key)],
        request: Request,
    ) -> AuditResult:
        """执行一次内容审核（PRD §4.1）。

        认证 → 每 Key 限流 → 编排器全流程；``standard`` 组出口裁剪 ``detail=None``，
        overrides 权限由编排器校验、本层把 ``PermissionError`` 映射为 403。
        """

        tier = str(key_row["tier"])
        limiter: KeyRateLimiter = request.app.state.rate_limiter
        if not limiter.allow(str(key_row["key"])):
            raise HTTPException(status_code=429, detail="请求过于频繁")
        orchestrator: AuditOrchestrator = request.app.state.orchestrator
        try:
            result = await orchestrator.process_audit(body, tier)
        except PermissionError:  # 编排器 ① overrides 权限校验：非 full 组携带 → 403
            raise HTTPException(status_code=403, detail="overrides 仅 full 组可用") from None
        if tier != "full":  # standard 组只返回基本判定（PRD §4.1 分级裁剪）
            result.detail = None
        return result

    # --------------------------------------------------------------- GET /health
    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """健康检查（免认证）：状态 / 版本 / 降级清单 / 缓存统计 / 运行时长。

        ``status`` 恒为 ``ok``（服务可达即健康）；降级组件经 ``degraded`` 清单与
        ``cache`` 统计暴露给运维（PRD §6 可观测）。
        """

        container_now: AppContext = request.app.state.container
        return {
            "status": "ok",
            "version": __version__,
            "degraded": list(container_now.degraded),
            "cache": (
                container_now.cache_layer.stats() if container_now.cache_layer is not None else None
            ),
            "uptime_s": round(time.monotonic() - request.app.state.startup_ts, 3),
        }

    # ---------------------------------------------- 全局异常处理（脱敏 JSON）
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """HTTPException → 原样 JSON（error 字段；detail 非字符串时脱敏为通用文案）。"""

        detail = exc.detail if isinstance(exc.detail, str) else "请求被拒绝"
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求体校验失败 → 422 脱敏 JSON（不回字段细节，防内部结构泄露）。"""

        return JSONResponse(status_code=422, content={"error": "请求参数校验失败"})

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """未预期异常 → 500 脱敏 JSON ``{"error": "internal error"}``。

        完整堆栈只进日志（``logger.exception``），不回响应体——拒绝泄露内部结构
        或任何密钥 / Key 明文（PRD §6 安全「错误响应脱敏」）。
        """

        logger.exception("审核 API 内部异常: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    return app
