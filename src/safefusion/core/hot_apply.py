"""全量热应用器（PRD v0.3.0 M4 C）：分组 → 应用器注册表，写库后立即生效。

设计要点：

- **分组分类**：
  - 参数类（：data:`PARAM_GROUPS`）：纯配置叶子，经
    ``AppContext.swap_components(candidate=...)`` 就地同步进运行中配置
    （对象身份不变，编排器 / 复核调度器引用即时可见），再执行组后置同步
    （阈值重建 / 词库规则重载）；
  - 组件重建类（：data:`REBUILD_GROUPS`）：先经 :func:`stage_apply` 在
    **锁外试建造**新引擎实例（失败抛异常 → 管理端 500、DB 不写、旧实例
    继续生效），落库后经 :func:`apply_hot` 在锁内**原子替换**进 AppContext；
  - 纯配置分组（：data:`CONFIG_ONLY_GROUPS`）：server / logging 仅同步配置
    叶子（网络绑定等无法热切的点由响应 ``apply_scope="config"`` 说明）；
- **SemanticEngine 重建**：embedding 替换后按「新 embedding + 现有 store +
  当前有效阈值」重建语义引擎并一并原子替换（store 不变，无需重载向量库）；
- **ReviewScheduler LLM 重建**：llm 分组热应用时经
  ``ReviewScheduler.reload_llm`` 原子替换调度器专用客户端——复核轮次开始
  时读取 ``self._llm``，替换发生在轮次间隙，调度线程安全；
- **回滚**：试建造失败不落库不替换（:func:`stage_apply` 抛异常）；落库后
  应用阶段仅做引用赋值与配置叶子同步，失败概率极低，仍由调用方捕获并
  回写 DB 旧组值（见 ``api.admin`` PUT 端点）。

测试注入：:func:`build_embedding` 等建造函数为**模块级名字**，单测可以直接
``monkeypatch.setattr(hot_apply, "build_embedding", fake_factory)`` 驱动
后端切换 / 失败回滚路径，无需真实 torch / httpx。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from safefusion.cache.caches import CacheLayer
from safefusion.engines.embedding import get_embedding_backend
from safefusion.engines.light_model import LightTextModel
from safefusion.engines.llm_client import LLMClient
from safefusion.engines.semantic import SemanticEngine

if TYPE_CHECKING:
    from safefusion.config import AppConfig

_logger = logging.getLogger("safefusion.hot_apply")

#: 参数类分组：直接改配置叶子 + 轻量后置同步（无实例重建）
PARAM_GROUPS = frozenset({"thresholds", "semantic", "review", "keyword", "image"})

#: 组件重建类分组：试建造新实例 + 锁内原子替换
REBUILD_GROUPS = frozenset({"embedding", "llm", "light_model", "cache"})

#: 纯配置分组：仅同步配置叶子，不作运行时组件替换或同步
#: （server 网络绑定 / logging handler 重建不在此里程碑热切，响应标注
#:  ``apply_scope="config"``；绑定类变更在下次启动生效）
CONFIG_ONLY_GROUPS = frozenset({"server", "logging"})

__all__ = [
    "CONFIG_ONLY_GROUPS",
    "PARAM_GROUPS",
    "REBUILD_GROUPS",
    "StagedSwap",
    "apply_admin_token",
    "apply_hot",
    "build_cache",
    "build_embedding",
    "build_light_model",
    "build_llm",
    "post_apply_sync",
    "stage_apply",
]


# ------------------------------------------------------------- 建造工厂（可注入）


def build_embedding(cfg: AppConfig) -> Any:
    """按候选配置建造 Embedding 后端（模块级名字，测试可 monkeypatch）。

    v0.3.0 M6 懒加载：local 后端以 ``local_files_only=True`` 装载（只读缓存，
    权重缺失快速失败 → 管理端 500 且不落库，绝不因配置应用意外联网下载）。
    """

    return get_embedding_backend(cfg.embedding, local_files_only=True)


def build_llm(cfg: AppConfig) -> LLMClient:
    """按候选配置建造 LLM 客户端（密钥字段剥离走环境变量）。"""

    return LLMClient(cfg.llm.model_dump(exclude={"api_key"}))


def build_light_model(cfg: AppConfig) -> LightTextModel:
    """按候选配置建造轻量文本模型（缺文件/torch 时 disabled，不抛）。"""

    return LightTextModel(cfg.light_model.model_path, cfg.light_model.config_path)


def build_cache(cfg: AppConfig) -> CacheLayer:
    """按候选配置建造缓存层（容量 / TTL / backend 热生效；旧缓存内容清空）。"""

    return CacheLayer(cfg.cache.model_dump())


def _build_semantic(ctx: Any, cfg: AppConfig, embedding: Any) -> SemanticEngine | None:
    """按「新 embedding + 现有 store + 候选阈值」重建语义引擎。

    阈值合并口径与 ``AppContext.build`` / ``reload_semantic_thresholds`` 一致；
    store 或 embedding 缺失时返回 None（语义层保持降级）。
    """

    if embedding is None or ctx.store is None:
        return None
    thresholds = {**cfg.thresholds.model_dump(), **cfg.semantic.model_dump()}
    return SemanticEngine(embedding, ctx.store, thresholds=thresholds)


# ------------------------------------------------------------- 试建造 / 应用


@dataclass
class StagedSwap:
    """一次热应用的预建造结果（锁外完成，替换在锁内瞬时执行）。

    Attributes:
        group: 目标分组名。
        replacements: ``{AppContext 字段名: 新实例}``，待锁内原子替换。
        semantic: embedding 分组重建后的语义引擎（可替换字段），随
            ``replacements`` 一并写入（键 "semantic"）。
    """

    group: str
    replacements: dict[str, Any] = field(default_factory=dict)

    @property
    def field_names(self) -> list[str]:
        """待替换的 AppContext 字段名列表。"""

        return list(self.replacements)


def stage_apply(ctx: Any, group: str, candidate: AppConfig) -> StagedSwap | None:
    """对重建类分组做**试建造**（锁外执行，不触碰运行中实例）。

    任一建造失败（构造抛异常）直接上抛——管理端据此返回 500 并**不落库**，
    旧实例继续生效（PRD「先验证后落库、失败自动回滚旧实例」）。

    Args:
        ctx: ``AppContext`` 实例。
        group: 分组名（须为 :data:`REBUILD_GROUPS` 成员）。
        candidate: 含新组值（且 env 仍最高优先）的候选有效配置。

    Returns:
        ``StagedSwap``；参数类 / 纯配置分组返回 None（无需预建造）。
    """

    if group == "embedding":
        new_embedding = build_embedding(candidate)
        new_semantic = _build_semantic(ctx, candidate, new_embedding)
        replacements: dict[str, Any] = {"embedding": new_embedding}
        if new_semantic is not None or ctx.semantic is not None:
            replacements["semantic"] = new_semantic
        return StagedSwap(group, replacements)
    if group == "llm":
        return StagedSwap(group, {"llm": build_llm(candidate)})
    if group == "light_model":
        new_light = build_light_model(candidate)
        if not new_light.disabled or ctx.light_model is not None:
            return StagedSwap(group, {"light_model": new_light})
        return None  # 旧实例本就是 disabled：无需替换
    if group == "cache":
        return StagedSwap(group, {"cache_layer": build_cache(candidate)})
    return None


def apply_hot(
    ctx: Any,
    group: str,
    candidate: AppConfig,
    staged: StagedSwap | None,
    reviewer: Any = None,
) -> str:
    """应用一次热应用：同步配置叶子 + 原子替换预建造组件 + 组后置同步。

    Args:
        ctx: ``AppContext`` 实例。
        group: 目标分组名。
        candidate: 新有效配置（已含新组值）。
        staged: 重建类分组的预建造结果（来自 :func:`stage_apply`）；参数类为 None。
        reviewer: ``ReviewScheduler`` 实例（llm 分组热应用时替换其专用客户端）；
            未提供或缺少 ``reload_llm`` 时跳过调度器侧替换（仅告警）。

    Returns:
        应用范围标识：``"runtime"``（运行时即时生效）或 ``"config"``
        （仅配置叶子，绑定类变更下次启动生效）。
    """

    replacements = staged.replacements if staged is not None else {}
    replaced = ctx.swap_components(replacements=replacements, candidate=candidate)
    post_apply_sync(ctx, group)
    if group == "llm" and "llm" in replaced and reviewer is not None:
        reload_llm = getattr(reviewer, "reload_llm", None)
        if callable(reload_llm):
            reload_llm(ctx.llm)
        else:
            _logger.warning("ReviewScheduler 未提供 reload_llm，本次复核调度器仍使用旧 LLM 客户端")
    return "config" if group in CONFIG_ONLY_GROUPS else "runtime"


def post_apply_sync(ctx: Any, group: str) -> None:
    """参数类分组的运行时后置同步（轻量，不重建实例）。

    - thresholds / semantic：按当前有效配置重建语义引擎阈值字典；
    - keyword：词库 + 正则规则重载（规则开关即时生效）。
    """

    if group in ("thresholds", "semantic"):
        ctx.reload_semantic_thresholds()
    elif group == "keyword":
        ctx.reload_keywords()


def apply_admin_token(token_store: Any, new_token: str) -> None:
    """热应用管理令牌：旧令牌立即失效（``AdminToken.set``）。

    Args:
        token_store: ``dependencies.AdminToken`` 容器（或提供 ``set`` 方法的
            同类对象）。
        new_token: 新管理令牌（已通过业务校验，如长度 ≥ 10）。
    """

    setter = getattr(token_store, "set", None)
    if not callable(setter):
        raise TypeError("token_store 需要可用的 set(new_token) 方法（AdminToken）")
    setter(new_token)
