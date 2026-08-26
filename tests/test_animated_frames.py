"""v0.2 M3 动图抽帧测试：extract_frames / decode_images 动图路径 / 编排器逐帧链路。

覆盖（T16 任务卡验收）：
- ``extract_frames`` 均匀抽帧（6 帧→5 帧、抽帧超总数取全部、端点含首尾）；
- ``decode_images`` 按 ``config.image.animated``：enabled → 抽帧扁平列表；
  disabled / mode=first / 未配置 → v0.1 首帧降级；非动图行为不变；
- 编排器端到端：白名单全帧命中 → basic_rules_pass；单帧白名单未命中 →
  整体违规（语义层断言）；任一帧命中永久黑名单 → 整体违规；enabled=False →
  仅首帧走链路；LLM 兜底档收到全部帧且 ``animated=True`` 触发多帧提示；
  静态多图请求 ``animated=False`` 不触发动图提示。

注：白名单帧使用噪声图（pHash 相互距离 >100，远超阈值 5），避免纯色帧
pHash 全同导致白名单误命中（v0.1 手测已确认纯色帧 pHash 距离为 0）。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
from PIL import Image

from safefusion.cache.caches import CacheLayer
from safefusion.config import AnimatedImageConfig, AppConfig
from safefusion.core.context import AppContext
from safefusion.core.orchestrator import AuditOrchestrator
from safefusion.engines.image_pipeline import (
    WhitelistMatcher,
    compute_hashes,
    decode_images,
    extract_frames,
)
from safefusion.engines.keyword_engine import KeywordEngine
from safefusion.engines.light_model import LightTextModel
from safefusion.models.schemas import AuditRequest, ImageInput
from safefusion.storage.database import Database

from .conftest import png_bytes
from .fakes import FakeLLM, FakeSemantic

#: 编排器测试统一使用默认阈值配置（动画默认 enabled/frames=5/uniform）
_DEFAULT_CFG = AppConfig.model_validate({})


def _gif_bytes(n_frames: int, seed: int = 0, size: tuple[int, int] = (32, 32)) -> bytes:
    """生成 n 帧噪声 GIF（每帧独立 seeded 随机图案，pHash 相互远离）。"""
    rng = np.random.default_rng(seed)
    imgs = [
        Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), mode="RGB")
        for _ in range(n_frames)
    ]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return buf.getvalue()


def _all_frames(data: bytes) -> list[Image.Image]:
    """用公开 API 解出全部帧（抽帧数远超总帧数即全量），与解码路径归一化一致。"""
    return extract_frames(Image.open(io.BytesIO(data)), 1_000_000)


def _frame_md5s(frames: list[Image.Image]) -> list[str]:
    return [compute_hashes(f)[0] for f in frames]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _mid_semantic() -> FakeSemantic:
    """LLM 档语义层结果（confidence 落在 [0.35, 0.75]）。"""
    return FakeSemantic(
        {
            "triggered": True,
            "confidence": 0.5,
            "category": "色情",
            "black_top": {"id": "b0", "score": 0.7, "category": "色情", "metadata": {}},
            "black_avg": 0.7,
            "white_avg": 0.3,
            "reason": None,
        }
    )


def _empty_keyword() -> KeywordEngine:
    kw = KeywordEngine()
    kw.load_categories({})
    return kw


def _container(
    db: Database | None,
    *,
    keyword: KeywordEngine | None = None,
    whitelist: WhitelistMatcher | None = None,
    semantic: FakeSemantic | None = None,
    llm: FakeLLM | None = None,
    cache: CacheLayer | None = None,
    cfg: AppConfig | None = None,
) -> AppContext:
    return AppContext(
        config=cfg or _DEFAULT_CFG,
        database=db,
        store=None,
        embedding=None,
        keyword_engine=keyword,
        light_model=LightTextModel(None, None),  # disabled：predict 不参与
        whitelist=whitelist,
        semantic=semantic,
        llm=llm,
        cache_layer=cache,
        degraded=[],
    )


class TestExtractFrames:
    """extract_frames 均匀抽帧（Pillow seek）。"""

    def test_sample_five_from_six_uniform(self) -> None:
        data = _gif_bytes(6)
        all_md5 = _frame_md5s(_all_frames(data))
        assert len(all_md5) == 6
        sampled = extract_frames(Image.open(io.BytesIO(data)), 5)
        md5s = _frame_md5s(sampled)
        assert len(md5s) == 5
        # 均匀取帧：0..5 上 5 个等距索引（round 取整）→ [0,1,2,4,5]
        assert md5s == [all_md5[i] for i in (0, 1, 2, 4, 5)]

    def test_frames_exceed_total_returns_all(self) -> None:
        data = _gif_bytes(4)
        all_md5 = _frame_md5s(_all_frames(data))
        sampled = extract_frames(Image.open(io.BytesIO(data)), 5)
        assert _frame_md5s(sampled) == all_md5  # 4 帧 GIF → 全部 4 帧

    def test_two_frames_endpoints(self) -> None:
        data = _gif_bytes(4)
        all_md5 = _frame_md5s(_all_frames(data))
        sampled = extract_frames(Image.open(io.BytesIO(data)), 2)
        assert _frame_md5s(sampled) == [all_md5[0], all_md5[3]]  # 首尾两帧

    def test_single_frame_request(self) -> None:
        data = _gif_bytes(4)
        all_md5 = _frame_md5s(_all_frames(data))
        sampled = extract_frames(Image.open(io.BytesIO(data)), 1)
        assert _frame_md5s(sampled) == [all_md5[0]]

    def test_static_image_single_frame(self) -> None:
        img = Image.open(io.BytesIO(png_bytes()))
        sampled = extract_frames(img, 5)
        assert len(sampled) == 1
        assert sampled[0].mode == "RGB"


class TestDecodeAnimated:
    """decode_images 按 config.image.animated 处理动图。"""

    async def test_enabled_extracts_all_frames(self) -> None:
        data = _gif_bytes(4)
        anim = AnimatedImageConfig()  # enabled=True / frames=5 / uniform
        frames = await decode_images([ImageInput(base64=_b64(data))], animated=anim)
        assert _frame_md5s(frames) == _frame_md5s(_all_frames(data))

    async def test_enabled_six_frames_sample_five(self) -> None:
        data = _gif_bytes(6)
        frames = await decode_images(
            [ImageInput(base64=_b64(data))], animated=AnimatedImageConfig()
        )
        assert len(frames) == 5

    async def test_disabled_first_frame_fallback(self) -> None:
        data = _gif_bytes(4)
        anim = AnimatedImageConfig(enabled=False)
        frames = await decode_images([ImageInput(base64=_b64(data))], animated=anim)
        assert len(frames) == 1
        assert _frame_md5s(frames) == [_frame_md5s(_all_frames(data))[0]]

    async def test_no_config_first_frame_legacy(self) -> None:
        data = _gif_bytes(4)
        frames = await decode_images([ImageInput(base64=_b64(data))])
        assert len(frames) == 1  # 未携带配置 → v0.1 首帧降级

    async def test_mode_first_falls_back(self) -> None:
        data = _gif_bytes(4)
        anim = AnimatedImageConfig(mode="first")
        frames = await decode_images([ImageInput(base64=_b64(data))], animated=anim)
        assert len(frames) == 1

    async def test_static_png_unaffected(self) -> None:
        frames = await decode_images(
            [ImageInput(base64=_b64(png_bytes()))], animated=AnimatedImageConfig()
        )
        assert len(frames) == 1
        assert frames[0].size == (32, 32)

    async def test_mixed_inputs_flattened_in_order(self) -> None:
        gif = _gif_bytes(4)
        png = png_bytes(color=(10, 200, 30))
        anim = AnimatedImageConfig()
        frames = await decode_images(
            [ImageInput(base64=_b64(gif)), ImageInput(base64=_b64(png))], animated=anim
        )
        assert len(frames) == 5
        gif_md5 = _frame_md5s(_all_frames(gif))
        assert _frame_md5s(frames[:4]) == gif_md5  # 动图帧在前，顺序保持
        png_frame = (await decode_images([ImageInput(base64=_b64(png))], animated=anim))[0]
        assert compute_hashes(frames[4])[0] == compute_hashes(png_frame)[0]


class TestWhitelistPerFrame:
    """白名单逐帧：全帧命中快放行；任一帧未命中 → 整体不快速放行。"""

    @staticmethod
    def _whitelist_all(tmp_path: Path, data: bytes) -> tuple[Database, WhitelistMatcher]:
        db = Database(tmp_path / "audit.db")
        matcher = WhitelistMatcher(db)
        for frame in _all_frames(data):
            md5_hex, phash = compute_hashes(frame)
            db.add_whitelist(md5_hex, str(phash), "frame")
        return db, matcher

    async def test_whitelist_all_frames_fast_pass(self, tmp_path: Path) -> None:
        data = _gif_bytes(4)
        db, matcher = self._whitelist_all(tmp_path, data)
        try:
            orch = AuditOrchestrator(
                _container(
                    db,
                    keyword=_empty_keyword(),
                    whitelist=matcher,
                    cache=CacheLayer({}),
                )
            )
            result = await orch.process_audit(
                AuditRequest(images=[ImageInput(base64=_b64(data))]), "full"
            )
            assert result.source == "basic_rules_pass"
            assert result.has_violation is False
            assert result.detail is not None
            assert [f.hit for f in result.detail.image_whitelist] == [True, True, True, True]
        finally:
            db.close()

    async def test_single_frame_miss_overall_violation(self, tmp_path: Path) -> None:
        data = _gif_bytes(4)
        db = Database(tmp_path / "audit.db")
        try:
            matcher = WhitelistMatcher(db)
            first = _all_frames(data)[0]
            md5_hex, phash = compute_hashes(first)
            db.add_whitelist(md5_hex, str(phash), "frame0")  # 仅首帧入白
            semantic = FakeSemantic(
                {
                    "triggered": True,
                    "confidence": 0.82,
                    "category": "色情",
                    "black_top": {"id": "b0", "score": 0.8, "category": "色情", "metadata": {}},
                    "black_avg": 0.8,
                    "white_avg": 0.2,
                    "reason": None,
                }
            )
            orch = AuditOrchestrator(
                _container(
                    db,
                    keyword=_empty_keyword(),
                    whitelist=matcher,
                    semantic=semantic,
                    cache=CacheLayer({}),
                )
            )
            result = await orch.process_audit(
                AuditRequest(images=[ImageInput(base64=_b64(data))]), "full"
            )
            # 任一帧白名单未命中 → 不快速放行 → 语义层断言违规
            assert result.has_violation is True
            assert result.source == "semantic"
            assert [f.hit for f in result.detail.image_whitelist] == [True, False, False, False]
        finally:
            db.close()

    async def test_any_frame_black_permanent_violates(self) -> None:
        data = _gif_bytes(4)
        md5s = _frame_md5s(_all_frames(data))
        cache = CacheLayer({})
        cache.load_permanent(black=[md5s[2]], white=[])  # 仅第 3 帧进永久黑名单
        orch = AuditOrchestrator(_container(None, keyword=_empty_keyword(), cache=cache))
        result = await orch.process_audit(
            AuditRequest(images=[ImageInput(base64=_b64(data))]), "full"
        )
        # 任一帧命中永久黑名单 → 整体违规（黑优先短路）
        assert result.has_violation is True
        assert result.source == "permanent_list"

    async def test_animated_disabled_first_frame_only(self, tmp_path: Path) -> None:
        data = _gif_bytes(4)
        db, matcher = self._whitelist_all(tmp_path, data)  # 全帧入白
        try:
            cfg = AppConfig.model_validate({"image": {"animated": {"enabled": False}}})
            orch = AuditOrchestrator(
                _container(
                    db,
                    keyword=_empty_keyword(),
                    whitelist=matcher,
                    cache=CacheLayer({}),
                    cfg=cfg,
                )
            )
            result = await orch.process_audit(
                AuditRequest(images=[ImageInput(base64=_b64(data))]), "full"
            )
            # v0.1 首帧降级：仅首帧参与白名单 → 命中 → 快放行
            assert result.source == "basic_rules_pass"
            assert [f.hit for f in result.detail.image_whitelist] == [True]
        finally:
            db.close()


class TestLLMMultiFrame:
    """LLM 兜底档：动图请求收到全部帧并带 animated=True（多帧提示）；静态多图不带。"""

    async def test_animated_judge_receives_all_frames(self) -> None:
        data = _gif_bytes(4)
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})  # 文本强信号 → 走语义层
        llm = FakeLLM(
            verdict={"is_violation": True, "category": "色情", "confidence": 0.7, "reason": "r"}
        )
        orch = AuditOrchestrator(_container(None, keyword=kw, semantic=_mid_semantic(), llm=llm))
        result = await orch.process_audit(
            AuditRequest(text="裸聊", images=[ImageInput(base64=_b64(data))]), "full"
        )
        assert result.source == "llm"
        assert llm.judge_calls == 1
        assert llm.last_animated is True  # 动图多帧 → 触发多帧连贯性提示
        assert len(llm.last_images) == 4  # 全部抽帧传入 LLM

    async def test_static_multi_image_no_animated_flag(self) -> None:
        kw = KeywordEngine()
        kw.load_categories({"色情": ["裸聊"]})
        llm = FakeLLM(
            verdict={"is_violation": True, "category": "色情", "confidence": 0.7, "reason": "r"}
        )
        orch = AuditOrchestrator(_container(None, keyword=kw, semantic=_mid_semantic(), llm=llm))
        result = await orch.process_audit(
            AuditRequest(
                text="裸聊",
                images=[
                    ImageInput(base64=_b64(png_bytes(color=(1, 2, 3)))),
                    ImageInput(base64=_b64(png_bytes(color=(4, 5, 6)))),
                ],
            ),
            "full",
        )
        assert result.source == "llm"
        assert llm.last_animated is False  # 静态多图 ≠ 动图多帧
        assert len(llm.last_images) == 2
