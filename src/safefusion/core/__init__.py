"""核心层：应用上下文（AppContext）与审核编排（T9 交付 orchestration）。

T1 仅提供 AppContext 聚合壳与占位 build()，装配逻辑由集成层 T9 补全。
"""

from .context import AppContext

__all__ = ["AppContext"]
