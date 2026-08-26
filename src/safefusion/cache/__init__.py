"""SafeFusion 缓存层：五级进程内缓存（PRD §2 ① 缓存层）。

对外暴露 :class:`CacheLayer`（审核缓存 / 高频缓存 / 图片去重缓存 /
短文本 LLM 缓存 / 永久黑白名单），核心实现见 :mod:`safefusion.cache.caches`。
"""

from .caches import CacheLayer

__all__ = ["CacheLayer"]
