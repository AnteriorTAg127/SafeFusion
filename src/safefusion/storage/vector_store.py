"""自研 numpy 向量库：黑白分池、内存余弦检索与 npz+JSON 持久化。

设计要点（对齐 PRD §3.3 / §6 与 开发/v0.1/分工.md 统一接口契约）：
- 每个池的向量以 ``(n, d) float32`` 矩阵常驻内存，行级 L2 归一化，
  余弦相似度等价于点积（``vectors @ query``）；
- Top-K：``np.argpartition`` 预筛出 ≥2K（最少 1024）个候选，
  再用大小为 K 的小顶堆精确挑选最大 K 个；
- 写操作用 ``threading.Lock`` 互斥，且采用 copy-on-write：``add`` 重建
  不可变 ``_PoolData`` 并在持锁时整体替换引用，读侧对旧快照无锁计算，
  天然线程安全（无 SELECT-then-WRITE 竞态）；
- 持久化：每池一个 ``<pool>.npz``（向量矩阵）+ ``<pool>.meta.json``
  （``{"ids": [...], "meta": {id: {...}}}``，ids 与矩阵行序一一对应）。

Note:
    ``load`` 信任持久化文件内容（假定由本类 ``save`` 产出），不重复归一化。
"""

import heapq
import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from ..logging_setup import get_logger

_logger = get_logger("storage.vector_store")

#: 池名全集：black（违规语料）/ white（安全语料）
POOLS: tuple[str, str] = ("black", "white")

#: L2 范数小于该值视为零向量（余弦相似度无定义，按零向量原样保留）
_ZERO_NORM_EPS: float = 1e-8

#: argpartition 预筛下限：候选数取 max(2*top_k, 该值)，再小堆精排
_MIN_PRESCREEN: int = 1024


class VectorItem(NamedTuple):
    """待入库向量条目（统一接口契约）。

    Attributes:
        id: 条目唯一标识（如语料行号 / 图片 md5）。
        pool: 所属池，``black`` 或 ``white``。
        vector: 原始向量（任意形状一维数组，自动转 float32 并 L2 归一化）。
        metadata: 随行元数据（如类别、来源），检索命中时原样返回。
    """

    id: str
    pool: str
    vector: np.ndarray
    metadata: dict[str, Any]


class SearchHit(NamedTuple):
    """检索命中结果（统一接口契约）。

    Attributes:
        id: 命中的向量条目 id。
        score: 与查询向量的余弦相似度（0~1）。
        metadata: 命中条目的元数据（浅拷贝）。
    """

    id: str
    score: float
    metadata: dict[str, Any]


class _PoolData(NamedTuple):
    """单个池的不可变内存状态（copy-on-write 快照）。

    Attributes:
        vectors: ``(n, d) float32`` 矩阵，行已 L2 归一化。
        ids: 与矩阵行序一一对应的 id 元组。
        meta: ``id -> metadata`` 映射。
    """

    vectors: np.ndarray
    ids: tuple[str, ...]
    meta: dict[str, dict[str, Any]]


def _l2_normalize_rows(mat: np.ndarray) -> np.ndarray:
    """按行 L2 归一化；零行原样保留（除数置 1，避免 NaN）。"""

    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms < _ZERO_NORM_EPS, 1.0, norms)
    return mat / norms


def _group_by_pool(items: list[VectorItem]) -> dict[str, list[VectorItem]]:
    """按池名对条目分组，校验池名合法性。"""

    grouped: dict[str, list[VectorItem]] = {}
    for item in items:
        if item.pool not in POOLS:
            raise ValueError(f"未知池名: {item.pool!r}，可选 {POOLS}（id={item.id}）")
        grouped.setdefault(item.pool, []).append(item)
    return grouped


class BaseVectorStore(ABC):
    """向量库抽象接口（对齐 开发/v0.1/分工.md「统一接口契约」）。

    约定：``add`` / ``search`` / ``save`` / ``load`` / ``count`` 五个方法族，
    池名取值 ``black`` / ``white``；查询向量要求与库内维度一致。
    """

    @abstractmethod
    def add(self, items: list[VectorItem]) -> None:
        """批量入库向量（支持增量追加；同池同 id 重复条目跳过后台）。"""

    @abstractmethod
    def search(self, query: np.ndarray, pool: str, top_k: int) -> list[SearchHit]:
        """在指定池检索与 query 余弦相似度最高的 top_k 条。"""

    @abstractmethod
    def save(self) -> None:
        """将当前全部池持久化到构造路径。"""

    @classmethod
    @abstractmethod
    def load(cls, path: str) -> "BaseVectorStore":
        """从持久化路径恢复向量库实例。"""

    @abstractmethod
    def count(self, pool: str) -> int:
        """返回指定池的向量条数（池未初始化时返回 0）。"""


class NumpyVectorStore(BaseVectorStore):
    """numpy 后端向量库（默认后端，PRD §3.3）。

    线程模型：``add`` / ``save`` 持 ``threading.Lock`` 并整体替换不可变
    ``_PoolData`` 快照；``search`` / ``count`` 只读旧快照，无锁执行，
    与写操作互不阻塞也互不干扰。

    Args:
        path: 持久化目录（save 写入 / load 恢复均使用该路径）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state: dict[str, _PoolData] = {}

    def add(self, items: list[VectorItem]) -> None:
        """批量入库向量，支持增量追加。

        Args:
            items: 待入库条目列表（空列表为无操作）。

        Raises:
            ValueError: 池名未知 / 向量非一维 / 与池内既有维度不一致 / 同批维度不一致。
        """

        if not items:
            return
        with self._lock:
            grouped = _group_by_pool(items)
            new_state: dict[str, _PoolData] = dict(self._state)
            for pool, chunk in grouped.items():
                new_state[pool] = self._merge_pool(pool, chunk, new_state.get(pool))
            self._state = new_state

    def _merge_pool(
        self, pool: str, chunk: list[VectorItem], old: _PoolData | None
    ) -> _PoolData:
        """将一批条目并入一个池，返回新的不可变快照（不修改旧快照）。

        池内已存在的 id 跳过并告警（幂等导入不重复、不覆盖）。
        """

        old_vecs = old.vectors if old is not None else None
        old_ids = old.ids if old is not None else ()
        old_meta = old.meta if old is not None else {}
        meta: dict[str, dict[str, Any]] = dict(old_meta)
        vec_list: list[np.ndarray] = []
        ids: list[str] = []
        dim: int | None = int(old_vecs.shape[1]) if old_vecs is not None else None
        for item in chunk:
            if item.id in meta or item.id in ids:
                _logger.warning("向量 id 已存在（pool=%s, id=%s），跳过该条", pool, item.id)
                continue
            vec = np.asarray(item.vector, dtype=np.float32)
            if vec.ndim != 1:
                raise ValueError(f"向量必须是 1 维，收到 {vec.ndim} 维（id={item.id}）")
            if dim is None:
                dim = int(vec.shape[0])
            elif vec.shape[0] != dim:
                raise ValueError(f"维度不一致：期望 {dim}，收到 {vec.shape[0]}（id={item.id}）")
            vec_list.append(vec)
            ids.append(item.id)
            meta[item.id] = dict(item.metadata)
        if not vec_list:
            # 全部重复只可能发生在池已存在时（池为空则无重复可言），old 必非 None
            assert old is not None
            return old
        new_vecs = _l2_normalize_rows(np.stack(vec_list, axis=0))
        if old_vecs is not None:
            new_vecs = np.concatenate([old_vecs, new_vecs], axis=0)
        return _PoolData(new_vecs, (*old_ids, *ids), meta)

    def search(self, query: np.ndarray, pool: str, top_k: int) -> list[SearchHit]:
        """在指定池检索 Top-K（余弦相似度=点积，行与查询均 L2 归一化）。

        Args:
            query: 查询向量（任意一维数组，自动转 float32 并归一化；
                零向量无相似度定义，返回空列表）。
            pool: 池名，``black`` 或 ``white``。
            top_k: 返回条数（>= 1；超过池内条数时返回全部）。

        Raises:
            ValueError: 池名未知或 ``top_k < 1``。
        """

        if pool not in POOLS:
            raise ValueError(f"未知池名: {pool!r}，可选 {POOLS}")
        if top_k < 1:
            raise ValueError(f"top_k 必须 >= 1，收到 {top_k}")
        data = self._state.get(pool)
        if data is None or data.vectors.shape[0] == 0:
            return []
        q = np.asarray(query, dtype=np.float32).ravel()
        q_norm = float(np.linalg.norm(q))
        if q_norm < _ZERO_NORM_EPS:
            return []
        scores = data.vectors @ (q / q_norm)
        n = int(scores.shape[0])
        k = min(top_k, n)
        cand_size = min(n, max(2 * k, _MIN_PRESCREEN))
        cand_idx = np.argpartition(scores, -cand_size)[-cand_size:]
        # 小顶堆精排：堆顶为当前第 K 大，仅大于堆顶的候选替换入堆
        heap: list[tuple[float, int]] = []
        for idx in cand_idx.tolist():
            score = float(scores[idx])
            if len(heap) < k:
                heapq.heappush(heap, (score, idx))
            elif score > heap[0][0]:
                heapq.heapreplace(heap, (score, idx))
        heap.sort(reverse=True)
        return [
            SearchHit(data.ids[idx], score, dict(data.meta[data.ids[idx]]))
            for score, idx in heap
        ]

    def save(self) -> None:
        """持久化全部池到构造路径：每池 ``<pool>.npz`` + ``<pool>.meta.json``。"""

        self._path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            snapshot = dict(self._state)
        for pool, data in snapshot.items():
            np.savez(self._path / f"{pool}.npz", vectors=data.vectors)
            payload = {"ids": list(data.ids), "meta": data.meta}
            (self._path / f"{pool}.meta.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    @classmethod
    def load(cls, path: str) -> "NumpyVectorStore":
        """从持久化目录恢复向量库（缺失的池保持为空）。

        Args:
            path: 与构造时一致的持久化目录，含 ``<pool>.npz`` 与 ``<pool>.meta.json``。

        Raises:
            ValueError: 矩阵行数与元数据条数不一致（文件损坏或手工修改）。
        """

        base = Path(path)
        store = cls(base)
        for pool in POOLS:
            npz_path = base / f"{pool}.npz"
            meta_path = base / f"{pool}.meta.json"
            if not npz_path.is_file() or not meta_path.is_file():
                continue
            with np.load(npz_path) as archived:
                vectors = np.asarray(archived["vectors"], dtype=np.float32)
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            ids = tuple(payload["ids"])
            meta = payload["meta"]
            if vectors.shape[0] != len(ids) or vectors.shape[0] != len(meta):
                raise ValueError(
                    f"持久化文件不一致（{base}）：矩阵 {vectors.shape[0]} 行 vs "
                    f"元数据 {len(ids)} id / {len(meta)} 条"
                )
            store._state[pool] = _PoolData(vectors, ids, meta)
        return store

    def count(self, pool: str) -> int:
        """返回指定池的向量条数（池未初始化返回 0）。"""

        data = self._state.get(pool)
        return int(data.vectors.shape[0]) if data is not None else 0
