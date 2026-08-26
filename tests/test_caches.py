"""五级缓存层测试：键稳定性 / LRU / TTL / 开关 / 去重两段 / 永久名单黑优先 / stats。

对应 T8 任务卡验收：键稳定性（参数顺序无关）、TTL 过期、各开关关闭时直通。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from safefusion.cache.backends import CacheBackendError, MemoryBackend, RedisBackend
from safefusion.cache.caches import CacheLayer, _hamming_distance
from safefusion.models.schemas import Overrides


class TestAuditKey:
    """audit_key：参数/帧序无关、参数敏感、pydantic 模型参与键。"""

    def _key(self, cache: CacheLayer, frames: list[str], params: dict) -> str:
        return cache.audit_key("text-hash", frames, params)

    def test_frame_order_independent(self) -> None:
        cache = CacheLayer({})
        assert self._key(cache, ["f1", "f2"], {}) == self._key(cache, ["f2", "f1"], {})

    def test_params_order_independent(self) -> None:
        cache = CacheLayer({})
        a = self._key(cache, ["f"], {"skip_llm": True, "tier": "full"})
        b = self._key(cache, ["f"], {"tier": "full", "skip_llm": True})
        assert a == b

    def test_params_sensitive(self) -> None:
        cache = CacheLayer({})
        key_true = self._key(cache, ["f"], {"skip_llm": True})
        key_false = self._key(cache, ["f"], {"skip_llm": False})
        assert key_true != key_false

    def test_text_hash_sensitive(self) -> None:
        cache = CacheLayer({})
        assert self._key(cache, ["f"], {}) != self._key(cache, ["g"], {})

    def test_pydantic_model_params_equivalent(self) -> None:
        cache = CacheLayer({})
        model_key = cache.audit_key("h", ["f"], {"overrides": Overrides(margin_w=0.1)})
        dict_key = cache.audit_key("h", ["f"], {"overrides": {"margin_w": 0.1}})
        assert model_key == dict_key

    def test_returns_sha256_hex(self) -> None:
        key = CacheLayer({}).audit_key("h", ["f"], {})
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


class TestTTLCacheBehaviors:
    """LRU + TTL：驱逐与过期。"""

    def _cache(self, **cfg) -> CacheLayer:
        return CacheLayer({"audit_cache": cfg})

    def test_lru_eviction(self) -> None:
        cache = self._cache(capacity=2, ttl=0)
        cache.put_audit_result("a", {"v": 1})
        cache.put_audit_result("b", {"v": 2})
        cache.put_audit_result("c", {"v": 3})
        assert cache.get_audit_result("a") is None  # 最久未用被驱逐
        assert cache.get_audit_result("b") is not None
        assert cache.get_audit_result("c") is not None

    def test_lru_refresh_on_get(self) -> None:
        cache = self._cache(capacity=2, ttl=0)
        cache.put_audit_result("a", {"v": 1})
        cache.put_audit_result("b", {"v": 2})
        cache.get_audit_result("a")  # 刷新 a 的 LRU 位置
        cache.put_audit_result("c", {"v": 3})
        assert cache.get_audit_result("a") is not None
        assert cache.get_audit_result("b") is None  # b 被驱逐

    def test_ttl_expiry(self) -> None:
        cache = self._cache(ttl=0.05)
        cache.put_audit_result("k", {"v": 1})
        assert cache.get_audit_result("k") is not None
        time.sleep(0.07)
        assert cache.get_audit_result("k") is None

    def test_ttl_zero_never_expires(self) -> None:
        cache = self._cache(ttl=0)
        cache.put_audit_result("k", {"v": 1})
        assert cache.get_audit_result("k") is not None

    def test_disabled_bypasses(self) -> None:
        cache = self._cache(enabled=False)
        cache.put_audit_result("k", {"v": 1})
        assert cache.get_audit_result("k") is None
        assert cache.get_high_freq("h") is None  # 高频缓存开关联动


class TestHighFreqAndShortText:
    """高频 / 短文本 LLM 缓存基本读写。"""

    def test_high_freq_roundtrip(self) -> None:
        cache = CacheLayer({})
        cache.put_high_freq("hash1", {"result": "x"})
        assert cache.get_high_freq("hash1") == {"result": "x"}
        assert cache.get_high_freq("hash2") is None

    def test_short_text_llm_roundtrip(self) -> None:
        cache = CacheLayer({})
        cache.put_short_text_llm("h", {"is_violation": True})
        assert cache.get_short_text_llm("h") == {"is_violation": True}


class TestDedupCache:
    """图片去重：MD5 精确 + pHash 近似两段。"""

    def _cache(self, **cfg) -> CacheLayer:
        return CacheLayer({"dedup": cfg})

    def test_md5_exact_hit(self) -> None:
        cache = self._cache()
        cache.put_dedup("md5-1", "abc", {"result": 1})
        assert cache.get_dedup(md5="md5-1") == {"result": 1}

    def test_phash_approximate_hit(self) -> None:
        cache = self._cache()
        # 32 位十六进制串：末位 0 vs 1 → 汉明距离 1（阈值内）
        cache.put_dedup("md5-a", "0" * 63 + "0", {"result": "base"})
        assert cache.get_dedup(phash="0" * 63 + "1", max_distance=1) == {"result": "base"}
        assert cache.get_dedup(phash="0" * 63 + "1", max_distance=0) is None  # 超阈值

    def test_md5_priority_over_phash(self) -> None:
        cache = self._cache()
        cache.put_dedup("m1", "a", {"result": "exact"})
        cache.put_dedup("m2", "b", {"result": "approx"})
        # m1 精确命中返回 exact；pHash 距离再近也轮不到 b
        assert cache.get_dedup(md5="m1", phash="b") == {"result": "exact"}

    def test_put_requires_key(self) -> None:
        with pytest.raises(ValueError):
            CacheLayer({}).put_dedup(None, None, {})
        with pytest.raises(ValueError):
            CacheLayer({}).get_dedup()

    def test_phash_disabled_no_approx(self) -> None:
        cache = self._cache(phash_enabled=False)
        cache.put_dedup("m1", "0" * 63 + "0", {"result": 1})
        assert cache.get_dedup(phash="0" * 63 + "1", max_distance=1) is None
        assert cache.get_dedup(md5="m1") == {"result": 1}  # 精确命中不受影响

    def test_stats_breakdown(self) -> None:
        cache = self._cache()
        cache.put_dedup("m1", "p1", {"r": 1})
        cache.get_dedup(md5="m1")
        cache.get_dedup(md5="m1")
        stats = cache.stats()["dedup"]
        assert stats["md5_hits"] == 2
        assert stats["phash_hits"] == 0
        assert stats["size"] == 1

    def test_ttl_expiry(self) -> None:
        cache = self._cache(ttl=0.05)
        cache.put_dedup("m1", None, {"r": 1})
        assert cache.get_dedup(md5="m1") is not None
        time.sleep(0.07)
        assert cache.get_dedup(md5="m1") is None


class TestPermanentLists:
    """永久黑白名单：黑优先 / 开关 / 失效重载。"""

    def test_black_priority(self) -> None:
        cache = CacheLayer({})
        cache.load_permanent(["h-black"], ["h-black"])  # 同一哈希既黑又白
        assert cache.check_permanent("h-black") == "black"
        assert cache.check_permanent("h-white") is None

    def test_white_hit(self) -> None:
        cache = CacheLayer({})
        cache.load_permanent([], ["h-white"])
        assert cache.check_permanent("h-white") == "white"

    def test_disabled_returns_none(self) -> None:
        cache = CacheLayer({"permanent_lists": False})
        cache.load_permanent(["h"], [])
        assert cache.check_permanent("h") is None

    def test_invalidate(self) -> None:
        cache = CacheLayer({})
        cache.load_permanent(["h"], [])
        cache.invalidate_permanent()
        assert cache.check_permanent("h") is None
        assert cache.stats()["permanent_lists"] == {"enabled": True, "black": 0, "white": 0}

    def test_reload_replaces(self) -> None:
        cache = CacheLayer({})
        cache.load_permanent(["a"], [])
        cache.load_permanent([], ["b"])
        assert cache.check_permanent("a") is None
        assert cache.check_permanent("b") == "white"

    def test_load_when_disabled_noop(self) -> None:
        cache = CacheLayer({"permanent_lists": False})
        cache.load_permanent(["h"], [])
        assert cache.stats()["permanent_lists"]["black"] == 0


class TestStatsAndHelpers:
    """stats 汇总与汉明距离工具。"""

    def test_stats_shape(self) -> None:
        cache = CacheLayer({})
        stats = cache.stats()
        assert set(stats) == {
            "audit_cache",
            "high_freq",
            "dedup",
            "short_text_llm",
            "permanent_lists",
        }
        assert stats["audit_cache"]["enabled"] is True
        cache.get_audit_result("none")  # 一次 miss
        assert cache.stats()["audit_cache"]["misses"] == 1

    def test_hamming_distance(self) -> None:
        assert _hamming_distance("00", "00") == 0
        assert _hamming_distance("00", "ff") == 8  # 8 个 bit 位翻转
        assert _hamming_distance("0f", "f0") == 8

    def test_aliases_accept_task_keys(self) -> None:
        # 任务卡键名（high_freq / max_size）与 T1 capacity 键兼容
        cache = CacheLayer({"high_freq": {"enabled": True, "max_size": 5, "ttl": 10}})
        assert cache.stats()["high_freq"]["enabled"] is True
        cache2 = CacheLayer({"high_freq_cache": {"enabled": True, "capacity": 5, "ttl": 10}})
        assert cache2.stats()["high_freq"]["enabled"] is True


class _FakeRedis:
    """内存假 Redis：满足 RedisBackend 使用的异步 get/set/expire/delete/keys/ping 语义。

    - ``expire`` 记录截止时刻，``get`` 命中过期键返回 None 并清除（模拟 Redis TTL）；
    - ``broken`` 置 True 后所有调用抛 ConnectionError（模拟运行期断连，测降级路径）。
    """

    def __init__(self, broken: bool = False) -> None:
        self._store: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("模拟 Redis 连接中断")

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> str | None:
        self._check()
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() > deadline:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._store.get(key)

    async def set(self, key: str, value: str, *args: Any, **kwargs: Any) -> None:
        self._check()
        self._store[key] = value
        self._expiry.pop(key, None)

    async def expire(self, key: str, seconds: int) -> None:
        self._check()
        self._expiry[key] = time.monotonic() + seconds

    async def delete(self, *keys: str) -> int:
        self._check()
        removed = 0
        for key in keys:
            if key in self._store:
                self._store.pop(key, None)
                self._expiry.pop(key, None)
                removed += 1
        return removed

    async def keys(self, pattern: str = "*") -> list[str]:
        import fnmatch

        self._check()
        return [key for key in list(self._store) if fnmatch.fnmatch(key, pattern)]


class TestMemoryBackend:
    """MemoryBackend：LRU 驱逐 / TTL / 删除清理 / 未过期快照。"""

    def test_lru_eviction(self) -> None:
        backend = MemoryBackend(capacity=2)
        backend.set("a", "1", 0)
        backend.set("b", "2", 0)
        backend.get("a")  # 刷新 a 的 LRU 位置
        backend.set("c", "3", 0)
        assert backend.get("a") == "1"
        assert backend.get("b") is None  # b 被驱逐
        assert backend.size() == 2

    def test_ttl_expiry(self) -> None:
        backend = MemoryBackend()
        backend.set("k", "v", 0.05)
        assert backend.get("k") == "v"
        time.sleep(0.07)
        assert backend.get("k") is None
        assert backend.size() == 0

    def test_ttl_zero_never_expires(self) -> None:
        backend = MemoryBackend()
        backend.set("k", "v", 0)
        assert backend.get("k") == "v"

    def test_items_excludes_expired(self) -> None:
        backend = MemoryBackend()
        backend.set("a", "1", 0.05)
        backend.set("b", "2", 0)
        time.sleep(0.07)
        assert backend.items() == [("b", "2")]

    def test_delete_and_clear(self) -> None:
        backend = MemoryBackend()
        backend.set("a", "1", 0)
        backend.set("b", "2", 0)
        backend.delete("a")
        assert backend.get("a") is None
        backend.clear()
        assert backend.size() == 0


class TestRedisBackend:
    """RedisBackend（注入内存假 Redis）：前缀 / TTL 走 expire / 清理 / 失败抛错。"""

    def _backend(self, **kwargs: Any) -> tuple[RedisBackend, _FakeRedis]:
        fake = kwargs.pop("fake", _FakeRedis())
        backend = RedisBackend(url="redis://127.0.0.1:6379/0", prefix="sf:", client=fake)
        return backend, fake

    def test_prefix_applied(self) -> None:
        backend, fake = self._backend()
        backend.set("k", "v", 0)
        assert fake._store == {"sf:k": "v"}
        assert backend.get("k") == "v"
        assert backend.get("missing") is None

    def test_ttl_via_expire(self) -> None:
        backend, fake = self._backend()
        backend.set("k", "v", 30)
        assert "sf:k" in fake._expiry  # TTL 已经 Redis expire 下发
        backend.set("forever", "v", 0)
        assert "sf:forever" not in fake._expiry  # ttl<=0 永不过期
        assert backend.get("k") == "v"

    def test_ttl_expiry_effect(self) -> None:
        backend, _ = self._backend()
        backend.set("k", "v", 0.05)
        assert backend.get("k") == "v"
        time.sleep(0.07)
        assert backend.get("k") is None

    def test_delete(self) -> None:
        backend, _ = self._backend()
        backend.set("k", "v", 0)
        backend.delete("k")
        assert backend.get("k") is None

    def test_clear_only_own_prefix(self) -> None:
        backend, fake = self._backend()
        backend.set("a", "1", 0)
        fake._store["other:x"] = "keep"  # 其它前缀键不受 clear 影响
        backend.clear()
        assert backend.get("a") is None
        assert fake._store == {"other:x": "keep"}

    def test_items_strips_prefix(self) -> None:
        backend, _ = self._backend()
        backend.set("k1", "v1", 0)
        backend.set("k2", "v2", 0)
        assert backend.items() == [("k1", "v1"), ("k2", "v2")]

    def test_size_is_none(self) -> None:
        backend, _ = self._backend()
        assert backend.size() is None

    def test_init_ping_failure_raises(self) -> None:
        with pytest.raises(CacheBackendError):
            RedisBackend(prefix="sf:", client=_FakeRedis(broken=True))

    def test_init_unreachable_url_raises(self) -> None:
        # 天然确定性：本环境 redis 模块缺失直接抛；即便已安装，127.0.0.1:1 也必然拒连
        with pytest.raises(CacheBackendError):
            RedisBackend(url="redis://127.0.0.1:1/0")


class TestRedisCacheLayer:
    """CacheLayer + RedisBackend：五级行为与 memory 等价（除 TTL 由 Redis 侧控制）。"""

    def _layer(self, cfg: dict | None = None) -> tuple[CacheLayer, _FakeRedis]:
        fake = _FakeRedis()
        backend = RedisBackend(prefix="sf:", client=fake)
        return CacheLayer(cfg or {}, backend=backend), fake

    def test_five_levels_equivalent_to_memory(self) -> None:
        cache, _ = self._layer()
        cache.put_audit_result("k1", {"v": 1})
        assert cache.get_audit_result("k1") == {"v": 1}
        cache.put_high_freq("h", {"r": 1})
        assert cache.get_high_freq("h") == {"r": 1}
        cache.put_short_text_llm("s", {"is_violation": True})
        assert cache.get_short_text_llm("s") == {"is_violation": True}
        cache.put_dedup("md5-1", "0" * 63 + "0", {"r": "base"})
        assert cache.get_dedup(md5="md5-1") == {"r": "base"}
        assert cache.get_dedup(phash="0" * 63 + "1", max_distance=1) == {"r": "base"}
        cache.load_permanent(["pb"], ["pw"])
        assert cache.check_permanent("pb") == "black"
        assert cache.check_permanent("pw") == "white"
        assert cache.stats()["permanent_lists"] == {"enabled": True, "black": 1, "white": 1}

    def test_redis_keys_prefixed(self) -> None:
        cache, fake = self._layer()
        cache.put_audit_result("k", {"v": 1})
        assert set(fake._store) == {"sf:k"}
        assert all(key.startswith("sf:") for key in fake._store)

    def test_ttl_controlled_by_redis_side(self) -> None:
        cache, _ = self._layer({"audit_cache": {"ttl": 0.05, "enabled": True}})
        cache.put_audit_result("k", {"v": 1})
        assert cache.get_audit_result("k") == {"v": 1}
        time.sleep(0.07)
        assert cache.get_audit_result("k") is None  # 过期由 Redis 侧 expire 生效

    def test_stats_meta_on_redis(self) -> None:
        cache, _ = self._layer()
        stats = cache.stats()
        assert stats["_meta"]["backend"] == "redis"
        assert stats["_meta"]["degraded_backend"] is None
        assert stats["audit_cache"]["size"] is None  # Redis 侧不精确统计

    def test_runtime_break_falls_back_to_memory(self) -> None:
        cache, fake = self._layer()
        cache.put_high_freq("h2", {"r": 2})
        assert cache.degraded_backend is None
        fake.broken = True
        cache.put_high_freq("h3", {"r": 3})  # 首次失败 → 降级 memory 并重试写成功
        assert cache.degraded_backend == "redis"
        assert cache.get_high_freq("h3") == {"r": 3}  # 降级当次写入不丢
        assert cache.get_high_freq("h2") is None  # 降级前缓存在 Redis，切 memory 后不可见


class TestRedisDegradation:
    """Redis 不可达 → 自动降级 memory + warning + degraded_backend 标记。"""

    def _assert_degraded(self, cache: CacheLayer) -> None:
        assert cache.degraded_backend == "redis"
        cache.put_audit_result("k", {"v": 1})
        assert cache.get_audit_result("k") == {"v": 1}
        meta = cache.stats()["_meta"]
        assert meta["backend"] == "memory"
        assert meta["degraded_backend"] == "redis"

    def test_unreachable_url_degrades(self) -> None:
        # 端口 1：redis 模块缺失直接抛；即便已安装也必然拒连 → 降级路径天然确定
        cache = CacheLayer({"backend": "redis", "redis": {"url": "redis://127.0.0.1:1/0"}})
        self._assert_degraded(cache)

    def test_constructor_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import safefusion.cache.caches as caches_mod

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise CacheBackendError("模拟 Redis 不可达")

        monkeypatch.setattr(caches_mod, "RedisBackend", _boom)
        cache = CacheLayer({"backend": "redis"})
        self._assert_degraded(cache)
