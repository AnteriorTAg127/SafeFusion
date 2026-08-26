"""五级缓存层测试：键稳定性 / LRU / TTL / 开关 / 去重两段 / 永久名单黑优先 / stats。

对应 T8 任务卡验收：键稳定性（参数顺序无关）、TTL 过期、各开关关闭时直通。
"""

from __future__ import annotations

import time

import pytest

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
