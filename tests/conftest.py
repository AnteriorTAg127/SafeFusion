"""测试公共夹具（T13 测试专项）。

- 配置工厂：``data_dir`` 一律指到 ``tmp_path``（绝不触碰真实 data/）；
- 数据库 / 测试 Key / 图片字节工厂；
- 真实降级装配的 ``AppContext``（无 torch / 云端 Key 时组件级降级，
  供 API 与编排层契约测试使用）。

假组件（FakeEmbedding / FakeStore / FakeLLM 等）集中在 ``fakes.py``，
由各测试模块直接导入。
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from safefusion.config import AppConfig
from safefusion.storage.database import Database

#: 会被测试污染的敏感环境变量（密钥 / 管理令牌）
_ENV_SENSITIVE: tuple[str, ...] = ("OPENAI_API_KEY", "ADMIN_PASSWORD")

#: 工作区内测试临时目录根（沙箱下系统 %TEMP% 与 pytest 默认 basetemp 不可写）
_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / "pytest_ws_tmp"


@pytest.fixture(autouse=True)
def _clean_sensitive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例开始前清空 SAFEFUSION_* 与敏感 Key 环境变量，保证配置/密钥测试隔离。"""
    for name in list(os.environ):
        if name.startswith("SAFEFUSION_") or name in _ENV_SENSITIVE:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tmp_path() -> Path:
    """替代 pytest 内置 tmp_path：工作区内每用例独立临时目录（用后清理）。"""
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = _TEST_TMP_ROOT / uuid.uuid4().hex
    directory.mkdir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def build_config(data_dir: str | Any, **overrides: Any) -> AppConfig:
    """构造 AppConfig：默认 ``data_dir`` 指向给定目录，其余字段可取默认值。"""
    return AppConfig.model_validate({"data_dir": str(data_dir), **overrides})


@pytest.fixture
def make_config(tmp_path: Any) -> Callable[..., AppConfig]:
    """配置工厂夹具：``make_config(thresholds={...}, ...)`` 覆盖任意配置分组。"""

    def _make(**overrides: Any) -> AppConfig:
        return build_config(tmp_path, **overrides)

    return _make


@pytest.fixture
def app_config(make_config: Callable[..., AppConfig]) -> AppConfig:
    """最小可用配置实例（data_dir 指向 tmp_path）。"""
    return make_config()


@pytest.fixture
def tmp_db(tmp_path: Any) -> Database:
    """tmp_path 下的真实 SQLite Database 实例（用后关闭）。"""
    db = Database(tmp_path / "audit.db")
    yield db
    db.close()


@pytest.fixture
def make_key(tmp_db: Database) -> Callable[..., str]:
    """测试 Key 工厂：往 tmp_db 创建 Key 并返回明文。"""

    def _make(
        tier: str = "standard",
        enabled: bool = True,
        note: str | None = None,
        key: str | None = None,
    ) -> str:
        plain = key or f"sf_test_{uuid.uuid4().hex}"
        tmp_db.create_key(plain, tier=tier, enabled=enabled, note=note)
        return plain

    return _make


def png_bytes(
    size: tuple[int, int] = (32, 32),
    color: tuple[int, int, int] = (200, 30, 30),
    fmt: str = "PNG",
) -> bytes:
    """生成一张纯色 PNG/JPEG/GIF 图片字节。"""
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def image_input_b64(data: bytes) -> Any:
    """图片字节 → schemas.ImageInput(base64=...)。"""
    from safefusion.models.schemas import ImageInput

    return ImageInput(base64=base64.b64encode(data).decode("ascii"))


@pytest.fixture
def build_app_context(tmp_path: Any) -> Callable[..., Any]:
    """真实降级装配工厂：``build_app_context(**overrides) -> AppContext``。

    无 torch / 云端 Key 环境下：embedding/semantic 等组件为 None（降级），
    其余组件（database/cache/wordlist 等）真实装配。
    """

    def _build(**overrides: Any) -> Any:
        from safefusion.core.context import AppContext

        return AppContext.build(build_config(tmp_path, **overrides))

    return _build
