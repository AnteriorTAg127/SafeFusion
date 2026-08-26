"""自研 numpy 向量库测试：余弦正确性 / Top-K 精确性 / save-load 往返 / 空库 /
count / 边界与异常。

对应 T2 任务卡验收：Top-K 与暴力计算一致、save/load 往返一致、空库安全。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from safefusion.storage.vector_store import NumpyVectorStore, VectorItem


def _rng() -> np.random.Generator:
    return np.random.default_rng(42)


def _brute_cosines(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """暴力余弦：与实现相互独立地计算（不经过实现内部的归一化）。"""
    rows = np.asarray(matrix, dtype=np.float64)
    q = np.asarray(query, dtype=np.float64)
    norms = np.linalg.norm(rows, axis=1)
    safe = np.where(norms == 0, 1.0, norms)
    return (rows @ q) / (safe * np.linalg.norm(q))


def _make_items(n: int, pool: str, dim: int = 8, seed: int = 0) -> list[VectorItem]:
    rng = np.random.default_rng(seed)
    items = []
    for i in range(n):
        vec = rng.standard_normal(dim)
        items.append(VectorItem(id=f"{pool}_{i}", pool=pool, vector=vec, metadata={"i": i}))
    return items


class TestSearchCorrectness:
    """查询结果与暴力余弦一致。"""

    def test_cosine_vs_brute_force(self, tmp_path: Path) -> None:
        items = _make_items(50, "black", dim=8)
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(items)
        query = _rng().standard_normal(8)
        hits = store.search(query, "black", 10)
        matrix = np.stack([np.asarray(it.vector, dtype=np.float32) for it in items])
        brute = _brute_cosines(query, matrix)
        order = np.argsort(-brute)[:10]
        assert len(hits) == 10
        for hit, idx in zip(hits, order, strict=True):
            assert hit.score == pytest.approx(float(brute[idx]))
            assert hit.id == f"black_{idx}"

    def test_top_k_exact_ordering(self, tmp_path: Path) -> None:
        items = _make_items(300, "black", dim=4, seed=7)
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(items)
        query = _rng().standard_normal(4)
        hits = store.search(query, "black", 5)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        # 与暴力全排序的精确 Top-5 一致
        matrix = np.stack([np.asarray(it.vector, dtype=np.float32) for it in items])
        brute = _brute_cosines(query, matrix)
        expected = sorted(range(300), key=lambda i: -brute[i])[:5]
        assert [h.id for h in hits] == [f"black_{i}" for i in expected]

    def test_top_k_larger_than_pool_returns_all(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(_make_items(3, "black", dim=4))
        assert len(store.search(_rng().standard_normal(4), "black", 100)) == 3

    def test_zero_vector_query_returns_empty(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(_make_items(5, "black", dim=4))
        assert store.search(np.zeros(4), "black", 3) == []


class TestLifecycle:
    """增量入库 / count / save-load 往返 / 空库。"""

    def test_add_incremental_and_count(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        assert store.count("black") == 0
        store.add(_make_items(10, "black", dim=4, seed=1))
        store.add(_make_items(3, "white", dim=4, seed=2))
        assert store.count("black") == 10
        assert store.count("white") == 3

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "vec"
        store = NumpyVectorStore(path)
        store.add(_make_items(20, "black", dim=6, seed=3) + _make_items(7, "white", dim=6, seed=4))
        store.save()

        loaded = NumpyVectorStore.load(str(path))
        assert loaded.count("black") == 20
        assert loaded.count("white") == 7
        query = _rng().standard_normal(6)
        before = [(h.id, h.score) for h in store.search(query, "black", 5)]
        after = [(h.id, h.score) for h in loaded.search(query, "black", 5)]
        assert before == after

    def test_load_missing_pools_is_empty_store(self, tmp_path: Path) -> None:
        store = NumpyVectorStore.load(str(tmp_path / "no_such_dir"))
        assert store.count("black") == 0
        assert store.search(_rng().standard_normal(4), "black", 5) == []

    def test_save_creates_npz_and_meta(self, tmp_path: Path) -> None:
        path = tmp_path / "vec"
        store = NumpyVectorStore(path)
        store.add([VectorItem("b0", "black", np.array([1.0, 0.0]), {"c": "色情"})])
        store.save()
        assert (path / "black.npz").is_file()
        assert (path / "black.meta.json").is_file()
        payload = json.loads((path / "black.meta.json").read_text(encoding="utf-8"))
        assert payload["ids"] == ["b0"]
        assert payload["meta"]["b0"]["c"] == "色情"

    def test_load_corrupt_meta_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "vec"
        store = NumpyVectorStore(path)
        store.add(_make_items(4, "black", dim=4))
        store.save()
        # 篡改 meta ids 数量 → 行数与元数据不一致
        meta_path = path / "black.meta.json"
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["ids"] = payload["ids"][1:]
        meta_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="持久化文件不一致"):
            NumpyVectorStore.load(str(path))


class TestInputValidation:
    """非法输入：池名 / top_k / 维度 / 重复 id。"""

    def test_unknown_pool_on_add(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        with pytest.raises(ValueError, match="未知池名"):
            store.add([VectorItem("x", "grey", np.array([1.0]), {})])

    def test_unknown_pool_on_search(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        with pytest.raises(ValueError, match="未知池名"):
            store.search(np.array([1.0]), "grey", 1)

    def test_top_k_less_than_one(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(_make_items(2, "black", dim=2))
        with pytest.raises(ValueError, match="top_k"):
            store.search(np.array([1.0, 0.0]), "black", 0)

    def test_dimension_mismatch(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        store.add(_make_items(2, "black", dim=4))
        with pytest.raises(ValueError, match="维度不一致"):
            store.add([VectorItem("x", "black", np.array([1.0, 0.0]), {})])

    def test_non_1d_vector_rejected(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        with pytest.raises(ValueError, match="1 维"):
            store.add([VectorItem("x", "black", np.zeros((2, 2)), {})])

    def test_duplicate_id_skipped(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        vec = np.array([1.0, 0.0])
        store.add([VectorItem("dup", "black", vec, {"v": 1})])
        store.add([VectorItem("dup", "black", vec, {"v": 2}), VectorItem("new", "white", vec, {})])
        assert store.count("black") == 1  # dup 跳过不覆盖
        assert store.count("white") == 1
        assert store.search(vec, "black", 1)[0].metadata == {"v": 1}

    def test_empty_add_noop(self, tmp_path: Path) -> None:
        store = NumpyVectorStore(tmp_path / "vec")
        store.add([])
        assert store.count("black") == 0
