"""R3 图片语料归一化单测（scripts/normalize_assets.py 图片扩展）。

按 R3 卡：``--images-dir`` 递归扫描 + ``--image-pool``（或目录名自动归属）
→ 独立清单 ``import_manifest_images.jsonl``，不混入文本清单；dry-run 只统计；
重复路径去重；损坏图片（Pillow 打不开）跳过并计入报告；非图片扩展名忽略。

覆盖：
- ``collect_image_rows`` 清单行结构（pool/text/image_path/source，text 恒为空串）；
  损坏跳过 / 非图片扩展名忽略 / 目录名自动归属 / 显式 --image-pool 覆盖；
- 多次 --images-dir（父子目录重叠扫描）按真实路径去重；
- 归属池无法推断（目录链无 black/white）→ 跳过并计入 unclassified；
- ``main`` 全链路：dry-run 不写盘 / 真实运行只写独立 images 清单（文本清单不受影响）/
  无效图片目录 rc=1；
- 图片全部为临时生成的最小图片（PNG/GIF 1x1），不依赖真实语料。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from .conftest import png_bytes

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "normalize_assets.py"


def _load_module() -> Any:
    """经 importlib 加载脚本模块（scripts/ 非包）。

    脚本内 dataclass 使用字符串注解（``from __future__ import annotations``），
    dataclasses 在解析 KW_ONLY/ClassVar 标记时需要 ``sys.modules`` 中有该模块，
    故执行前先注册。
    """
    spec = importlib.util.spec_from_file_location("normalize_assets", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["normalize_assets"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> Any:
    return _load_module()


def _gif_bytes(color: tuple[int, int, int] = (30, 200, 90)) -> bytes:
    """生成一张 1x1 最小 GIF 字节。"""
    img = Image.new("RGB", (1, 1), color)
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


@pytest.fixture
def image_tree(tmp_path: Path) -> dict[str, Path]:
    """构造图片语料树：black/white 子目录各 1 张有效图 + 损坏图 + 非图片文件。"""
    black = tmp_path / "images" / "black"
    white = tmp_path / "images" / "white"
    black.mkdir(parents=True)
    white.mkdir(parents=True)
    (black / "b1.png").write_bytes(png_bytes(size=(1, 1), color=(0, 0, 0)))
    (white / "w1.gif").write_bytes(_gif_bytes())
    (white / "broken.png").write_bytes(b"this is not an image")
    (white / "notes.txt").write_text("ignored", encoding="utf-8")
    (white / "icon.svg").write_text("<svg/>", encoding="utf-8")
    return {"root": tmp_path / "images", "black": black, "white": white}


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class TestCollectImageRows:
    def test_structure_and_pool_detect(self, mod: Any, image_tree: dict[str, Path]) -> None:
        rows, stats = mod.collect_image_rows([image_tree["root"]], None)
        # black 1 张有效；white 1 张有效、1 张损坏（txt/svg 不是图片扩展名，不扫描）
        assert stats.scanned == 3
        assert stats.unique == 3
        assert stats.valid == 2
        assert stats.invalid == 1
        assert stats.unclassified == 0
        assert stats.pools == {"black": 1, "white": 1}
        by_name = {Path(row["image_path"]).name: row for row in rows}
        assert set(by_name) == {"b1.png", "w1.gif"}
        for row in rows:
            assert set(row) == {"pool", "text", "image_path", "source"}
            assert row["text"] == ""
            assert row["source"] == "images"
            assert row["image_path"]
        assert by_name["b1.png"]["pool"] == "black"
        assert by_name["w1.gif"]["pool"] == "white"
        # 损坏文件列入报告
        assert len(stats.invalid_files) == 1
        assert stats.invalid_files[0][0].endswith("broken.png")

    def test_explicit_pool_overrides(self, mod: Any, image_tree: dict[str, Path]) -> None:
        rows, stats = mod.collect_image_rows([image_tree["black"]], "white")
        assert stats.valid == 1
        assert len(rows) == 1
        assert rows[0]["pool"] == "white"

    def test_dedup_overlapping_roots(self, mod: Any, image_tree: dict[str, Path]) -> None:
        # 父目录 + 子目录重叠扫描：b1.png 被扫到两次 → 去重保留首见
        rows, stats = mod.collect_image_rows([image_tree["root"], image_tree["black"]], None)
        assert stats.scanned == 4
        assert stats.deduped == 1
        assert stats.unique == 3
        assert stats.valid == 2
        assert len(rows) == 2

    def test_unclassified_dir_skipped(self, mod: Any, tmp_path: Path) -> None:
        photos = tmp_path / "photos"
        photos.mkdir()
        (photos / "x.png").write_bytes(png_bytes())
        rows, stats = mod.collect_image_rows([photos], None)
        assert rows == []
        assert stats.unclassified == 1
        assert stats.valid == 0

    def test_missing_root_raises(self, mod: Any, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            mod.collect_image_rows([tmp_path / "nope"], None)

    def test_extension_filter(self, mod: Any, tmp_path: Path) -> None:
        black = tmp_path / "black"
        black.mkdir()
        (black / "a.png").write_bytes(png_bytes())
        (black / "b.JPG").write_bytes(png_bytes())  # 大写扩展名同样识别
        (black / "c.avif").write_bytes(png_bytes())  # 不在白名单 → 忽略
        rows, stats = mod.collect_image_rows([black], None)
        assert stats.scanned == 2
        assert stats.valid == 2


class TestMainImages:
    def test_dry_run_writes_nothing(
        self, mod: Any, image_tree: dict[str, Path], tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        rc = mod.main(
            [
                "--node-data",
                str(tmp_path / "no-node"),
                "--cherry-data",
                str(tmp_path / "no-cherry"),
                "--images-dir",
                str(image_tree["root"]),
                "--out",
                str(out),
                "--dry-run",
            ]
        )
        assert rc == 0
        assert not (out / "vectors" / "import_manifest_images.jsonl").exists()

    def test_main_writes_images_manifest_only(
        self, mod: Any, image_tree: dict[str, Path], tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        rc = mod.main(
            [
                "--node-data",
                str(tmp_path / "no-node"),
                "--cherry-data",
                str(tmp_path / "no-cherry"),
                "--images-dir",
                str(image_tree["root"]),
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        img_manifest = out / "vectors" / "import_manifest_images.jsonl"
        assert img_manifest.is_file()
        rows = _read_rows(img_manifest)
        assert len(rows) == 2
        assert all(row["text"] == "" and row["source"] == "images" for row in rows)
        assert {row["pool"] for row in rows} == {"black", "white"}
        # 文本清单独立存在且不含图片行
        txt_manifest = out / "vectors" / "import_manifest.jsonl"
        assert txt_manifest.is_file()
        assert all("image_path" not in row for row in _read_rows(txt_manifest))

    def test_invalid_images_dir_rc1(
        self, mod: Any, image_tree: dict[str, Path], tmp_path: Path
    ) -> None:
        rc = mod.main(
            [
                "--node-data",
                str(tmp_path / "no-node"),
                "--cherry-data",
                str(tmp_path / "no-cherry"),
                "--images-dir",
                str(tmp_path / "missing"),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 1

    def test_image_pool_arg_parsed(self, mod: Any) -> None:
        args = mod.parse_args(["--images-dir", "data/images", "--image-pool", "black"])
        assert args.images_dir == ["data/images"]
        assert args.image_pool == "black"
        args = mod.parse_args(["--images-dir", "data/images"])
        assert args.image_pool is None
