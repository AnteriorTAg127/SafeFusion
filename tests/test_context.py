"""AppContext 热重载与规则开关测试（PRD v0.2 M4）：reload_keywords / reload_rules /
regex_rules_enabled=False 透传。全走 tmp_path 真实 SQLite（AppContext.build 降级装配）。"""

from __future__ import annotations

from safefusion.core.context import AppContext
from safefusion.engines.keyword_engine import KeywordHitData

from .conftest import build_config

_M1_HIT = KeywordHitData("加我", "广告", "加我", 0, 2)


def _seed_keyword_rule(tmp_path) -> AppContext:
    """装配 context + 写入词条/豁免规则，返回已热重载的 AppContext。"""
    ctx = AppContext.build(build_config(tmp_path))  # 默认 regex_rules_enabled=True
    ctx.database.add_keywords([("广告", "加我", None)])
    ctx.database.add_rules([("广告", "加我好友", "exempt", "天气豁免")])
    assert ctx.reload_keywords() is True
    return ctx


class TestRulesSwitch:
    """regex_rules_enabled=False：规则层整体跳过（disambiguate 透传，v0.1 行为）。"""

    def test_build_disabled_passthrough(self, tmp_path) -> None:
        ctx = AppContext.build(build_config(tmp_path, keyword={"regex_rules_enabled": False}))
        assert ctx.keyword_engine is not None
        assert ctx.keyword_engine.rules_enabled is False
        # 词库照常命中，但规则层关闭 → 无豁免
        ctx.database.add_keywords([("广告", "加我", None)])
        ctx.reload_keywords()
        assert len(ctx.keyword_engine.scan("加我好友")) == 1
        kept, exempted = ctx.keyword_engine.disambiguate("加我好友", [_M1_HIT])
        assert len(kept) == 1
        assert exempted == []

    def test_build_false_does_not_load_rules_even_if_present(self, tmp_path) -> None:
        ctx = AppContext.build(build_config(tmp_path, keyword={"regex_rules_enabled": False}))
        ctx.database.add_keywords([("广告", "加我", None)])
        ctx.database.add_rules([("广告", "加我好友", "exempt", None)])
        ctx.reload_keywords()
        assert ctx.keyword_engine.rules_enabled is False


class TestBuildLoadsRules:
    """build 时按 db rules 表加载；reload_keywords 免重启生效。"""

    def test_build_enabled_empty_rules_ok(self, tmp_path) -> None:
        ctx = AppContext.build(build_config(tmp_path))
        assert ctx.keyword_engine is not None
        assert ctx.keyword_engine.rules_enabled is True  # 默认开启（规则为空表也启用）

    def test_reload_keywords_rules_effective(self, tmp_path) -> None:
        ctx = _seed_keyword_rule(tmp_path)
        assert ctx.keyword_engine.rules_enabled is True
        kept, exempted = ctx.keyword_engine.disambiguate("加我好友", [_M1_HIT])
        assert kept == []
        assert len(exempted) == 1

    def test_reload_rules_after_write_takes_effect(self, tmp_path) -> None:
        ctx = AppContext.build(build_config(tmp_path))
        ctx.database.add_keywords([("广告", "加我", None)])
        # 写入规则前 → 无豁免；写入 + reload_rules 后 → 豁免生效（免重启）
        ctx.reload_keywords()
        kept, _ = ctx.keyword_engine.disambiguate("加我好友", [_M1_HIT])
        assert len(kept) == 1
        ctx.database.add_rules([("广告", "加我好友", "exempt", None)])
        assert ctx.reload_rules() is True
        kept, exempted = ctx.keyword_engine.disambiguate("加我好友", [_M1_HIT])
        assert kept == []
        assert len(exempted) == 1

    def test_reload_keywords_removes_old_word(self, tmp_path) -> None:
        ctx = _seed_keyword_rule(tmp_path)
        assert len(ctx.keyword_engine.scan("加我")) == 1
        ctx.database.delete_keyword(ctx.database.list_keywords()[0]["id"])
        assert ctx.reload_keywords() is True
        assert ctx.keyword_engine.scan("加我") == []

    def test_reload_rules_deactivation_applies(self, tmp_path) -> None:
        ctx = _seed_keyword_rule(tmp_path)
        rule_id = ctx.database.list_rules()[0]["id"]
        assert ctx.database.set_rule_active(rule_id, False) is True
        assert ctx.reload_rules() is True
        kept, _ = ctx.keyword_engine.disambiguate("加我好友", [_M1_HIT])
        assert len(kept) == 1  # 停用规则不再参与消歧


class TestReloadFailure:
    """重载失败路径：返回 False 且不抛异常（旧实例语义）。"""

    def test_unassembled_returns_false(self) -> None:
        assert AppContext().reload_keywords() is False
        assert AppContext().reload_rules() is False

    def test_engine_reload_error_returns_false(self, tmp_path, monkeypatch) -> None:
        ctx = AppContext.build(build_config(tmp_path))
        ctx.database.add_keywords([("广告", "加我", None)])
        ctx.database.add_rules([("广告", "加我好友", "exempt", None)])

        def _boom(categories: dict, rules: list[dict] | None = None) -> None:
            raise ValueError("注入失败")

        monkeypatch.setattr(ctx.keyword_engine, "reload", _boom)
        assert ctx.reload_keywords() is False
