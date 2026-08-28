<script setup lang="ts">
/**
 * 审核证据面板（PRD v0.3.0 §M9 A3，T37 新增共享组件）：
 * 把 AuditResult.detail 渲染为分层证据（对齐旧版 L1/L2/L3 形态，调研 §3.5 S1/S2/S3）：
 * - 判定总览：是否违规 / 置信度 / 耗时 / 渠道（来源中文映射）+ 缓存命中提示；
 * - 黑白对抗三值：detail.semantic 的 black_avg / white_avg / margin（黑−白差值；
 *   后端未返回 threshold，预留 threshold 字段存在则渲染，见 types.ts 说明）；
 * - 各层分节：关键词命中（词 + [词库类别] + 命中文段）、正则消歧（豁免及原因）、
 *   轻量文本模型、语义 Top 命中（相似度% + 类别 + 条目 id 兜底典型句）、
 *   图片白名单（逐帧）、LLM 结论（理由全文）；
 * - 层内未命中显示灰色「未命中」tag；降级标记双检测（detail.degraded 字符串 /
 *   语义 margin===null）顶部横幅展示；
 * - 原始 JSON <details> 折叠兜底（复用 JsonTree，textContent 安全渲染）。
 *
 * 纯展示组件（无副作用、不发请求），可被 AuditView 详情复用（T41 侧接线）。
 * 安全：全部内容经 Vue 插值（默认 textContent 转义），绝不使用 v-html。
 */
import { computed } from 'vue'
import type { AuditDetail, AuditResult, KeywordHit, RegexFilteredHit } from '../api/types'
import JsonTree from '../views/components/JsonTree.vue'

const props = withDefaults(
  defineProps<{
    /** 审核结果（POST /admin/test-audit 响应体；null 渲染空态占位） */
    result: AuditResult | null
    /** 审核中（渲染 loading 占位，不展示旧结果） */
    loading?: boolean
    /** 本次审核前端实测耗时（ms；0 渲染 —） */
    durationMs?: number
  }>(),
  { loading: false, durationMs: 0 },
)

// ---------- 判定总览 ----------
/** 判定来源 → 中文（schemas.Source 五值 + 兜底原文） */
const SOURCE_LABELS: Record<string, string> = {
  semantic: '语义层',
  llm: 'LLM 兜底',
  basic_rules_pass: '基础规则放行',
  cache: '缓存命中',
  permanent_list: '永久黑白名单',
}

const sourceText = computed(() => {
  const src = props.result?.source
  return src ? (SOURCE_LABELS[src] ?? src) : '—'
})

const durationText = computed(() =>
  props.durationMs > 0 ? `${Math.round(props.durationMs)} ms` : '—',
)

const timeText = computed(() => {
  const ts = props.result?.timestamp ?? ''
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
})

// ---------- detail 分层 ----------
const detail = computed<AuditDetail | null>(() => props.result?.detail ?? null)
const sem = computed(() => detail.value?.semantic ?? null)
const kw = computed(() => detail.value?.keyword ?? null)
const kwHits = computed<KeywordHit[]>(() => kw.value?.hits ?? [])
const regexHits = computed<RegexFilteredHit[]>(() => kw.value?.regex_filtered ?? [])
const semHits = computed(() => sem.value?.black_top ?? [])
const lm = computed(() => detail.value?.light_model ?? null)
const llm = computed(() => detail.value?.llm ?? null)
const wlFrames = computed(() => detail.value?.image_whitelist ?? [])

/** 语义层降级：margin===null（orchestrator 降级时置 None，且三值为 0/空 Top） */
const semanticDegraded = computed(() => sem.value !== null && sem.value.margin === null)

/** 降级横幅文本：detail.degraded（日志侧字符串）或语义降级推断 */
const degradedText = computed(() => {
  const raw = detail.value?.degraded
  if (raw) return raw
  if (semanticDegraded.value) return 'semantic 层不可用（降级）：语义证据缺失，判定为保守取向或已降级放行'
  return ''
})

// ---------- 格式化 ----------
/** 0~1 数值 → 百分数文本；null/undefined/NaN → — */
function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

/** 0~1 风险分 → 两位小数文本 */
function fmtScore(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(2)
}

/** 距离等整型/可空值 → 文本 */
function fmtVal(v: unknown): string {
  return v === null || v === undefined ? '—' : String(v)
}

/** 请求 ID 截断（title 显示全文） */
function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id
}
</script>

<template>
  <div class="evidence-panel">
    <!-- 审核中占位 -->
    <div v-if="loading" class="card ev-loading">
      <span class="ev-loading-ico" aria-hidden="true">⏳</span>
      <span>审核中…（试运行以管理端 full 权限执行；首次可能触发语义层懒装配，请稍候）</span>
    </div>

    <!-- 有结果 -->
    <template v-else-if="result">
      <!-- 判定总览 -->
      <div class="card ev-card">
        <div class="card-title"><span>⚖️ 判定总览</span></div>
        <div class="ev-verdict">
          <span :class="result.has_violation ? 'tag tag-danger' : 'tag tag-success'">
            {{ result.has_violation ? '⚠️ 违规' : '✅ 通过' }}
          </span>
          <span class="ev-verdict-item">置信度 <b>{{ fmtPct(result.confidence) }}</b></span>
          <span v-if="result.category" class="ev-verdict-item">类别 <b>{{ result.category }}</b></span>
          <span class="ev-verdict-item">耗时 <b>{{ durationText }}</b></span>
          <span class="ev-verdict-item">渠道 <b>{{ sourceText }}</b></span>
          <span v-if="result.cache_hit" class="tag tag-gray">缓存命中</span>
        </div>
        <p class="ev-meta">
          请求 <span class="mono">{{ shortId(result.request_id) }}</span> · 时间 {{ timeText }}
        </p>
        <p v-if="degradedText" class="ev-degraded">⚠️ 降级：{{ degradedText }}</p>
      </div>

      <!-- 无明细分支（快捷路径如永久黑白名单 / standard 组） -->
      <div v-if="!detail" class="card ev-card">
        <span class="tag tag-gray">无分层证据</span>
        <p class="ev-note">
          detail 为 null：请求命中快捷路径（永久黑白名单等）或非 full 组结果；
          试运行固定以 full 组执行，正常应有完整明细。
        </p>
      </div>

      <template v-else>
        <!-- 黑白对抗相似度（S3） -->
        <div v-if="sem" class="card ev-card">
          <div class="card-title"><span>🆚 黑白对抗相似度</span></div>
          <div class="ev-signals">
            <div class="ev-signal" :class="{ muted: semanticDegraded }">
              <span class="ev-signal-label">黑库平均</span>
              <b>{{ fmtPct(sem.black_avg) }}</b>
            </div>
            <div class="ev-signal" :class="{ muted: semanticDegraded }">
              <span class="ev-signal-label">白库平均</span>
              <b>{{ fmtPct(sem.white_avg) }}</b>
            </div>
            <div class="ev-signal" :class="{ muted: semanticDegraded }">
              <span class="ev-signal-label">差值（黑 − 白）</span>
              <b>{{ sem.margin === null ? '不可用' : fmtPct(sem.margin) }}</b>
            </div>
            <div v-if="sem.threshold !== undefined && sem.threshold !== null" class="ev-signal">
              <span class="ev-signal-label">生效阈值</span>
              <b>{{ fmtPct(sem.threshold) }}</b>
            </div>
          </div>
          <p v-if="semanticDegraded" class="ev-note">
            语义层降级（margin 为 null）：三值无可信证据，黑/白均分归零展示。
          </p>
        </div>

        <!-- L1 关键词命中（S2：词 + [来源词库] + 命中文段） -->
        <div class="card ev-card">
          <div class="card-title"><span>🔤 关键词命中</span></div>
          <template v-if="kwHits.length">
            <div v-for="(hit, i) in kwHits" :key="`${hit.keyword}-${hit.start}-${i}`" class="ev-chip-row">
              <span class="chip-word">{{ hit.keyword }}</span>
              <span class="chip-meta">[{{ hit.category }}]</span>
              <span class="chip-match">命中文段：{{ hit.matched }}</span>
            </div>
          </template>
          <span v-else class="tag tag-gray">未命中</span>
        </div>

        <!-- 正则消歧（豁免命中及原因） -->
        <div class="card ev-card">
          <div class="card-title"><span>🧩 正则消歧（豁免）</span></div>
          <template v-if="regexHits.length">
            <div v-for="(hit, i) in regexHits" :key="`${hit.keyword}-${hit.matched}-${i}`" class="ev-chip-row">
              <span class="chip-word">{{ hit.keyword }}</span>
              <span class="chip-meta">[{{ hit.category }}]</span>
              <span class="chip-match">文段：{{ hit.matched }}</span>
              <span class="chip-reason">豁免原因：{{ hit.reason }}</span>
            </div>
          </template>
          <span v-else class="tag tag-gray">未命中（无豁免）</span>
        </div>

        <!-- 轻量文本模型（fasttext 信号） -->
        <div class="card ev-card">
          <div class="card-title"><span>🔎 轻量文本模型</span></div>
          <template v-if="lm">
            <span :class="lm.violation ? 'tag tag-danger' : 'tag tag-success'">
              {{ lm.violation ? '违规信号' : '安全信号' }}
            </span>
            <span class="ev-inline">标签 {{ fmtVal(lm.label) }}</span>
            <span class="ev-inline">风险分 {{ fmtScore(lm.score) }}</span>
          </template>
          <span v-else class="tag tag-gray">未配置 / 未触发</span>
        </div>

        <!-- L2 语义 Top 命中（相似度% + 类别 + 条目 id 兜底典型句，后端无典型句字段） -->
        <div class="card ev-card">
          <div class="card-title"><span>🧠 语义 Top 命中</span></div>
          <template v-if="semHits.length">
            <div v-for="(hit, i) in semHits" :key="`${hit.id}-${i}`" class="ev-row">
              <span class="ev-rank">{{ i + 1 }}</span>
              <span class="tag tag-blue">{{ fmtPct(hit.score) }}</span>
              <span class="ev-inline">{{ hit.category || '未分类' }}</span>
              <span class="ev-inline mono">条目 {{ hit.id }}</span>
            </div>
            <p v-if="semanticDegraded" class="ev-note">语义层降级，Top 列表为空，以上为不可信或历史占位。</p>
          </template>
          <template v-else>
            <span class="tag tag-gray">未命中</span>
            <p v-if="semanticDegraded" class="ev-note">语义层降级，未产生有效 Top 结果。</p>
            <p v-else-if="!sem" class="ev-note">文本安全快速放行或未进入语义层（basic_rules_pass）。</p>
          </template>
        </div>

        <!-- 图片白名单（逐帧） -->
        <div class="card ev-card">
          <div class="card-title"><span>🖼️ 图片白名单（逐帧）</span></div>
          <template v-if="wlFrames.length">
            <div v-for="(f, i) in wlFrames" :key="i" class="ev-row">
              <span class="ev-rank">帧 {{ f.frame }}</span>
              <span :class="f.hit ? 'tag tag-success' : 'tag tag-gray'">
                {{ f.hit ? '命中白名单' : '未命中' }}
              </span>
              <span class="ev-inline">pHash 距离 {{ fmtVal(f.distance) }}</span>
            </div>
          </template>
          <span v-else class="tag tag-gray">无图片输入</span>
        </div>

        <!-- L3 LLM 结论（理由全文） -->
        <div class="card ev-card">
          <div class="card-title"><span>🤖 LLM 结论</span></div>
          <template v-if="llm">
            <div class="ev-llm-verdict">
              <span :class="llm.is_violation ? 'tag tag-danger' : 'tag tag-success'">
                {{ llm.is_violation ? '判定违规' : '判定安全' }}
              </span>
              <span v-if="llm.category" class="ev-inline">类别 {{ llm.category }}</span>
              <span v-if="llm.confidence !== null" class="ev-inline">置信度 {{ fmtPct(llm.confidence) }}</span>
            </div>
            <p v-if="llm.reason" class="ev-reason">{{ llm.reason }}</p>
          </template>
          <template v-else>
            <span class="tag tag-gray">未触发</span>
            <p class="ev-note">LLM 兜底仅在语义置信度处于中间档时触发（或 skip_llm / 不可用回退语义结论）。</p>
          </template>
        </div>

        <!-- 原始 JSON 折叠兜底（复用 JsonTree，textContent 安全渲染） -->
        <details class="card ev-raw">
          <summary class="ev-raw-summary">🔗 原始 JSON（结构化之外的技术兜底）</summary>
          <div class="ev-raw-body">
            <JsonTree :value="result" />
          </div>
        </details>
      </template>
    </template>

    <!-- 空态占位（父级通常用 EmptyState 接管，此处仅兜底） -->
    <div v-else class="card ev-card">
      <span class="tag tag-gray">暂无结果</span>
      <p class="ev-note">发送试运行文本后，分层证据将显示在这里。</p>
    </div>
  </div>
</template>

<style scoped>
/* ---------- 面板容器 ---------- */
.ev-card {
  margin-bottom: 14px;
}

/* 审核中占位 */
.ev-loading {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-2);
  font-size: 0.86rem;
}

.ev-loading-ico {
  font-size: 1.1rem;
  animation: ev-pulse 1.2s ease-in-out infinite;
}

@keyframes ev-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}

/* ---------- 判定总览 ---------- */
.ev-verdict {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  font-size: 0.86rem;
  color: var(--text-2);
}

.ev-verdict-item b {
  color: var(--text);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.ev-meta {
  margin-top: 8px;
  font-size: 0.72rem;
  color: var(--text-3);
}

.ev-degraded {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--danger-light);
  color: var(--danger);
  font-size: 0.78rem;
  line-height: 1.6;
}

/* ---------- 黑白对抗 ---------- */
.ev-signals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}

.ev-signal {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-hover);
}

.ev-signal b {
  font-size: 1.05rem;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.ev-signal.muted b {
  color: var(--text-3);
}

.ev-signal-label {
  font-size: 0.72rem;
  color: var(--text-3);
  font-weight: 600;
}

/* ---------- 分层内容 ---------- */
.ev-chip-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
  padding: 5px 0;
  font-size: 0.82rem;
  border-bottom: 1px dashed var(--border);
}

.ev-chip-row:last-child {
  border-bottom: none;
}

.chip-word {
  font-weight: 600;
  color: var(--danger);
}

.chip-meta {
  font-size: 0.74rem;
  color: var(--text-3);
}

.chip-match,
.chip-reason {
  color: var(--text-2);
  word-break: break-all;
}

.chip-reason {
  font-size: 0.76rem;
  color: var(--text-3);
}

.ev-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 5px 0;
  font-size: 0.82rem;
  border-bottom: 1px dashed var(--border);
}

.ev-row:last-child {
  border-bottom: none;
}

.ev-rank {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--surface-hover);
  color: var(--text-3);
  font-size: 0.7rem;
  font-weight: 700;
}

.ev-inline {
  color: var(--text-2);
}

.ev-llm-verdict {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  font-size: 0.84rem;
}

.ev-reason {
  margin-top: 10px;
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  background: var(--surface-hover);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 0.82rem;
  color: var(--text-2);
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}

.ev-note {
  margin-top: 8px;
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.7;
}

/* ---------- 原始 JSON ---------- */
.ev-raw {
  padding: 12px 20px;
  margin-bottom: 0;
}

.ev-raw-summary {
  cursor: pointer;
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-2);
  user-select: none;
}

.ev-raw-body {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}

/* ---------- 标签（审计侧同名语义；自含避免依赖视图 scoped 类） ---------- */
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}

.tag-danger {
  background: var(--danger-light);
  color: var(--danger);
}

.tag-success {
  background: var(--success-light);
  color: var(--success);
}

.tag-blue {
  background: var(--primary-light);
  color: var(--primary);
}

.tag-gray {
  background: var(--surface-hover);
  color: var(--text-3);
}

.mono {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.72rem;
}
</style>