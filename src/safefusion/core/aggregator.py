"""纯函数决策模块：基础规则汇总 / 三档置信度分档 / 最终裁决合并（T9 集成核心）。

本模块只包含无副作用纯函数，供编排器
:mod:`safefusion.core.orchestrator` 调用：

- :func:`summarize_basic` —— 基础规则层汇总，判定「唯一快速放行通道」
  （PRD §3.1：所有帧白名单命中 **且** 文本零风险）；
- :func:`decide_tier` —— 三档置信度动作分档（PRD §3.4）；
- :func:`merge_final` —— LLM 与语义层结果的最终合并（PRD §3.1 FIN / LLM 回退）。
"""

from typing import Any, Literal

#: 三档置信度动作：安全 / LLM 兜底档 / 违规
Tier = Literal["safe", "llm", "violation"]


def summarize_basic(
    keyword_detail: dict[str, Any] | None,
    light_result: dict[str, Any] | None,
    whitelist_hits: list[dict[str, Any]] | None,
) -> dict[str, bool]:
    """汇总基础规则层结果，判定是否存在风险信号与是否全部安全。

    Args:
        keyword_detail: 关键词层结果 ``{"hits": [...], "regex_filtered": [...]}``，
            ``hits`` 为正则消歧后的**保留**命中（强信号）；可为 None。
        light_result: 轻量模型输出 ``{"label", "score", "violation"}``；可为
            None（模型 disabled / 推理失败）。
        whitelist_hits: 逐帧白名单结果 ``[{"frame", "hit", "distance"}, ...]``；
            可为 None。text 与图片任一帧未命中白名单即产生风险信号。

    Returns:
        ``{"risk_signals": bool, "all_safe": bool}``。风险信号 = 保留关键词命中
        >0 或 light.violation 或任一帧白名单未命中；「全部安全」= 无任何信号。
        纯文本请求（帧列表为空）仅由文本侧决定，纯图片请求（无文本）仅由帧侧
        决定——与 PRD §3.1「所有帧白名单命中 且 文本零风险」严格一致。
    """

    risk_signals = False
    hits = (keyword_detail or {}).get("hits") or []
    if hits:
        risk_signals = True
    if light_result is not None and light_result.get("violation"):
        risk_signals = True
    for frame in whitelist_hits or []:
        if not frame.get("hit"):
            risk_signals = True
    return {"risk_signals": risk_signals, "all_safe": not risk_signals}


def decide_tier(confidence: float, low: float, high: float) -> Tier:
    """按 PRD §3.4 三档置信度动作分档。

    默认区段：``confidence < low``（默认 0.35）→ 安全；``confidence > high``
    （默认 0.75）→ 违规；介于两者之间（**含边界 low / high**）→ LLM 兜底档。
    与 PRD「低 < 0.35 / 高 > 0.75」的严格不等号一致：恰等于 low 或 high 时
    无法准确判断，归入 LLM 档。

    Args:
        confidence: 综合置信度（0~1）。
        low: 低档上界（安全强信号上限，默认 0.35）。
        high: 高档下界（违规强信号下限，默认 0.75）。

    Returns:
        ``"safe"`` | ``"llm"`` | ``"violation"``。
    """

    if confidence < low:
        return "safe"
    if confidence > high:
        return "violation"
    return "llm"


#: LLM 成功时综合置信度的 LLM 侧权重（默认 0.6，语义侧 0.4）
_DEFAULT_LLM_WEIGHT: float = 0.6


def merge_final(
    llm_verdict: dict[str, Any] | None,
    semantic_result: dict[str, Any],
    *,
    llm_weight: float = _DEFAULT_LLM_WEIGHT,
) -> tuple[bool, float, str | None, str]:
    """合并 LLM 与语义层结果，产出最终判定。

    - **LLM 成功**（``llm_verdict`` 非 None）：``source="llm"``；综合置信度取
      LLM 置信度与语义置信度按 ``llm_weight`` / ``1 - llm_weight`` 加权
      （默认 0.6 / 0.4，LLM 侧权重更高）；LLM 未输出 confidence 时以语义置信度
      代入；category 取 LLM 类别，缺失时回退语义类别。
    - **LLM 缺失 / 失败**（``llm_verdict=None``）：回退语义层结果 ——
      ``triggered`` 决定是否违规，``source="semantic"``。

    Args:
        llm_verdict: LLM 判定 ``{"is_violation", "category", "confidence",
            "reason"}``；不可用 / 解析失败 / 超时为 None。
        semantic_result: 语义层结果 ``{"triggered", "confidence",
            "category", ...}``。
        llm_weight: LLM 侧权重（0~1，越界自动裁剪）。

    Returns:
        ``(has_violation, confidence, category, source)`` 四元组。
    """

    sem_conf = float(semantic_result.get("confidence") or 0.0)
    sem_cat = semantic_result.get("category")
    if llm_verdict is None:
        return bool(semantic_result.get("triggered")), sem_conf, sem_cat, "semantic"
    llm_conf = llm_verdict.get("confidence")
    llm_conf_f = float(llm_conf) if llm_conf is not None else sem_conf
    weight = max(0.0, min(1.0, llm_weight))
    confidence = weight * llm_conf_f + (1.0 - weight) * sem_conf
    category = llm_verdict.get("category") or sem_cat
    return bool(llm_verdict.get("is_violation")), confidence, category, "llm"
