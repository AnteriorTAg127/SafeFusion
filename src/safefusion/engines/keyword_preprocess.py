"""词库导入预处理（PRD v0.4.0 M0 / T55）。

原始词库存在两类结构：
1. ``词A+词B``：表示同一句话中同时含有这些词才命中（AND 组合）；
2. 大量谐音词：当前引擎已用拼音检测覆盖，重复存谐音没有意义。

本模块提供：
- :func:`parse_keyword_line`：把单条原始词条解析为 ``{match_type, terms}``；
- :func:`normalize_keyword`：全半角 / 繁简 / 去符号 / 小写归一；
- :func:`pinyin_key`：拼音等价键（lazy_pinyin NORMAL 音节 join）；
- :func:`preprocess_keyword_items`：批量预处理，谐音归并只保留规范词，
  AND 组合保留为组合规则。
"""

from __future__ import annotations

import re
from typing import Any

from pypinyin import Style, lazy_pinyin

#: 组合词分隔符（原始词库用 ``+`` 表示 AND）
_AND_SEP_RE = re.compile(r"\s*\+\s*")

#: 归一化时剥离的符号（全半角标点 / 空白 / 常见装饰符号）
_STRIP_RE = re.compile(r"[\s，。！？：；、（）()【】\[\]《》〈〉“”\"''.,!?:;·•@#*&%$^~`\\/|_-]+")


def _to_halfwidth(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    return "".join(out)


#: 简繁对照（与 keyword_engine._TRAD_SIMP_PAIRS 同源，保持独立避免循环导入）
_TRAD_SIMP_PAIRS: dict[str, str] = {
    "赌": "賭",
    "卖": "賣",
    "网": "網",
    "线": "線",
    "钱": "錢",
    "约": "約",
    "药": "藥",
    "门": "門",
    "开": "開",
    "关": "關",
    "发": "發",
    "龙": "龍",
    "飞": "飛",
    "车": "車",
    "号": "號",
    "电": "電",
    "话": "話",
    "视": "視",
    "频": "頻",
    "买": "買",
    "币": "幣",
    "台": "臺",
    "湾": "灣",
    "独": "獨",
    "传": "傳",
    "统": "統",
    "联": "聯",
    "恋": "戀",
    "爱": "愛",
    "会": "會",
    "员": "員",
    "银": "銀",
    "戏": "戲",
    "补": "補",
    "课": "課",
    "楼": "樓",
    "盘": "盤",
    "贷": "貸",
    "诈": "詐",
    "骗": "騙",
    "汇": "匯",
    "妈": "媽",
    "证": "證",
    "书": "書",
    "节": "節",
    "图": "圖",
    "码": "碼",
    "录": "錄",
}
_SIMP_FROM_TRAD: dict[str, str] = {trad: simp for simp, trad in _TRAD_SIMP_PAIRS.items()}


def normalize_keyword(word: str) -> str:
    """归一化词条：全角转半角、繁体转简体、剥离符号空白、小写。"""
    text = _to_halfwidth(word or "")
    text = "".join(_SIMP_FROM_TRAD.get(ch, ch) for ch in text)
    text = _STRIP_RE.sub("", text)
    return text.lower()


def pinyin_key(word: str) -> str:
    """拼音等价键：对归一化词条生成 NORMAL 全拼（音节 join）。"""
    normalized = normalize_keyword(word)
    return "'".join(lazy_pinyin(normalized, style=Style.NORMAL))


def parse_keyword_line(line: str) -> dict[str, Any] | None:
    """解析单条原始词条。

    Returns:
        ``{"match_type": "single"|"and", "terms": [str, ...]}``；
        空白行返回 None。
    """
    text = (line or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in _AND_SEP_RE.split(text) if p.strip()]
    if len(parts) > 1:
        return {"match_type": "and", "terms": parts}
    return {"match_type": "single", "terms": parts}


def preprocess_keyword_items(
    items: list[tuple[str, str, str | None]],
) -> list[dict[str, Any]]:
    """批量预处理词库导入条目。

    Args:
        items: ``(category, word, source)`` 三元组。

    Returns:
        预处理后的条目列表：``{category, word, source, match_type, terms}``。
        同音谐音词归并：同类别下拼音等价键相同的词条只保留首个（规范词）。
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for category, raw_word, source in items:
        parsed = parse_keyword_line(raw_word)
        if parsed is None:
            continue
        match_type = parsed["match_type"]
        terms = parsed["terms"]
        key = (category or "", pinyin_key("+".join(terms) if match_type == "and" else terms[0]))
        if key in seen:
            continue  # 同音/重复 → 只保留规范词（首个）
        seen[key] = {
            "category": category,
            "word": raw_word.strip(),
            "source": source,
            "match_type": match_type,
            "terms": terms,
        }
    return list(seen.values())
