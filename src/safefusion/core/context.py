"""应用上下文聚合壳：集中持有配置与全部组件实例。

- 各组件字段当前均为占位（默认 None），由集成层 T9 的 ``AppContext.build()`` 装配后填充；
- 使用 TYPE_CHECKING 字符串注解：避免在包骨架阶段引入对尚未建成模块的运行时依赖。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..cache.caches import CacheLayer
    from ..config import AppConfig
    from ..engines.embedding import BaseEmbedding
    from ..engines.image_pipeline import WhitelistMatcher
    from ..engines.keyword_engine import KeywordEngine
    from ..engines.light_model import LightTextModel
    from ..engines.llm_client import LLMClient
    from ..engines.semantic import SemanticEngine
    from ..storage.vector_store import BaseVectorStore


@dataclass
class AppContext:
    """应用级依赖容器。

    装配顺序（由 T9 保证）：先构建存储与引擎组件实例，再填充本容器字段，
    随后交给编排器 ``AuditOrchestrator`` 使用；组件缺失时为 None 表示降级/未启用。
    """

    config: "AppConfig | None" = None
    store: "BaseVectorStore | None" = None
    embedding: "BaseEmbedding | None" = None
    keyword_engine: "KeywordEngine | None" = None
    light_model: "LightTextModel | None" = None
    whitelist: "WhitelistMatcher | None" = None
    semantic: "SemanticEngine | None" = None
    llm: "LLMClient | None" = None
    cache_layer: "CacheLayer | None" = None

    @classmethod
    def build(cls, config: "AppConfig") -> "AppContext":
        """装配全部组件并返回完整 AppContext（当前为占位实现）。

        Args:
            config: 已加载的应用总配置。

        Raises:
            NotImplementedError: 装配逻辑由集成层（T9）补全。
        """

        raise NotImplementedError("AppContext.build 由集成层（T9）实现")
