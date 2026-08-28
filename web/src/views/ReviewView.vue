<script setup lang="ts">
/**
 * 定时复核页（T24）：
 * - 状态卡片：复核调度（间隔分钟/启用状态）、上次运行时间、运行中标记、
 *   报告目录、auto_tune
 * - 「立即复核」按钮 → POST /admin/review/run（200 = 完成，202 = 已有复核执行中）
 * - 最近报告卡片：GET /admin/review/status 返回 last_report（最近一轮），展示
 *   采样/复核/一致率/分歧/建议；报告文件名由 ts 推导（服务端写盘规则
 *   {ts 的 ':' → '-'}.json，core/review.py _write_report）
 *
 * 字段对齐（依据 src/safefusion/core/review.py status()/ReviewReport.as_dict()、
 * src/safefusion/api/admin.py）：
 *   GET /admin/review/status → { enabled, interval_min, running, last_run_ts,
 *   last_report(dict|null), reports_dir, auto_tune }
 *   last_report 结构：{ ts, sampled, reviewed, consistent, consistent_rate,
 *   suggestions[], skipped_reason, mode, disagreements, stats }
 *   POST /admin/review/run → 200 { status:"ok", summary } | 202
 *   { status:"running", message, status_detail } | 未注入 reviewer 时 501
 *
 * 已知后端缺口（写入报告 TODO）：
 * - 无报告下载/查看端点：status 只返回最近一份报告元信息与 reports_dir →
 *   报告文件路径仅以文本展示（前端不做文件系统访问），下载留 TODO。
 * - 一次性只保留最近报告（scheduler 仅存 _last_report），无历史报告列表。
 */
import { onMounted, ref } from 'vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { apiGet, apiPost } from '../api/client'
import { useToastStore } from '../stores/toast'

/** GET /admin/review/status 响应结构（core/review.py status()） */
interface ReviewStatus {
  enabled: boolean
  interval_min: number
  running: boolean
  last_run_ts: string | null
  last_report: Record<string, unknown> | null
  reports_dir: string
  auto_tune: boolean
}

/** POST /admin/review/run 响应结构（200/202 两个形态） */
interface RunReviewResult {
  status: string // 'ok' | 'running'
  message?: string
  summary?: Record<string, unknown>
  status_detail?: Record<string, unknown>
}

const toast = useToastStore()

const status = ref<ReviewStatus | null>(null)
const loading = ref(false)
const running = ref(false)

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function fmtTime(ts: unknown): string {
  const s = textOf(ts)
  if (!s) return '从未运行'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

/** 一致率 → 百分数文本 */
function fmtRate(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

/** 报告文件名：服务端写盘规则 {ts 的 ':' 全部替换为 '-'}.json */
function reportFilename(ts: unknown): string {
  const s = textOf(ts)
  return s ? `${s.replace(/:/g, '-')}.json` : '—'
}

function numOf(value: unknown): number {
  return typeof value === 'number' && !Number.isNaN(value) ? value : 0
}

/** 最近报告的对象化访问（last_report 为动态 dict） */
function reportObject(): Record<string, unknown> {
  return (status.value?.last_report ?? {}) as Record<string, unknown>
}

function suggestions(): Array<Record<string, unknown>> {
  const s = reportObject().suggestions
  return Array.isArray(s) ? (s as Array<Record<string, unknown>>) : []
}

/** 报告补充统计条目（对象 → 键值对数组，供模板 v-for，避免模板内类型断言） */
function statsEntries(): Array<[string, unknown]> {
  const s = reportObject().stats
  if (s && typeof s === 'object' && !Array.isArray(s)) {
    return Object.entries(s as Record<string, unknown>)
  }
  return []
}

/** 报告分歧明细（missed 漏判 / false_alarm 误报） */
function disagreements(): Record<string, unknown> {
  const d = reportObject().disagreements
  return d && typeof d === 'object' && !Array.isArray(d) ? (d as Record<string, unknown>) : {}
}

// ---------- 数据加载 ----------
async function loadStatus(): Promise<void> {
  loading.value = true
  try {
    status.value = await apiGet<ReviewStatus>('/review/status')
  } catch (error) {
    console.warn('[ReviewView] 加载复核状态失败：', error)
  } finally {
    loading.value = false
  }
}

// ---------- 立即复核 ----------
async function runReview(): Promise<void> {
  running.value = true
  try {
    const res = await apiPost<RunReviewResult>('/review/run')
    if (res.status === 'ok') {
      toast.success(
        `复核完成：采样 ${numOf(res.summary?.sampled)} 条，复核 ${numOf(res.summary?.reviewed)} 条`,
      )
    } else if (res.status === 'running') {
      toast.info(textOf(res.message) || '复核已在执行中，本次手动触发已忽略')
    }
    await loadStatus() // 刷新上次运行时间与最近报告
  } catch (error) {
    console.warn('[ReviewView] 触发复核失败：', error)
  } finally {
    running.value = false
  }
}

onMounted(() => {
  void loadStatus()
})
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">⏱️ 定时复核</h2>
    <p class="page-hint">
      用 LLM 二次判定抽样复核审核记录中置信度中带样本，产出一致率与阈值建议，辅助校准判定阈值。
      主操作 = 「🚀 立即复核」；interval_min &gt; 0 时按分钟自动执行。
    </p>

    <!-- 状态卡片 -->
    <div class="stats-grid">
      <StatCard
        icon="⏱️"
        :value="status ? (status.enabled ? `每 ${status.interval_min} 分钟` : '未启用（仅手动）') : '…'"
        label="复核调度"
        tone="blue"
      />
      <StatCard icon="🕒" :value="status ? fmtTime(status.last_run_ts) : '…'" label="上次运行时间" tone="green" />
      <StatCard
        icon="🔄"
        :value="status ? (status.running ? '运行中' : '空闲') : '…'"
        label="运行状态"
        tone="orange"
      />
      <StatCard
        icon="📦"
        :value="status ? status.reports_dir : '…'"
        label="报告目录（服务端）"
        tone="purple"
      />
    </div>

    <!-- 手动触发 + 调度说明 -->
    <div class="card">
      <div class="card-title">
        <span>🚀 手动触发复核</span>
        <button type="button" class="btn btn-primary btn-sm" :disabled="running || (status?.running ?? false)" @click="runReview">
          {{ running ? '触发中...' : '立即复核' }}
        </button>
      </div>
      <p class="desc-text">
        复核从审计记录中采样置信度中带（band_low~band_high）样本，LLM 二次判定后产出
        一致率与阈值建议报告。当前 schema 仅存 text_hash（无原文）时自动降级为
        统计模式（skipped_reason=text_unavailable，TODO 见后端）；无 LLM 密钥时跳过
        （llm_unavailable）。interval_min &gt; 0 时后台自动调度；并发触发返回 202。
      </p>
      <p v-if="status" class="desc-text dim">
        auto_tune：{{ status.auto_tune ? '开启' : '关闭（仅出建议，不自动改阈值）' }}
      </p>
    </div>

    <!-- 最近报告卡片 -->
    <div class="card">
      <div class="card-title">
        <span>📄 最近复核报告</span>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="loadStatus">
          🔄 刷新
        </button>
      </div>

      <div v-if="loading" class="loading">加载中...</div>

      <div v-else-if="!status?.last_report" class="empty-state">
        <EmptyState
          icon="📄"
          title="暂无复核报告"
          :hint="'复核从审核记录中采样置信度中带（band_low~band_high）样本，LLM 二次判定后产出一致率与阈值建议。\n点击「立即复核」触发一轮；在系统设置「定时复核」分组把 interval_min 设为大于 0 即可按分钟自动执行。无 LLM 密钥时报告会标记跳过（llm_unavailable）。'"
          action-text="立即复核"
          @action="runReview"
        />
      </div>

      <template v-else>
        <!-- 报告元信息 -->
        <div class="report-meta">
          <div class="report-row">
            <span class="dl">报告时间：</span>
            <span>{{ fmtTime(reportObject().ts) }}</span>
            <span class="tag tag-blue">{{ textOf(reportObject().mode) || '—' }} 模式</span>
            <span v-if="textOf(reportObject().skipped_reason)" class="tag tag-orange">
              skipped: {{ textOf(reportObject().skipped_reason) }}
            </span>
          </div>
          <div class="report-row">
            <span class="dl">采样/复核/一致：</span>
            <span>
              {{ numOf(reportObject().sampled) }} / {{ numOf(reportObject().reviewed) }} /
              {{ numOf(reportObject().consistent) }}
            </span>
            <span class="dl">一致率：</span>
            <span>{{ fmtRate(reportObject().consistent_rate) }}</span>
          </div>
          <div class="report-row" v-if="Object.keys(disagreements()).length > 0">
            <span class="dl">分歧：</span>
            <span>漏判 {{ numOf(disagreements().missed) }}，误报 {{ numOf(disagreements().false_alarm) }}</span>
          </div>
          <div class="report-row">
            <span class="dl">报告文件（服务端，前端不访问文件系统）：</span>
            <span class="mono">{{ textOf(status?.reports_dir) }}/{{ reportFilename(reportObject().ts) }}</span>
          </div>
        </div>

        <!-- 若报告带 stats（统计模式补充统计），展开展示 -->
        <div v-if="statsEntries().length > 0" class="report-stats">
          <div class="sub-title">补充统计</div>
          <div class="stats-chips">
            <span v-for="[k, v] in statsEntries()" :key="k" class="chip">
              {{ k }}：{{ typeof v === 'object' ? JSON.stringify(v) : textOf(v) }}
            </span>
          </div>
        </div>

        <!-- 阈值建议列表 -->
        <div v-if="suggestions().length > 0" class="report-suggestions">
          <div class="sub-title">阈值建议</div>
          <div v-for="(s, i) in suggestions()" :key="i" class="suggestion">
            <span class="chip chip-action">{{ textOf(s.action) }}</span>
            <span class="mono">{{ textOf(s.key) }}</span>
            <span class="dl">{{ textOf(s.current) }} → {{ textOf(s.suggested) }}</span>
            <div class="suggestion-rationale">{{ textOf(s.rationale) }}</div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* 标题下操作提示行（PRD v0.3.0 §M1：每页用途 + 主操作） */
.page-hint {
  font-size: 0.76rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: -8px 0 14px;
}

.desc-text {
  font-size: 0.8rem;
  color: var(--text-2);
  line-height: 1.7;
  margin-top: 6px;
}

.dim {
  color: var(--text-3);
}

/* 报告元信息 */
.report-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.82rem;
  color: var(--text-2);
}

.report-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.dl {
  color: var(--text-3);
  font-weight: 600;
}

.mono {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.76rem;
  word-break: break-all;
}

.sub-title {
  font-size: 0.8rem;
  font-weight: 700;
  color: var(--text-2);
  margin: 14px 0 8px;
}

/* 标签 / 芯片 */
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}

.tag-blue {
  background: var(--primary-light);
  color: var(--primary);
}

.tag-orange {
  background: #fff7e8;
  color: #b5711a;
}

.chip {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  background: var(--surface-hover);
  color: var(--text-2);
  word-break: break-all;
}

.chip-action {
  background: var(--primary-light);
  color: var(--primary);
  font-weight: 700;
}

.stats-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* 建议列表 */
.report-suggestions {
  border-top: 1px solid var(--border);
  margin-top: 12px;
  padding-top: 4px;
}

.suggestion {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  font-size: 0.8rem;
  line-height: 1.5;
}

.suggestion-rationale {
  width: 100%;
  font-size: 0.74rem;
  color: var(--text-3);
  margin-top: 2px;
}
</style>