"""数据契约（schemas）测试：ImageInput 二选一 / Overrides / AuditRequest 校验 /
AuditResult source 枚举与 detail 嵌套模型。

对应 PRD §4.1 与 分工.md「统一接口契约」。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from safefusion.models.schemas import (
    AuditDetail,
    AuditRequest,
    AuditResult,
    ImageInput,
    KeywordDetail,
    Overrides,
)


class TestImageInput:
    """url 与 base64 必须且只能提供一个。"""

    def test_url_only(self) -> None:
        assert ImageInput(url="https://example.com/a.png").url == "https://example.com/a.png"

    def test_base64_only(self) -> None:
        assert ImageInput(base64="aGVsbG8=").base64 == "aGVsbG8="

    def test_both_none_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImageInput()

    def test_both_set_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImageInput(url="http://x", base64="aGVsbG8=")


class TestOverrides:
    """Overrides 全部字段可选；合法值类型为 float。"""

    def test_empty_ok(self) -> None:
        assert Overrides() is not None

    def test_all_fields(self) -> None:
        ov = Overrides(
            semantic_threshold=0.6,
            margin_w=0.07,
            confidence_low=0.3,
            confidence_high=0.8,
        )
        assert ov.confidence_high == 0.8
        assert ov.confidence_low == 0.3

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Overrides(semantic_threshold="high")


class TestAuditRequest:
    """审核请求：默认值 / 嵌套校验 / overrides 挂载。"""

    def test_empty_request_ok(self) -> None:
        req = AuditRequest()
        assert req.text is None
        assert req.images == []
        assert req.context is None
        assert req.skip_llm is False
        assert req.overrides is None

    def test_full_fields(self) -> None:
        req = AuditRequest(
            text="hello",
            images=[ImageInput(base64="x"), ImageInput(url="http://y")],
            context="ctx",
            skip_llm=True,
            overrides=Overrides(margin_w=0.01),
        )
        assert len(req.images) == 2
        assert req.skip_llm is True
        assert req.overrides is not None and req.overrides.margin_w == 0.01

    def test_images_validation_propagates(self) -> None:
        with pytest.raises(ValidationError):
            AuditRequest(images=[ImageInput()])

    def test_blank_but_provided_url_accepted(self) -> None:
        # ImageInput 只校验 url/base64 二选一的存在性；空白串视为「已提供」
        # （深层校验发生在解码层 decode_images），此处记录该行为
        assert ImageInput(url="  ").url == "  "


class TestAuditResult:
    """判定来源枚举 / 必填字段 / detail 嵌套模型。"""

    @pytest.mark.parametrize(
        "source",
        ["semantic", "llm", "basic_rules_pass", "cache", "permanent_list"],
    )
    def test_valid_sources(self, source: str) -> None:
        result = AuditResult(
            request_id="r1",
            timestamp="2026-01-01T00:00:00Z",
            has_violation=True,
            confidence=0.8,
            source=source,
        )
        assert result.source == source  # type: ignore[comparison-overlap]
        assert result.cache_hit is False
        assert result.detail is None

    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditResult(
                request_id="r1",
                timestamp="t",
                has_violation=False,
                confidence=0.0,
                source="bogus",
            )

    def test_missing_required_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditResult(has_violation=True)

    def test_detail_nested(self) -> None:
        detail = AuditDetail(
            keyword=KeywordDetail(
                hits=[
                    {"keyword": "裸聊", "category": "色情", "matched": "裸聊", "start": 0, "end": 2}
                ]
            )
        )
        result = AuditResult(
            request_id="r2",
            timestamp="t",
            has_violation=True,
            confidence=0.9,
            category="色情",
            source="semantic",
            detail=detail,
        )
        assert result.detail is not None
        assert result.detail.keyword is not None
        assert result.detail.keyword.hits[0].keyword == "裸聊"

    def test_detail_dict_validation(self) -> None:
        detail = AuditDetail.model_validate(
            {
                "semantic": {
                    "black_top": [{"id": "b0", "score": 0.7}],
                    "black_avg": 0.7,
                    "white_avg": 0.2,
                }
            }
        )
        assert detail.semantic is not None
        assert detail.semantic.black_top[0].score == 0.7
