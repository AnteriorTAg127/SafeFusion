"""缓存后端抽象层（SafeFusion v0.2 M5）。

v0.1 五级缓存为纯进程内实现；v0.2 抽象为可插拔后端：

- :class:`CacheBackend` 协议：键值均为**已序列化字符串**的存储接口；
- :class:`MemoryBackend`：进程内 ``OrderedDict`` + ``time.monotonic`` 的
  LRU/TTL 实现（自 v0.1 ``caches.py`` 容器抽取，容量参数化，默认 memory 后端）；
- :class:`RedisBackend`：基于 ``redis.asyncio``（可选依赖，延迟导入），
  键统一加前缀，TTL 走 Redis ``expire``，容量 / LRU 由 Redis 侧策略管理；
  连接失败（构造期初始化或首次调用）抛 :class:`CacheBackendError`，
  由上层（``CacheLayer``）捕获并降级为 memory。
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

logger = logging.getLogger("safefusion.cache.backends")

#: 后端单次调用 / 连接等待超时（秒）
_DEFAULT_TIMEOUT = 2.0


class CacheBackendError(RuntimeError):
    """缓存后端不可用（redis 模块缺失 / 连接失败 / 首次调用失败），由上层降级处理。"""


class CacheBackend(Protocol):
    """缓存后端协议：键值为已序列化字符串的键值存储。

    实现要求（方法均需线程安全）：
    - ``get(key)``：取值，缺失 / 过期返回 ``None``；
    - ``set(key, value, ttl_seconds)``：写入；``ttl_seconds <= 0`` 表示永不过期；
    - ``delete(key)`` / ``clear()``：删除单个键 / 本后端全部键；
    - ``items()``：全部**未过期**条目的 ``(key, value)`` 快照
      （③ 图片去重 pHash 近似扫描依赖全量遍历）；
    - ``size()``：当前条目数；无法精确统计的后端返回 ``None``。
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: float) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...

    def items(self) -> list[tuple[str, str]]: ...

    def size(self) -> int | None: ...


class MemoryBackend:
    """进程内 LRU + TTL 后端（``OrderedDict`` 维护 LRU 顺序 + ``time.monotonic`` 计时）。

    语义与 v0.1 容器一致：get/set 刷新条目的 LRU 位置，容量超出驱逐最久未用；
    TTL 惰性清理（访问与快照时发现过期即删除）。容量参数化，由各缓存级别按
    自身 ``capacity`` 传入（默认 10000）。
    """

    def __init__(self, capacity: int = 10000) -> None:
        self._capacity = max(1, int(capacity))
        #: key → (写入时刻, ttl（<=0 永不过期）, 值)
        self._store: OrderedDict[str, tuple[float, float, str]] = OrderedDict()
        self._lock = threading.RLock()

    def _expired(self, ts: float, ttl: float, now: float) -> bool:
        return ttl > 0 and now - ts > ttl

    def get(self, key: str) -> str | None:
        """取值；命中返回并刷新 LRU 顺序，过期/缺失返回 None（过期惰性删除）。"""
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            ts, ttl, value = item
            if self._expired(ts, ttl, now):
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """写入；同键覆盖视为最近使用，超容量时驱逐最久未用条目。"""
        with self._lock:
            if key in self._store:
                del self._store[key]
            self._store[key] = (time.monotonic(), max(0.0, float(ttl_seconds)), value)
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        """删除单个键（不存在则忽略）。"""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """清空全部条目。"""
        with self._lock:
            self._store.clear()

    def items(self) -> list[tuple[str, str]]:
        """返回未过期条目的 ``(key, value)`` 快照（顺带惰性清理过期键）。"""
        now = time.monotonic()
        with self._lock:
            live: list[tuple[str, str]] = []
            for key, (ts, ttl, value) in list(self._store.items()):
                if self._expired(ts, ttl, now):
                    del self._store[key]
                    continue
                live.append((key, value))
            return live

    def size(self) -> int:
        """当前未过期条目数。"""
        return len(self.items())


class _LoopThread(threading.Thread):
    """RedisBackend 专用事件循环线程：把异步调用同步桥接到独立循环。"""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="safefusion-redis-io")
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.start()
        deadline = time.monotonic() + _DEFAULT_TIMEOUT
        while not self.loop.is_running() and time.monotonic() < deadline:
            time.sleep(0.001)
        if not self.loop.is_running():
            raise CacheBackendError("缓存后端事件循环线程启动失败")

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


class RedisBackend:
    """``redis.asyncio`` 后端：键统一前缀 + TTL 走 Redis ``expire``。

    - ``redis`` 为**可选依赖**：构造时延迟 ``import redis.asyncio``，缺失抛
      :class:`CacheBackendError`（memory 模式完全不受影响）；
    - 默认 ``Redis.from_url(url, decode_responses=True)`` 建连并 ``ping`` 校验，
      不可达（init 或首次调用）抛 :class:`CacheBackendError`，由上层降级；
    - 同步方法经 :class:`_LoopThread` 独立事件循环桥接
      （``asyncio.run_coroutine_threadsafe``，单次调用超时 ``_DEFAULT_TIMEOUT``）；
      键值操作均为亚毫秒级，从应用事件循环串行调用整理可接受；
    - 容量 / LRU 由 Redis 侧（``maxmemory`` 策略）管理，本类不实现 LRU 驱逐。

    Args:
        url: Redis 连接 URL（默认 ``redis://127.0.0.1:6379/0``）。
        prefix: 缓存键统一前缀（默认 ``sf:``；日志脱敏不泄露 URL 凭据）。
        client: 可选注入的客户端（异步语义与 ``redis.asyncio`` 一致，如测试用
            内存假 Redis），跳过真实建连与依赖检查。
    """

    def __init__(
        self,
        url: str = "redis://127.0.0.1:6379/0",
        prefix: str = "sf:",
        client: Any | None = None,
    ) -> None:
        self._prefix = prefix
        self._runner = _LoopThread()
        if client is None:
            try:
                import redis.asyncio as redis_asyncio  # 可选依赖：延迟导入
            except ImportError as exc:  # pragma: no cover - 依赖安装环境相关
                raise CacheBackendError(
                    "redis 模块未安装，无法使用 Redis 缓存后端（已降级 memory）"
                ) from exc
            try:
                client = redis_asyncio.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=_DEFAULT_TIMEOUT,
                )
            except Exception as exc:
                raise CacheBackendError(f"Redis 客户端创建失败: {exc}") from exc
        self._client = client
        # 连接校验：init 即探测（模块缺失 / 地址不可达都走上层降级）
        try:
            self._call(self._client.ping())
        except Exception as exc:
            raise CacheBackendError(f"Redis 连接失败（{_safe_url(url)}）: {exc}") from exc
        logger.info("缓存后端：redis（%s，prefix=%r）", _safe_url(url), prefix)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _call(self, coro: Any, timeout: float = _DEFAULT_TIMEOUT) -> Any:
        """把异步操作调度到专用事件循环并阻塞等待结果；失败统一抛 CacheBackendError。"""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._runner.loop)
            return future.result(timeout=timeout)
        except Exception as exc:
            raise CacheBackendError(f"Redis 操作失败: {exc}") from exc

    def get(self, key: str) -> str | None:
        """取值（键自动加前缀）；缺失 / 过期返回 None。"""
        return self._call(self._client.get(self._key(key)))

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """写入；``ttl_seconds > 0`` 时经 Redis ``expire`` 设置过期。"""
        raw_key = self._key(key)
        self._call(self._client.set(raw_key, value))
        if ttl_seconds > 0:
            self._call(self._client.expire(raw_key, int(ttl_seconds)))

    def delete(self, key: str) -> None:
        """删除单个键。"""
        self._call(self._client.delete(self._key(key)))

    def clear(self) -> None:
        """删除本前缀下全部键（KEYS + DEL；仅测试 / 小规模场景使用）。"""
        keys = self._call(self._client.keys(f"{self._prefix}*")) or []
        if keys:
            self._call(self._client.delete(*keys))

    def items(self) -> list[tuple[str, str]]:
        """返回本前缀下全部 ``(去前缀键, 值)``（KEYS + 逐个 GET，规模有限可接受）。"""
        keys = self._call(self._client.keys(f"{self._prefix}*")) or []
        result: list[tuple[str, str]] = []
        for raw_key in keys:
            value = self._call(self._client.get(raw_key))
            if value is not None:
                result.append((raw_key[len(self._prefix) :], value))
        return result

    def size(self) -> None:
        """Redis 侧不做精确统计（容量由 maxmemory 策略管理），返回 None。"""
        return None


def _safe_url(url: str) -> str:
    """脱敏日志用 URL：去掉 userinfo（防连接串中的密码外泄）。"""
    return url.rsplit("@", 1)[-1]
