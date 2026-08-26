"""五级缓存层（SafeFusion T8，v0.2 后端可插拔）。

呈漏斗第一道防线（PRD §2 ①、§3.1 流程图 K1~K4）：
① 审核缓存（完整键：文本哈希 + 帧哈希排序拼接 + 关键参数，T8 任务卡 §audit_key）；
② 高频缓存（仅无上下文请求，LRU + TTL）；
③ 图片去重缓存（仅单图无文本请求：MD5 精确 + pHash 汉明距离近似两段）；
④ 短文本 LLM 缓存；
⑤ 永久黑白名单（启动时由编排层注入内容哈希，管理端改动后失效重载）。

v0.2（M5）后端抽象（详见 :mod:`safefusion.cache.backends`）：
- 五级存取委托 ``CacheBackend``（键值均为已序列化字符串）；
- ``memory``（默认，进程内 OrderedDict + monotonic LRU/TTL，迁移 v0.1 现状）
  与 ``redis``（redis.asyncio，键加 ``sf:`` 前缀，TTL 走 expire）双后端，
  按 ``config.cache.backend`` 切换（config.py 已提供 backend/redis 键）；
- Redis 不可达（init 或首次调用失败）自动降级 memory 并 record warning，
  降级标记写入 ``CacheLayer.degraded_backend``。

其余设计要点：
- 每级独立开关，关闭时 get 一律返回 None（不计统计）、put 直接跳过；
- TTL 惰性清理（访问 / 快照时发现过期即删除）；memory 后端容量超出时 LRU 驱逐；
- ``audit_key`` 用 ``json.dumps(..., sort_keys=True)`` + 帧哈希排序，
  保证参数 / 帧序无关（稳定序列化保证跨后端键一致）。
"""

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from .backends import CacheBackend, CacheBackendError, MemoryBackend, RedisBackend

logger = logging.getLogger("safefusion.cache")

#: 配置键兼容表：任务卡键名 → T1 ``config.py`` CacheConfig 的键名
#: （``max_size`` ↔ ``capacity``、``high_freq`` ↔ ``high_freq_cache`` 等）。
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "audit_cache": ("audit_cache", "audit"),
    "high_freq": ("high_freq", "high_freq_cache"),
    "dedup": ("dedup", "dedup_cache"),
    "short_text_llm": ("short_text_llm", "short_text_llm_cache"),
}

#: 各级默认配置（任务卡默认值优先，未给出者沿用 T1 config.py 默认）
_DEFAULTS: dict[str, dict[str, Any]] = {
    "audit_cache": {"enabled": True, "ttl": 3600.0, "max_size": 4096},
    "high_freq": {"enabled": True, "ttl": 300.0, "max_size": 1000},
    "dedup": {"enabled": True, "ttl": 86400.0, "max_size": 8192, "phash_enabled": True},
    "short_text_llm": {"enabled": True, "ttl": 86400.0, "max_size": 2000},
}


def _jsonable(obj: Any) -> Any:
    """把 pydantic 模型等对象转换为 JSON 可序列化结构（audit_key 的 params 使用）。

    - pydantic v2 模型 → ``model_dump(exclude_none=True)``（仅非 None 字段参与键，
      与编排层只把“实际设置过的覆盖”计入参数摘要的语义一致）；
    - dict / list / tuple 递归转换；
    - bytes → hex 字符串；
    - 其余（str/int/float/bool/None）原样返回。
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def _serialize(value: Any) -> str:
    """值 → 已序列化字符串（跨后端统一存储形态；dict 值须 JSON 可序列化）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _deserialize(raw: str) -> Any:
    """已序列化字符串 → 值。"""
    return json.loads(raw)


def _hamming_distance(a: str, b: str) -> int:
    """计算两个 pHash 十六进制串的位级汉明距离。"""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _as_bool(value: Any, default: bool = True) -> bool:
    """宽松布尔解析：bool 直取；字符串识别 false/0/no/off/空；其余回退默认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return default if value is None else bool(value)


def _pick_sub(root: dict, names: tuple[str, ...]) -> dict:
    """按别名顺序取子配置 dict；不存在或非 dict 返回空 dict。"""
    for name in names:
        value = root.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _capacity(sub: dict, default: int) -> int:
    """提取容量（兼容任务卡 ``max_size`` 与 T1 ``capacity``），下限 1。"""
    return max(1, int(sub.get("max_size", sub.get("capacity", default))))


def _ttl(sub: dict, default: float) -> float:
    """提取 TTL（秒）；<=0 表示永不过期。"""
    return max(0.0, float(sub.get("ttl", default)))


class _TTLCache:
    """LRU + TTL 缓存容器（审核 / 高频 / 短文本三级复用）。

    开关与统计在本层维护，存取（含 LRU 顺序、TTL、容量驱逐）委托给
    ``CacheBackend``：MemoryBackend 保留 v0.1 LRU/TTL 语义；Redis 后端 TTL 由
    Redis 侧控制，容量由 ``maxmemory`` 策略管理。后端调用失败时该级就地降级
    为进程内 memory 后端（不崩，经回调上报降级）。
    """

    def __init__(
        self,
        name: str,
        enabled: bool,
        capacity: int,
        ttl: float,
        backend: CacheBackend,
        on_degrade: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._name = name
        self._enabled = enabled
        self._capacity = capacity
        self._ttl = ttl
        self._backend = backend
        self._on_degrade = on_degrade
        self._hits = 0
        self._misses = 0
        self._writes = 0

    @property
    def enabled(self) -> bool:
        """本级开关状态。"""
        return self._enabled

    def _fallback(self, exc: Exception) -> None:
        """后端调用失败：该级降级为进程内 memory 后端并上报（不崩）。"""
        logger.warning("缓存级 %s 后端不可用（%s），该级降级为 memory", self._name, exc)
        self._backend = MemoryBackend(self._capacity)
        if self._on_degrade is not None:
            self._on_degrade(self._name, exc)

    def get(self, key: str) -> Any | None:
        """取值；命中返回并刷新 LRU 顺序，过期或缺失返回 None。

        关闭时直接返回 None 且不计统计（stats 只反映启用状态下的缓存行为）。
        """
        if not self._enabled:
            return None
        try:
            raw = self._backend.get(key)
        except Exception as exc:
            self._fallback(exc)
            self._misses += 1
            return None
        if raw is None:
            self._misses += 1
            return None
        try:
            value = _deserialize(raw)
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.debug("缓存级 %s 反序列化失败（键 %s）: %s", self._name, key, exc)
            self._misses += 1
            with suppress(Exception):
                self._backend.delete(key)
            return None
        self._hits += 1
        return value

    def put(self, key: str, value: Any) -> None:
        """写入；同键覆盖视为最近使用。关闭时跳过。"""
        if not self._enabled:
            return
        try:
            self._backend.set(key, _serialize(value), self._ttl)
        except Exception as exc:
            self._fallback(exc)
            # 降级后重试一次（memory 后端不会重复失败），保证降级当次写入不丢
            try:
                self._backend.set(key, _serialize(value), self._ttl)
            except Exception:
                return
        self._writes += 1

    def snapshot(self) -> dict[str, int | bool | None]:
        """统计快照（供 /health 指标摘要）；size 为后端当前条目数（Redis 为 None）。"""
        try:
            size = self._backend.size()
        except Exception:
            size = None
        return {
            "enabled": self._enabled,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "size": size,
        }


@dataclass
class _DedupEntry:
    """图片去重缓存条目（序列化为 JSON 存储；TTL 由后端控制）。"""

    md5: str | None
    phash: str | None
    value: Any

    def to_json(self) -> str:
        """序列化为存储字符串。"""
        return _serialize({"md5": self.md5, "phash": self.phash, "value": self.value})

    @classmethod
    def from_json(cls, raw: str) -> "_DedupEntry":
        """从存储字符串还原（缺字段视为 None）。"""
        data = _deserialize(raw)
        return cls(md5=data.get("md5"), phash=data.get("phash"), value=data.get("value"))


class _DedupCache:
    """图片去重缓存：MD5 精确命中优先，未命中且启用 pHash 时线性比较汉明距离。

    存储委托 ``CacheBackend``：精确命中走 ``get("m:{md5}")``；pHash 近似扫描取
    ``backend.items()`` 全量快照逐条比较。默认容量 8192 下线性扫描可接受；
    条目量大后可建 md5 → phash 哈希桶把近似查找降为 O(k)（TODO 二期）。
    """

    def __init__(
        self,
        name: str,
        enabled: bool,
        capacity: int,
        ttl: float,
        phash_enabled: bool,
        backend: CacheBackend,
        on_degrade: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._name = name
        self._enabled = enabled
        self._capacity = capacity
        self._ttl = ttl
        self._phash_enabled = phash_enabled
        self._backend = backend
        self._on_degrade = on_degrade
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._md5_hits = 0
        self._phash_hits = 0

    def _fallback(self, exc: Exception) -> None:
        """后端调用失败：该级降级为进程内 memory 后端并上报（不崩）。"""
        logger.warning("缓存级 %s 后端不可用（%s），该级降级为 memory", self._name, exc)
        self._backend = MemoryBackend(self._capacity)
        if self._on_degrade is not None:
            self._on_degrade(self._name, exc)

    def _parse(self, raw: str) -> _DedupEntry | None:
        """解析存储条目；无法解析返回 None（不中断扫描）。"""
        try:
            return _DedupEntry.from_json(raw)
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.debug("去重条目反序列化失败: %s", exc)
            return None

    def put(self, md5: str | None, phash: str | None, value: Any) -> None:
        """写入去重条目；md5 与 phash 至少一个非 None。关闭时跳过。"""
        if not self._enabled:
            return
        if md5 is None and phash is None:
            raise ValueError("put_dedup 至少需要 md5 或 phash 之一")
        entry = _DedupEntry(md5=md5, phash=phash, value=value)
        key = f"m:{md5}" if md5 is not None else f"p:{phash}"
        try:
            self._backend.set(key, entry.to_json(), self._ttl)
        except Exception as exc:
            self._fallback(exc)
            # 降级后重试一次（memory 后端不会重复失败），保证降级当次写入不丢
            try:
                self._backend.set(key, entry.to_json(), self._ttl)
            except Exception:
                return
        self._writes += 1

    def get(
        self,
        *,
        md5: str | None = None,
        phash: str | None = None,
        max_distance: int = 3,
    ) -> Any | None:
        """查询历史结果：优先 MD5 精确命中；未命中且启用 pHash 并给出 ``phash``
        时线性比较汉明距离（取距离最小且 ≤ ``max_distance`` 的条目）。

        Args:
            md5: 图片 MD5 十六进制串；与 ``phash`` 至少提供其一。
            phash: 图片 pHash 十六进制串（用于近似匹配）。
            max_distance: pHash 近似命中汉明距离阈值。

        Raises:
            ValueError: ``md5`` 与 ``phash`` 均为 None。
        """
        if not self._enabled:
            return None
        if md5 is None and phash is None:
            raise ValueError("get_dedup 至少需要 md5 或 phash 之一")
        if md5 is not None:
            try:
                raw = self._backend.get(f"m:{md5}")
            except Exception as exc:
                self._fallback(exc)
                raw = None
            if raw is not None:
                entry = self._parse(raw)
                if entry is not None:
                    self._hits += 1
                    self._md5_hits += 1
                    return entry.value
        if phash is not None and self._phash_enabled:
            try:
                snapshot = self._backend.items()
            except Exception as exc:
                self._fallback(exc)
                snapshot = []
            best_key: str | None = None
            best_entry: _DedupEntry | None = None
            best_dist = max_distance + 1
            for key, raw in snapshot:
                entry = self._parse(raw)
                if entry is None or entry.phash is None or len(entry.phash) != len(phash):
                    continue
                dist = _hamming_distance(entry.phash, phash)
                if dist <= max_distance and dist < best_dist:
                    best_key, best_entry, best_dist = key, entry, dist
            if best_entry is not None:
                # 刷新该条目的 LRU 位置（Redis 后端无本地 LRU 语义，等价于读一次）
                if best_key is not None:
                    with suppress(Exception):
                        self._backend.get(best_key)
                self._hits += 1
                self._phash_hits += 1
                return best_entry.value
        self._misses += 1
        return None

    def snapshot(self) -> dict[str, int | bool | None]:
        """统计快照（含 md5/phash 细分命中数）；size 为后端当前条目数（Redis 为 None）。"""
        try:
            size = self._backend.size()
        except Exception:
            size = None
        return {
            "enabled": self._enabled,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "size": size,
            "md5_hits": self._md5_hits,
            "phash_hits": self._phash_hits,
        }


def _make_backend_factory(root: dict) -> tuple[Callable[[int], CacheBackend], str, str | None]:
    """按 ``root`` 配置选择后端：返回 (后端工厂, 生效后端种类, 降级标记)。

    - ``backend == "redis"``：尝试构建 :class:`RedisBackend`（建连并 ping 校验），
      失败（redis 模块缺失 / 连接不可达）→ 自动降级 memory 并返回降级标记
      ``"redis"``（CacheLayer 记录 warning 与 ``degraded_backend``）；
    - 否则返回 memory 工厂：每级按自身容量新建独立 ``MemoryBackend`` 实例；
      redis 情形返回单个共享实例（容量由 Redis 侧管理），五级共用。
    """
    kind = str(root.get("backend", "memory")).lower()
    if kind == "redis":
        redis_cfg = root.get("redis")
        if not isinstance(redis_cfg, dict):
            redis_cfg = {}
        url = str(redis_cfg.get("url", "redis://127.0.0.1:6379/0"))
        prefix = str(redis_cfg.get("prefix", "sf:"))
        try:
            shared = RedisBackend(url=url, prefix=prefix)
        except CacheBackendError as exc:
            logger.warning("Redis 缓存后端不可用（%s），自动降级为 memory", exc)
            return (lambda capacity: MemoryBackend(capacity)), "memory", "redis"
        return (lambda capacity: shared), "redis", None
    return (lambda capacity: MemoryBackend(capacity)), "memory", None


class CacheLayer:
    """五级缓存层（审核 / 高频 / 图片去重 / 短文本 LLM / 永久黑白名单）。

    配置结构（任务卡默认，兼容 T1 ``CacheConfig`` 键名与 ``capacity`` 字段）：:

        {
            "backend": "memory",    # v0.2：memory | redis
            "redis": {"url": "redis://127.0.0.1:6379/0", "prefix": "sf:"},
            "audit_cache":    {"enabled": true, "ttl": 3600, "max_size": 4096},
            "high_freq":      {"enabled": true, "ttl": 300,  "max_size": 1000},
            "dedup":          {"enabled": true, "ttl": 86400, "max_size": 8192,
                               "phash_enabled": true},
            "short_text_llm": {"enabled": true, "ttl": 86400, "max_size": 2000},
            "permanent_lists": true,
        }

    - ``permanent_lists`` 可为 bool 或 ``{"enabled": bool}``；
    - 键别名：``high_freq`` ↔ ``high_freq_cache``、``dedup`` ↔ ``dedup_cache``、
      ``short_text_llm`` ↔ ``short_text_llm_cache``，容量键 ``max_size`` ↔ ``capacity``；
    - 五级存取委托 ``CacheBackend``：显式传入 ``backend`` 实例时五级共享该实例；
      缺省按 ``root["backend"]`` 由 :func:`_make_backend_factory` 选择——redis
      不可达自动降级 memory 并 warning，降级标记写入 ``self.degraded_backend``
      （如 ``"redis"``；未降级为 None）；每级开关与 audit_key/permanent 逻辑不变；
    - stats()：memory 后端返回 v0.1 全量结构；redis（或已降级）后端各容器
      ``size`` 为 None，并追加 ``_meta`` 注明后端与降级状态；
    - 单图无文本 / 无上下文等调用约束由编排层（T9）把控，本层不校验。
    """

    def __init__(
        self,
        cfg: dict | None = None,
        *,
        backend: CacheBackend | None = None,
    ) -> None:
        """按 ``cfg`` 构建五级缓存；``backend`` 缺省时按 ``cfg["backend"]`` 选后端。"""
        root: dict = cfg if isinstance(cfg, dict) else {}
        if backend is not None:

            def factory(_size: int) -> CacheBackend:
                """显式后端：五级共享同一实例。"""
                return backend

            if isinstance(backend, MemoryBackend):
                self._backend_kind = "memory"
            elif isinstance(backend, RedisBackend):
                self._backend_kind = "redis"
            else:
                self._backend_kind = "custom"
            self.degraded_backend: str | None = None
        else:
            factory, self._backend_kind, self.degraded_backend = _make_backend_factory(root)

        audit_sub = _pick_sub(root, _KEY_ALIASES["audit_cache"])
        audit_capacity = _capacity(audit_sub, _DEFAULTS["audit_cache"]["max_size"])
        self._audit = _TTLCache(
            "audit_cache",
            _as_bool(audit_sub.get("enabled"), True),
            audit_capacity,
            _ttl(audit_sub, _DEFAULTS["audit_cache"]["ttl"]),
            backend=factory(audit_capacity),
            on_degrade=self._on_degrade,
        )
        high_sub = _pick_sub(root, _KEY_ALIASES["high_freq"])
        high_capacity = _capacity(high_sub, _DEFAULTS["high_freq"]["max_size"])
        self._high_freq = _TTLCache(
            "high_freq",
            _as_bool(high_sub.get("enabled"), True),
            high_capacity,
            _ttl(high_sub, _DEFAULTS["high_freq"]["ttl"]),
            backend=factory(high_capacity),
            on_degrade=self._on_degrade,
        )
        dedup_sub = _pick_sub(root, _KEY_ALIASES["dedup"])
        dedup_capacity = _capacity(dedup_sub, _DEFAULTS["dedup"]["max_size"])
        self._dedup = _DedupCache(
            "dedup",
            _as_bool(dedup_sub.get("enabled"), True),
            dedup_capacity,
            _ttl(dedup_sub, _DEFAULTS["dedup"]["ttl"]),
            _as_bool(dedup_sub.get("phash_enabled"), True),
            backend=factory(dedup_capacity),
            on_degrade=self._on_degrade,
        )
        short_sub = _pick_sub(root, _KEY_ALIASES["short_text_llm"])
        short_capacity = _capacity(short_sub, _DEFAULTS["short_text_llm"]["max_size"])
        self._short_text_llm = _TTLCache(
            "short_text_llm",
            _as_bool(short_sub.get("enabled"), True),
            short_capacity,
            _ttl(short_sub, _DEFAULTS["short_text_llm"]["ttl"]),
            backend=factory(short_capacity),
            on_degrade=self._on_degrade,
        )
        perm_raw = root.get("permanent_lists", True)
        self._perm_enabled = _as_bool(perm_raw) if isinstance(perm_raw, dict) else bool(perm_raw)
        self._black: set[str] = set()
        self._white: set[str] = set()
        self._perm_lock = threading.RLock()

    def _on_degrade(self, level_name: str, exc: Exception) -> None:
        """（容器回调）运行期后端失败：记录降级标记，保持首次降级信息。"""
        if self._backend_kind != "memory" and self.degraded_backend is None:
            self.degraded_backend = self._backend_kind
            logger.warning(
                "缓存后端 %s 运行期不可用（缓存级 %s，首次：%s），已降级为 memory",
                self._backend_kind,
                level_name,
                exc,
            )

    # ---------- 审核缓存（①） ----------

    def audit_key(self, text_hash: str, frame_hashes: list[str], params: dict) -> str:
        """构造审核缓存键：帧哈希排序 + 文本哈希 + 参数稳定序列化 → sha256 hex。

        与参数顺序无关：``params`` 经 ``json.dumps(sort_keys=True)`` 序列化，
        ``frame_hashes`` 先排序再参与拼接；``params`` 中的 pydantic 模型自动转换。

        Args:
            text_hash: 规范化文本的哈希（可为空串，纯图片请求）。
            frame_hashes: 各帧哈希（内容近似帧顺序无关，此处排序保证等价）。
            params: 关键参数（如 ``{"skip_llm": ...}``、overrides 摘要），须 JSON 可序列化。
        """
        frames = sorted(frame_hashes)
        payload = json.dumps(
            {"text": text_hash, "frames": frames, "params": _jsonable(params)},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_audit_result(self, key: str) -> dict | None:
        """读取审核缓存（key 由 :meth:`audit_key` 生成）；未命中/过期返回 None。"""
        value = self._audit.get(key)
        return value if isinstance(value, dict) else None

    def put_audit_result(self, key: str, result: dict) -> None:
        """写入审核缓存（result 必须是可序列化 dict；关闭时跳过）。"""
        self._audit.put(key, result)

    # ---------- 高频缓存（②，无上下文请求专用） ----------

    def get_high_freq(self, text_hash: str) -> dict | None:
        """读取高频缓存（键为文本哈希）；未命中/过期返回 None。"""
        value = self._high_freq.get(text_hash)
        return value if isinstance(value, dict) else None

    def put_high_freq(self, text_hash: str, result: dict) -> None:
        """写入高频缓存；关闭时跳过。"""
        self._high_freq.put(text_hash, result)

    # ---------- 图片去重缓存（③，仅单图无文本请求） ----------

    def put_dedup(self, md5: str | None, phash_hex: str | None, result: dict) -> None:
        """写入单图审核结果；md5 与 phash_hex 至少一个非 None。

        Args:
            md5: 图片 MD5（十六进制）；为 None 时以 phash_hex 作主键。
            phash_hex: 图片 pHash（十六进制）；为 None 则不参与近似匹配。
            result: 该图片的历史审核结果（dict）。
        """
        self._dedup.put(md5, phash_hex, result)

    def get_dedup(
        self,
        *,
        md5: str | None = None,
        phash: str | None = None,
        max_distance: int = 3,
    ) -> dict | None:
        """查询图片历史结果：MD5 精确命中优先；未命中且启用 pHash 时按汉明距离近似。

        Args:
            md5: 图片 MD5；与 ``phash`` 至少提供其一（都无则抛 ValueError）。
            phash: 图片 pHash（近似匹配用）。
            max_distance: 近似命中汉明距离阈值（默认 3，可配置）。
        """
        value = self._dedup.get(md5=md5, phash=phash, max_distance=max_distance)
        return value if isinstance(value, dict) else None

    # ---------- 短文本 LLM 缓存（④） ----------

    def get_short_text_llm(self, text_hash: str) -> dict | None:
        """读取短文本 LLM 缓存（键为文本哈希）；未命中/过期返回 None。"""
        value = self._short_text_llm.get(text_hash)
        return value if isinstance(value, dict) else None

    def put_short_text_llm(self, text_hash: str, result: dict) -> None:
        """写入短文本 LLM 缓存（LLM 结构化输出 dict）；关闭时跳过。"""
        self._short_text_llm.put(text_hash, result)

    # ---------- 永久黑白名单（⑤） ----------

    def load_permanent(self, black: list[str], white: list[str]) -> None:
        """覆盖式注入永久黑白名单（元素为内容哈希；启动时由编排层调用）。

        直接替换内部集合（全量重载语义），重复元素经 set 去重。
        """
        with self._perm_lock:
            if not self._perm_enabled:
                return
            self._black = set(black)
            self._white = set(white)
            logger.debug("永久名单载入: black=%d white=%d", len(self._black), len(self._white))

    def check_permanent(self, content_hash: str) -> Literal["black", "white"] | None:
        """检查内容哈希是否命中永久名单；黑名单优先（更严格），未命中返回 None。

        监控名单被关闭时恒返回 None（直通）。
        """
        if not self._perm_enabled:
            return None
        with self._perm_lock:
            if content_hash in self._black:
                return "black"
            if content_hash in self._white:
                return "white"
            return None

    def invalidate_permanent(self) -> None:
        """清空永久名单（管理端写库后调用；编排层应随后重新 load_permanent）。"""
        with self._perm_lock:
            self._black.clear()
            self._white.clear()
            logger.info("永久黑白名单已失效，等待重新加载")

    # ---------- 统计 ----------

    def stats(self) -> dict[str, dict[str, Any]]:
        """各级缓存统计摘要（供 /health 指标）：命中/未命中/写入次数与当前容量。

        memory 后端返回 v0.1 全量结构；redis（或已降级）后端各容器 ``size``
        为 None（Redis 侧不精确统计），并追加 ``_meta`` 注明后端与降级状态。
        """
        result = {
            "audit_cache": self._audit.snapshot(),
            "high_freq": self._high_freq.snapshot(),
            "dedup": self._dedup.snapshot(),
            "short_text_llm": self._short_text_llm.snapshot(),
            "permanent_lists": {
                "enabled": self._perm_enabled,
                "black": len(self._black),
                "white": len(self._white),
            },
        }
        if self._backend_kind != "memory" or self.degraded_backend is not None:
            if self.degraded_backend is not None:
                note = f"已从 {self.degraded_backend} 降级为 memory（原后端不可用）"
            else:
                note = "Redis 后端：LRU/容量由 Redis 侧管理，size 不精确统计（None）"
            result["_meta"] = {
                "backend": self._backend_kind,
                "degraded_backend": self.degraded_backend,
                "note": note,
            }
        return result
