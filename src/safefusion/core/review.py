"""定时复核模块（PRD v0.2 M7）：采样中置信度带 → LLM 复核 → 一致率 → 阈值建议。

设计要点：
- ``Reviewer.review_once`` 采样 ``audit_logs`` 中 ``confidence ∈ [band_low, band_high]``
  的最近 ``sample_size`` 条记录（DB 不支持按置信度区间过滤，分页拉取后内存过滤，
  页数与页大小设上限防全表扫描）；
- **原文可用性（重要结论）**：v0.1/v0.2 的 ``audit_logs`` 表只存 ``text_hash``
  （规范化文本的 SHA-256）与 ``detail_json``（关键词命中片段 / 轻量模型 /
  白名单 / 语义 / LLM 判定明细），**不落原文全文**。因此当前数据模型下复核
  **只能降级为统计模式**（``mode="statistical"``、``skipped_reason="text_unavailable"``）：
  报告仅含带内样本的分布统计与基于分布的初步阈值建议，无法逐条 LLM 二次判定。
  若未来 schema 在 ``detail_json``（或新增列）内嵌原文（``text`` / ``content`` /
  ``normalized`` / ``原文`` 键），``_recover_text`` 会自动识别并启用逐条复核
  （``mode="full"``）——该路径已实现并经 FakeLLM 单测覆盖；
- 无 LLM 密钥（``llm.available=False``）→ ``skipped_reason="llm_unavailable"``，
  不抛异常；
- 阈值建议按「违规一致率」方向生成，数值 ±0.05，clamp 到 [0, 1]；
  ``auto_tune=False``（决策 B 默认）时只出建议、不自动改阈值；
- ``ReviewScheduler``：``interval_min > 0`` 时在独立守护线程的 asyncio 事件循环
  内自动调度；``trigger()`` 手动触发（并发安全，执行中触发返回 None）；
  报告 JSON 写 ``{data_dir}/review_reports/{ts}.json``（目录自动创建）。

线程 / 事件循环约定：自动调度与手动触发均收敛到**同一个**事件循环
（调度线程的循环；未启动自动调度时用调用方循环），且建议调用方为复核器
注入**专用** ``LLMClient``（``AsyncOpenAI`` 客户端懒创建并绑定首个使用它的
循环，与审核 API / 管理 API 的循环隔离，避免跨循环复用）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

_logger = get_logger("core.review")

#: 逐条 LLM 复核的并发上限（asyncio.Semaphore，背压保护）
_REVIEW_CONCURRENCY = 4

#: 采样分页参数：DB 不支持置信度区间过滤，分页拉取后内存过滤；
#: 页大小 1000、最多 50 页（5 万条），防止采样把全表扫一遍
_SAMPLE_PAGE_SIZE = 1000
_SAMPLE_MAX_PAGES = 50

#: 违规一致率建议方向分界：≥ 此值视为「高」（建议扩大复核带），
#: ≤ 此值视为「低」（建议收窄复核带）
_HIGH_AGREE_RATE = 0.9
_LOW_AGREE_RATE = 0.5

#: 阈值建议步长（±0.05，PRD v0.2 M7）
_SUGGEST_DELTA = 0.05

#: detail_json 内嵌原文的候选键（未来 schema 支持后自动启用逐条复核）
_TEXT_KEYS = ("text", "content", "normalized", "原文")


def _utc_now() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（秒精度）。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any, default: float) -> float:
    """尽力转 float，失败回退默认值（配置防御）。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ReviewReport:
    """一轮复核的报告对象（``as_dict`` 供落盘与 API 返回）。

    Attributes:
        ts: 报告时间戳（ISO 8601，UTC）。
        sampled: 中置信度带内采样条数。
        reviewed: 成功逐条 LLM 复核的条数（统计模式为 0）。
        consistent: 复核与原始判定一致的条数。
        consistent_rate: 一致率 ``consistent / reviewed``；无复核样本时为 None。
        suggestions: 阈值建议列表（结构见 :func:`_build_suggestions`）。
        skipped_reason: 跳过原因（``llm_unavailable`` / ``text_unavailable`` /
            ``no_samples`` / ``error:<类型>``），正常复核为 None。
        mode: ``full``（逐条复核）或 ``statistical``（无原文，仅统计）。
        disagreements: 不一致明细 ``{"missed": 漏判, "false_alarm": 误报}``。
        stats: 补充统计（judge_failed / text_missing / 各类一致率等）。
    """

    ts: str
    sampled: int
    reviewed: int
    consistent: int
    consistent_rate: float | None
    suggestions: list[dict[str, Any]]
    skipped_reason: str | None = None
    mode: str = "full"
    disagreements: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化 dict（含规格要求的全部键，skipped_reason 可为 null）。"""

        return {
            "ts": self.ts,
            "sampled": self.sampled,
            "reviewed": self.reviewed,
            "consistent": self.consistent,
            "consistent_rate": self.consistent_rate,
            "suggestions": self.suggestions,
            "skipped_reason": self.skipped_reason,
            "mode": self.mode,
            "disagreements": self.disagreements,
            "stats": self.stats,
        }


def _sample_band(
    db: Any, band_low: float, band_high: float, sample_size: int
) -> list[dict[str, Any]]:
    """从审核记录中采样中置信度带内的最近 ``sample_size`` 条（时间倒序）。

    ``Database.query_logs`` 不支持置信度区间过滤，这里分页拉取（每页 1000 条、
    至多 50 页）逐条内存过滤，凑齐即停；带内记录不足时返回已收集部分。

    Args:
        db: ``storage.database.Database``（鸭子类型，提供 ``query_logs``）。
        band_low / band_high: 采样置信度下界 / 上界（含端点）。
        sample_size: 采样上限。

    Returns:
        审核记录 dict 列表（request_id / ts / text_hash / has_violation /
        confidence / source / detail_json 等，时间倒序）。
    """

    sampled: list[dict[str, Any]] = []
    for page in range(_SAMPLE_MAX_PAGES):
        try:
            page_rows = db.query_logs(limit=_SAMPLE_PAGE_SIZE, offset=page * _SAMPLE_PAGE_SIZE)
        except Exception as exc:
            _logger.warning("复核采样：query_logs 第 %d 页失败: %s", page, exc)
            if page == 0:
                raise  # 首页即失败 = 存储不可用整体失败（交 _run_once 产出 error 报告）
            break  # 后续页失败：保留已采集部分，降级继续
        for row in page_rows:
            confidence = _to_float(row.get("confidence"), math.nan)
            if math.isnan(confidence) or not (band_low <= confidence <= band_high):
                continue
            sampled.append(row)
            if len(sampled) >= sample_size:
                return sampled
        if len(page_rows) < _SAMPLE_PAGE_SIZE:
            break
    return sampled


def _recover_text(row: dict[str, Any]) -> str | None:
    """从一条审核记录尝试恢复原文（供 LLM 复核）。

    当前 schema 只存 ``text_hash`` + ``detail_json``，不落原文全文；
    本函数检查 ``detail_json`` 内的预定义键（``text`` / ``content`` /
    ``normalized`` / ``原文``），命中非空字符串即返回，否则返回 None。

    Args:
        row: 审核记录行 dict（含 ``detail_json``）。

    Returns:
        恢复的原文，无法恢复时 None。
    """

    raw = row.get("detail_json")
    if not raw:
        return None
    try:
        detail = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    for key in _TEXT_KEYS:
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _band_stats(rows: list[dict[str, Any]], band_low: float, band_high: float) -> dict[str, Any]:
    """统计模式下的带内样本分布统计（无原文时的降级输出）。"""

    total = len(rows)
    violations = sum(1 for row in rows if bool(row.get("has_violation")))
    confs = [_to_float(row.get("confidence"), math.nan) for row in rows]
    confs = [c for c in confs if not math.isnan(c)]
    sources: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown")
        sources[source] = sources.get(source, 0) + 1
    stats: dict[str, Any] = {
        "band": {"low": band_low, "high": band_high},
        "original_violations": violations,
        "original_safes": total - violations,
        "violation_share": round(violations / total, 3) if total else None,
        "confidence": {
            "min": round(min(confs), 3) if confs else None,
            "median": round(sorted(confs)[len(confs) // 2], 3) if confs else None,
            "max": round(max(confs), 3) if confs else None,
        },
        "sources": sources,
    }
    return stats


def _statistical_suggestions(
    rows: list[dict[str, Any]],
    band_low: float,
    band_high: float,
    low: float,
    high: float,
    auto_tune: bool,
) -> list[dict[str, Any]]:
    """统计模式（无原文）下的初步阈值建议：按带内违规占比给出观察性方向。

    ``action="watch"`` 表示仅供人工参考、无精确数值调整（逐条复核缺原文无法
    给出基于一致率的精确建议）。
    """

    if not rows:
        return []
    total = len(rows)
    violations = sum(1 for row in rows if bool(row.get("has_violation")))
    share = violations / total
    if share >= 0.7:
        rationale = (
            f"统计模式（audit_logs 未落原文，无法逐条 LLM 复核）：带内样本违规占比 "
            f"{share:.0%}，违规信号在 {band_high:.2f} 高档边界附近偏强，建议人工核查 "
            f"confidence_high 上边界样本；待原文落库（detail_json 内嵌 text 等键）后"
            f"启用逐条复核给出精确建议"
        )
        return [
            {
                "action": "watch",
                "key": "confidence_high",
                "current": high,
                "suggested": high,
                "delta": 0.0,
                "rationale": rationale,
                "auto_tune": auto_tune,
                "alternative": None,
            }
        ]
    if share <= 0.3:
        rationale = (
            f"统计模式（audit_logs 未落原文，无法逐条 LLM 复核）：带内样本违规占比 "
            f"{share:.0%}，多为安全样本，建议人工核查 confidence_low 下边界样本；待原文"
            f"落库（detail_json 内嵌 text 等键）后启用逐条复核给出精确建议"
        )
        return [
            {
                "action": "watch",
                "key": "confidence_low",
                "current": low,
                "suggested": low,
                "delta": 0.0,
                "rationale": rationale,
                "auto_tune": auto_tune,
                "alternative": None,
            }
        ]
    return []


def _clamp(value: float) -> float:
    """夹取到 [0, 1] 并保留两位小数。"""

    return round(max(0.0, min(1.0, value)), 2)


def _build_suggestions(
    violation_agree_rate: float | None,
    low: float,
    high: float,
    auto_tune: bool,
) -> list[dict[str, Any]]:
    """按违规一致率生成阈值建议（±0.05，clamp 到 [0, 1]）。

    方向约定（PRD v0.2 M7 / 决策 B）：
    - 违规一致率高（≥ 0.9）：LLM 复核与管线在违规判定上高度一致 → 复核手段可信，
      建议**扩大** LLM 复核带（上调 ``confidence_high``，或下调 ``confidence_low``），
      让更多边界样本先经复核再自动判级；
    - 违规一致率低（≤ 0.5）：复核与管线分歧明显 → 建议**收窄**复核带
      （下调 ``confidence_high``，或上调 ``confidence_low``），更依赖置信度分档；
    - 居中：暂不调整。
    ``auto_tune`` 仅透传配置值；无论 true/false 本模块都只输出建议不自动改阈值。

    Args:
        violation_agree_rate: 违规一致率（管线判违规的样本中 LLM 复核同样判违规
            的占比）；无违规样本可参照时为 None。
        low / high: 当前 ``confidence_low`` / ``confidence_high``。
        auto_tune: ``config.review.auto_tune`` 透传值。

    Returns:
        建议列表（单条主建议，``alternative`` 字段内嵌备选方向）。
    """

    if violation_agree_rate is None:
        return [
            {
                "action": "hold",
                "key": "confidence_high",
                "current": high,
                "suggested": high,
                "delta": 0.0,
                "rationale": "带内无管线判违规的样本可供比对，暂不调整阈值",
                "auto_tune": auto_tune,
                "alternative": None,
            }
        ]
    if violation_agree_rate >= _HIGH_AGREE_RATE:
        primary: dict[str, Any] = {
            "action": "raise",
            "key": "confidence_high",
            "current": high,
            "suggested": _clamp(high + _SUGGEST_DELTA),
            "delta": _SUGGEST_DELTA,
        }
        alternative: dict[str, Any] = {
            "action": "lower",
            "key": "confidence_low",
            "current": low,
            "suggested": _clamp(low - _SUGGEST_DELTA),
            "delta": -_SUGGEST_DELTA,
        }
        rationale = (
            f"违规一致率 {violation_agree_rate:.0%} ≥ {_HIGH_AGREE_RATE:.0%}：LLM 复核与管线"
            f"在违规判定上高度一致，复核手段可信 → 建议扩大 LLM 复核带（confidence_high "
            f"{high:.2f}→{primary['suggested']:.2f}，或 confidence_low {low:.2f}→"
            f"{alternative['suggested']:.2f}），让更多边界样本先经复核再自动判级"
        )
    elif violation_agree_rate <= _LOW_AGREE_RATE:
        primary = {
            "action": "lower",
            "key": "confidence_high",
            "current": high,
            "suggested": _clamp(high - _SUGGEST_DELTA),
            "delta": -_SUGGEST_DELTA,
        }
        alternative = {
            "action": "raise",
            "key": "confidence_low",
            "current": low,
            "suggested": _clamp(low + _SUGGEST_DELTA),
            "delta": _SUGGEST_DELTA,
        }
        rationale = (
            f"违规一致率 {violation_agree_rate:.0%} ≤ {_LOW_AGREE_RATE:.0%}：LLM 复核与管线"
            f"在违规判定上分歧明显 → 建议收窄 LLM 复核带（confidence_high "
            f"{high:.2f}→{primary['suggested']:.2f}，或 confidence_low {low:.2f}→"
            f"{alternative['suggested']:.2f}），更依赖置信度分档自动判定"
        )
    else:
        return [
            {
                "action": "hold",
                "key": "confidence_high",
                "current": high,
                "suggested": high,
                "delta": 0.0,
                "rationale": (
                    f"违规一致率 {violation_agree_rate:.0%} 居中（{_LOW_AGREE_RATE:.0%}~"
                    f"{_HIGH_AGREE_RATE:.0%}），暂不调整阈值"
                ),
                "auto_tune": auto_tune,
                "alternative": None,
            }
        ]
    return [
        {
            **primary,
            "rationale": rationale,
            "auto_tune": auto_tune,
            "alternative": alternative,
        }
    ]


class Reviewer:
    """中置信度带复核算法（无状态，供调度器与手动调用）。

    复核契约：``Reviewer.review_once(db, llm, config_review, thresholds=None)``。
    ``config_review`` 需提供 ``band_low`` / ``band_high`` / ``sample_size`` /
    ``auto_tune`` 属性（``config.ReviewConfig``）；``thresholds`` 可选，提供
    ``confidence_low`` / ``confidence_high``（``config.ThresholdsConfig``），
    缺省时以采样带边界作为当前阈值代理（默认配置下二者数值一致）。
    """

    async def review_once(
        self,
        db: Any,
        llm: Any,
        config_review: Any,
        thresholds: Any | None = None,
    ) -> ReviewReport:
        """执行一轮复核并返回报告（不落盘，落盘由调度器负责）。

        Args:
            db: 审核记录存储（鸭子类型，提供 ``query_logs``）。
            llm: LLM 客户端（鸭子类型，提供 ``available`` 与异步 ``judge``）；
                无密钥 / 缺失时跳过（``skipped_reason="llm_unavailable"``）。
            config_review: 复核配置（``band_low`` / ``band_high`` /
                ``sample_size`` / ``auto_tune``）。
            thresholds: 当前阈值（``confidence_low`` / ``confidence_high``），
                可选；缺省以 ``band_low`` / ``band_high`` 代理。

        Returns:
            :class:`ReviewReport`；任何跳过 / 失败路径都返回报告而非抛异常
            （单轮失败不拖垮调度）。
        """

        ts = _utc_now()
        band_low = _to_float(getattr(config_review, "band_low", 0.35), 0.35)
        band_high = _to_float(getattr(config_review, "band_high", 0.75), 0.75)
        sample_size = max(1, int(getattr(config_review, "sample_size", 50)))
        auto_tune = bool(getattr(config_review, "auto_tune", False))

        if thresholds is None:
            low, high = band_low, band_high
        else:
            low = _to_float(getattr(thresholds, "confidence_low", band_low), band_low)
            high = _to_float(getattr(thresholds, "confidence_high", band_high), band_high)

        if llm is None or not getattr(llm, "available", False):
            _logger.warning("定时复核跳过：LLM 客户端不可用（无密钥或未装配）")
            return ReviewReport(
                ts=ts,
                sampled=0,
                reviewed=0,
                consistent=0,
                consistent_rate=None,
                suggestions=[],
                skipped_reason="llm_unavailable",
                mode="statistical",
            )

        rows = _sample_band(db, band_low, band_high, sample_size)
        sampled = len(rows)
        if sampled == 0:
            _logger.info("定时复核：带内无样本（band=[%.2f, %.2f]）", band_low, band_high)
            return ReviewReport(
                ts=ts,
                sampled=0,
                reviewed=0,
                consistent=0,
                consistent_rate=None,
                suggestions=[],
                skipped_reason="no_samples",
                mode="statistical",
            )

        # 原文可取得性：当前 schema 只有 text_hash → 全部不可取 → 统计模式
        entries = [
            {
                "request_id": row["request_id"],
                "text": _recover_text(row),
                "has_violation": bool(row.get("has_violation")),
            }
            for row in rows
        ]
        recoverable = [entry for entry in entries if entry["text"] is not None]
        text_missing = sampled - len(recoverable)

        if not recoverable:
            _logger.warning(
                "定时复核降级为统计模式：audit_logs 仅存 text_hash 无法恢复原文 "
                "（sample=%d, band=[%.2f, %.2f]）",
                sampled,
                band_low,
                band_high,
            )
            return ReviewReport(
                ts=ts,
                sampled=sampled,
                reviewed=0,
                consistent=0,
                consistent_rate=None,
                suggestions=_statistical_suggestions(
                    rows, band_low, band_high, low, high, auto_tune
                ),
                skipped_reason="text_unavailable",
                mode="statistical",
                stats={**_band_stats(rows, band_low, band_high), "text_missing": text_missing},
            )

        verdicts = await self._rejudge(
            llm, [(entry["request_id"], entry["text"]) for entry in recoverable]
        )
        verdict_map = dict(verdicts)

        reviewed = 0
        consistent = 0
        agree_violation = 0
        orig_violations = 0
        missed = 0  # 原始安全，复核判违规（漏判）
        false_alarm = 0  # 原始违规，复核判安全（误报）
        judge_failed = 0  # LLM 复核失败（返回 None / 抛异常）
        for entry in recoverable:
            verdict = verdict_map[entry["request_id"]]
            if verdict is None:
                judge_failed += 1
                continue
            reviewed += 1
            original = entry["has_violation"]
            llm_violation = bool(verdict.get("is_violation"))
            if original:
                orig_violations += 1
            if llm_violation == original:
                consistent += 1
                if original:
                    agree_violation += 1
            elif original:
                false_alarm += 1
            else:
                missed += 1

        consistent_rate = consistent / reviewed if reviewed else None
        violation_agree_rate = agree_violation / orig_violations if orig_violations else None
        safe_agree_rate = (
            (consistent - agree_violation) / (reviewed - orig_violations)
            if reviewed - orig_violations > 0
            else None
        )

        suggestions = _build_suggestions(violation_agree_rate, low, high, auto_tune)
        stats: dict[str, Any] = {
            "judge_failed": judge_failed,
            "text_missing": text_missing,
            "violation_agree_rate": (
                round(violation_agree_rate, 3) if violation_agree_rate is not None else None
            ),
            "safe_agree_rate": round(safe_agree_rate, 3) if safe_agree_rate is not None else None,
        }
        _logger.info(
            "定时复核完成：sampled=%d reviewed=%d consistent=%d rate=%s",
            sampled,
            reviewed,
            consistent,
            f"{consistent_rate:.1%}" if consistent_rate is not None else "n/a",
        )
        return ReviewReport(
            ts=ts,
            sampled=sampled,
            reviewed=reviewed,
            consistent=consistent,
            consistent_rate=(round(consistent_rate, 3) if consistent_rate is not None else None),
            suggestions=suggestions,
            skipped_reason=None,
            mode="full",
            disagreements={"missed": missed, "false_alarm": false_alarm},
            stats=stats,
        )

    async def _rejudge(
        self, llm: Any, items: list[tuple[str, str]]
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """对给定 (request_id, 原文) 列表并发执行 LLM 复核（上限 4 路）。

        Args:
            llm: LLM 客户端（异步 ``judge(text, images, context, ...)``）。
            items: ``(request_id, text)`` 列表。

        Returns:
            ``(request_id, verdict)`` 列表；LLM 失败 / 异常对应 verdict 为 None。
        """

        semaphore = asyncio.Semaphore(_REVIEW_CONCURRENCY)

        async def one(request_id: str, text: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                try:
                    verdict = await llm.judge(text, [], None, cache_hint=f"review:{request_id}")
                except Exception as exc:
                    _logger.warning("复核 LLM 调用异常 request_id=%s: %s", request_id, exc)
                    return request_id, None
                return request_id, verdict

        return await asyncio.gather(*(one(rid, text) for rid, text in items))


class ReviewScheduler:
    """定时复核调度器：后台自动调度 + 手动触发 + 报告落盘。

    Args:
        db: 审核记录存储（``storage.database.Database``）。
        llm: LLM 客户端（建议为复核器**专用**实例，避免与审核管线共享
            ``AsyncOpenAI`` 客户端跨事件循环复用）。
        config: 应用配置（``config.review`` / ``config.thresholds`` /
            ``config.data_dir``）。
        data_dir: 报告目录根（缺省取 ``config.data_dir``）；报告写
            ``{data_dir}/review_reports/{ts}.json``。
        interval_seconds: 调度间隔覆盖（秒）；为 None 时取
            ``config.review.interval_min * 60``。供测试缩短间隔。
    """

    def __init__(
        self,
        db: Any,
        llm: Any,
        config: Any,
        data_dir: str | Path | None = None,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        """初始化调度器（不启动；自动调度需调用 :meth:`start`）。"""

        self._db = db
        self._llm = llm
        self._config = config
        self._review_cfg = getattr(config, "review", None)
        if self._review_cfg is None:
            raise ValueError("ReviewScheduler 需要 config.review 配置（PRD v0.2 M7）")
        self._thresholds = getattr(config, "thresholds", None)
        data_dir = (
            Path(data_dir) if data_dir is not None else Path(getattr(config, "data_dir", "./data"))
        )
        self._reports_dir = data_dir / "review_reports"

        self._reviewer = Reviewer()
        self._interval = (
            float(interval_seconds)
            if interval_seconds is not None
            else float(getattr(self._review_cfg, "interval_min", 0) * 60)
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._last_report: ReviewReport | None = None
        self._last_run_ts: str | None = None

    # ------------------------------------------------------------ 自动调度

    def start(self) -> None:
        """启动后台自动调度：``interval_min > 0`` 才启动（守护线程 + 独立事件循环）。

        手动触发（:meth:`trigger`）在自动调度启动后经
        ``run_coroutine_threadsafe`` 收敛到同一循环，保证所有复核调用
        （含 LLM 客户端绑定）不跨循环。
        """

        if self._thread is not None or self._interval <= 0:
            return
        self._stop_event = asyncio.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="safefusion-review"
        )
        self._thread.start()
        _logger.info("定时复核自动调度已启动：间隔 %.0f 秒", self._interval)

    def stop(self) -> None:
        """停止自动调度（等待线程退出；未启动时为 no-op）。"""

        if self._loop is None or self._stop_event is None or self._thread is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=5)

    def _thread_main(self) -> None:
        """调度线程入口：运行自动调度循环直到 stop（异常退出不静默）。"""

        assert self._loop is not None and self._stop_event is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._auto_loop())
        except Exception as exc:  # 调度循环异常退出：记录错误不崩溃进程
            _logger.error("定时复核调度线程异常退出: %r", exc)

    async def _auto_loop(self) -> None:
        """自动调度主循环：按间隔执行一轮复核，stop 事件触发即退出。"""

        assert self._stop_event is not None
        while not self._stop_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            if self._stop_event.is_set():
                break
            await self._run_once()

    # ------------------------------------------------------------ 手动触发

    async def trigger(self) -> ReviewReport | None:
        """手动触发一次复核（并发安全）。

        Returns:
            :class:`ReviewReport`；若已有复核在执行（自动轮次或并发手动触发），
            本次触发被忽略并返回 None（不会重复执行 / 重复落盘）。
        """

        if self._running:
            _logger.warning("定时复核：已有复核在执行，忽略本次手动触发")
            return None
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._run_once_guarded(), self._loop)
            return await asyncio.wrap_future(future)
        return await self._run_once_guarded()

    async def _run_once_guarded(self) -> ReviewReport | None:
        """在调度循环内执行一轮复核（互斥守卫：执行中触发返回 None）。"""

        if self._running:
            return None
        self._running = True
        try:
            return await self._run_once()
        finally:
            self._running = False

    async def _run_once(self) -> ReviewReport:
        """执行一轮复核并落盘报告，更新状态（含失败路径，不抛异常）。"""

        ts = _utc_now()
        try:
            report = await self._reviewer.review_once(
                self._db,
                self._llm,
                self._review_cfg,
                thresholds=self._thresholds,
            )
        except Exception as exc:
            _logger.exception("定时复核执行失败: %r", exc)
            report = ReviewReport(
                ts=ts,
                sampled=0,
                reviewed=0,
                consistent=0,
                consistent_rate=None,
                suggestions=[],
                skipped_reason=f"error:{type(exc).__name__}",
                mode="statistical",
                stats={"error": str(exc)},
            )
        self._last_report = report
        self._last_run_ts = report.ts
        self._write_report(report)
        return report

    def _write_report(self, report: ReviewReport) -> Path | None:
        """报告 JSON 写 ``{data_dir}/review_reports/{ts}.json``（目录自动创建，失败告警）。"""

        try:
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            # Windows 文件名不允许 ':'，ISO 时间戳替换为 '-'
            filename = f"{report.ts.replace(':', '-')}.json"
            path = self._reports_dir / filename
            path.write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return path
        except OSError as exc:
            _logger.warning("复核报告写盘失败: %s", exc)
            return None

    # ------------------------------------------------------------ 状态查询

    def status(self) -> dict[str, Any]:
        """调度状态：是否启用 / 间隔 / 运行中 / 上次运行时间 / 最近报告 / 报告目录。"""

        return {
            "enabled": self._interval > 0,
            "interval_min": int(getattr(self._review_cfg, "interval_min", 0)),
            "running": self._running,
            "last_run_ts": self._last_run_ts,
            "last_report": self._last_report.as_dict() if self._last_report is not None else None,
            "reports_dir": str(self._reports_dir),
            "auto_tune": bool(getattr(self._review_cfg, "auto_tune", False)),
        }
