"""图片管线测试：base64/data-URI/URL 解码、动图首帧、哈希稳定性、白名单匹配。

对应 T4b 任务卡验收：URL/base64 解码、GIF 首帧、pHash 距离匹配。
URL 拉取使用受控假 httpx 客户端（无网络）；白名单用真实 Database 实例。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import imagehash
import numpy as np
import pytest
from PIL import Image

from safefusion.engines.image_pipeline import (
    WhitelistMatcher,
    compute_hashes,
    decode_images,
)
from safefusion.models.schemas import ImageInput
from safefusion.storage.database import Database

from .conftest import png_bytes


def _gif_bytes(colors: list[tuple[int, int, int]], size: tuple[int, int] = (16, 16)) -> bytes:
    imgs = [Image.new("RGB", size, color) for color in colors]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class FakeHttpClient:
    """``httpx.AsyncClient`` 替身：按序产出预置响应或异常。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = iter(outcomes)

    async def __aenter__(self) -> FakeHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str) -> Any:
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestDecodeBase64:
    """base64 / data-URI 解码与动图首帧降级。"""

    async def test_decode_png_bytes(self) -> None:
        import base64

        data = png_bytes(size=(24, 24))
        imgs = await decode_images([ImageInput(base64=base64.b64encode(data).decode("ascii"))])
        assert len(imgs) == 1
        assert imgs[0].mode == "RGB"
        assert imgs[0].size == (24, 24)

    async def test_decode_data_uri(self) -> None:
        import base64

        data = png_bytes()
        uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        imgs = await decode_images([ImageInput(base64=uri)])
        assert imgs[0].size == (32, 32)

    async def test_gif_takes_first_frame(self) -> None:
        data = _gif_bytes([(255, 0, 0), (0, 0, 255)])
        imgs = await decode_images([ImageInput(base64=_b64(data))])
        assert imgs[0].getpixel((2, 2)) == (255, 0, 0)  # 首帧红色

    async def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError, match="base64 解码失败"):
            await decode_images([ImageInput(base64="!!!not-base64!!!")])

    async def test_invalid_image_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="无法解码为图像"):
            await decode_images([ImageInput(base64=_b64(b"not an image"))])

    async def test_order_preserved(self) -> None:
        import base64

        a = Image.new("RGB", (8, 8), (255, 0, 0))
        b = Image.new("RGB", (8, 8), (0, 0, 255))
        buf_a, buf_b = io.BytesIO(), io.BytesIO()
        a.save(buf_a, format="PNG")
        b.save(buf_b, format="PNG")
        imgs = await decode_images(
            [
                ImageInput(base64=base64.b64encode(buf_a.getvalue()).decode()),
                ImageInput(base64=base64.b64encode(buf_b.getvalue()).decode()),
            ]
        )
        assert imgs[0].getpixel((0, 0)) == (255, 0, 0)
        assert imgs[1].getpixel((0, 0)) == (0, 0, 255)

    async def test_empty_input(self) -> None:
        assert await decode_images([]) == []


class TestDecodeUrl:
    """URL 拉取（假客户端）：200 成功 / 非 200 / 网络异常。"""

    async def test_url_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from safefusion.engines import image_pipeline as ip

        monkeypatch.setattr(
            ip.httpx,
            "AsyncClient",
            lambda *a, **k: FakeHttpClient([FakeResponse(200, png_bytes())]),
        )
        imgs = await decode_images([ImageInput(url="https://example.com/a.png")])
        assert imgs[0].size == (32, 32)

    async def test_url_non_200_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from safefusion.engines import image_pipeline as ip

        monkeypatch.setattr(
            ip.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient([FakeResponse(404, b"")])
        )
        with pytest.raises(ValueError, match="HTTP 404"):
            await decode_images([ImageInput(url="https://example.com/x.png")])

    async def test_url_network_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        from safefusion.engines import image_pipeline as ip

        exc = httpx.ConnectError("连接失败", request=httpx.Request("GET", "https://example.com/"))
        monkeypatch.setattr(ip.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient([exc]))
        with pytest.raises(ValueError, match="图片解码失败"):
            await decode_images([ImageInput(url="https://example.com/a.png")])


class TestComputeHashes:
    """compute_hashes：同一内容 md5/phash 稳定、不同内容不同、256 位 pHash。"""

    def test_stable_for_same_content(self) -> None:
        img = Image.new("RGB", (16, 16), (10, 200, 30))
        assert compute_hashes(img) == compute_hashes(img)

    def test_different_content_different_md5(self) -> None:
        a = Image.new("RGB", (16, 16), (10, 200, 30))
        b = Image.new("RGB", (16, 16), (11, 200, 30))
        md5_a, _ = compute_hashes(a)
        md5_b, _ = compute_hashes(b)
        assert md5_a != md5_b

    def test_phash_hex_length(self) -> None:
        _, phash = compute_hashes(Image.new("RGB", (16, 16), (5, 5, 5)))
        assert len(str(phash)) == 64  # 16x16 = 256 位 = 64 个十六进制字符
        assert isinstance(phash, imagehash.ImageHash)


class TestWhitelistMatcher:
    """白名单匹配：真实 Database 实例上 add 后命中与 miss、距离阈值。"""

    def test_add_and_match_hit(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            matcher = WhitelistMatcher(db)
            data = png_bytes()
            meta = matcher.add_image(data, note="样板图")
            assert meta["md5"]
            assert meta["phash_hex"]
            # 同图 → 距离 0 命中
            from safefusion.engines.image_pipeline import _bytes_to_first_frame

            img = _bytes_to_first_frame(data)
            _, phash = compute_hashes(img)
            row = matcher.match(phash, max_distance=5)
            assert row is not None
            assert row["distance"] == 0
            assert row["md5"] == meta["md5"]
        finally:
            db.close()

    def test_add_from_path(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        path = tmp_path / "img.png"
        path.write_bytes(png_bytes(color=(0, 0, 200)))
        try:
            matcher = WhitelistMatcher(db)
            meta = matcher.add_image(str(path), note="路径入库")
            assert meta["phash_hex"]
            row = matcher.match(imagehash.hex_to_hash(meta["phash_hex"]), max_distance=0)
            assert row is not None
            assert row["distance"] == 0
        finally:
            db.close()

    def test_miss_beyond_threshold(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            matcher = WhitelistMatcher(db)
            matcher.add_image(png_bytes(color=(10, 10, 10)), note="白")
            # 噪声图案图（非纯色）→ pHash 与纯色白名单距离明显 > 0
            rng = np.random.default_rng(3)
            other = Image.fromarray(
                rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8), mode="RGB"
            )
            _, phash = compute_hashes(other)
            row = matcher.match(phash, max_distance=0)
            assert row is None
        finally:
            db.close()

    def test_empty_whitelist_no_match(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            matcher = WhitelistMatcher(db)
            img = Image.new("RGB", (16, 16), (1, 2, 3))
            _, phash = compute_hashes(img)
            assert matcher.match(phash, max_distance=5) is None
        finally:
            db.close()

    def test_save_load_noop(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "audit.db")
        try:
            matcher = WhitelistMatcher(db)
            matcher.save()  # 契约兼容：DB 即持久化
            matcher.load()
        finally:
            db.close()


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
