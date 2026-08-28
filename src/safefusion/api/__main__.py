"""统一入口：``python -m safefusion.api`` 同时拉起审核 API（:8000）与管理 API（:8001）。

- 单个共享 ``AppContext``（``api.app.build_container``）供两个应用复用——存储 /
  缓存 / 模型实例只装配一次，避免同进程内重复 build（T10 任务卡「避免重复 build」）；
- 管理 API 在**守护线程**中以 ``uvicorn.Server`` 运行：uvicorn 在非主线程会跳过
  信号捕获，SIGINT/SIGTERM 仍由主线程的审核 API（``uvicorn.run``）统一处理；
- ``create_admin_app`` 的管理令牌解析沿用 T11 逻辑（config → 环境变量
  ``ADMIN_PASSWORD`` → 自动生成并打印一次，见 ``api.admin._resolve_admin_token``）；
- **定时复核（PRD v0.2 M7 / T20）**：数据库可用时为 ``create_admin_app`` 注入
  ``ReviewScheduler``。复核器使用**专用** ``LLMClient``（独立构造函数新建实例），
  避免与审核管线共享 ``AsyncOpenAI`` 客户端跨事件循环复用（审核 / 管理 / 复核
  各自运行在独立循环）；``config.review.interval_min > 0`` 时调度器在自身守护
  线程内自动调度，手动触发经 ``/admin/review/run``。数据库不可用时 reviewer 为
  None，``/admin/review/*`` 返回 501。

启动信息（端口 / degraded 清单）经结构化日志（``api.__main__``）输出；密钥与
API Key 一律不写入日志明文，自动生成的管理令牌按 T11 逻辑仅此一次打印。

v0.3.0（M4 配置 DB 化）变更：
- **启动迁移**：``load_config`` 之后先打开 ``{data_dir}/audit.db`` 并执行
  ``migrate_overrides_file``——检测到旧 ``data/config_overrides.json`` 时
  一次性导入 settings 表并归档为 ``.migrated``（失败仅告警、不阻止启动）；
- **优先级**：``effective_config(base, db)`` 按 内置默认 < config.yaml <
  DB settings < 环境变量 合并（env 只读内存生效、绝不写 DB），替换 v0.2.1
  的「重启生效」覆盖层合并；DB 连接随容器复用（``AppContext.build(database=db)``）；
- 管理 API 注入共享 ``container``：``PUT /admin/config/{group}`` 写 DB 后
  **立即热应用**（不再重启生效，见 ``core.hot_apply``）；
- **前端静态托管**：``web/dist/index.html`` 存在时把管理 API ``/`` 挂载为
  ``StaticFiles(html=True)``（同源免 CORS，决策 H）；须在 ``include_router``
  之后挂载（Starlette 按注册顺序匹配路由，``/admin/*`` 优先命中 API）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import uvicorn
from starlette.staticfiles import StaticFiles

from safefusion.api import app as audit_api
from safefusion.api.admin import create_admin_app
from safefusion.config import load_config
from safefusion.core.config_override import effective_config, migrate_overrides_file
from safefusion.core.review import ReviewScheduler
from safefusion.engines.llm_client import LLMClient
from safefusion.logging_setup import get_logger, setup_logging
from safefusion.storage.database import Database

logger = get_logger("api.__main__")


def _find_web_dist() -> Path | None:
    """按「启动目录 → 仓库根」顺序查找 ``web/dist``（需含 index.html 才有效）。"""

    candidates = (
        Path.cwd() / "web" / "dist",
        Path(__file__).resolve().parents[3] / "web" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def maybe_mount_web_dist(app: Any, dist_dir: str | Path | None = None) -> bool:
    """``web/dist/index.html`` 存在时把 ``/`` 挂载为静态托管（html=True）。

    - 不存在时保持现状（不 mount，不影响既有行为与测试 / 部署，PRD 风险表）；
    - **挂载顺序**：须在所有路由注册（``include_router``）之后调用——Starlette
      按注册顺序匹配路由，``/admin/*`` 优先命中 API 路由，不会被 ``/`` 静态
      目录吞掉；dist 存在时 FastAPI 默认 ``/docs`` 等文档路由让位于前端。

    Args:
        app: 待挂载的 FastAPI 应用（管理 API）。
        dist_dir: 前端构建产物目录；缺省按 :func:`_find_web_dist` 自动探测。

    Returns:
        是否成功挂载。
    """

    target = Path(dist_dir) if dist_dir is not None else _find_web_dist()
    if target is None or not (target / "index.html").is_file():
        return False
    app.mount("/", StaticFiles(directory=str(target), html=True), name="web")
    return True


def main() -> None:
    """双服务启动：审核 API（``server.port``）+ 管理 API（``server.admin_port``，守护线程）。"""

    base_config = load_config(None)
    setup_logging(base_config)

    # v0.3.0 M4：启动一次性迁移（旧覆盖层 → settings 表），失败仅告警不阻止启动
    data_dir = Path(base_config.data_dir)
    db: Database | None = None
    try:
        db = Database(data_dir / "audit.db")
        migrate_overrides_file(data_dir, db)
    except Exception as exc:
        logger.warning("配置覆盖层迁移异常（按空 settings 继续启动）: %s", exc)

    # v0.3.0 M6：进程内 HF 缓存根统一为 {data_dir}/models/hf（环境变量
    # HF_HOME 已设置时尊重之——HF 惯例，transformers / huggingface_hub 均
    # 按该变量解析缓存）。模型懒装配 / 后台下载 / 管理端状态探测落同一缓存根。
    os.environ.setdefault("HF_HOME", str(data_dir / "models" / "hf"))

    # 合并 DB settings：内置默认 < config.yaml < DB settings < 环境变量
    config = effective_config(base_config, db=db)
    setup_logging(config)

    # 共享组件容器：两个应用复用同一 AppContext，build 只执行一次
    container = audit_api.build_container(config, database=db)

    if container.database is None:
        logger.warning(
            "存储层不可用：管理 API 会启动，但 /admin/* 端点无法正常服务（依赖 Database）"
        )

    # 定时复核（PRD v0.2 M7）：复核器用专用 LLMClient（独立 AsyncOpenAI 懒创建，
    # 与审核/管理循环隔离）；数据库缺失时不注入（/admin/review/* 返回 501）。
    reviewer = None
    if container.database is not None:
        reviewer = ReviewScheduler(
            db=container.database,
            llm=LLMClient(config.llm.model_dump(exclude={"api_key"})),
            config=config,
            data_dir=config.data_dir,
        )
        reviewer.start()  # interval_min > 0 时后台自动调度；0 则仅手动触发

    admin_app = create_admin_app(
        db=container.database,
        whitelist_matcher=container.whitelist,  # 可能为 None；admin 端点实际走 db，注入仅为满足签名
        rebuild_hook=None,  # TODO(后续集成)：注入向量库重建钩子（normalize_assets 产物清单）
        reload_hook=container.reload_rules,  # 管理端写规则即热重载
        # ^（T17 集成钩子②，主模型 2026-08-26）
        config=base_config,  # v0.3.0：基配置（不含 DB），DB 层由 db 提供（来源标识依赖）
        reviewer=reviewer,  # ^（T20 定时复核注入，2026-08-27）
        container=container,  # v0.3.0 M4：热应用目标（PUT /admin/config 写 DB 后即时生效）
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(admin_app, host=config.server.host, port=config.server.admin_port)
    )
    threading.Thread(target=admin_server.run, daemon=True, name="safefusion-admin").start()

    if maybe_mount_web_dist(admin_app):
        logger.info(
            "web/dist 存在，管理 API 已静态托管前端（http://%s:%d/）",
            config.server.host,
            config.server.admin_port,
        )

    audit_app = audit_api.create_app(config, container=container)

    degraded = ",".join(container.degraded) if container.degraded else "-"
    logger.info(
        "SafeFusion 启动：审核 API http://%s:%d ｜ 管理 API http://%s:%d ｜ degraded=%s",
        config.server.host,
        config.server.port,
        config.server.host,
        config.server.admin_port,
        degraded,
    )
    uvicorn.run(audit_app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
