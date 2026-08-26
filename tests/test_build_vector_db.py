"""T15 向量库构建脚本单测（无 torch 环境可跑）：纯逻辑 + 假 CLIP 后端全链路。

按 ``开发/v0.2/分工.md`` T15 卡：脚本核心逻辑（清单解析 / done_ids 断点续跑 /
分批编码降级 / 持久化报告）不依赖真实模型，可注入假 CLIP 后端在无 torch 环境下
验证；真实模型（Chinese-CLIP 权重）用例定义于 ``test_ml_integration.py``
（@pytest.mark.integration，ML 环境执行）。

覆盖：
- ``parse_args`` 默认值与开关；``read_manifest`` 恒定 id 与各类跳过行；
- ``load_done_ids`` 优先 done_ids.json、损坏回退 meta；
- ``main`` dry-run 不写盘 / 清单缺失 rc=1 / 后端不可用输出指引并 rc=1；
- ``main`` 小样本全量构建（假后端）→ black/white 双池入库、done_ids 落盘；
  二次运行断点续跑全部跳过（不重复编码）；
- ``build_items`` metadata 截断；``encode_texts_safely`` 整批失败降级逐条 / 单条失败跳过。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from safefusion.storage.vector_store import NumpyVectorStore, VectorItem

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_vector_db.py"


def _load_module() -> Any:
    """经 importlib 加载脚本模块（scripts/ 非包；模块级仅插入 src 到 sys.path）。"""
    spec = importlib.util.spec_from_file_location("build_vector_db", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> Any:
    return _load_module()


def _write_manifest(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


class _FakeClip:
    """假 Chinese-CLIP 后端：固定 8 维随机向量，计数编码调用。"""

    def __init__(self) -> None:
        self.text_calls = 0
        self.image_calls = 0

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        self.text_calls += 1
        rng = np.random.default_rng(42)
        return np.asarray([rng.normal(size=8) for _ in texts], dtype=np.float32)

    def encode_images(self, images: list[Any]) -> np.ndarray:
        self.image_calls += 1
        rng = np.random.default_rng(7)
        return np.asarray([rng.normal(size=8) for _ in images], dtype=np.float32)


class TestParseArgs:
    def test_defaults(self, mod: Any) -> None:
        args = mod.parse_args([])
        assert args.batch == 64
        assert args.device == "auto"
        assert args.max_rows == 0
        assert args.resume is True
        assert args.dry_run is False
        assert args.out == "data/vectors/"

    def test_flags(self, mod: Any) -> None:
        args = mod.parse_args(
            ["--max-rows", "5", "--dry-run", "--no-resume", "--batch", "8", "--device", "cpu"]
        )
        assert args.max_rows == 5
        assert args.dry_run is True
        assert args.resume is False
        assert args.batch == 8
        assert args.device == "cpu"


class TestReadManifest:
    def test_ids_and_skipped_lines(self, mod: Any, tmp_path: Path) -> None:
        manifest = _write_manifest(
            tmp_path / "m.jsonl",
            [
                {"pool": "black", "text": "甲", "category": "色情", "source": "s1"},
                {"pool": "white", "text": "乙"},
                "not-json-line",
                {"pool": "unknown", "text": "x"},
                {"pool": "black", "text": "   "},
                {"pool": "black", "image_path": "a.png"},  # 图像条目无需 text
            ],
        )
        rows, skipped = mod.read_manifest(manifest)
        assert [rid for rid, _row in rows] == ["black:1", "white:1", "black:2"]
        assert skipped == 3

    def test_missing_manifest_raises(self, mod: Any, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            mod.read_manifest(tmp_path / "nope.jsonl")


class TestDoneIds:
    def test_prefers_done_ids_file(self, mod: Any, tmp_path: Path) -> None:
        out = tmp_path / "vec"
        out.mkdir()
        (out / "done_ids.json").write_text(
            json.dumps({"version": 1, "ids": ["black:1", "white:2"]}), encoding="utf-8"
        )
        (out / "black.meta.json").write_text(json.dumps({"ids": ["black:9"]}), encoding="utf-8")
        assert mod.load_done_ids(out) == {"black:1", "white:2"}

    def test_corrupt_done_ids_falls_back_to_meta(self, mod: Any, tmp_path: Path) -> None:
        out = tmp_path / "vec"
        out.mkdir()
        (out / "done_ids.json").write_text("{corrupt", encoding="utf-8")
        (out / "black.meta.json").write_text(json.dumps({"ids": ["black:3"]}), encoding="utf-8")
        assert mod.load_done_ids(out) == {"black:3"}


class TestMainNoTorch:
    """无 torch 路径：dry-run / 缺失清单 / 后端失败指引（均不触碰 ML 依赖）。"""

    def test_dry_run_reads_only(
        self, mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        manifest = _write_manifest(
            tmp_path / "m.jsonl",
            [
                {"pool": "black", "text": "甲", "category": "色情"},
                {"pool": "white", "text": "乙"},
            ],
        )
        out = tmp_path / "out"
        rc = mod.main(["--manifest", str(manifest), "--out", str(out), "--dry-run"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "dry-run" in captured
        assert "black 1 / white 1" in captured
        assert not out.exists()  # dry-run 不编码不写盘

    def test_missing_manifest_returns_one(self, mod: Any, tmp_path: Path) -> None:
        rc = mod.main(["--manifest", str(tmp_path / "nope.jsonl")])
        assert rc == 1

    def test_backend_failure_prints_guidance(
        self,
        mod: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        manifest = _write_manifest(
            tmp_path / "m.jsonl", [{"pool": "black", "text": "甲"}]
        )

        def _boom(model: str | None, device: str) -> None:
            raise RuntimeError("torch 未安装")

        monkeypatch.setattr(mod, "build_backend", _boom)
        rc = mod.main(["--manifest", str(manifest), "--out", str(tmp_path / "out")])
        assert rc == 1
        captured = capsys.readouterr().out
        assert "uv sync --extra ml" in captured  # 指引而非裸 traceback
        assert "HuggingFace" in captured


class TestMainFullRunWithFakeBackend:
    """假 CLIP 后端全链路：小样本构建 ≥ 断点续跑跳过（不重复编码）。"""

    def test_build_then_resume_skips_all(
        self,
        mod: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        manifest = _write_manifest(
            tmp_path / "m.jsonl",
            [
                {"pool": "black", "text": "甲", "category": "色情", "source": "s1"},
                {"pool": "black", "text": "乙", "category": "广告", "source": "s1"},
                {"pool": "black", "text": "丙", "category": "色情", "source": "s2"},
                {"pool": "white", "text": "丁", "source": "w1"},
                {"pool": "white", "text": "戊", "source": "w1"},
            ],
        )
        out = tmp_path / "vec"
        fake = _FakeClip()
        monkeypatch.setattr(mod, "build_backend", lambda model, device: fake)

        rc = mod.main(["--manifest", str(manifest), "--out", str(out), "--max-rows", "5"])
        assert rc == 0
        store = NumpyVectorStore.load(out)
        assert store.count("black") == 3
        assert store.count("white") == 2
        assert (out / "done_ids.json").is_file()
        assert len(json.loads((out / "done_ids.json").read_text(encoding="utf-8"))["ids"]) == 5
        first_run_calls = fake.text_calls
        assert first_run_calls >= 1

        # 断点续跑：二次运行全部命中 done_ids → 不编码直接结束
        rc2 = mod.main(["--manifest", str(manifest), "--out", str(out), "--max-rows", "5"])
        assert rc2 == 0
        captured = capsys.readouterr()
        assert "black 0 / white 0" in captured.out  # 完成报告显示本次零编码
        assert fake.text_calls == first_run_calls  # 未发起任何二次编码（断点续跑全部跳过）


class TestHelpers:
    def test_build_items_truncates_text_and_metadata(self, mod: Any) -> None:
        vec = np.ones(4, dtype=np.float32)
        pairs = [
            ("black:1", {"pool": "black", "text": "x" * 300, "category": "色情", "source": "s1"})
        ]
        items = mod.build_items(pairs, [("black:1", vec)])
        assert isinstance(items[0], VectorItem)
        assert items[0].metadata["text"] == "x" * 200
        assert items[0].metadata["category"] == "色情"
        assert items[0].metadata["source"] == "s1"

    def test_encode_texts_safely_batch_fail_then_retry(self, mod: Any) -> None:
        class _FailBatch:
            def __init__(self) -> None:
                self.calls = 0

            def encode_texts(self, texts: list[str]) -> np.ndarray:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("batch down")
                return np.asarray([np.ones(4, dtype=np.float32) for _ in texts])

        backend = _FailBatch()
        out = mod.encode_texts_safely(backend, [("a", {"text": "x"}), ("b", {"text": "y"})])
        assert [rid for rid, _vec in out] == ["a", "b"]

    def test_encode_texts_safely_single_failure_skipped(self, mod: Any) -> None:
        class _FailAll:
            def encode_texts(self, texts: list[str]) -> np.ndarray:
                raise RuntimeError("always down")

        out = mod.encode_texts_safely(_FailAll(), [("a", {"text": "x"}), ("b", {"text": "y"})])
        assert out == []

    def test_format_backend_error_hints(self, mod: Any) -> None:
        msg = mod.format_backend_error(None, RuntimeError("boom"))
        assert "uv sync --extra ml" in msg
        assert "HuggingFace" in msg
        assert "transformers 5.x" in msg
