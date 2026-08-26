"""五级进程内缓存层（SafeFusion T8）。

呈漏斗第一道防线（PRD §2 ①、§3.1 流程图 K1~K4）：
① 审核缓存（完整键：文本哈希 + 帧哈希排序拼接 + 关键参数，T8 任务卡 §audit_key）；
② 高频缓存（仅无上下文请求，LRU + TTL）；
③ 图片去重缓存（仅单图无文本请求：MD5 精确 + pHash 汉明距离近似两段）；
④ 短文本 LLM 缓存；
⑤ 永久黑白名单（启动时由编排层注入内容哈希，管理端改动后失效重载）。

设计要点：
- 纯进程内实现：``OrderedDict`` 维护条目的 LRU 顺序 + ``time.monotonic`` 计时；
- 每级独立开关，关闭时 get 一律返回 None（不计统计）、put 直接跳过；
- TTL 采用惰性清理（get 时发现过期即删除）；容量超出时 LRU 驱逐最久未用条目；
- 过期清理与驱逐均在锁内进行，线程安全（每级独立 ``threading.RLock``）；
- ``audit_key`` 用 ``json.dumps(..., sort_keys=True)`` + 帧哈希排序，保证参数/帧序无关；
- 后端扩展点：保持本文件公开方法签名不变，将各级内部容器替换为
  Redis 等远端实现即可平滑迁移（``audit_key`` 的稳定序列化保证跨后端键一致）。
"""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

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
    """进程内 LRU + TTL 缓存容器（key → (写入时间, 值)），供审核/高频/短文本三级复用。"""

    def __init__(self, name: str, enabled: bool, capacity: int, ttl: float) -> None:
        self._name = name
        self._enabled = enabled
        self._capacity = capacity
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._writes = 0

    @property
    def enabled(self) -> bool:
        """本级开关状态。"""
        return self._enabled

    def get(self, key: str) -> Any | None:
        """取值；命中返回并更新 LRU 顺序，过期或缺失返回 None。

        关闭时直接返回 None 且不计统计（stats 只反映启用状态下的缓存行为）。
        """
        if not self._enabled:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self._misses += 1
                return None
            ts, value = item
            if self._ttl > 0 and now - ts > self._ttl:
                # 过期惰性清理
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        """写入；同键覆盖视为最近使用，超容量时驱逐最久未用条目。关闭时跳过。"""
        if not self._enabled:
            return
        with self._lock:
            if key in self._store:
                del self._store[key]
            self._store[key] = (time.monotonic(), value)
            self._writes += 1
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def snapshot(self) -> dict[str, int | bool]:
        """统计快照（供 /health 指标摘要）。"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "size": len(self._store),
            }


@dataclass
class _DedupEntry:
    """图片去重缓存条目：md5 精确键 + pHash 近似键 + 结果 + 写入时间。"""

    md5: str | None
    phash: str | None
    value: Any
    ts: float


class _DedupCache:
    """图片去重缓存：MD5 精确命中优先，未命中且启用 pHash 时线性比较汉明距离。

    TODO(二期)：条目量大后可建 ``md5 → phash`` 的哈希桶或空间索引，
    把 pHash 近似查找从 O(n) 降为 O(k)；当前容量 8192 下线性扫描可接受。
    """

    def __init__(
        self,
        name: str,
        enabled: bool,
        capacity: int,
        ttl: float,
        phash_enabled: bool = True,
    ) -> None:
        self._name = name
        self._enabled = enabled
        self._capacity = capacity
        self._ttl = ttl
        self._phash_enabled = phash_enabled
        self._store: OrderedDict[str, _DedupEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._md5_hits = 0
        self._phash_hits = 0

    def put(self, md5: str | None, phash: str | None, value: Any) -> None:
        """写入去重条目；md5 与 phash 至少一个非 None。关闭时跳过。"""
        if not self._enabled:
            return
        if md5 is None and phash is None:
            raise ValueError("put_dedup 至少需要 md5 或 phash 之一")
        with self._lock:
            key = f"m:{md5}" if md5 is not None else f"p:{phash}"
            if key in self._store:
                del self._store[key]
            self._store[key] = _DedupEntry(md5=md5, phash=phash, value=value, ts=time.monotonic())
            self._writes += 1
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)

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
        now = time.monotonic()
        with self._lock:
            if md5 is not None:
                entry = self._store.get(f"m:{md5}")
                if entry is not None:
                    if self._ttl > 0 and now - entry.ts > self._ttl:
                        del self._store[f"m:{md5}"]
                    else:
                        self._store.move_to_end(f"m:{md5}")
                        self._hits += 1
                        self._md5_hits += 1
                        return entry.value
            if phash is not None and self._phash_enabled:
                best_key: str | None = None
                best_entry: _DedupEntry | None = None
                best_dist = max_distance + 1
                for key, entry in list(self._store.items()):
                    if entry.phash is None or len(entry.phash) != len(phash):
                        # 无 pHash 或哈希规格（长度）不同 → 不参与近似比较
                        continue
                    if self._ttl > 0 and now - entry.ts > self._ttl:
                        del self._store[key]
                        continue
                    dist = _hamming_distance(entry.phash, phash)
                    if dist <= max_distance and dist < best_dist:
                        best_key, best_entry, best_dist = key, entry, dist
                if best_entry is not None:
                    self._store.move_to_end(best_key)
                    self._hits += 1
                    self._phash_hits += 1
                    return best_entry.value
            self._misses += 1
            return None

    def snapshot(self) -> dict[str, int | bool]:
        """统计快照（含 md5/phash 细分命中数）。"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "size": len(self._store),
                "md5_hits": self._md5_hits,
                "phash_hits": self._phash_hits,
            }


class CacheLayer:
    """五级进程内缓存层（审核 / 高频 / 图片去重 / 短文本 LLM / 永久黑白名单）。

    配置结构（任务卡默认，兼容 T1 ``CacheConfig`` 键名与 ``capacity`` 字段）：:

        {
            "audit_cache":     {"enabled": true, "ttl": 3600, "max_size": 4096},
            "high_freq":       {"enabled": true, "ttl": 300,  "max_size": 1000},
            "dedup":           {"enabled": true, "ttl": 86400, "max_size": 8192,
                                "phash_enabled": true},
            "short_text_llm":  {"enabled": true, "ttl": 86400, "max_size": 2000},
            "permanent_lists": true,
        }

    - ``permanent_lists`` 可为 bool 或 ``{"enabled": bool}``；
    - 键别名：``high_freq`` ↔ ``high_freq_cache``、``dedup`` ↔ ``dedup_cache``、
      ``short_text_llm`` ↔ ``short_text_llm_cache``，容量键 ``max_size`` ↔ ``capacity``；
    - 单图无文本 / 无上下文等调用约束由编排层（T9）把控，本层不校验。
    """

    def __init__(self, cfg: dict | None = None) -> None:
        """按 ``cfg`` 构建五级缓存；``cfg`` 为空时全部启用并使用默认容量/TTL。"""
        root: dict = cfg if isinstance(cfg, dict) else {}
        audit_sub = _pick_sub(root, _KEY_ALIASES["audit_cache"])
        self._audit = _TTLCache(
            "audit_cache",
            _as_bool(audit_sub.get("enabled"), True),
            _capacity(audit_sub, _DEFAULTS["audit_cache"]["max_size"]),
            _ttl(audit_sub, _DEFAULTS["audit_cache"]["ttl"]),
        )
        high_sub = _pick_sub(root, _KEY_ALIASES["high_freq"])
        self._high_freq = _TTLCache(
            "high_freq",
            _as_bool(high_sub.get("enabled"), True),
            _capacity(high_sub, _DEFAULTS["high_freq"]["max_size"]),
            _ttl(high_sub, _DEFAULTS["high_freq"]["ttl"]),
        )
        dedup_sub = _pick_sub(root, _KEY_ALIASES["dedup"])
        self._dedup = _DedupCache(
            "dedup",
            _as_bool(dedup_sub.get("enabled"), True),
            _capacity(dedup_sub, _DEFAULTS["dedup"]["max_size"]),
            _ttl(dedup_sub, _DEFAULTS["dedup"]["ttl"]),
            _as_bool(dedup_sub.get("phash_enabled"), True),
        )
        short_sub = _pick_sub(root, _KEY_ALIASES["short_text_llm"])
        self._short_text_llm = _TTLCache(
            "short_text_llm",
            _as_bool(short_sub.get("enabled"), True),
            _capacity(short_sub, _DEFAULTS["short_text_llm"]["max_size"]),
            _ttl(short_sub, _DEFAULTS["short_text_llm"]["ttl"]),
        )
        perm_raw = root.get("permanent_lists", True)
        self._perm_enabled = _as_bool(perm_raw) if isinstance(perm_raw, dict) else bool(perm_raw)
        self._black: set[str] = set()
        self._white: set[str] = set()
        self._perm_lock = threading.RLock()

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
        """各级缓存统计摘要（供 /health 指标）：命中/未命中/写入次数与当前容量。"""
        return {
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
