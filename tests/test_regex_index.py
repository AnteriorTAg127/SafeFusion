"""RegexRuleEngine 命中词索引测试（PRD v0.3.0）：短语提取 / 可索引性判定 /
倒排索引子集查询 / 正确性（索引路径 vs 全量路径逐字比对）/ 加速断言。

设计契约（正确性优先，索引仅为加速）：
- 索引裁剪**唯一**依据：规则可索引裁剪（``indexable``，命中必含某关键短语）
  且该短语未在原文出现 → 规则必然不命中 → 跳过；
- 其余一切情形（短语集合为空 / 判定构造含可缩量词 / 字符类 / 短语已出现）
  → 与旧行为一致地全量 regex.search；
- ``hit_words`` 命中词提示只放大扫描子集，绝不用于排除。
"""

from __future__ import annotations

import random
import re
from typing import Any

import pytest

from safefusion.engines.keyword_engine import (
    KeywordEngine,
    KeywordHitData,
    RegexRuleEngine,
    _analyze_pattern,
)

# 捕获原始 compile（计数包装内部仍用真 compile，避免猴子补丁自我递归）
_ORIG_COMPILE = re.compile


def _full_scan_disambiguate(
    engine: RegexRuleEngine, text: str, hits: list[KeywordHitData], window: int = 8
) -> tuple[list[KeywordHitData], list[dict]]:
    """旧行为参考实现：全部规则逐条扫描（索引路径的独立判定基准）。

    逐字复刻索引引入前的 disambiguate 语义（exempt 首条命中豁免 / violate
    全文追加 + 排序），用于证明索引裁剪不会改变任何判定结果。
    """

    kept: list[KeywordHitData] = []
    exempted: list[dict] = []
    for hit in hits:
        ctx = text[max(0, hit.start - window) : min(len(text), hit.end + window)]
        cause: dict | None = None
        for rule in engine._exempt_rules:
            rule_category = rule["raw"].get("category")
            if rule_category and rule_category != hit.category:
                continue
            if rule["pattern"].search(ctx):
                cause = rule["raw"]
                break
        if cause is not None:
            exempted.append({"hit": hit, "rule": cause})
        else:
            kept.append(hit)
    for rule in engine._violate_rules:
        match = rule["pattern"].search(text)
        if match is not None:
            kept.append(
                KeywordHitData(
                    keyword=rule["raw"]["pattern"],
                    category=rule["raw"].get("category") or "",
                    matched=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
    kept.sort(key=lambda h: (h.start, h.end, h.category, h.keyword))
    return kept, exempted


def _build_test_rules(n: int, rng: random.Random) -> list[dict]:
    """生成 n 条混合类型规则（字面量 / 交替 / 威胁型 / 无字面量）。

    覆盖：可索引裁剪（纯字面量、| 交替、(?i) 内联、{2,3} 保底量词）、
    威胁型（* / ? / {0..} 可缩量词、可选分组）、无短语（元字符 \\d / 空格
    分隔 / 字符类 / .{0,N}）与可豁免类别。
    """

    literal_pool = ["傻逼", "加我", "你好", "垃圾", "代开", "QQ", "VIP", "ab", "bc", "赌"]
    alt_pool = ["加我|你好", "代开(发票|票)", "(?:傻|煞)逼", "(?i)vip", "猪|狗东西"]
    threat_pool = ["ab*", "ab?", "赌{0,2}", "加我{0,3}", "(?:加我)?", "微信(?:|号)", "(?:ab|cd)?"]
    no_lit_pool = [
        r"\d{5,12}",
        "加.{0,3}我",
        r"[\u4e00-\u9fff]{1}",
        r"[a-z]+",
        "傻 逼",
        r"\s+",
        ".*",
        r"群\d{2,8}群",
    ]
    rules: list[dict] = []
    for i in range(n):
        kind = i % 4
        if kind == 0:
            pattern = rng.choice(literal_pool)
        elif kind == 1:
            pattern = rng.choice(alt_pool)
        elif kind == 2:
            pattern = rng.choice(threat_pool)
        else:
            pattern = rng.choice(no_lit_pool)
        action = "violate" if (i % 7) != 0 else "exempt"
        rules.append(
            {
                "pattern": pattern,
                "category": "广告" if action == "exempt" else rng.choice(["广告", "诈骗", "色情"]),
                "action": action,
            }
        )
    return rules


def _random_text(rng: random.Random, keywords: list[str]) -> tuple[str, list[KeywordHitData]]:
    """随机拼一段文本：混入部分关键词并生成对应命中列表（其余为噪声字符）。"""

    pieces: list[str] = []
    hits: list[KeywordHitData] = []
    noise_cjk = "啊的了我你他是不"
    noise_ascii = "xvq"
    for _step in range(rng.randint(2, 8)):
        roll = rng.random()
        if roll < 0.45:
            word = rng.choice(keywords)
            pieces.append(word)
            hits.append(
                KeywordHitData(
                    word,
                    rng.choice(["广告", "色情"]),
                    word,
                    0,  # 占位，最后按拼接位置重算
                    0,
                )
            )
        elif roll < 0.7:
            pieces.append("".join(rng.choice(noise_cjk) for _ in range(rng.randint(1, 6))))
        elif roll < 0.9:
            pieces.append("".join(rng.choice(noise_ascii) for _ in range(rng.randint(1, 5))))
        else:
            pieces.append(str(rng.randint(0, 10 ** rng.randint(1, 4))))
    text = "".join(pieces)
    real_hits = []
    for hit in hits:
        start = text.find(hit.keyword, 0)
        if start >= 0:
            real_hits.append(
                KeywordHitData(
                    hit.keyword, hit.category, hit.keyword, start, start + len(hit.keyword)
                )
            )
    return text, sorted(real_hits, key=lambda h: h.start)


class TestAnalyzePattern:
    """关键短语提取与可索引裁剪判定。"""

    def test_phrase_and_indexable_table(self) -> None:
        cases: list[tuple[str, set[str], bool]] = [
            ("傻逼", {"傻逼"}, True),
            ("傻逼|傻比", {"傻逼", "傻比"}, True),
            ("ab*", {"ab"}, False),
            ("ab?", {"ab"}, False),
            ("ab{0,3}", {"ab"}, False),
            ("ab{2,3}", {"ab"}, True),
            ("ab+", {"ab"}, True),
            ("(?:ab|cd){2}", {"ab", "cd"}, True),
            ("(ab|cd)?", {"ab", "cd"}, False),
            ("(?:加我)?", {"加我"}, False),
            (r"[a-z]{2}", set(), False),
            (r"\d{5,12}", set(), False),
            ("加.{0,3}我", set(), False),
            ("代开(发票|票)", {"代开", "发票"}, True),
            (r"群\d{5,12}", set(), False),
            ("(?:ab){0,3}", {"ab"}, False),
            ("(?i)vip", {"vip"}, True),
            ("傻 逼", set(), False),  # 空格分隔使短语断裂：命中"傻 逼"不含"傻逼"子串
            ("微信(?:|号)", {"微信"}, True),
        ]
        for pattern, expect_phrases, expect_indexable in cases:
            phrases, indexable = _analyze_pattern(pattern)
            assert phrases == frozenset(expect_phrases), pattern
            assert indexable == expect_indexable, pattern

    def test_case_normalization(self) -> None:
        # 短语统一小写（原文存在性按大小写不敏感判定，guardian 原匹配亦不区分大小写）
        phrases, indexable = _analyze_pattern("QQ|AbC")
        assert phrases == frozenset({"qq", "abc"})
        assert indexable is True


class TestSelectRulesForPhrases:
    """倒排索引子集查询。"""

    def test_subset_by_phrase(self) -> None:
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": "代开发票", "category": "诈骗", "action": "violate"},
                {"pattern": "加我|你好", "category": "广告", "action": "violate"},
                {"pattern": "加我好友", "category": "广告", "action": "exempt"},
                {"pattern": r"\d{5,12}", "category": "广告", "action": "violate"},
            ]
        )
        selected = rr.select_rules_for_phrases(["你好"])
        assert [r["raw"]["pattern"] for r in selected] == ["加我|你好"]
        # 无短语规则不进索引（天然回退全量扫描），命中词含其字面也不返回
        assert rr.select_rules_for_phrases(["12345"]) == []

    def test_subset_case_insensitive_and_dedup(self) -> None:
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": "QQ", "action": "violate"},
                {"pattern": "qq群", "action": "violate"},
                {"pattern": "加我", "action": "violate"},
            ]
        )
        patterns = [r["raw"]["pattern"] for r in rr.select_rules_for_phrases(["QQ", "qq"])]
        assert patterns == ["QQ", "qq群"]


class TestDisambiguateIndexCorrectness:
    """正确性：索引路径（含命中词提示）与旧行为全量路径判定完全一致。"""

    def test_single_rule_equivalence(self) -> None:
        rr = RegexRuleEngine()
        rr.load([{"pattern": "代开(发票|票)", "category": "诈骗", "action": "violate"}])
        for text in ("要代开发票吗", "要代开票吗", "今天天气不错", "", "代开 发票"):
            hit = KeywordHitData("代开", "广告", "代开", 1, 3) if text else None
            hits = [hit] if hit is not None else []
            reference = _full_scan_disambiguate(rr, text, hits)
            for hit_words in (None, ["代开"], [], ["无关词"]):
                assert rr.disambiguate(text, hits, hit_words) == reference, (text, hit_words)

    def test_200_rules_60_random_samples(self) -> None:
        # 任务卡验收：200 条规则（含字面量 / 不含字面量）× 60 个随机样本
        rng = random.Random(20260827)
        rules = _build_test_rules(200, rng)
        rr = RegexRuleEngine()
        rr.load(rules)
        keywords = ["傻逼", "加我", "你好", "垃圾", "代开", "QQ", "VIP", "ab", "bc", "赌"]
        for sample in range(60):
            text, hits = _random_text(rng, keywords)
            reference = _full_scan_disambiguate(rr, text, hits)
            for index, hit_words in enumerate(
                (None, [], ["加我"], ["VIP"], [h.keyword for h in hits])
            ):
                kept, exempted = rr.disambiguate(text, hits, hit_words)
                assert kept == reference[0], (sample, index, text, hit_words)
                assert exempted == reference[1], (sample, index, text, hit_words)

    def test_empty_text_and_empty_hits(self) -> None:
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": ".*", "action": "violate"},  # 无短语 → 全量扫描 → 命中空串
                {"pattern": "ab", "action": "violate"},  # 可索引 → 空文本跳过（不会命中）
                {"pattern": "ab?", "category": "广告", "action": "exempt"},
            ]
        )
        assert rr.disambiguate("", []) == _full_scan_disambiguate(rr, "", [])
        hit = KeywordHitData("", "", "", 0, 0)
        assert rr.disambiguate("", [hit]) == _full_scan_disambiguate(rr, "", [hit])


class TestAcceleration:
    """加速断言：命中词只含 A 时，仅 A 相关规则被 regex.search。"""

    def test_only_relevant_rules_searched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[_CountingPattern] = []

        def counting_compile(pattern: str, *args: Any, **kwargs: Any) -> _CountingPattern:
            wrapped = _CountingPattern(pattern)
            created.append(wrapped)
            return wrapped

        monkeypatch.setattr(re, "compile", counting_compile)
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": "AA", "category": "广告", "action": "violate"},
                {"pattern": "BB", "category": "广告", "action": "violate"},
                {"pattern": r"\d{5,9}", "category": "广告", "action": "violate"},
            ]
        )
        pat_a = next(p for p in created if p.pattern == "AA")
        pat_b = next(p for p in created if p.pattern == "BB")
        pat_no_lit = next(p for p in created if p.pattern == r"\d{5,9}")
        for item in (pat_a, pat_b, pat_no_lit):
            item.search_calls = 0

        # 命中词只含 AA 词，原文也只含 AA 短语 → 仅 AA 规则被扫描；无短语规则恒全量
        text = "来点AA东西"
        rr.disambiguate(text, [], hit_words=["AA"])
        assert pat_a.search_calls >= 1
        assert pat_b.search_calls == 0  # BB 短语未在原文出现 → 索引裁剪跳过
        assert pat_no_lit.search_calls >= 1  # 无短语规则回退全量扫描

        # 原文同时含 BB 短语，即使命中词只给 AA → BB 规则也必须被扫描（正确性覆盖层）
        for item in (pat_a, pat_b, pat_no_lit):
            item.search_calls = 0
        rr.disambiguate("来点AA和BB东西", [], hit_words=["AA"])
        assert pat_a.search_calls >= 1
        assert pat_b.search_calls >= 1  # 文本 AC 覆盖：命中词提示不用于排除
        assert pat_no_lit.search_calls >= 1

    def test_exempt_pruning_keeps_first_match(self) -> None:
        # 前序可豁免规则因短语缺失被裁剪后，后续规则仍按原顺序生效
        rr = RegexRuleEngine()
        rr.load(
            [
                {"pattern": "官方加我", "category": "广告", "action": "exempt"},
                {"pattern": "加我", "category": "广告", "action": "exempt"},
            ]
        )
        hit = KeywordHitData("加我", "广告", "加我", 1, 3)
        kept, exempted = rr.disambiguate("来加我好友", [hit], hit_words=["加我"])
        assert kept == []
        assert len(exempted) == 1
        assert exempted[0]["rule"]["pattern"] == "加我"

    def test_case_insensitive_presence_no_false_skip(self) -> None:
        # 原文 "VIP"（大写）→ 短语 "vip" 按小写存在性命中 → (?i)vip 规则被扫描并命中
        rr = RegexRuleEngine()
        rr.load([{"pattern": "(?i)vip", "category": "广告", "action": "violate"}])
        kept, _ = rr.disambiguate("这是VIP会员", [])
        assert len(kept) == 1
        assert kept[0].category == "广告"


class TestKeywordEngineIntegration:
    """经 KeywordEngine 全链路（scan + disambiguate + 命中词透传）验证。"""

    def test_scan_disambiguate_with_hit_words(self) -> None:
        eng = KeywordEngine()
        eng.reload(
            {"广告": ["加我"]},
            [
                {"category": "广告", "pattern": "加我", "action": "violate"},
                {"category": "广告", "pattern": "加我好友", "action": "exempt"},
            ],
        )
        text = "来加我好友吧"
        hits = eng.scan(text)
        assert len(hits) == 1
        kept, exempted = eng.disambiguate(text, hits, [h.keyword for h in hits])
        # 命中被豁免；violate 规则全文命中追加一条强命中
        assert [h.keyword for h in kept] == ["加我"]
        assert len(exempted) == 1
        assert exempted[0]["rule"]["pattern"] == "加我好友"
        # 不传命中词（None）结果一致
        kept2, exempted2 = eng.disambiguate(text, hits)
        assert (kept2, exempted2) == (kept, exempted)

    def test_rules_disabled_passthrough_with_hit_words(self) -> None:
        # 规则层关闭（rules_enabled=False）时命中词参数不改变 v0.1 透传语义
        eng = KeywordEngine()
        eng.reload({"广告": ["加我"]})
        assert eng.rules_enabled is False
        kept, exempted = eng.disambiguate("加我", [], hit_words=["加我"])
        assert kept == []
        assert exempted == []


class _CountingPattern:
    """计数用假正则：search 调用计数并委托真 compile。"""

    def __init__(self, raw: str) -> None:
        self.pattern = raw
        self.search_calls = 0

    def search(self, text: str):
        self.search_calls += 1
        return _ORIG_COMPILE(self.pattern).search(text)
