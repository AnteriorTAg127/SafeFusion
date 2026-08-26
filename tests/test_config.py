"""配置加载测试：三层合并（默认值 → YAML → 环境变量）、密钥剥离与解析。

对应 T1 任务卡验收与 PRD §6（密钥仅环境变量）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from safefusion.config import AppConfig, load_config


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class TestDefaults:
    """无 YAML 无环境变量时全部取内置默认值（对齐 PRD §3.4 / 分工配置键）。"""

    def test_core_defaults(self) -> None:
        cfg = load_config(None)
        assert cfg.server.port == 8000
        assert cfg.server.admin_port == 8001
        assert cfg.data_dir == "./data"
        assert cfg.thresholds.semantic_threshold == 0.67
        assert cfg.thresholds.margin_w == 0.05
        assert cfg.thresholds.confidence_low == 0.35
        assert cfg.thresholds.confidence_high == 0.75
        assert cfg.thresholds.phash_whitelist_distance == 5
        assert cfg.thresholds.phash_dedup_distance == 3

    def test_nested_defaults(self) -> None:
        cfg = load_config(None)
        assert cfg.embedding.backend == "local"
        assert cfg.embedding.local.model_name == "OFA-Sys/chinese-clip-vit-base-patch16"
        assert cfg.llm.timeout == 3.0
        assert cfg.llm.max_retry == 1
        assert cfg.llm.short_text_max_length == 20
        assert cfg.cache.audit_cache.ttl == 3600
        assert cfg.cache.permanent_lists is True
        assert cfg.light_model.model_path is None
        assert cfg.logging.json_lines is True

    def test_model_validate_empty_matches(self) -> None:
        assert AppConfig.model_validate({}).server.port == 8000


class TestYamlLayer:
    """YAML 覆盖默认值 / 顶层结构 / 未知键 / 缺失文件。"""

    def test_yaml_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        _write_yaml(
            path,
            {
                "server": {"port": 9090},
                "thresholds": {"semantic_threshold": 0.55, "margin_w": 0.1},
                "llm": {"short_text_max_length": 50},
            },
        )
        cfg = load_config(str(path))
        assert cfg.server.port == 9090
        assert cfg.thresholds.semantic_threshold == 0.55
        assert cfg.thresholds.margin_w == 0.1
        # 未覆盖的键仍取默认
        assert cfg.thresholds.confidence_low == 0.35
        assert cfg.llm.short_text_max_length == 50

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        _write_yaml(path, {"nope_key": 1})
        with pytest.raises(ValueError):
            load_config(str(path))

    def test_top_level_list_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="顶层必须是映射"):
            load_config(str(path))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "no_such.yaml"))

    def test_empty_yaml_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        path.write_text("", encoding="utf-8")
        assert load_config(str(path)).server.port == 8000


class TestSecretHandling:
    """YAML 中的 api_key 被剥离；密钥只从环境变量解析。"""

    def test_yaml_secret_stripped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = tmp_path / "cfg.yaml"
        _write_yaml(
            path,
            {
                "llm": {"api_key": "yaml-secret", "model": "gpt-test"},
                "embedding": {"cloud": {"api_key": "cloud-yaml-secret"}},
            },
        )
        with caplog.at_level(logging.WARNING, logger="safefusion.config"):
            cfg = load_config(str(path))
        assert cfg.llm.api_key is None
        assert cfg.embedding.cloud.api_key is None
        assert "禁止填写 api_key" in caplog.text

    def test_llm_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_LLM_API_KEY", "env-llm")
        cfg = load_config(None)
        assert cfg.llm.api_key == "env-llm"

    def test_llm_key_fallback_to_api_key_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "fallback-llm")
        cfg = load_config(None)
        assert cfg.llm.api_key == "fallback-llm"

    def test_llm_key_custom_env_name(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        _write_yaml(path, {"llm": {"api_key_env": "MY_LLM_KEY"}})
        monkeypatch.setenv("MY_LLM_KEY", "custom-llm")
        assert load_config(str(path)).llm.api_key == "custom-llm"

    def test_embedding_key_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_EMBEDDING_API_KEY", "env-emb")
        cfg = load_config(None)
        assert cfg.embedding.cloud.api_key == "env-emb"

    def test_no_key_env_means_none(self) -> None:
        cfg = load_config(None)
        assert cfg.llm.api_key is None
        assert cfg.embedding.cloud.api_key is None


class TestEnvLayer:
    """SAFEFUSION_<路径>_<键> 环境变量覆盖；未知变量忽略；非法值报错。"""

    def test_env_overrides_scalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.7")
        monkeypatch.setenv("SAFEFUSION_SERVER_PORT", "9999")
        cfg = load_config(None)
        assert cfg.thresholds.semantic_threshold == 0.7
        assert cfg.server.port == 9999

    def test_env_ignores_secret_like_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _api_key 结尾的环境变量由 _resolve_secret_keys 解析，不走普通覆盖
        monkeypatch.setenv("SAFEFUSION_LLM_API_KEY", "abc")
        cfg = load_config(None)
        assert cfg.llm.api_key == "abc"

    def test_env_invalid_scalar_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAFEFUSION_SERVER_PORT", "not-an-int")
        from pydantic import ValidationError

        with pytest.raises(ValidationError):  # 赋值校验失败（validate_assignment）
            load_config(None)

    def test_env_unknown_var_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SAFEFUSION_TOTALLY_UNKNOWN", "x")
        with caplog.at_level(logging.WARNING, logger="safefusion.config"):
            cfg = load_config(None)
        assert cfg.server.port == 8000
        assert "忽略未识别的配置环境变量" in caplog.text

    def test_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        path = tmp_path / "cfg.yaml"
        _write_yaml(path, {"thresholds": {"semantic_threshold": 0.55}})
        monkeypatch.setenv("SAFEFUSION_THRESHOLDS_SEMANTIC_THRESHOLD", "0.8")
        assert load_config(str(path)).thresholds.semantic_threshold == 0.8


class TestYamlToPydantic:
    """YAML 直接喂给 pydantic 模型的等价性（load_config 的数据来源）。"""

    def test_yaml_no_unknown_nested(self) -> None:
        with pytest.raises(ValueError):
            AppConfig.model_validate({"thresholds": {"semantic_threshold": 0.5, "bogus": 1}})


class TestLoggingSetup:
    """日志组件：命名空间 / JSON 行格式化器 / 级别初始化（T1 基础设施）。"""

    def test_get_logger_namespace(self) -> None:
        from safefusion.logging_setup import get_logger

        assert get_logger("config").name == "safefusion.config"
        assert get_logger("safefusion.already").name == "safefusion.already"

    def test_json_line_formatter(self) -> None:
        import json
        import logging

        from safefusion.logging_setup import JsonLineFormatter

        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            "safefusion.core.orchestrator",
            logging.INFO,
            "orchestrator.py",
            1,
            "audit done",
            None,
            None,
        )
        record.request_id = "req-123"
        payload = json.loads(formatter.format(record))
        assert payload["level"] == "INFO"
        assert payload["message"] == "audit done"
        assert payload["logger"] == "safefusion.core.orchestrator"
        assert payload["request_id"] == "req-123"
        assert "ts" in payload

    def test_setup_logging_sets_level(self, make_config) -> None:
        import logging

        from safefusion.logging_setup import setup_logging

        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_level = root.level
        try:
            setup_logging(make_config(logging={"level": "DEBUG", "json_lines": False}))
            assert root.level == logging.DEBUG
        finally:
            root.handlers[:] = old_handlers
            root.setLevel(old_level)
