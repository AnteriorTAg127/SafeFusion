"""关键词引擎（Aho-Corasick + 拼音变体 + 正则消歧）。

对应分工文档 T3 任务卡与 PRD §5（词库变体展开）：

- ``KeywordEngine``：pyahocorasick 自动机匹配关键词。词库构建时按
  generate_variants 展开变体（拼音全拼 / 首字母、全半角、繁简体、常见符号
  分隔），命中后回映射到原文位置；拼音串匹配通过「汉字段→拼音串」预计算索引
  实现回映射（支持同音词场景，如词库「捡闻」命中正文「见闻」）。
- ``RegexRuleEngine``：正则语境消歧。exempt 规则命中命中片段上下文时豁免该
  命中；violate 规则在全文命中时追加为强命中（类别取规则值）。
- ``generate_variants``：独立变体生成函数，供词库构建与测试复用。

设计决策：

- 变体命中分为两类扫描：① 原文直扫（变体以原样出现在文本中，如 "jiewen"、
  "＋Ｑ"、"裸@聊"），位置即原文位置；② 拼音展开扫描（正文汉字段转拼音串后
  扫描，命中映射回对应汉字段），单字词不做同音匹配（``_MIN_PINYIN_HAN_LEN``）
  以避免同音误报爆炸；拼音首字母变体仅对词长 ≥3 汉字的词生成
  （``_MIN_PINYIN_INIT_HAN_LEN``），防止 2 字词首字母串（如"安南"→"an"）
  钻进其它词拼音串内部（如"今天"→"tian" 含 "an"）被误命中（PRD v0.2 M1）。
- 同一位置的多类别命中全部保留；去重键为 (category, keyword, start, end)。
- 热重载（PRD v0.2 M4）：``KeywordEngine.reload`` 在模块级锁内一次性原子替换
  自动机 + 规则引擎，构造失败不替换（回退旧实例语义）；``rules_enabled`` 为
  False 时 ``disambiguate`` 原样保留命中（v0.1 行为）。
"""

import re
import threading
from typing import Literal, NamedTuple

import ahocorasick
from pypinyin import Style, lazy_pinyin

from ..logging_setup import get_logger

__all__ = ["KeywordEngine", "KeywordHitData", "RegexRuleEngine", "generate_variants"]

_logger = get_logger("engines.keyword_engine")

#: 热重载原子替换锁（模块级）：并发 reload / scan / disambiguate 时不会观察到
#: 「新词库 + 旧规则」之类的混合状态（PRD v0.2 M4）
_RELOAD_LOCK = threading.Lock()

#: 同音匹配要求关键词至少含 2 个汉字（单字词同音误报率过高）
_MIN_PINYIN_HAN_LEN = 2

#: 拼音首字母变体要求关键词至少含 3 个汉字：2 字词的首字母串（如"an"）会钻进
#: 其它较长词的拼音串内部（如"今天"→"tian" 含 "an"）被误命中并错位回映射，
#: 是 v0.1 手测误报率主要来源（PRD v0.2 M1）
_MIN_PINYIN_INIT_HAN_LEN = 3

#: 符号替换变体使用的分隔符
_SEPARATORS = ("@", "·", "・", "*")

#: 正则豁免的上下文扩展窗口（命中片段前后各扩大 N 个字符）
_DEFAULT_CONTEXT_WINDOW = 8

#: 简体 → 繁体 对照表（仅覆盖审核词常见变体；完整转换生产建议引入 opencc）
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

#: 变体类型：pinyin_* 参与拼音展开扫描，其余仅参与原文直扫
_VariantKind = Literal[
    "literal", "pinyin_full", "pinyin_init", "fullwidth", "case", "traditional", "symbol"
]

_CJK_RE = r"[\u4e00-\u9fff]+"


class KeywordHitData(NamedTuple):
    """一次关键词命中。

    Attributes:
        keyword: 词库中的关键词原文。
        category: 词库类别（如 色情 / 广告）。
        matched: 在原文中实际匹配到的文本片段。
        start: 命中片段在原文中的起始下标（含）。
        end: 命中片段在原文中的结束下标（不含）。
    """

    keyword: str
    category: str
    matched: str
    start: int
    end: int


def _to_fullwidth(text: str) -> str:
    """把字符串中的 ASCII 字符转成全角（其他字符原样保留）。"""

    return "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in text)


def _traditional(text: str) -> str:
    """按内置对照表做简体→繁体转换（表外字符原样保留）。"""

    return "".join(_TRAD_SIMP_PAIRS.get(c, c) for c in text)


def _simplified(text: str) -> str:
    """按内置对照表做繁体→简体转换（表外字符原样保留）。"""

    return "".join(_SIMP_FROM_TRAD.get(c, c) for c in text)


def _gen_variants_with_kind(word: str) -> list[tuple[str, _VariantKind]]:
    """生成 (变体串, 变体类型) 列表，保序、去重、非空。"""

    seen: set[str] = set()
    result: list[tuple[str, _VariantKind]] = []

    def add(variant: str, kind: _VariantKind) -> None:
        if variant and variant not in seen:
            seen.add(variant)
            result.append((variant, kind))

    add(word, "literal")
    add("".join(lazy_pinyin(word, style=Style.NORMAL)), "pinyin_full")
    # 首字母变体仅对 ≥3 汉字的词生成（2 字词如"安南"→"an" 会钻进"今天"→
    # "tian" 等其它词拼音串内部造成误报，PRD v0.2 M1）
    if _han_count(word) >= _MIN_PINYIN_INIT_HAN_LEN:
        add("".join(lazy_pinyin(word, style=Style.FIRST_LETTER)), "pinyin_init")
    for base in (word, word.upper(), word.lower()):
        add(base, "case")
        add(_to_fullwidth(base), "fullwidth")
    add(_traditional(word), "traditional")
    add(_simplified(word), "traditional")
    for sep in _SEPARATORS:
        add(sep.join(word), "symbol")
    return result


def generate_variants(word: str) -> list[str]:
    """生成关键词的匹配变体列表（供词库构建与测试复用）。

    变体类型：原文、拼音全拼、拼音首字母、全半角（含大小写）、繁简体、
    常见符号分隔（@ / · / ・ / *）。顺序稳定、去重、不含空串。
    拼音首字母变体仅对词长 ≥3 汉字的词生成（2 字词不再生成，见
    ``_MIN_PINYIN_INIT_HAN_LEN``）。

    Args:
        word: 关键词原文。

    Returns:
        变体字符串列表，首个元素恒为原文。
    """

    return [variant for variant, _kind in _gen_variants_with_kind(word)]


def _segment_text(text: str) -> list[tuple[bool, int, str]]:
    """把文本切成 连续汉字段 / 其他 交替片段。

    Returns:
        [(是否为汉字段, 原文起始下标, 片段内容), ...]
    """

    segments: list[tuple[bool, int, str]] = []
    pos = 0
    for match in re.finditer(_CJK_RE, text):
        if match.start() > pos:
            segments.append((False, pos, text[pos : match.start()]))
        segments.append((True, match.start(), match.group()))
        pos = match.end()
    if pos < len(text):
        segments.append((False, pos, text[pos:]))
    return segments


def _build_pinyin_index(text: str) -> tuple[str, list[int | None]]:
    """预计算「汉字段→拼音串」索引：正文汉字段转拼音串，其余原样保留。

    返回 ``(search_text, posmap)``：``search_text`` 用于自动机扫描，
    ``posmap[i]`` 为 ``search_text[i]`` 对应的原文下标（拼音展开时对应其
    汉字起始下标，非汉字段对应自身）。

    Args:
        text: 原文。

    Returns:
        (用于扫描的拼音展开文本, 位置映射表)。
    """

    parts: list[str] = []
    posmap: list[int | None] = []
    for is_han, start, chars in _segment_text(text):
        if is_han:
            syllables = lazy_pinyin(chars, style=Style.NORMAL)
            for idx, syllable in enumerate(syllables):
                parts.append(syllable)
                posmap.extend([start + idx] * len(syllable))
        else:
            parts.append(chars)
            posmap.extend(range(start, start + len(chars)))
    return "".join(parts), posmap


def _han_count(text: str) -> int:
    """统计字符串中的汉字数量。"""

    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def _build_automaton(
    categories: dict[str, list[str]],
) -> tuple[ahocorasick.Automaton | None, int]:
    """按类别词库构建 Aho-Corasick 自动机（变体全部展开进自动机）。

    Args:
        categories: 类别名 → 该类别关键词列表。

    Returns:
        ``(自动机, 变体总数)``；词库为空时自动机为 None（scan 依空值守卫返回
        空命中，与既有 load_categories 空词库语义一致，主模型集成修复
        2026-08-26 修复 T10 报告缺陷①）。
    """

    automaton = ahocorasick.Automaton()
    variant_map: dict[str, list[tuple[str, str, _VariantKind]]] = {}
    seen: set[tuple[str, str]] = set()
    for category, words in categories.items():
        for raw_word in words:
            word = raw_word.strip()
            if not word:
                _logger.warning("词库存在空词条，已跳过：category=%s", category)
                continue
            key = (category, word)
            if key in seen:
                _logger.warning("词库重复词条，已跳过：category=%s word=%s", category, word)
                continue
            seen.add(key)
            for variant, kind in _gen_variants_with_kind(word):
                variant_map.setdefault(variant, []).append((word, category, kind))
    for variant, entries in variant_map.items():
        automaton.add_word(variant, (variant, entries))
    if not variant_map:
        return None, 0
    automaton.make_automaton()
    return automaton, len(variant_map)


class KeywordEngine:
    """Aho-Corasick 关键词匹配引擎（含拼音 / 变体展开与原文位置回映射）。"""

    def __init__(self) -> None:
        self._automaton: ahocorasick.Automaton | None = None
        self._loaded = False
        # 正则消歧规则层（PRD v0.2 M4）：默认关闭（disambiguate 透传）
        self._regex = RegexRuleEngine()
        self._rules_enabled = False

    @property
    def loaded(self) -> bool:
        """是否已加载词库。"""

        return self._loaded

    @property
    def rules_enabled(self) -> bool:
        """正则规则层是否启用；False 时 :meth:`disambiguate` 原样保留命中。"""

        return self._rules_enabled

    def load_categories(self, categories: dict[str, list[str]]) -> None:
        """加载词库并构建自动机（重复调用会覆盖旧词库；不触碰规则层）。

        每个关键词按 ``generate_variants`` 展开变体后全部加入自动机；
        同一关键词可属于多个类别，同一类别下重复词条给出警告并跳过。

        Args:
            categories: 类别名 → 该类别关键词列表。
        """

        automaton, variant_count = _build_automaton(categories)
        with _RELOAD_LOCK:
            self._automaton = automaton
            self._loaded = True
        _logger.info("关键词引擎加载完成：%d 类别 / %d 个变体", len(categories), variant_count)

    def reload(self, categories: dict[str, list[str]], rules: list[dict] | None = None) -> None:
        """重建词库自动机与正则规则层并原子替换（热重载入口，PRD v0.2 M4）。

        先在锁外完成全部构造（自动机 + 规则引擎），再于模块级锁内一次性替换
        内部状态：并发扫描 / 消歧不会观察到「新词库 + 旧规则」等混合状态。
        构造失败（规则 action 非法、pattern 不是有效正则等）时抛 ``ValueError``
        且内部状态保持旧实例不变（回退旧实例语义，由调用方捕获后决定策略）。

        Args:
            categories: 类别名 → 该类别关键词列表（重建自动机）。
            rules: 正则消歧规则（数据库 rules 表行或规则字典列表，行含
                category / pattern / action / note）；None 表示规则层关闭
                （``disambiguate`` 原样保留命中，等价 v0.1 行为）。

        Raises:
            ValueError: 规则 action 非法或 pattern 不是有效正则；此时内部
                状态不变。
        """

        new_regex: RegexRuleEngine | None = None
        if rules is not None:
            new_regex = RegexRuleEngine()
            new_regex.load_from_rows(rules)
        automaton, variant_count = _build_automaton(categories)
        with _RELOAD_LOCK:
            self._automaton = automaton
            self._loaded = True
            if new_regex is not None:
                self._regex = new_regex
                self._rules_enabled = True
            else:
                # 规则层关闭：重置为空规则引擎（disambiguate 透传）
                self._regex = RegexRuleEngine()
                self._rules_enabled = False
        _logger.info(
            "关键词引擎热重载完成：%d 类别 / %d 个变体 / 规则层=%s",
            len(categories),
            variant_count,
            "启用" if rules is not None else "关闭",
        )

    def disambiguate(
        self, text: str, hits: list[KeywordHitData]
    ) -> tuple[list[KeywordHitData], list[dict]]:
        """对关键词命中执行正则消歧（规则层未启用时直接透传）。

        Args:
            text: 原文。
            hits: 关键词命中列表。

        Returns:
            (保留命中, 被豁免命中列表)；规则层关闭（``rules_enabled`` 为 False）
            时返回 ``(list(hits), [])``，与 v0.1 行为一致。
        """

        if not self._rules_enabled:
            return list(hits), []
        return self._regex.disambiguate(text, hits)

    def scan(self, text: str) -> list[KeywordHitData]:
        """扫描文本，返回全部关键词命中。

        两遍扫描：原文直扫 + 拼音展开扫描（汉字段→拼音串预计算索引回映射）。
        同位置多类别命中全部保留；结果按 start 升序，其次 end / category /
        keyword 以保证确定性。

        Args:
            text: 待扫描的原文（可为空串）。

        Returns:
            命中列表；词库未加载或文本为空时返回空列表。
        """

        if not self._loaded or self._automaton is None or not text:
            return []
        hits: list[KeywordHitData] = []
        automaton = self._automaton
        # 原文直扫：变体以原样出现，命中位置即原文位置
        for end_idx, (variant, entries) in automaton.iter(text):
            start = end_idx - len(variant) + 1
            end = end_idx + 1
            for word, category, _kind in entries:
                hits.append(KeywordHitData(word, category, text[start:end], start, end))
        # 拼音展开扫描：正文汉字段→拼音串后扫描，命中回映射原文汉字段
        search_text, posmap = _build_pinyin_index(text)
        if search_text != text:
            for end_idx, (variant, entries) in automaton.iter(search_text):
                start = end_idx - len(variant) + 1
                for word, category, kind in entries:
                    if kind not in ("pinyin_full", "pinyin_init"):
                        continue
                    if _han_count(word) < _MIN_PINYIN_HAN_LEN:
                        continue
                    orig_start = posmap[start]
                    orig_end = posmap[end_idx]
                    if orig_start is None or orig_end is None:
                        _logger.debug("拼音变体命中无法回映射原文，已丢弃：%r", variant)
                        continue
                    orig_end += 1
                    if not (0 <= orig_start < orig_end <= len(text)):
                        _logger.debug("拼音变体命中回映射越界，已丢弃：%r", variant)
                        continue
                    hits.append(
                        KeywordHitData(
                            word, category, text[orig_start:orig_end], orig_start, orig_end
                        )
                    )
        # 去重（同一 (category, keyword, start, end) 保留一条），按 start 排序
        unique: dict[tuple[str, str, int, int], KeywordHitData] = {}
        for hit in hits:
            unique.setdefault((hit.category, hit.keyword, hit.start, hit.end), hit)
        return sorted(unique.values(), key=lambda h: (h.start, h.end, h.category, h.keyword))


class RegexRuleEngine:
    """正则语境消歧引擎（exempt 豁免 / violate 追加强命中）。

    规则格式（load 入参）：``{"pattern": <正则字符串>, "category": <类别>,
    "action": "violate" | "exempt"}``。

    - exempt：规则命中「命中片段 + 前后扩展窗口」构成的上下文，且规则类别与
      命中类别一致时（规则未声明类别则作用于全部命中），豁免该命中；
    - violate：规则在全文任意位置命中时，追加一条强命中（category 取规则值，
      keyword 取 pattern）。
    """

    def __init__(self, context_window: int = _DEFAULT_CONTEXT_WINDOW) -> None:
        """初始化消歧引擎。

        Args:
            context_window: exempt 判定的上下文扩展窗口（命中片段前后各 N 字符）。
        """

        self._context_window = max(0, int(context_window))
        self._exempt_rules: list[dict] = []
        self._violate_rules: list[dict] = []

    def load(self, rules: list[dict]) -> None:
        """加载消歧规则（重复调用会覆盖旧规则）。

        Args:
            rules: 规则字典列表，字段见类 docstring。

        Raises:
            ValueError: action 非法或 pattern 不是有效正则（配置期快速失败）。
        """

        exempt: list[dict] = []
        violate: list[dict] = []
        for rule in rules:
            action = rule.get("action")
            if action not in ("violate", "exempt"):
                raise ValueError(
                    f"规则 action 必须为 violate 或 exempt，实际为 {action!r}：{rule!r}"
                )
            pattern = rule.get("pattern")
            try:
                compiled = re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise ValueError(f"无效正则 pattern={pattern!r}：{exc}") from exc
            item = {"pattern": compiled, "raw": dict(rule)}
            (violate if action == "violate" else exempt).append(item)
        self._exempt_rules = exempt
        self._violate_rules = violate

    def load_from_rows(self, rules: list[dict]) -> None:
        """从数据库 rules 表行加载消歧规则（行含 category / pattern / action / note）。

        与 :meth:`load` 语义一致（exempt 豁免 / violate 追加强命中），但入参为
        数据库行字典：额外携带的 id / is_active / created_at 等元数据被忽略；
        category 为空串视为不限定类别（作用于全部命中）。``is_active`` 过滤由
        调用方负责（通常传 ``Database.list_rules(active_only=True)`` 的结果）。

        Args:
            rules: 行字典列表。

        Raises:
            ValueError: action 非法或 pattern 不是有效正则（配置期快速失败）。
        """

        normalized = [
            {
                "pattern": row.get("pattern"),
                "category": row.get("category") or None,
                "action": row.get("action"),
                "note": row.get("note"),
            }
            for row in rules
        ]
        self.load(normalized)

    def disambiguate(
        self, text: str, hits: list[KeywordHitData]
    ) -> tuple[list[KeywordHitData], list[dict]]:
        """对关键词命中执行正则消歧。

        Args:
            text: 原文。
            hits: 关键词命中列表。

        Returns:
            (保留命中, 被豁免命中列表)；被豁免元素为 ``{"hit": ..., "rule": ...}``
            字典；未加载规则时 (原样保留, [])。
        """

        if not self._exempt_rules and not self._violate_rules:
            return list(hits), []
        kept: list[KeywordHitData] = []
        exempted: list[dict] = []
        window = self._context_window
        for hit in hits:
            ctx = text[max(0, hit.start - window) : min(len(text), hit.end + window)]
            cause = self._match_exempt(ctx, hit.category)
            if cause is not None:
                exempted.append({"hit": hit, "rule": cause})
            else:
                kept.append(hit)
        for rule in self._violate_rules:
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

    def _match_exempt(self, ctx: str, category: str) -> dict | None:
        """在上下文串中查找第一条类别匹配的 exempt 规则，命中返回原始规则。"""

        for rule in self._exempt_rules:
            rule_category = rule["raw"].get("category")
            if rule_category and rule_category != category:
                continue
            if rule["pattern"].search(ctx):
                return rule["raw"]
        return None
