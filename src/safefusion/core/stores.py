"""多向量库注册表：路径登记 + 按需懒加载 + LRU 缓存（线程安全）。

从 :class:`~safefusion.core.context.AppContext` 拆出（Brooks-Lint P7）：
把「多向量库映射 / 懒加载 / LRU 淘汰」这一职责与「组件装配 / 热应用 / 语义懒装配」
解耦。原实现中 ``_store_cache`` 在事件循环线程、懒装配后台线程、管理线程三处
无锁读写（LRU 的「触碰移尾 / 淘汰最旧」非原子）；本类把缓存读写统一收敛到
``RLock`` 内（加载 I/O 在锁外执行，避免长时间持锁），修复该并发隐患。
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any

from ..logging_setup import get_logger
from ..storage.vector_store import NumpyVectorStore

_logger = get_logger("core.stores")

#: 默认 LRU 容量（只保留最近 N 个库，避免多个大库同时常驻内存）
DEFAULT_CACHE_MAX = 2


class StoreRegistry:
    """向量库注册表：``name -> 目录`` 登记 + 首次访问时懒加载 + LRU 缓存。

    Args:
        paths: ``name -> 持久化目录`` 映射（启动只登记，不加载）。
        name: 当前生效的库名（对应 embedding provider 的 ``vector_store``）。
        cache_max: LRU 缓存容量上限。
    """

    def __init__(
        self,
        paths: dict[str, Path] | None = None,
        name: str = "default",
        cache_max: int = DEFAULT_CACHE_MAX,
    ) -> None:
        self._paths: dict[str, Path] = dict(paths or {})
        self._name = name
        self._cache_max = max(1, int(cache_max))
        self._cache: dict[str, Any] = {}
        self._lock = RLock()

    # ------------------------------------------------------------- 只读视图

    @property
    def paths(self) -> dict[str, Path]:
        """``name -> 目录`` 映射（启动登记结果）。"""
        return self._paths

    @property
    def name(self) -> str:
        """当前生效库名。"""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        """热应用切换当前库名。"""
        self._name = value

    @property
    def cache(self) -> dict[str, Any]:
        """已加载实例的 LRU 缓存（供测试 / 观测；请勿在外部直接修改）。"""
        return self._cache

    def has(self, name: str) -> bool:
        """是否登记了该库名。"""
        return name in self._paths

    # --------------------------------------------------------------- 加载

    def get(self, name: str | None = None) -> Any | None:
        """按名称懒加载向量库；未登记的名称返回 None。

        已加载实例放入 LRU（触碰移尾、超容淘汰最旧）；加载 I/O 在锁外执行。
        """
        target = name or self._name or "default"
        with self._lock:
            cached = self._cache.get(target)
            if cached is not None:
                self._cache.pop(target, None)
                self._cache[target] = cached
                return cached
        path = self._paths.get(target)
        if path is None:
            _logger.warning("请求的向量库未配置: %s（可用: %s）", target, ",".join(self._paths))
            return None
        try:
            if (path / "black.npz").is_file() or (path / "white.npz").is_file():
                loaded = NumpyVectorStore.load(str(path))
            else:
                loaded = NumpyVectorStore(str(path))
                loaded.save()
        except Exception as exc:
            _logger.warning("NumpyVectorStore[%s] 懒加载失败（降级为 None）: %s", target, exc)
            return None
        self.register(target, loaded)
        return loaded

    def register(self, name: str, store: Any) -> None:
        """登记（或替换）一个已加载实例并执行 LRU 淘汰（热应用替换时调用）。"""
        with self._lock:
            self._cache.pop(name, None)
            self._cache[name] = store
            while len(self._cache) > self._cache_max:
                oldest = next(iter(self._cache))
                self._cache.pop(oldest, None)
