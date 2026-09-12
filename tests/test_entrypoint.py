"""统一入口配置文件路径解析测试（v0.5.0 回归：入口曾硬编码 ``load_config(None)``）。

覆盖：
- ``--config`` 命令行参数优先于 ``SAFEFUSION_CONFIG`` 环境变量；
- 两者皆缺时返回 None（回落「内置默认值 + 环境变量」）；
- 未知参数（uvicorn 等透传）不致解析失败；
- ``main()`` 确实把解析结果交给 ``load_config``，而非写死 None；
- 解析结果与 ``load_config`` 串联后，YAML 中的值真实生效（端到端）。

背景：README 与 docs/setup-guide 引导用户「复制 config.example.yaml 为
config.yaml」，但入口此前硬编码 ``load_config(None)``，导致该文件从未被加载、
配置静默失效（详见 开发/v0.5.0/报告核验-03-SafeFusion缺陷与修复方案.md 缺陷 3）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

api_main = importlib.import_module("safefusion.api.__main__")

_YAML_BODY = "server:\n  admin_port: 18101\nthresholds:\n  semantic_threshold: 0.91\n"


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主机的 SAFEFUSION_CONFIG，避免用例结果随环境漂移。"""

    monkeypatch.delenv("SAFEFUSION_CONFIG", raising=False)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(_YAML_BODY, encoding="utf-8")
    return path


class TestResolveConfigPath:
    """``_resolve_config_path``：``--config`` > ``SAFEFUSION_CONFIG`` > None。"""

    def test_cli_argument(self, config_file: Path) -> None:
        assert api_main._resolve_config_path(["--config", str(config_file)]) == str(config_file)

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        env_path = tmp_path / "from-env.yaml"
        monkeypatch.setenv("SAFEFUSION_CONFIG", str(env_path))
        assert api_main._resolve_config_path([]) == str(env_path)

    def test_cli_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch, config_file: Path, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("SAFEFUSION_CONFIG", str(tmp_path / "from-env.yaml"))
        assert api_main._resolve_config_path(["--config", str(config_file)]) == str(config_file)

    def test_absent_returns_none(self) -> None:
        assert api_main._resolve_config_path([]) is None

    def test_unknown_args_tolerated(self) -> None:
        """uvicorn 等透传的未知参数不应让入口解析直接失败。"""

        assert api_main._resolve_config_path(["--foo", "bar"]) is None

    def test_unknown_args_do_not_mask_config(self, config_file: Path) -> None:
        argv = ["--foo", "bar", "--config", str(config_file)]
        assert api_main._resolve_config_path(argv) == str(config_file)


class TestMainWiring:
    """``main()`` 与 ``load_config`` 的接线（缺陷 3 的直接回归点）。"""

    def test_main_forwards_config_path(
        self, monkeypatch: pytest.MonkeyPatch, config_file: Path
    ) -> None:
        """入口必须把解析出的路径传给 load_config（此前写死 None）。"""

        seen: dict[str, object] = {}

        class _Abort(Exception):
            """哨兵：在 load_config 处中断 main，避免真正拉起两个服务。"""

        def _fake_load(path: object = None) -> None:
            seen["path"] = path
            raise _Abort

        monkeypatch.setattr(api_main, "load_config", _fake_load)
        with pytest.raises(_Abort):
            api_main.main(["--config", str(config_file)])
        assert seen["path"] == str(config_file)

    def test_main_without_config_passes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        class _Abort(Exception):
            """哨兵：同上。"""

        def _fake_load(path: object = None) -> None:
            seen["path"] = path
            raise _Abort

        monkeypatch.setattr(api_main, "load_config", _fake_load)
        with pytest.raises(_Abort):
            api_main.main([])
        assert seen["path"] is None

    def test_config_file_values_effective(self, config_file: Path) -> None:
        """端到端：经入口解析的路径交给 load_config 后，YAML 值真实生效。"""

        resolved = api_main._resolve_config_path(["--config", str(config_file)])
        loaded = api_main.load_config(resolved)
        assert loaded.server.admin_port == 18101
        assert loaded.thresholds.semantic_threshold == pytest.approx(0.91)
