"""图片管线：解码 / 抽帧 / 哈希 / 白名单匹配（PRD §3.1/§3.2，分工文档契约 T4b；v0.2 M3 动图抽帧）。

- ``decode_images``：base64 / URL 两种输入解码为 PIL.Image 列表；动图
  （GIF/APNG，``n_frames>1`` 或 ``format=GIF``）按 ``animated`` 配置（对应
  ``config.image.animated``）处理：enabled + mode=uniform → 均匀抽帧（默认 5
  帧）返回全部帧（扁平列表，每帧一个元素）；disabled / mode=first / 未给配置
  → 维持 v0.1 首帧降级（PRD §3.1）；URL 拉取使用 httpx 异步客户端（异步网络
  请求，禁止 requests），超时默认 10s；解码失败抛 ``ValueError`` 并带原因。
- ``extract_frames``：Pillow seek 均匀取帧（\"motion 连贯性\" 抽帧）。
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
from typing import TYPE_CHECKING, Any

import httpx
import imagehash
from PIL import Image

from safefusion.logging_setup import get_logger
from safefusion.models.schemas import ImageInput

if TYPE_CHECKING:
    from safefusion.config import AnimatedImageConfig

logger: logging.Logger = get_logger("engines.image_pipeline")

#: 哈希尺寸：16x16 = 256 位感知哈希（与 PRD §3.1/§3.2 一致）
HASH_SIZE = 16

#: 白名单行中 pHash 十六进制串字段的候选键名（兼容未来 T2 实现差异）
_PHASH_KEYS = ("phash_hex", "phash")


async def decode_images(
    inputs: list[ImageInput],
    *,
    timeout: float = 10.0,
    animated: AnimatedImageConfig | None = None,
) -> list[Image.Image]:
    """将图片输入列表解码为 PIL.Image 帧列表（保持输入顺序，动图展开为多帧）。

    Args:
        inputs: 图片输入列表，每项 ``url`` 与 ``base64`` 二选一
            （``models.schemas.ImageInput`` 校验）。
        timeout: URL 拉取超时（秒），默认 10s。
        animated: 动图抽帧配置（``config.image.animated``）；enabled 且
            mode=uniform 时动图按 ``frames`` 均匀抽帧（每帧作为列表独立元素，
            多个输入依序拼接）；为 None 或 disabled 时保持 v0.1 首帧降级行为。

    Returns:
        按输入顺序的 PIL.Image 帧列表（已归一化为 RGB；动图 enabled 时输出
        全部抽帧，静态图每输入一帧）。
    """
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "SafeFusion/0.1"},
    ) as client:
        frames: list[Image.Image] = []
        for inp in inputs:
            frames.extend(await _decode_one(inp, client, animated))
        return frames


async def _decode_one(
    inp: ImageInput, client: httpx.AsyncClient, animated: AnimatedImageConfig | None
) -> list[Image.Image]:
    """解码单个输入为帧列表；任何失败统一抛 ValueError 并带原因。"""

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
        return _bytes_to_frames(data, animated)
    except ValueError:
        raise
    except Exception as exc:  # 网络层 / 解码层异常统一转为 ValueError
        raise ValueError(f"图片解码失败（url={inp.url!r}）: {exc}") from exc


def extract_frames(img: Image.Image, frames: int) -> list[Image.Image]:
    """均匀抽取动图帧（Pillow seek，PRD v0.2 M3）。

    在 ``0..n_frames-1`` 上取 ``frames`` 个等距索引（含首尾，round 取整），
    逐帧 ``seek`` 后归一化 RGB；Pillow 的 GIF 插件在 ``seek`` 时会自动合成
    帧间差异（disposal），因此每帧即为完整帧。

    Args:
        img: 已打开的动图 PIL.Image（``is_animated`` 为 True 或
            ``n_frames > 1``；静态图 / 单帧动图返回单帧）。
        frames: 目标抽帧数（≥1）；超过实际总帧数时返回全部帧。

    Returns:
        按时间顺序均匀抽取的帧列表（每帧已归一化为 RGB）。
    """
    total = int(getattr(img, "n_frames", 1) or 1)
    count = max(1, min(int(frames or 1), total))
    if count <= 1:
        img.seek(0)
        return [_to_rgb(img)]
    if count == total:
        indices = list(range(total))
    else:
        # count < total ⇒ step > 1 ⇒ 索引互不重复，无需去重
        step = (total - 1) / (count - 1)
        indices = sorted({round(step * i) for i in range(count)})
    out: list[Image.Image] = []
    for idx in indices:
        img.seek(idx)
        out.append(_to_rgb(img))
    return out


def _is_animated(img: Image.Image) -> bool:
    """动图判定：PIL ``is_animated``（n_frames>1）或 ``format == "GIF"``。

    与 PRD v0.2 M3「PIL n_frames>1 或 format=GIF」一致：多帧 GIF/APNG 或
    任何 GIF 格式（含单帧 GIF）都按动图路径处理。
    """
    if getattr(img, "is_animated", False):
        return True
    return getattr(img, "format", None) == "GIF"


def _bytes_to_frames(data: bytes, animated: AnimatedImageConfig | None) -> list[Image.Image]:
    """字节 → 帧列表：动图按 ``animated`` 配置抽帧，否则/禁用取首帧（归一化 RGB）。"""

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ValueError(f"图片数据无法解码为图像: {exc}") from exc
    if not _is_animated(img):
        return [_to_rgb(img)]
    if (
        animated is not None
        and getattr(animated, "enabled", False)
        and getattr(animated, "mode", "uniform") == "uniform"
    ):
        return extract_frames(img, int(getattr(animated, "frames", 5) or 1))
    # disabled / mode=first / 未携带配置 → v0.1 首帧降级（PRD §3.1）
    img.seek(0)
    return [_to_rgb(img)]


def _bytes_to_first_frame(data: bytes) -> Image.Image:
    """字节 → PIL.Image；动图取首帧（v0.1 降级，PRD §3.1）；归一化为 RGB。"""

    return _bytes_to_frames(data, None)[0]


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
