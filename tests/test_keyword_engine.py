"""关键词引擎测试：基础命中 / 拼音变体 / 同音回映射 / 全半角 / 繁简 / 符号分隔 /
多类别 / 空词库 / 正则消歧（exempt & violate）。

场景素材来自 T3 自检脚本（开发/v0.1/tmp/t3_check.py），用例为 pytest 重写。
"""

from __future__ import annotations

import pytest

from safefusion.engines.keyword_engine import (
    KeywordEngine,
    KeywordHitData,
    RegexRuleEngine,
    generate_variants,
)


def has_hit(hits: list[KeywordHitData], keyword: str, start: int, end: int) -> bool:
    return any(h.keyword == keyword and h.start == start and h.end == end for h in hits)


class TestBasicMatching:
    """基础词命中与位置映射。"""

    def test_multi_keyword_positions_and_order(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊"], "广告": ["加我"]})
        hits = eng.scan("来裸聊加我")
        assert len(hits) == 2
        assert has_hit(hits, "裸聊", 1, 3)
        assert has_hit(hits, "加我", 3, 5)
        assert [h.start for h in hits] == sorted(h.start for h in hits)

    def test_category_recorded(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊"]})
        hits = eng.scan("裸聊")
        assert hits[0].category == "色情"
        assert hits[0].matched == "裸聊"

    def test_scan_without_load_returns_empty(self) -> None:
        assert KeywordEngine().scan("裸聊") == []

    def test_scan_empty_text(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊"]})
        assert eng.scan("") == []

    def test_same_position_multiple_categories(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊"], "广告": ["裸聊"]})
        hits = eng.scan("来裸聊")
        assert len(hits) == 2
        assert all(h.keyword == "裸聊" and (h.start, h.end) == (1, 3) for h in hits)


class TestPinyinVariants:
    """拼音全拼 / 首字母变体与同音词回映射。"""

    def test_pinyin_full(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"测试": ["捡闻"]})
        hits = eng.scan("jianwen 一下")
        assert has_hit(hits, "捡闻", 0, 7)
        assert any(h.matched == "jianwen" for h in hits)

    def test_pinyin_full_task_word(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["接吻"]})
        hits = eng.scan("jiewen 一下")
        assert has_hit(hits, "接吻", 0, 6)

    def test_pinyin_no_false_hit(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"测试": ["捡闻"]})
        hits = eng.scan("jiewen 一下")
        assert not has_hit(hits, "捡闻", 0, 7)

    def test_pinyin_initials(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"敏感": ["境外"]})
        hits = eng.scan("有jw风险提示")
        assert has_hit(hits, "境外", 1, 3)
        assert any(h.matched == "jw" for h in hits)

    def test_same_sound_back_mapping(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"测试": ["捡闻"]})
        hits = eng.scan("见闻很多")
        assert has_hit(hits, "捡闻", 0, 2)
        assert any(h.matched == "见闻" for h in hits)

    def test_pinyin_requires_min_han_length(self) -> None:
        # 单字词不做同音匹配：正文「接住」拼音展开串 jiezhu 含 "jie"，但
        # _han_count("接")=1 < 2 → 拼音展开命中被过滤，不产生重复命中
        eng = KeywordEngine()
        eng.load_categories({"测试": ["接"]})
        hits = eng.scan("接住")
        assert len(hits) == 1
        assert has_hit(hits, "接", 0, 1)


class TestOrthographicVariants:
    """全半角 / 繁简 / 符号分隔变体。"""

    def test_fullwidth(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"广告": ["+q"]})
        assert has_hit(eng.scan("＋Ｑ"), "+q", 0, 2)
        assert has_hit(eng.scan("＋ｑ"), "+q", 0, 2)

    def test_traditional_from_simplified(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"赌博": ["赌博"]})
        hits = eng.scan("線上賭博")
        assert has_hit(hits, "赌博", 2, 4)
        assert any(h.matched == "賭博" for h in hits)

    def test_simplified_from_traditional(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"赌博": ["賭博"]})
        hits = eng.scan("线上赌博")
        assert has_hit(hits, "賭博", 2, 4)
        assert any(h.matched == "赌博" for h in hits)

    def test_symbol_separator(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊"]})
        hits = eng.scan("来裸@聊找我")
        assert has_hit(hits, "裸聊", 1, 4)
        assert any(h.matched == "裸@聊" for h in hits)


class TestEmptyLexicon:
    """空词库加载与扫描（T10 报告缺陷①修复后的行为）。"""

    def test_empty_categories_scan_empty(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({})
        assert eng.loaded is True
        assert eng.scan("任何文本") == []

    def test_blank_words_skipped(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["   ", "裸聊"]})
        assert len(eng.scan("裸聊")) == 1

    def test_duplicate_words_warn_and_skip(self) -> None:
        eng = KeywordEngine()
        eng.load_categories({"色情": ["裸聊", "裸聊"]})
        assert len(eng.scan("裸聊")) == 1


class TestGenerateVariants:
    """公共变体生成函数可复用。"""

    def test_literal_first_and_dedup(self) -> None:
        variants = generate_variants("裸聊")
        assert variants[0] == "裸聊"
        assert len(variants) == len(set(variants))
        assert "" not in variants

    def test_contains_expected_kinds(self) -> None:
        variants = generate_variants("赌博")
        assert "dubo" in variants
        assert "db" in variants
        assert "賭博" in variants  # 繁简转换
        assert "赌@博" in variants  # 符号分隔（基于原文）
        assert "＋Ｑ" in generate_variants("+q")


class TestRegexRuleEngine:
    """正则语境消歧：exempt 豁免 / violate 强命中 / 无规则直通 / 配置校验。"""

    def _hit(self, keyword: str = "加我", category: str = "广告", start: int = 2) -> KeywordHitData:
        text_len = start + len(keyword)
        return KeywordHitData(keyword, category, keyword, start, text_len)

    def test_no_rules_passthrough(self) -> None:
        kept, exempted = RegexRuleEngine().disambiguate("欢迎加我好友交流", [self._hit()])
        assert len(kept) == 1
        assert exempted == []

    def test_exempt_removes_hit_with_reason(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "加我好友", "category": "广告", "action": "exempt"}])
        kept, exempted = rr.disambiguate("欢迎加我好友交流", [self._hit()])
        assert kept == []
        assert len(exempted) == 1
        assert exempted[0]["hit"].keyword == "加我"
        assert exempted[0]["rule"]["action"] == "exempt"

    def test_exempt_category_mismatch_keeps_hit(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "加我好友", "category": "色情", "action": "exempt"}])
        kept, _ = rr.disambiguate("欢迎加我好友交流", [self._hit()])
        assert len(kept) == 1

    def test_exempt_without_category_applies_to_all(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "加我好友", "action": "exempt"}])
        kept, _ = rr.disambiguate("欢迎加我好友交流", [self._hit()])
        assert kept == []

    def test_context_window_controls_exempt(self) -> None:
        # 窗口 0：豁免正则在窗口外 → 不豁免
        rr = RegexRuleEngine(context_window=0)
        rr.load([{"pattern": "好友", "category": "广告", "action": "exempt"}])
        # 命中片段"加我"在 [2,4)，上下文窗口 0 不含"好友"（位于 [2,4) 之后）
        kept, _ = rr.disambiguate("加我好友", [self._hit(start=0)])
        assert len(kept) == 1

    def test_exempt_within_window(self) -> None:
        rr = RegexRuleEngine()  # 默认窗口 8
        rr.load([{"pattern": "好友", "category": "广告", "action": "exempt"}])
        kept, exempted = rr.disambiguate("加我好友", [self._hit(start=0)])
        assert kept == []
        assert len(exempted) == 1

    def test_violate_appends_strong_hit(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "代开(发票|票)", "category": "诈骗", "action": "violate"}])
        kept, exempted = rr.disambiguate("要代开发票吗", [])
        assert exempted == []
        assert len(kept) == 1
        assert kept[0].category == "诈骗"
        assert kept[0].matched == "代开发票"
        assert kept[0].keyword == "代开(发票|票)"

    def test_violate_no_match_nothing_appended(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "代开发票", "category": "诈骗", "action": "violate"}])
        kept, _ = rr.disambiguate("正常的文本", [self._hit()])
        assert len(kept) == 1  # 原命中保留，无追加

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="action"):
            RegexRuleEngine().load([{"pattern": "x", "action": "ban"}])

    def test_invalid_regex_raises(self) -> None:
        with pytest.raises(ValueError, match="无效正则"):
            RegexRuleEngine().load([{"pattern": "([", "action": "exempt"}])

    def test_exempt_plus_violate_compose(self) -> None:
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": "官方加我", "category": "广告", "action": "exempt"},
                {"pattern": "加我", "category": "广告", "action": "violate"},
            ]
        )
        kept, exempted = rr.disambiguate("官方加我微信号", [self._hit()])
        # 原文命中被 exempt 豁免；violate 规则在全文命中 → 追加一条强命中
        assert len(exempted) == 1
        assert exempted[0]["hit"].keyword == "加我"
        assert len(kept) == 1
        assert kept[0].category == "广告"
        assert kept[0].matched == "加我"
