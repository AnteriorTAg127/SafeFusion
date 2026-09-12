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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from safefusion.cache.caches import CacheLayer
from safefusion.engines.embedding import get_embedding_backend
from safefusion.engines.light_model import LightTextModel
from safefusion.engines.llm_client import build_llm as build_llm_client
from safefusion.engines.semantic import SemanticEngine, build_semantic_thresholds

if TYPE_CHECKING:
    from safefusion.config import AppConfig

_logger = logging.getLogger("safefusion.hot_apply")

#: 分组应用方式（GROUP_REGISTRY 的取值）
KIND_PARAM = "param"
KIND_REBUILD = "rebuild"
KIND_CONFIG_ONLY = "config_only"

#: **分组分类的唯一来源**（Brooks-Lint P6）：新增配置分组只在此登记一次；
#: 三个下游集合与 stage_apply / post_apply_sync 的分发均由其派生，避免"漏登记即静默不生效"。
#:   - param：直接改配置叶子 + 轻量后置同步（无实例重建）
#:   - rebuild：试建造新实例 + 锁内原子替换（rerank 本身不持实例，但改变语义引擎重排后端）
#:   - config_only：仅同步配置叶子（server 网络绑定 / logging handler 重建下次启动生效）
GROUP_REGISTRY: dict[str, str] = {
    "thresholds": KIND_PARAM,
    "semantic": KIND_PARAM,
    "review": KIND_PARAM,
    "keyword": KIND_PARAM,
    "image": KIND_PARAM,
    "embedding": KIND_REBUILD,
    "llm": KIND_REBUILD,
    "rerank": KIND_REBUILD,
    "light_model": KIND_REBUILD,
    "cache": KIND_REBUILD,
    "server": KIND_CONFIG_ONLY,
    "logging": KIND_CONFIG_ONLY,
}

#: 参数类分组：直接改配置叶子 + 轻量后置同步（无实例重建）
PARAM_GROUPS = frozenset(g for g, k in GROUP_REGISTRY.items() if k == KIND_PARAM)

#: 组件重建类分组：试建造新实例 + 锁内原子替换
REBUILD_GROUPS = frozenset(g for g, k in GROUP_REGISTRY.items() if k == KIND_REBUILD)

#: 纯配置分组：仅同步配置叶子，不作运行时组件替换或同步
CONFIG_ONLY_GROUPS = frozenset(g for g, k in GROUP_REGISTRY.items() if k == KIND_CONFIG_ONLY)

__all__ = [
    "CONFIG_ONLY_GROUPS",
    "GROUP_REGISTRY",
    "KIND_CONFIG_ONLY",
    "KIND_PARAM",
    "KIND_REBUILD",
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


def build_llm(cfg: AppConfig) -> Any:
    """按候选配置建造 LLM 客户端（PRD v0.4.0 M1/M3：支持单后端或多 provider）。

    返回 ``LLMClient`` 或 ``MultiLLM``（两者均提供 ``available`` 与
    ``async judge(...)``）。
    """

    return build_llm_client(cfg.llm)


def build_light_model(cfg: AppConfig) -> LightTextModel:
    """按候选配置建造轻量文本模型（缺文件/torch 时 disabled，不抛）。"""

    return LightTextModel(cfg.light_model.model_path, cfg.light_model.config_path)


def build_cache(cfg: AppConfig) -> CacheLayer:
    """按候选配置建造缓存层（容量 / TTL / backend 热生效；旧缓存内容清空）。"""

    return CacheLayer(cfg.cache.model_dump())


def _build_semantic(
    ctx: Any, cfg: AppConfig, embedding: Any, store: Any | None
) -> SemanticEngine | None:
    """按「新 embedding + 指定 store + 候选阈值」重建语义引擎。

    阈值合并口径与装配（``AppContext``）/ 热重载一致——统一走
    :func:`~safefusion.engines.semantic.build_semantic_thresholds`（单一来源）。

    Args:
        ctx: ``AppContext`` 实例（当前实现未直接使用，保留以统一签名）。
        cfg: 候选有效配置（提供 thresholds / semantic / rerank 分组）。
        embedding: 新 Embedding 后端；为 None 时不重建。
        store: 目标向量库实例；为 None 时不重建（语义层保持降级）。

    Returns:
        重建后的 ``SemanticEngine``；``embedding`` 或 ``store`` 缺失时返回 None。
    """

    if embedding is None or store is None:
        return None
    return SemanticEngine(
        embedding,
        store,
        thresholds=build_semantic_thresholds(cfg),
        rerank_config=cfg.rerank,
    )


def _resolve_store_name_for_candidate(candidate: AppConfig) -> str | None:
    """按候选配置解析当前 embedding provider 对应的向量库名称。"""
    providers = candidate.embedding.providers
    if not providers:
        return "default"
    active = candidate.embedding.active_provider
    if active is not None:
        for p in providers:
            if p.name == active:
                return p.vector_store or "default"
    return min(providers, key=lambda p: p.priority).vector_store or "default"


def _resolve_store_for_candidate(ctx: Any, candidate: AppConfig) -> Any | None:
    """按候选配置取当前 provider 对应的向量库实例（懒加载，不常驻）。"""
    name = _resolve_store_name_for_candidate(candidate)
    if name is None:
        return None
    getter = getattr(ctx, "get_store", None)
    if callable(getter):
        return getter(name)
    return None


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


def _stage_embedding(ctx: Any, candidate: AppConfig) -> StagedSwap:
    """embedding：新后端 + 按 provider 解析的向量库 + 重建语义引擎。"""
    new_embedding = build_embedding(candidate)
    # 多向量库：按候选配置解析当前 provider 对应的 store
    new_store = _resolve_store_for_candidate(ctx, candidate)
    new_semantic = _build_semantic(ctx, candidate, new_embedding, new_store)
    replacements: dict[str, Any] = {"embedding": new_embedding}
    if new_store is not None:
        replacements["store"] = new_store
        replacements["store_name"] = _resolve_store_name_for_candidate(candidate)
    if new_semantic is not None or ctx.semantic is not None:
        replacements["semantic"] = new_semantic
    return StagedSwap("embedding", replacements)


def _stage_llm(_ctx: Any, candidate: AppConfig) -> StagedSwap:
    """llm：新建单客户端或多 provider 包装。"""
    return StagedSwap("llm", {"llm": build_llm(candidate)})


def _stage_rerank(ctx: Any, candidate: AppConfig) -> StagedSwap | None:
    """rerank：只影响语义引擎的重排后端；语义引擎未装配时无需替换。"""
    new_semantic = _build_semantic(ctx, candidate, ctx.embedding, ctx.store)
    if new_semantic is None and ctx.semantic is None:
        return None
    return StagedSwap("rerank", {"semantic": new_semantic})


def _stage_light_model(ctx: Any, candidate: AppConfig) -> StagedSwap | None:
    """light_model：新实例；旧实例本就是 disabled 时无需替换。"""
    new_light = build_light_model(candidate)
    if not new_light.disabled or ctx.light_model is not None:
        return StagedSwap("light_model", {"light_model": new_light})
    return None


def _stage_cache(_ctx: Any, candidate: AppConfig) -> StagedSwap:
    """cache：新建缓存层（容量 / TTL / backend 热生效；旧缓存内容清空）。"""
    return StagedSwap("cache", {"cache_layer": build_cache(candidate)})


#: 重建类分组 → 试建造器（必须在 :data:`GROUP_REGISTRY` 中登记为 ``rebuild``）
REBUILD_APPLIERS: dict[str, Callable[[Any, AppConfig], StagedSwap | None]] = {
    "embedding": _stage_embedding,
    "llm": _stage_llm,
    "rerank": _stage_rerank,
    "light_model": _stage_light_model,
    "cache": _stage_cache,
}


def stage_apply(ctx: Any, group: str, candidate: AppConfig) -> StagedSwap | None:
    """对重建类分组做**试建造**（锁外执行，不触碰运行中实例）。

    任一建造失败（构造抛异常）直接上抛——管理端据此返回 500 并**不落库**，
    旧实例继续生效（PRD「先验证后落库、失败自动回滚旧实例」）。

    分发经 :data:`REBUILD_APPLIERS`（与 :data:`GROUP_REGISTRY` 同源的声明式表），
    不再使用 if/elif 字符串链——新增重建类分组只需补一行登记。

    Args:
        ctx: ``AppContext`` 实例。
        group: 分组名（须为 :data:`REBUILD_GROUPS` 成员）。
        candidate: 含新组值（且 env 仍最高优先）的候选有效配置。

    Returns:
        ``StagedSwap``；参数类 / 纯配置分组返回 None（无需预建造）。
    """

    applier = REBUILD_APPLIERS.get(group)
    return applier(ctx, candidate) if applier is not None else None


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


def _sync_semantic_thresholds(ctx: Any) -> None:
    """按当前有效配置重建语义引擎阈值字典（热生效）。"""
    ctx.reload_semantic_thresholds()


def _sync_keywords(ctx: Any) -> None:
    """词库 + 正则规则热重载（规则开关即时生效）。"""
    ctx.reload_keywords()


#: 参数类分组 → 组后置同步（声明式表，与 GROUP_REGISTRY 同源）
POST_APPLIERS: dict[str, Callable[[Any], None]] = {
    "thresholds": _sync_semantic_thresholds,
    "semantic": _sync_semantic_thresholds,
    "keyword": _sync_keywords,
}


def post_apply_sync(ctx: Any, group: str) -> None:
    """参数类分组的运行时后置同步（轻量，不重建实例）。

    - thresholds / semantic：按当前有效配置重建语义引擎阈值字典；
    - keyword：词库 + 正则规则重载（规则开关即时生效）。

    分发经 :data:`POST_APPLIERS`；未登记的分组无后置同步（空操作）。
    """

    applier = POST_APPLIERS.get(group)
    if applier is not None:
        applier(ctx)


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
