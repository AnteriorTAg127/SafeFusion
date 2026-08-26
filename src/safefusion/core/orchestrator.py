"""审核编排器：按 PRD §3.1 串联五级缓存 / 基础规则 / 语义检索 / LLM / 汇总决策。

:class:`AuditOrchestrator` 持有 :class:`~safefusion.core.context.AppContext`
全部已装配组件，对单个 :class:`AuditRequest` 执行 PRD §3.1 主审核流程
（T9 任务卡 ①~⑩）：

① overrides 权限校验（仅 full 组可用，否则 PermissionError → T10 映射 403）；
② 预处理：文本规范化（NFKC + strip + 统一空白）→ sha256；图片解码 + md5/phash；
③ 审核缓存（完整键：文本哈希 + 帧哈希 + skip_llm + overrides 摘要）；
④ 永久黑白名单（黑优先，逐一检查文本哈希与各帧 md5）；
⑤ 高频缓存（仅无 context 的文本请求）；
⑥ 基础规则（关键词 + 正则消歧、轻量文本模型、逐帧图片白名单）——无短路；
⑦ 汇总：全部安全 → 快速放行（source=basic_rules_pass，PRD 唯一快速放行通道）；
⑧ 语义检索；**降级 ≠ 安全**（reason 非 None 时不作任何安全断言）；
⑨ 三档置信度动作 + LLM 兜底与回退；
⑩ 组装 AuditResult（detail 仅 full 组）+ 写各级缓存 + 写审核日志。

降级策略（全程不抛，PRD §2 降级链）：

- 任一步骤异常由最外层兜底捕获：返回 ``source="semantic"``、``confidence=0.0``
  的安全结果，错误信息写入审计日志 detail_json 的 ``degraded`` 键并打 error 日志；
- 语义层降级（``reason`` 非 None）时跳过置信度分档：无基础规则强信号（保留
  关键词命中 / 轻量模型判违规）→ 按安全处理并标注 degraded；有强信号 → 判违规
  （语义不可用时的保守取向，category 以关键词类别兜底）；
- LLM 档在 ``skip_llm`` / LLM 不可用 / judge 失败时回退语义层结果。
"""

import hashlib
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

import imagehash
from PIL import Image

from ..engines.image_pipeline import compute_hashes, decode_images
from ..engines.keyword_engine import KeywordHitData
from ..logging_setup import get_logger
from ..models.schemas import AuditDetail, AuditRequest, AuditResult
from .aggregator import decide_tier, merge_final, summarize_basic
from .context import AppContext


def _high_freq_key(text_hash: str, tier: str) -> str:
    """高频缓存键：文本哈希掺入 Key 分组，standard / full 结果互不污染。

    T8 契约的 ``get/put_high_freq(text_hash)`` 键仅含文本哈希，而 standard
    写入的结果无 detail，full 档直接命中会造成明细降级（主模型集成修复，
    2026-08-26，T10 自检暴露）。
    """

    return hashlib.sha256(f"{text_hash}:{tier}".encode()).hexdigest()


_logger = get_logger("core.orchestrator")


class AuditOrchestrator:
    """编排与决策集成核心：持有一组已装配组件，逐请求执行完整审核流程。"""

    def __init__(self, container: AppContext) -> None:
        """初始化编排器。

        Args:
            container: ``AppContext.build()`` 装配出的组件容器。
        """

        self.context = container
        self._cfg = container.config
        # 正则消歧规则：统一走 ctx.keyword_engine（T17 热重载后管理端写入即生效；
        # rules_enabled=False 时 disambiguate 原样透传，与 v0.1 行为一致）。
        # 不再持有独立空规则引擎（T17 集成钩子①，主模型 2026-08-26）。

    # ------------------------------------------------------------------ 主入口

    async def process_audit(self, req: AuditRequest, key_tier: str) -> AuditResult:
        """执行一次完整审核（PRD §3.1 主流程）。

        Args:
            req: 审核请求（PRD §4.1）。
            key_tier: 调用方 API Key 分组（``standard`` / ``full``）。

        Returns:
            审核结果（PRD §4.1 响应结构）。

        Raises:
            PermissionError: 非 full 组携带 overrides 字段时抛出（T10 映射 403）。
        """

        # ① overrides 权限：仅 full 组可用
        if req.overrides is not None and key_tier != "full":
            raise PermissionError("overrides 仅 full 组可用")
        request_id = uuid.uuid4().hex
        try:
            return await self._process(req, key_tier)
        except PermissionError:
            raise
        except Exception as exc:  # 全程兜底：任何组件异常不崩，返回安全结果
            _logger.error("process_audit 异常兜底 request_id=%s: %r", request_id, exc)
            return self._error_fallback(str(exc), key_tier, request_id)

    # ------------------------------------------------------------ 主流程实现

    async def _process(self, req: AuditRequest, key_tier: str) -> AuditResult:
        ctx = self.context
        cfg = self._cfg
        th = cfg.thresholds

        # ---------- ② 预处理 ----------
        normalized = _normalize_text(req.text) if req.text is not None else ""
        text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        frames: list[Image.Image] = []
        frame_md5s: list[str] = []
        frame_phash_hexes: list[str] = []
        frame_phashes: list[imagehash.ImageHash] = []
        if req.images:
            # v0.2 M3：动图按 config.image.animated 均匀抽帧，输出全部帧
            # （每帧独立元素，whitelist 逐帧、语义层池化平均天然按帧生效）
            frames = await decode_images(req.images, animated=self._cfg.image.animated)
            for img in frames:
                md5_hex, phash = compute_hashes(img)
                frame_md5s.append(md5_hex)
                frame_phash_hexes.append(str(phash))
                frame_phashes.append(phash)
        # 动图判定：任一输入展开出多帧 ⇒ 该请求含动图（静态多图请求帧数==输入数；
        # 用于 LLM 兜底档的多帧连贯性提示，PRD §3.5 / v0.2 M3）
        animated_request = len(frames) > len(req.images or [])

        # 请求级参数覆盖（① 已保证 overrides 仅 full 组到达此处）
        overrides_dump = (
            req.overrides.model_dump(exclude_none=True) if req.overrides is not None else {}
        )
        ov = {k: v for k, v in overrides_dump.items() if k in ("semantic_threshold", "margin_w")}
        conf_low = float(overrides_dump.get("confidence_low", th.confidence_low))
        conf_high = float(overrides_dump.get("confidence_high", th.confidence_high))

        cache_layer = ctx.cache_layer

        # ---------- ③ 审核缓存（完整键） ----------
        cache_key: str | None = None
        if cache_layer is not None:
            cache_key = cache_layer.audit_key(
                text_hash,
                frame_md5s,
                # 键含 tier：standard 与 full 的缓存隔离，避免 full 命中
                # standard 写入的无 detail 结果（主模型集成修复，2026-08-26，
                # 修复 T10 报告缺陷②）。
                {"skip_llm": req.skip_llm, "overrides": overrides_dump, "tier": key_tier},
            )
            cached = cache_layer.get_audit_result(cache_key)
            if cached is not None:
                return self._serve_cached(cached, key_tier)

        # ---------- ④ 永久黑白名单（黑优先） ----------
        if cache_layer is not None:
            tag = self._permanent_hit(cache_layer, text_hash if normalized else None, frame_md5s)
            if tag == "black":
                return self._simple_result(True, 0.0, None, "permanent_list")
            if tag == "white":
                return self._simple_result(False, 0.0, None, "permanent_list")

        # ---------- ⑤ 高频缓存（仅无 context 的文本请求） ----------
        if cache_layer is not None and normalized and req.context is None:
            high_hit = cache_layer.get_high_freq(_high_freq_key(text_hash, key_tier))
            if high_hit is not None:
                return self._serve_cached(high_hit, key_tier)

        # ---------- ⑥ 基础规则层（无短路；顺序执行即可，CPU 快） ----------
        keyword_detail: dict[str, Any] = {"hits": [], "regex_filtered": []}
        light_result: dict[str, Any] | None = None
        whitelist_matches: list[dict[str, Any]] = []
        kept_hits: list[KeywordHitData] = []
        strong_signal = False  # 关键词保留命中 / 轻量模型违规（语义降级时的证据）

        if ctx.keyword_engine is not None and normalized:
            raw_hits = ctx.keyword_engine.scan(normalized)
            kept_hits, exempted = ctx.keyword_engine.disambiguate(normalized, raw_hits)
            keyword_detail = {
                "hits": [dict(hit._asdict()) for hit in kept_hits],
                "regex_filtered": [_exempted_to_dict(item) for item in exempted],
            }

        if ctx.light_model is not None and not ctx.light_model.disabled and normalized:
            light_result = ctx.light_model.predict(normalized)

        whitelist_matches = []
        for idx, phash in enumerate(frame_phashes):
            row = None
            if ctx.whitelist is not None:
                row = ctx.whitelist.match(phash, th.phash_whitelist_distance)
            whitelist_matches.append(
                {
                    "frame": idx,
                    "hit": row is not None,
                    "distance": row.get("distance") if row is not None else None,
                }
            )

        if kept_hits:
            strong_signal = True
        if light_result is not None and light_result.get("violation"):
            strong_signal = True

        # ---------- ⑦ 汇总：唯一快速放行通道 ----------
        summary = summarize_basic(keyword_detail, light_result, whitelist_matches)
        detail_payload: dict[str, Any] | None = None
        if key_tier == "full":
            detail_payload = {
                "keyword": keyword_detail,
                "light_model": light_result,
                "image_whitelist": whitelist_matches,
            }
        if summary["all_safe"]:
            result = self._assemble(key_tier, False, 0.0, None, "basic_rules_pass", detail_payload)
            self._persist(
                req,
                key_tier,
                result,
                cache_key,
                text_hash,
                normalized,
                frame_md5s,
                frame_phash_hexes,
                detail_payload,
            )
            return result

        # ---------- ⑧ 语义检索（降级 ≠ 安全） ----------
        semantic_result = self._run_semantic(normalized or None, frames, ov)
        semantic_detail = _semantic_detail(semantic_result)
        if detail_payload is not None:
            detail_payload["semantic"] = semantic_detail

        sem_conf = float(semantic_result.get("confidence") or 0.0)
        sem_cat = semantic_result.get("category")
        sem_degraded = semantic_result.get("reason") is not None

        if sem_degraded:
            # 降级：reason 非 None 且 triggered=False 是「不可用」不是「安全」，
            # 跳过置信度分档直接裁决
            if strong_signal:
                category = sem_cat
                if category is None and keyword_detail["hits"]:
                    category = keyword_detail["hits"][0].get("category")
                has_violation, confidence, source = True, 0.0, "semantic"
            else:
                category = None
                has_violation, confidence, source = False, 0.0, "semantic"
            if detail_payload is not None:
                detail_payload["degraded"] = f"semantic:{semantic_result['reason']}"
        else:
            tier = decide_tier(sem_conf, conf_low, conf_high)
            if tier == "safe":
                has_violation, confidence, category, source = (
                    False,
                    sem_conf,
                    None,
                    "semantic",
                )
            elif tier == "violation":
                has_violation, confidence, category, source = (
                    True,
                    sem_conf,
                    sem_cat,
                    "semantic",
                )
            else:
                # ⑨ LLM 兜底档：skip_llm / 不可用 / 失败 → 回退语义层结果
                verdict = await self._llm_judge(
                    req,
                    normalized or None,
                    frames,
                    text_hash,
                    cache_layer,
                    animated=animated_request,
                )
                if verdict is not None:
                    if detail_payload is not None:
                        detail_payload["llm"] = {
                            "is_violation": bool(verdict.get("is_violation")),
                            "category": verdict.get("category"),
                            "confidence": verdict.get("confidence"),
                            "reason": verdict.get("reason"),
                        }
                    has_violation, confidence, category, source = merge_final(
                        verdict, semantic_result
                    )
                else:
                    has_violation, confidence, category, source = merge_final(None, semantic_result)

        # ---------- ⑩ 组装 + 写缓存 + 写审计日志 ----------
        result = self._assemble(
            key_tier, has_violation, confidence, category, source, detail_payload
        )
        self._persist(
            req,
            key_tier,
            result,
            cache_key,
            text_hash,
            normalized,
            frame_md5s,
            frame_phash_hexes,
            detail_payload,
        )
        return result

    # ------------------------------------------------------------ 分段辅助

    async def _llm_judge(
        self,
        req: AuditRequest,
        text: str | None,
        frames: list[Image.Image],
        text_hash: str,
        cache_layer: Any,
        *,
        animated: bool = False,
    ) -> dict[str, Any] | None:
        """LLM 兜底裁决：短文本 LLM 缓存优先；跳过 / 不可用 / 失败返回 None。

        Args:
            req: 审核请求（skip_llm / context 参与判定）。
            text: 净化后的待审核文本，可为 None。
            frames: 已解码帧列表（动图时为全部抽帧）。
            text_hash: 文本哈希（短文本 LLM 缓存键）。
            cache_layer: 缓存层（短文本 LLM 缓存读写；可为 None）。
            animated: 是否为动图多帧请求；True 时 ``LLMClient.judge`` 附加
                多帧连贯性提示词（v0.2 M3 / PRD §3.5）。
        """

        llm = self.context.llm
        if req.skip_llm or llm is None or not llm.available:
            return None
        short = bool(text) and len(text) <= self._cfg.llm.short_text_max_length
        verdict: dict[str, Any] | None = None
        if short and cache_layer is not None:
            verdict = cache_layer.get_short_text_llm(text_hash)
        if verdict is None:
            verdict = await llm.judge(text, frames, req.context, animated=animated)
            if verdict is not None and short and cache_layer is not None:
                cache_layer.put_short_text_llm(text_hash, verdict)
        return verdict

    def _run_semantic(
        self, text: str | None, frames: list[Image.Image], ov: dict[str, Any]
    ) -> dict[str, Any]:
        """执行语义检索；引擎缺失 / 异常均返回带 reason 的降级结果。"""

        semantic = self.context.semantic
        if semantic is None:
            return _semantic_degraded("semantic_disabled")
        try:
            result = semantic.audit(text, frames, ov)
            if not isinstance(result, dict):
                raise ValueError("semantic.audit 返回类型非法")
            return result
        except Exception as exc:
            _logger.warning("语义层 audit 异常（降级）: %s", exc)
            return _semantic_degraded("semantic_exception")

    @staticmethod
    def _permanent_hit(
        cache_layer: Any, text_hash: str | None, frame_md5s: list[str]
    ) -> str | None:
        """永久黑白名单检测：黑优先。返回 ``"black"`` / ``"white"`` / None。"""

        hashes: list[str] = []
        if text_hash:
            hashes.append(text_hash)
        hashes.extend(frame_md5s)
        for content_hash in hashes:
            if cache_layer.check_permanent(content_hash) == "black":
                return "black"
        for content_hash in hashes:
            if cache_layer.check_permanent(content_hash) == "white":
                return "white"
        return None

    @staticmethod
    def _serve_cached(stored: dict[str, Any], key_tier: str) -> AuditResult:
        """从缓存结果重建响应：刷新 request_id/timestamp、source=cache、detail 仅 full。"""

        result = AuditResult.model_validate(stored)
        result.request_id = uuid.uuid4().hex
        result.timestamp = _utc_now()
        result.source = "cache"
        result.cache_hit = True
        if key_tier != "full":
            result.detail = None
        return result

    @staticmethod
    def _simple_result(
        has_violation: bool, confidence: float, category: str | None, source: str
    ) -> AuditResult:
        """构造无 detail 的即时返回结果（永久黑白名单等短路路径）。"""

        return AuditResult(
            request_id=uuid.uuid4().hex,
            timestamp=_utc_now(),
            has_violation=has_violation,
            confidence=confidence,
            category=category,
            source=source,
            cache_hit=False,
            detail=None,
        )

    @staticmethod
    def _assemble(
        key_tier: str,
        has_violation: bool,
        confidence: float,
        category: str | None,
        source: str,
        detail_payload: dict[str, Any] | None,
    ) -> AuditResult:
        """组装 AuditResult；detail 仅 full 组填充（schemas 的 AuditDetail 子模型）。"""

        return AuditResult(
            request_id=uuid.uuid4().hex,
            timestamp=_utc_now(),
            has_violation=has_violation,
            confidence=float(confidence),
            category=category,
            source=source,
            cache_hit=False,
            detail=(
                AuditDetail.model_validate(detail_payload) if detail_payload is not None else None
            ),
        )

    def _error_fallback(self, message: str, key_tier: str, request_id: str) -> AuditResult:
        """最外层兜底：source="semantic"、confidence=0.0 的安全结果，错误写入日志。"""

        detail_payload: dict[str, Any] | None = None
        if key_tier == "full":
            detail_payload = {"degraded": f"error: {message}"}
        result = AuditResult(
            request_id=request_id,
            timestamp=_utc_now(),
            has_violation=False,
            confidence=0.0,
            category=None,
            source="semantic",
            cache_hit=False,
            detail=(
                AuditDetail.model_validate(detail_payload) if detail_payload is not None else None
            ),
        )
        db = self.context.database
        if db is not None:
            try:
                db.insert_audit_log(
                    request_id,
                    False,
                    "semantic",
                    detail=detail_payload,
                    key_tier=key_tier,
                )
            except Exception:
                _logger.warning("错误兜底审计日志写入失败: %s", message)
        return result

    # ------------------------------------------------------------ ⑩ 收尾

    def _persist(
        self,
        req: AuditRequest,
        key_tier: str,
        result: AuditResult,
        cache_key: str | None,
        text_hash: str,
        normalized: str,
        frame_md5s: list[str],
        frame_phash_hexes: list[str],
        detail_payload: dict[str, Any] | None,
    ) -> None:
        """⑩ 写审核缓存 / 高频缓存 / 图片去重缓存 + 写审计日志（SQLite）。"""

        cache_layer = self.context.cache_layer
        db = self.context.database

        if cache_layer is not None and cache_key is not None:
            cache_layer.put_audit_result(cache_key, result.model_dump())
        if cache_layer is not None and normalized and req.context is None:
            cache_layer.put_high_freq(_high_freq_key(text_hash, key_tier), result.model_dump())
        if cache_layer is not None and not normalized and len(frame_phash_hexes) == 1:
            cache_layer.put_dedup(frame_md5s[0], frame_phash_hexes[0], result.model_dump())
        if db is not None:
            db.insert_audit_log(
                result.request_id,
                result.has_violation,
                result.source,
                ts=result.timestamp,
                text_hash=text_hash or None,
                confidence=result.confidence,
                category=result.category,
                detail=detail_payload,
                key_tier=key_tier,
            )
        _logger.info(
            "audit done request_id=%s has_violation=%s source=%s confidence=%.3f",
            result.request_id,
            result.has_violation,
            result.source,
            result.confidence,
        )


# ------------------------------------------------------------------ 模块工具


def _utc_now() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（毫秒精度）。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_text(text: str) -> str:
    """文本规范化：NFKC + 首尾去空白 + 连续空白折叠为单空格。"""

    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).strip().split())


def _semantic_degraded(reason: str) -> dict[str, Any]:
    """构造语义层降级结果（与 engines.semantic 的降级键结构一致）。"""

    return {
        "triggered": False,
        "confidence": 0.0,
        "category": None,
        "black_top": None,
        "black_avg": 0.0,
        "white_avg": 0.0,
        "reason": reason,
    }


def _semantic_detail(semantic_result: dict[str, Any]) -> dict[str, Any]:
    """语义层结果 → AuditDetail.semantic 字典形态（black_top 单 dict → 列表）。"""

    black_top = semantic_result.get("black_top")
    top_list: list[dict[str, Any]] = []
    if isinstance(black_top, dict):
        top_list.append(
            {
                "id": str(black_top.get("id") or ""),
                "score": float(black_top.get("score") or 0.0),
                "category": black_top.get("category"),
            }
        )
    black_avg = float(semantic_result.get("black_avg") or 0.0)
    white_avg = float(semantic_result.get("white_avg") or 0.0)
    margin = None if semantic_result.get("reason") is not None else round(black_avg - white_avg, 6)
    return {
        "black_top": top_list,
        "black_avg": black_avg,
        "white_avg": white_avg,
        "margin": margin,
    }


def _exempted_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    """RegexRuleEngine.disambiguate 豁免项 ``{"hit", "rule"}`` → RegexFilteredHit 字典。"""

    hit = item["hit"]
    rule = item.get("rule") or {}
    reason = rule.get("reason") or rule.get("pattern") or ""
    return {
        "keyword": hit.keyword,
        "category": hit.category,
        "matched": hit.matched,
        "reason": str(reason),
    }
