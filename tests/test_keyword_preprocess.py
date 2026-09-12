"""词库导入预处理测试（PRD v0.4.0 M0 / T55）。

覆盖：
- ``词A+词B`` 解析为组合规则，不再作为单个词条入库；
- 同音谐音词归并到规范词；
- 管理端导入接口预处理；
- KeywordEngine 对组合规则要求全部 term 命中才命中。
"""

from __future__ import annotations

from safefusion.engines.keyword_engine import KeywordEngine
from safefusion.engines.keyword_preprocess import parse_keyword_line, preprocess_keyword_items


class TestParseKeywordLine:
    def test_plain_word(self) -> None:
        assert parse_keyword_line("赌博") == {"match_type": "single", "terms": ["赌博"]}

    def test_plus_compound(self) -> None:
        assert parse_keyword_line("办证+加微信") == {
            "match_type": "and",
            "terms": ["办证", "加微信"],
        }

    def test_plus_with_spaces(self) -> None:
        assert parse_keyword_line(" 办证 + 加微信 ") == {
            "match_type": "and",
            "terms": ["办证", "加微信"],
        }

    def test_empty_returns_none(self) -> None:
        assert parse_keyword_line("  ") is None


class TestPreprocessKeywordItems:
    def test_homophone_dedup(self) -> None:
        # 「干死」与「干四」拼音等价（gan'si），归并后只保留规范词「干死」
        items = [("色情", "干死", "src"), ("色情", "干四", "src")]
        out = preprocess_keyword_items(items)
        words = [row["word"] for row in out]
        assert words == ["干死"]

    def test_plus_compound_not_single(self) -> None:
        items = [("广告", "办证+加微信", "src")]
        out = preprocess_keyword_items(items)
        assert out[0]["match_type"] == "and"
        assert out[0]["word"] == "办证+加微信"
        assert out[0]["terms"] == ["办证", "加微信"]

    def test_plain_passthrough(self) -> None:
        items = [("色情", "裸聊", "src")]
        out = preprocess_keyword_items(items)
        assert out[0]["match_type"] == "single"
        assert out[0]["terms"] == ["裸聊"]


class TestKeywordEngineAnd:
    def _engine(self, keywords: list[str], category: str = "test") -> KeywordEngine:
        eng = KeywordEngine()
        eng.reload({category: keywords})
        return eng

    def test_and_hit_requires_all_terms(self) -> None:
        eng = self._engine(["办证+加微信"])
        # 只含一个 term → 不命中
        assert eng.scan("快速办证") == []
        # 两个 term 都出现 → 命中（matched 为整个组合标记）
        hits = eng.scan("快速办证加微信")
        assert len(hits) == 1
        assert hits[0].keyword == "办证+加微信"

    def test_single_unchanged(self) -> None:
        eng = self._engine(["赌博"])
        assert len(eng.scan("网络赌博害人")) == 1
