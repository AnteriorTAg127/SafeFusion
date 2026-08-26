"""轻量文本风险模型测试：disabled 三路径与 predict None + 假 torch 下的启用路径。

对应 T4a 任务卡「无模型时 disabled 路径」。本环境无 torch，启用路径用
最小 FakeTorch（numpy 实现）走通 forward/predict 数值链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from safefusion.engines.light_model import LightTextModel, fnv1a64, ngram_features

# ---------------------------------------------------------------------------
# 最小 FakeTorch：仅覆盖 light_model 用到的算子（numpy 实现）
# ---------------------------------------------------------------------------


class FakeTensor:
    """numpy 数组包装，实现 _forward / predict 用到的有限算子。"""

    def __init__(self, arr: Any) -> None:
        self.arr = np.asarray(arr, dtype=np.float64)
        self.shape = self.arr.shape

    def unsqueeze(self, dim: int) -> FakeTensor:
        return FakeTensor(np.expand_dims(self.arr, dim))

    def sum(self, dim: int | None = None, keepdim: bool = False) -> FakeTensor:
        return FakeTensor(self.arr.sum(axis=dim, keepdims=keepdim))

    def clamp(self, min: float | None = None, max: float | None = None) -> FakeTensor:
        return FakeTensor(np.clip(self.arr, min, max))

    def argmax(self, dim: int | None = None) -> Any:
        if dim is None:
            return int(np.argmax(self.arr))
        idx = np.argmax(self.arr, axis=dim)
        return FakeTensor(idx)

    def __mul__(self, other: Any) -> FakeTensor:
        return FakeTensor(self.arr * (other.arr if isinstance(other, FakeTensor) else other))

    def __truediv__(self, other: Any) -> FakeTensor:
        return FakeTensor(self.arr / (other.arr if isinstance(other, FakeTensor) else other))

    def __getitem__(self, idx: Any) -> FakeTensor:
        return FakeTensor(self.arr[idx])

    def __float__(self) -> float:
        return float(np.asarray(self.arr).reshape(-1)[0])

    def __int__(self) -> int:
        return int(np.asarray(self.arr).reshape(-1)[0])


class FakeEmbedding:
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: int | None = None,
    ) -> None:
        self._num = num_embeddings
        self._dim = embedding_dim
        self.padding_idx = padding_idx
        self.weight = np.zeros((num_embeddings, embedding_dim), dtype=np.float64)

    def load_state_dict(self, state: dict[str, Any]) -> None:
        self.weight = np.asarray(state["weight"], dtype=np.float64)

    def eval(self) -> FakeEmbedding:
        return self

    def __call__(self, x: FakeTensor) -> FakeTensor:
        idx = np.asarray(x.arr, dtype=np.int64)
        return FakeTensor(self.weight[idx])


class FakeLinear:
    def __init__(self, in_features: int, out_features: int) -> None:
        self.weight = np.zeros((out_features, in_features), dtype=np.float64)
        self.bias = np.zeros((out_features,), dtype=np.float64)

    def load_state_dict(self, state: dict[str, Any]) -> None:
        self.weight = np.asarray(state["weight"], dtype=np.float64)
        self.bias = np.asarray(state["bias"], dtype=np.float64)

    def eval(self) -> FakeLinear:
        return self

    def __call__(self, h: FakeTensor) -> FakeTensor:
        return FakeTensor(h.arr @ self.weight.T + self.bias)


class FakeNN:
    Embedding = FakeEmbedding
    Linear = FakeLinear


class _NoGrad:
    def __enter__(self) -> _NoGrad:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class FakeTorch:
    """替代 torch 模块的最小实现（按需扩展）。"""

    nn = FakeNN
    long = np.int64
    float32 = np.float32

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    def load(self, path: str | Path, map_location: str | None = None) -> dict[str, Any]:
        return self._state

    @staticmethod
    def as_tensor(data: Any, dtype: Any = None) -> FakeTensor:
        return FakeTensor(np.asarray(data))

    @staticmethod
    def full(shape: tuple[int, ...], fill_value: Any, dtype: Any = None) -> FakeTensor:
        return FakeTensor(np.full(shape, fill_value))

    @staticmethod
    def ones(shape: tuple[int, ...], dtype: Any = None) -> FakeTensor:
        return FakeTensor(np.ones(shape))

    @staticmethod
    def zeros(shape: tuple[int, ...], dtype: Any = None) -> FakeTensor:
        return FakeTensor(np.zeros(shape))

    @staticmethod
    def softmax(x: FakeTensor, dim: int = -1) -> FakeTensor:
        arr = x.arr
        exp = np.exp(arr - arr.max(axis=dim, keepdims=True))
        return FakeTensor(exp / exp.sum(axis=dim, keepdims=True))

    def no_grad(self) -> _NoGrad:
        return _NoGrad()


def _valid_config(nbuckets: int = 32, emb_dim: int = 4) -> dict[str, Any]:
    return {
        "nbuckets": nbuckets,
        "emb_dim": emb_dim,
        "ngram_min": 3,
        "ngram_max": 4,
        "pad_ch": "<",
        "end_ch": ">",
        "classes": ["安全", "违规"],
        "class_to_idx": {"安全": 0, "违规": 1},
        "violation_class": "违规",
    }


def _write_model_files(tmp_path: Path, config: dict[str, Any]) -> tuple[Path, Path]:
    model_file = tmp_path / "fasttext.pt"
    model_file.write_bytes(b"not-a-real-torch-file")  # FakeTorch.load 不读内容
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    return model_file, config_file


class _FakeWeight:
    """state dict 权重包装：提供 ``.shape`` / ``.dim()`` 且可经 np.asarray 还原。"""

    def __init__(self, arr: Any) -> None:
        self._data = np.asarray(arr, dtype=np.float32)

    @property
    def shape(self) -> tuple[int, ...]:
        return self._data.shape

    def dim(self) -> int:
        return self._data.ndim

    def __array__(self, dtype: Any = None) -> np.ndarray:
        return self._data.astype(dtype) if dtype else self._data


def _fake_state(nbuckets: int, emb_dim: int, n_classes: int = 2) -> dict[str, Any]:
    rng = np.random.default_rng(5)
    return {
        "emb.weight": _FakeWeight(rng.standard_normal((nbuckets + 1, emb_dim))),
        "fc.weight": _FakeWeight(rng.standard_normal((n_classes, emb_dim))),
        "fc.bias": _FakeWeight(rng.standard_normal((n_classes,))),
    }


class TestDisabledPaths:
    """缺路径 / 缺文件 / torch 缺失 → disabled=True，predict 返回 None。"""

    def test_disabled_when_paths_none(self) -> None:
        model = LightTextModel(None, None)
        assert model.disabled is True
        assert model.predict("任何文本") is None

    def test_disabled_when_files_missing(self, tmp_path: Path) -> None:
        model = LightTextModel(str(tmp_path / "no.pt"), str(tmp_path / "no.json"))
        assert model.disabled is True
        assert model.predict("x") is None

    def test_disabled_when_one_file_missing(self, tmp_path: Path) -> None:
        config = _valid_config()
        model_file, _ = _write_model_files(tmp_path, config)
        model = LightTextModel(str(model_file), str(tmp_path / "missing.json"))
        assert model.disabled is True

    def test_disabled_when_torch_import_fails(self, tmp_path: Path) -> None:
        model_file, config_file = _write_model_files(tmp_path, _valid_config())

        def _no_torch() -> Any:
            raise ImportError("torch 缺失（模拟）")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(LightTextModel, "_import_torch", staticmethod(_no_torch))
        try:
            model = LightTextModel(str(model_file), str(config_file))
        finally:
            monkeypatch.undo()
        assert model.disabled is True
        assert model.predict("x") is None

    def test_disabled_when_config_malformed(self, tmp_path: Path) -> None:
        config = _valid_config()
        del config["nbuckets"]  # 缺必填超参 → 初始化失败降级
        model_file, config_file = _write_model_files(tmp_path, config)
        model = LightTextModel(str(model_file), str(config_file))
        assert model.disabled is True


class TestHashing:
    """fnv1a64 与 ngram_features 的确定性 / 桶界。"""

    def test_fnv1a64_deterministic(self) -> None:
        assert fnv1a64("w:裸聊".encode()) == fnv1a64("w:裸聊".encode())
        assert fnv1a64(b"a") != fnv1a64(b"b")

    def test_ngram_features_in_bounds(self) -> None:
        ids = ngram_features("裸聊", nbuckets=32, ngram_min=3, ngram_max=4, pad_ch="<", end_ch=">")
        assert all(0 <= i < 32 for i in ids)
        # 1 个整词桶 + 2 个 ngram 长度 × 滑窗数
        padded = "<裸聊>"
        expected_count = 1 + (len(padded) - 3 + 1) + (len(padded) - 4 + 1)
        assert len(ids) == expected_count


class TestEnabledPathWithFakeTorch:
    """注入 FakeTorch 后完整初始化 + predict 数值链路。"""

    def test_predict_with_fake_torch(self, tmp_path: Path) -> None:
        config = _valid_config()
        model_file, config_file = _write_model_files(tmp_path, config)
        fake_torch = FakeTorch(_fake_state(config["nbuckets"], config["emb_dim"]))

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(LightTextModel, "_import_torch", staticmethod(lambda: fake_torch))
        try:
            model = LightTextModel(str(model_file), str(config_file))
        finally:
            monkeypatch.undo()

        assert model.disabled is False
        result = model.predict("这是一个普通文本")
        assert result is not None
        assert result["label"] in {"安全", "违规"}
        assert 0.0 <= result["score"] <= 1.0
        assert isinstance(result["violation"], bool)

    def test_predict_empty_text(self, tmp_path: Path) -> None:
        config = _valid_config()
        model_file, config_file = _write_model_files(tmp_path, config)
        fake_torch = FakeTorch(_fake_state(config["nbuckets"], config["emb_dim"]))

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(LightTextModel, "_import_torch", staticmethod(lambda: fake_torch))
        try:
            model = LightTextModel(str(model_file), str(config_file))
        finally:
            monkeypatch.undo()
        result = model.predict("")
        # 空文本走 pad 行 + 全零 mask 路径，仍返回合法结构（不崩）
        assert result is not None
        assert result["label"] in {"安全", "违规"}

    def test_prediction_consistent_for_same_text(self, tmp_path: Path) -> None:
        config = _valid_config()
        model_file, config_file = _write_model_files(tmp_path, config)
        fake_torch = FakeTorch(_fake_state(config["nbuckets"], config["emb_dim"]))

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(LightTextModel, "_import_torch", staticmethod(lambda: fake_torch))
        try:
            model = LightTextModel(str(model_file), str(config_file))
        finally:
            monkeypatch.undo()
        assert model.predict("相同输入") == model.predict("相同输入")

    def test_violation_label_maps_index(self, tmp_path: Path) -> None:
        # 构造 fc 使违规类 logits 恒最大 → violation=True（验证 violation_idx 语义）
        config = _valid_config()
        model_file, config_file = _write_model_files(tmp_path, config)
        state = _fake_state(config["nbuckets"], config["emb_dim"])
        emb_dim = config["emb_dim"]
        state["fc.weight"] = _FakeWeight(np.zeros((2, emb_dim), dtype=np.float32))
        # 违规类（idx=1）恒胜
        state["fc.bias"] = _FakeWeight(np.array([-5.0, 5.0], dtype=np.float32))
        fake_torch = FakeTorch(state)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(LightTextModel, "_import_torch", staticmethod(lambda: fake_torch))
        try:
            model = LightTextModel(str(model_file), str(config_file))
        finally:
            monkeypatch.undo()
        result = model.predict("任何输入")
        assert result["label"] == "违规"
        assert result["violation"] is True
        # softmax([-5, 5]) ≈ 0.99995…，不精确等于 1
        assert result["score"] > 0.99
