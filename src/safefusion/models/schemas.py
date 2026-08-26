"""数据契约层：请求 / 响应模型（严格对齐 PRD §4.1 与 开发/v0.1/分工.md「统一接口契约」）。

包含审核请求（ImageInput / Overrides / AuditRequest）与审核结果
（AuditResult 及其 detail 子模型：keyword / light_model / image_whitelist / semantic / llm）。
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

#: 判定来源枚举：语义层 / LLM 兜底 / 基础规则快速放行 / 各级缓存 / 永久黑白名单
Source = Literal["semantic", "llm", "basic_rules_pass", "cache", "permanent_list"]


class ImageInput(BaseModel):
    """输入图片：``url`` 与 ``base64`` 二选一。"""

    url: str | None = Field(
        default=None, description="图片 URL（http/https），与 base64 二选一"
    )
    base64: str | None = Field(
        default=None, description="图片 base64 编码（不含 data: 前缀），与 url 二选一"
    )

    @model_validator(mode="after")
    def _check_url_or_base64(self) -> "ImageInput":
        """校验 url 与 base64 必须且只能提供一个。"""

        if (self.url is None) == (self.base64 is None):
            raise ValueError("ImageInput 的 url 与 base64 必须且只能提供一个")
        return self


class Overrides(BaseModel):
    """请求级参数覆盖（仅 full 组 API Key 生效，标准组请求使用将被拒绝）。"""

    semantic_threshold: float | None = Field(
        default=None, description="语义层判定违规的相似度阈值覆盖"
    )
    margin_w: float | None = Field(
        default=None, description="黑均分−白均分差值的 margin 比较基准覆盖"
    )
    confidence_low: float | None = Field(
        default=None, description="置信度低档上界覆盖"
    )
    confidence_high: float | None = Field(
        default=None, description="置信度高档下界覆盖"
    )


class AuditRequest(BaseModel):
    """审核请求体（POST /v1/audit）。"""

    text: str | None = Field(
        default=None, description="待检测文本，可为空或 None（纯图片请求）"
    )
    images: list[ImageInput] = Field(
        default_factory=list, description="输入图片列表（v0.1 仅静态图），可为空"
    )
    context: str | None = Field(
        default=None, description="审核上下文，仅注入 LLM 提示，不参与其他层"
    )
    skip_llm: bool = Field(
        default=False, description="跳过 LLM 兜底层（强制回退语义层结果）"
    )
    overrides: Overrides | None = Field(
        default=None, description="请求级参数覆盖（仅 full 组 Key 可用）"
    )


class KeywordHitModel(BaseModel):
    """关键词命中明细（detail.keyword.hits 元素）。"""

    keyword: str = Field(description="命中的关键词原文")
    category: str = Field(description="词库类别（如 色情 / 赌博 / 辱骂）")
    matched: str = Field(description="实际匹配到的文本片段（含拼音 / 变体展开）")
    start: int = Field(description="命中片段在原文中的起始下标（含）")
    end: int = Field(description="命中片段在原文中的结束下标（不含）")


class RegexFilteredHit(BaseModel):
    """被正则消歧层豁免的关键词命中及原因（detail.keyword.regex_filtered 元素）。"""

    keyword: str = Field(description="被豁免的关键词")
    category: str = Field(description="词库类别")
    matched: str = Field(description="实际匹配到的文本片段")
    reason: str = Field(description="豁免原因（命中的 exempt 正则说明）")


class KeywordDetail(BaseModel):
    """关键词层明细。"""

    hits: list[KeywordHitModel] = Field(
        default_factory=list, description="保留的关键词命中列表"
    )
    regex_filtered: list[RegexFilteredHit] = Field(
        default_factory=list, description="被正则消歧豁免的命中及原因"
    )


class LightModelResult(BaseModel):
    """轻量文本风险模型输出（detail.light_model）。"""

    label: str = Field(description="模型标签（如 违规 / 安全）")
    score: float = Field(description="风险得分（0~1）")
    violation: bool = Field(default=False, description="是否判定为违规信号")


class ImageWhitelistHit(BaseModel):
    """单帧图片白名单检查结果（detail.image_whitelist 元素）。"""

    frame: int = Field(description="帧序号（0 起）")
    hit: bool = Field(description="是否命中白名单")
    distance: int | None = Field(
        default=None, description="与最近白名单图片的 pHash 汉明距离（命中时给出）"
    )


class SemanticTopHit(BaseModel):
    """语义检索 Top 命中条目（semantic.black_top 元素）。"""

    id: str = Field(description="向量库条目 id")
    score: float = Field(description="余弦相似度（0~1）")
    category: str | None = Field(default=None, description="条目类别（如违规类别名）")


class SemanticDetail(BaseModel):
    """语义检索层明细（detail.semantic）。"""

    black_top: list[SemanticTopHit] = Field(
        default_factory=list, description="黑库最高相似度 Top-K 命中"
    )
    black_avg: float = Field(default=0.0, description="黑库 Top-K 平均相似度")
    white_avg: float = Field(default=0.0, description="白库 Top-K 平均相似度")
    margin: float | None = Field(
        default=None, description="黑均分−白均分差值与 margin 的比较结果（差值）"
    )


class LLMDetail(BaseModel):
    """LLM 兜底层明细（detail.llm）。"""

    is_violation: bool = Field(description="LLM 判定是否违规")
    category: str | None = Field(default=None, description="LLM 判定类别")
    confidence: float | None = Field(default=None, description="LLM 输出置信度（0~1）")
    reason: str | None = Field(default=None, description="LLM 判定理由")


class AuditDetail(BaseModel):
    """审核结果明细（full 组 API Key 返回，standard 组为 None）。"""

    keyword: KeywordDetail | None = Field(
        default=None, description="关键词命中与正则消歧结果"
    )
    light_model: LightModelResult | None = Field(
        default=None, description="轻量文本风险模型输出"
    )
    image_whitelist: list[ImageWhitelistHit] | None = Field(
        default=None, description="各帧白名单检查结果"
    )
    semantic: SemanticDetail | None = Field(
        default=None, description="语义检索层结果"
    )
    llm: LLMDetail | None = Field(default=None, description="LLM 兜底层结果")


class AuditResult(BaseModel):
    """审核响应体（POST /v1/audit，PRD §4.1）。"""

    request_id: str = Field(description="请求唯一标识（UUID）")
    timestamp: str = Field(description="审核完成时间（ISO 8601）")
    has_violation: bool = Field(description="是否判定违规")
    confidence: float = Field(description="综合置信度（0~1）")
    category: str | None = Field(
        default=None, description="判定类别（违规时给出，如 色情 / 赌博）"
    )
    source: Source = Field(
        description="判定来源：semantic / llm / basic_rules_pass / cache / permanent_list"
    )
    cache_hit: bool = Field(
        default=False, description="是否命中各级缓存直接返回"
    )
    detail: AuditDetail | None = Field(
        default=None, description="审核明细（仅 full 组填充，standard 组为 None）"
    )
