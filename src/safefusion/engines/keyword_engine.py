"""关键词引擎（Aho-Corasick + 拼音变体 + 正则消歧）。

对应分工文档 T3 任务卡与 PRD §5（词库变体展开）：

- ``KeywordEngine``：pyahocorasick 自动机匹配关键词。词库构建时按
  generate_variants 展开变体（拼音全拼 / 首字母、全半角、繁简体、常见符号
  分隔），命中后回映射到原文位置；拼音串匹配通过「汉字段→拼音串」预计算索引
  实现回映射（支持同音词场景，如词库「捡闻」命中正文「见闻」）。
- ``RegexRuleEngine``：正则语境消歧。exempt 规则命中命中片段上下文时豁免该
  命中；violate 规则在全文命中时追加为强命中（类别取规则值）。
- 正则规则索引（PRD v0.3.0）：``RegexRuleEngine`` 加载时从每条 pattern 提取
  「关键短语」（连续汉字串 / 连续 ASCII 字母数字串，长度≥2）建倒排索引，
  ``disambiguate`` 可选接收 ``hit_words`` 命中词提示，只对「短语出现在原文」
  的规则子集执行 regex 扫描；**正确性优先、索引仅为加速** —— 可索引裁剪的
  规则仅在「必然不匹配」时才跳过（跳过条件：规则可证明"命中必含某短语"且
  该短语未在原文出现），其余规则（短语为空 / 判定构造含可缩量词 / 字符类等）
  一律回退全量扫描，判定结果与旧行为完全一致。
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
from collections.abc import Iterable
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

#: 关键短语提取：连续汉字串或连续 ASCII 字母数字串（长度≥2）
_LITERAL_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}")

#: 可索引裁剪威胁①：字面量短语的尾字符被 * / ? / {0..} 修饰 → 该字符可缩减到
#: 零次出现，「命中必含该短语」不成立（如 ``ab*`` 可只命中 ``a``）。
_OPTIONAL_TAIL_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9](?:[*?]|\{\s*0(?:\s*,\s*\d*\s*)?\})")

#: 可索引裁剪威胁②：含短语的分组整体被 * / ? / {0..} 修饰 → 整组可出现零次。
_OPTIONAL_GROUP_RE = re.compile(r"\)(?:[*?]|\{\s*0(?:\s*,\s*\d*\s*)?\})")


def _strip_regex_escapes(pattern: str) -> str:
    """去掉正则转义序列（``\\x`` → ``x``），便于在原始文本上做字面量分析。

    ``\\d`` 脱去反斜杠后残留的 ``d`` 是单字符（不足 2 字符不会被当作短语），
    仅用于让后续的字符类 / 量词分析在无转义干扰的文本上进行。
    """

    return re.sub(r"\\.", "", pattern)


def _analyze_pattern(pattern: str) -> tuple[frozenset[str], bool]:
    """分析一条正则 pattern，返回 ``(关键短语集合, 是否可索引裁剪)``。

    关键短语（候选字面量表）：去掉转义并在剔除量词花括号后（避免 ``{5,12}``
    中的数字被误当成字面量短语），抽取连续汉字串与连续 ASCII 字母数字串
    （长度≥2），全部小写。

    可索引裁剪（indexable）：短语集合非空，且 pattern 中不存在任何「可能让
    某个短语匹配成功却不作为文本连续片段出现」的构造 —— 字符类、字面量尾
    字符被 ``*`` / ``?`` / ``{0..}`` 修饰、或含短语的分组整体被 ``*`` /
    ``?`` / ``{0..}`` 修饰。凡是无法证明「命中必含某短语」的 pattern 一律
    返回 indexable=False：该规则不做索引裁剪，永远全量扫描。

    ``indexable=True`` 的规则满足：**规则命中原文 ⇒ 至少一个关键短语以
    连续子串形式出现在原文中**（大小写不敏感）。因此当全部短语都未在原文
    出现时，该规则必然不命中，可以安全跳过 —— 这是索引裁剪的唯一依据。

    Args:
        pattern: 正则 pattern 原始字符串。

    Returns:
        (小写关键短语集合, 是否可索引裁剪)。
    """

    cleaned = _strip_regex_escapes(pattern or "")
    threatened = (
        "[" in cleaned
        or _OPTIONAL_TAIL_RE.search(cleaned) is not None
        or _OPTIONAL_GROUP_RE.search(cleaned) is not None
    )
    no_quantifier_braces = re.sub(r"\{[^{}]*\}", "", cleaned)
    phrases = frozenset(run.lower() for run in _LITERAL_RUN_RE.findall(no_quantifier_braces))
    return phrases, bool(phrases) and not threatened


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
        self,
        text: str,
        hits: list[KeywordHitData],
        hit_words: Iterable[str] | None = None,
    ) -> tuple[list[KeywordHitData], list[dict]]:
        """对关键词命中执行正则消歧（规则层未启用时直接透传）。

        Args:
            text: 原文。
            hits: 关键词命中列表。
            hit_words: 原文命中词（来自 :meth:`scan`），透传给
                ``RegexRuleEngine.disambiguate`` 作为命中词索引的加速提示；
                仅加速不改变判定（正确性契约见 RegexRuleEngine）。

        Returns:
            (保留命中, 被豁免命中列表)；规则层关闭（``rules_enabled`` 为 False）
            时返回 ``(list(hits), [])``，与 v0.1 行为一致。
        """

        if not self._rules_enabled:
            return list(hits), []
        return self._regex.disambiguate(text, hits, hit_words)

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

    命中词索引（正确性优先，索引仅为加速）：加载时从每条 pattern 提取
    「关键短语」（连续汉字串 / 连续 ASCII 字母数字串，长度≥2，见
    :func:`_analyze_pattern`），构建「短语 → 规则」倒排索引与短语 AC 自动机。
    :meth:`disambiguate` 可选接收 ``hit_words`` 命中词提示，只对「关键短语
    出现在原文（大小写不敏感）或含于命中词」的规则子集执行 regex 扫描；
    其余规则仅在**可证明必然不匹配**（``indexable`` 且全部短语未在原文出现）
    时跳过，否则回退全量扫描 —— 判定结果与逐条全量扫描完全一致，索引只
    减少必然会落空的 regex.search 调用。实例由 ``KeywordEngine.reload`` 在
    模块级锁外完整构造后一次性原子替换，生产路径不存在对在线实例的并发
    变更（``load`` 仅用于构造阶段或测试）。
    """

    def __init__(self, context_window: int = _DEFAULT_CONTEXT_WINDOW) -> None:
        """初始化消歧引擎。

        Args:
            context_window: exempt 判定的上下文扩展窗口（命中片段前后各 N 字符）。
        """

        self._context_window = max(0, int(context_window))
        self._exempt_rules: list[dict] = []
        self._violate_rules: list[dict] = []
        #: 关键短语(小写) → 规则对象列表 的倒排索引（加载时构建）
        self._phrase_index: dict[str, list[dict]] = {}
        #: 全部关键短语构成的 AC 自动机：对原文一次性枚举出现的全部短语
        #: （避免 ``"a|b"`` 组合正则 + finditer 在重叠短语下漏报子串）
        self._phrase_automaton: ahocorasick.Automaton | None = None

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
            phrases, indexable = _analyze_pattern(pattern)
            item = {
                "pattern": compiled,
                "raw": dict(rule),
                "phrases": phrases,
                "indexable": indexable,
            }
            (violate if action == "violate" else exempt).append(item)
        self._exempt_rules = exempt
        self._violate_rules = violate
        self._build_phrase_index(exempt, violate)

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
        self,
        text: str,
        hits: list[KeywordHitData],
        hit_words: Iterable[str] | None = None,
    ) -> tuple[list[KeywordHitData], list[dict]]:
        """对关键词命中执行正则消歧（索引加速，判定与全量扫描完全一致）。

        Args:
            text: 原文。
            hits: 关键词命中列表。
            hit_words: 调用方提供的原文命中词（如 ``KeywordEngine.scan``
                返回命中的关键词）。仅作为**加速超集提示**：含于命中词的
                短语必在原文中（命中词是原文子串），可提前并入扫描子集；
                **绝不用于排除** —— 排除某条规则的唯一依据是「该规则可
                索引裁剪且全部短语未在原文出现」，因此传与不传、传多传少
                都不改变判定结果（正确性优先，索引为加速）。

        Returns:
            (保留命中, 被豁免命中列表)；被豁免元素为 ``{"hit": ..., "rule": ...}``
            字典；未加载规则时 (原样保留, [])。
        """

        if not self._exempt_rules and not self._violate_rules:
            return list(hits), []
        present = self._present_phrases(text, hit_words)
        kept: list[KeywordHitData] = []
        exempted: list[dict] = []
        window = self._context_window
        for hit in hits:
            ctx = text[max(0, hit.start - window) : min(len(text), hit.end + window)]
            cause = self._match_exempt(ctx, hit.category, present)
            if cause is not None:
                exempted.append({"hit": hit, "rule": cause})
            else:
                kept.append(hit)
        for rule in self._violate_rules:
            # 索引加速：可索引裁剪且全部短语未出现在原文 → 该规则必然不命中。
            # 其余（不可索引 / 短语已出现）与旧行为一致地全量 regex.search。
            # ``not present`` 短路：原文无任何短语时直接跳过全部可索引规则，
            # 省去逐条的集合交集（实测为主要加速形态）。
            if rule["indexable"] and (not present or not (rule["phrases"] & present)):
                continue
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

    def _present_phrases(self, text: str, hit_words: Iterable[str] | None) -> set[str]:
        """计算「确凿出现在原文中的索引短语」集合（小写）。

        两个来源，均只会放大扫描子集、不会收窄：
        - 对原文做短语 AC 扫描 —— 权威来源，保证不遗漏任何出现在原文的短语；
        - 命中词提示 —— 命中词为原文子串，含于命中词的短语必在原文。
        """

        present: set[str] = set()
        automaton = self._phrase_automaton
        if automaton is None:
            return present
        if text:
            for _end, phrase in automaton.iter(text.lower()):
                present.add(phrase)
        if hit_words:
            for word in hit_words:
                lowered = (word or "").lower()
                if not lowered:
                    continue
                for _end, phrase in automaton.iter(lowered):
                    present.add(phrase)
        return present

    def select_rules_for_phrases(self, phrases: Iterable[str]) -> list[dict]:
        """按短语集合查询索引命中的规则子集（测试与诊断用）。

        仅返回「至少含其中一个关键短语」的规则内部字典（保持加载顺序）；
        不含短语的规则（不可能被索引命中）不在返回中 —— 它们由
        :meth:`disambiguate` 天然回退全量扫描，语义不受影响。

        Args:
            phrases: 短语集合（大小写不敏感）。

        Returns:
            命中规则的内部字典列表（含 pattern / raw / phrases / indexable）。
        """

        selected: list[dict] = []
        seen: set[int] = set()
        for phrase in phrases:
            for rule in self._phrase_index.get(str(phrase).lower(), ()):
                if id(rule) not in seen:
                    seen.add(id(rule))
                    selected.append(rule)
        return selected

    def _build_phrase_index(self, exempt: list[dict], violate: list[dict]) -> None:
        """构建「关键短语 → 规则」倒排索引与短语 AC 自动机（加载时一次性完成）。

        ``_phrase_index``：短语(小写) → 规则对象列表；``_phrase_automaton``：
        全部短语构成的 AC 自动机 —— 用 AC 而非 ``"(?:a|b|…)"`` 组合正则做
        原文扫描，是为了完整枚举重叠/包含关系的全部短语（组合正则在同位置
        只报一个可选分支，会漏报「互为前缀」的短语而误裁剪规则）。
        """

        index: dict[str, list[dict]] = {}
        for rules in (exempt, violate):
            for rule in rules:
                for phrase in rule["phrases"]:
                    index.setdefault(phrase, []).append(rule)
        self._phrase_index = index
        if not index:
            self._phrase_automaton = None
            return
        automaton = ahocorasick.Automaton()
        for phrase in index:
            automaton.add_word(phrase, phrase)
        automaton.make_automaton()
        self._phrase_automaton = automaton

    def _match_exempt(self, ctx: str, category: str, present: set[str]) -> dict | None:
        """在上下文串中查找第一条类别匹配的 exempt 规则，命中返回原始规则。

        Args:
            ctx: 命中片段 + 扩展窗口构成的上下文串。
            category: 命中类别。
            present: 出现在原文的索引短语集合（小写）。

        索引裁剪：可索引规则且全部短语未在原文出现时跳过 —— 上下文是原文
        子串，该规则必然无法命中 ctx，与旧行为（逐条 search）判定等价。
        """

        for rule in self._exempt_rules:
            if rule["indexable"] and (not present or not (rule["phrases"] & present)):
                continue
            rule_category = rule["raw"].get("category")
            if rule_category and rule_category != category:
                continue
            if rule["pattern"].search(ctx):
                return rule["raw"]
        return None
