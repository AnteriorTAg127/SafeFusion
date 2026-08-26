"""图片管线：解码 / 哈希 / 白名单匹配（PRD §3.1/§3.2，分工文档契约 T4b）。

- ``decode_images``：base64 / URL 两种输入解码为 PIL.Image；GIF/动图按 v0.1
  降级策略取首帧（PRD §3.1）；URL 拉取使用 httpx 异步客户端（异步网络请求，
  禁止 requests），超时默认 10s；解码失败抛 ``ValueError`` 并带原因。
- ``compute_hashes``：返回 ``(md5_hex, phash)``，pHash 使用
  ``imagehash.phash(img, hash_size=16)``（256 位）。
- ``WhitelistMatcher``：图片白名单匹配器，DB 即持久化（``whitelist_meta`` 表）；
  v0.1 数据量小，匹配采用线性扫描比较汉明距离，索引优化留 TODO（二期）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
from pathlib import Path
from typing import Any

import httpx
import imagehash
from PIL import Image

from safefusion.logging_setup import get_logger
from safefusion.models.schemas import ImageInput

logger: logging.Logger = get_logger("engines.image_pipeline")

#: 哈希尺寸：16x16 = 256 位感知哈希（与 PRD §3.1/§3.2 一致）
HASH_SIZE = 16

#: 白名单行中 pHash 十六进制串字段的候选键名（兼容未来 T2 实现差异）
_PHASH_KEYS = ("phash_hex", "phash")


async def decode_images(inputs: list[ImageInput], *, timeout: float = 10.0) -> list[Image.Image]:
    """将图片输入列表解码为 PIL.Image 列表（保持输入顺序）。

    Args:
        inputs: 图片输入列表，每项 ``url`` 与 ``base64`` 二选一
            （``models.schemas.ImageInput`` 校验）。
        timeout: URL 拉取超时（秒），默认 10s。

    Returns:
        按输入顺序的 PIL.Image 列表（已归一化为 RGB，动图取首帧）。

    Raises:
        ValueError: 任一输入缺失来源 / base64 非法 / 图片无法解码 /
            URL 拉取失败（非 200 或网络错误）。
    """

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "SafeFusion/0.1"},
    ) as client:
        return [await _decode_one(inp, client) for inp in inputs]


async def _decode_one(inp: ImageInput, client: httpx.AsyncClient) -> Image.Image:
    """解码单个输入；任何失败统一抛 ValueError 并带原因。"""

    if (inp.url is None) == (inp.base64 is None):
        raise ValueError("ImageInput 的 url 与 base64 必须且只能提供一个")
    try:
        if inp.base64 is not None:
            data = _b64_to_bytes(inp.base64)
        else:
            resp = await client.get(inp.url)  # httpx 网络错误在此抛出
            if resp.status_code != 200:
                raise ValueError(f"URL 拉取失败 HTTP {resp.status_code}: {inp.url}")
            data = resp.content
        return _bytes_to_first_frame(data)
    except ValueError:
        raise
    except Exception as exc:  # 网络层 / 解码层异常统一转为 ValueError
        raise ValueError(f"图片解码失败（url={inp.url!r}）: {exc}") from exc


def _b64_to_bytes(b64: str) -> bytes:
    """base64 解码；容忍可选的 ``data:...;base64,`` 前缀。"""

    payload = b64.strip()
    if payload.startswith("data:"):
        # data URI：仅保留逗号后的 base64 主体
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"base64 解码失败: {exc}") from exc


def _bytes_to_first_frame(data: bytes) -> Image.Image:
    """字节 → PIL.Image；动图取首帧（v0.1 降级，PRD §3.1）；归一化为 RGB。"""

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ValueError(f"图片数据无法解码为图像: {exc}") from exc
    if getattr(img, "is_animated", False):
        img.seek(0)  # 动图默认取首帧（Pillow 打开时即第 0 帧，seek 兜底保证）
    return _to_rgb(img)


def _to_rgb(img: Image.Image) -> Image.Image:
    """归一化为 RGB：带透明通道的图先合成到白底，避免黑底伪影。"""

    if img.mode in ("RGBA", "LA", "P") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def compute_hashes(img: Image.Image) -> tuple[str, imagehash.ImageHash]:
    """计算图像的内容哈希。

    Args:
        img: PIL.Image（建议为 ``decode_images`` 归一化后的 RGB）。

    Returns:
        ``(md5_hex, phash)``：md5 取 **PNG 编码字节**（无损、确定性，与源格式
        无关，同一像素内容不同格式 md5 一致）；phash 为 ``imagehash.phash(img,
        hash_size=16)``。
    """

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    md5_hex = hashlib.md5(buf.getvalue()).hexdigest()
    phash = imagehash.phash(img, hash_size=HASH_SIZE)
    return md5_hex, phash


class WhitelistMatcher:
    """图片白名单匹配器（pHash 汉明距离）。

    构造注入 T2 提供的数据库对象（``safefusion.storage.database.Database``，
    鸭子类型）：仅使用 ``whitelist_meta`` 表，依赖以下方法：

    - ``add_whitelist(md5: str, phash_hex: str, note: str) -> None``
    - ``list_whitelist() -> list[dict]``，行含键 ``phash_hex``（及可选
      ``id`` / ``md5`` / ``note`` / ``created_at``）

    v0.1 数据量小，``match`` 采用线性扫描；索引优化（预分桶 / 感知哈希索引）
    留 TODO 二期。
    """

    def __init__(self, db: Any) -> None:
        """初始化匹配器。

        Args:
            db: 实现上述协议的数据访问对象（T2 Database 实例）。
        """

        self._db = db

    def add_image(self, path_or_bytes: str | Path | bytes, note: str = "") -> dict[str, str]:
        """计算白名单图片哈希并写入 ``whitelist_meta`` 表。

        Args:
            path_or_bytes: 图片文件路径或图片字节（PNG/JPEG/GIF 等）。
            note: 备注（如来源描述）。

        Returns:
            入库后的元数据 ``{"md5", "phash_hex", "note"}``。

        Raises:
            ValueError: 文件不可读或图片无法解码。
        """

        data = _read_image_bytes(path_or_bytes)
        img = _bytes_to_first_frame(data)
        md5_hex, phash = compute_hashes(img)
        self._db.add_whitelist(md5_hex, str(phash), note)
        logger.info("白名单图片入库: md5=%s phash=%s note=%r", md5_hex, phash, note)
        return {"md5": md5_hex, "phash_hex": str(phash), "note": note}

    def match(self, phash: imagehash.ImageHash, max_distance: int) -> dict[str, Any] | None:
        """在全部白名单中找与 ``phash`` 汉明距离最近的条目。

        Args:
            phash: 待匹配图像的感知哈希（``compute_hashes`` 产出）。
            max_distance: 命中阈值（汉明距离 ≤ 阈值才命中；config 默认
                ``thresholds.phash_whitelist_distance=5``）。

        Returns:
            命中的白名单行元数据并附带 ``distance``（最近距离）；无命中返回
            ``None``。

        TODO(二期): 数据量增大后以预分桶 / 感知哈希索引替代线性扫描。
        """

        best_dist = max_distance + 1
        best_row: dict[str, Any] | None = None
        for row in self._db.list_whitelist():
            phash_hex = _pick_phash_hex(row)
            if phash_hex is None:
                logger.warning("白名单行缺少 phash 字段，已跳过: %s", row)
                continue
            distance = phash - imagehash.hex_to_hash(phash_hex)
            if distance < best_dist:
                best_dist = distance
                best_row = row
        if best_row is None:
            return None
        # distance 必须是原生 int：imagehash 返回 numpy 标量（np.int64），
        # 会令编排器 _persist 的 json.dumps 抛 TypeError（S2 缺陷修复，
        # 由 T13 test_debug 专项的 TestSrcDefectS2 用例回归验证）。
        return {**best_row, "distance": int(best_dist)}

    def save(self) -> None:
        """持久化：no-op（DB 即持久化，契约中的 save/load 以 DB 为准）。"""

    def load(self) -> None:
        """加载：no-op（DB 即持久化，启动时由 DB 层直接读取）。"""


def _read_image_bytes(path_or_bytes: str | Path | bytes) -> bytes:
    """统一入口：路径读字节 / 字节直通；读取失败抛 ValueError 带原因。"""

    if isinstance(path_or_bytes, bytes):
        return path_or_bytes
    path = Path(path_or_bytes)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"图片文件读取失败: {path} ({exc})") from exc


def _pick_phash_hex(row: dict[str, Any]) -> str | None:
    """从白名单行中取 pHash 十六进制串（兼容多种键名）。"""

    for key in _PHASH_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None
