<script setup lang="ts">
/**
 * 审核记录页（T24 + T41 增强）：
 * - 筛选栏：时间范围（datetime-local）、结论（全部/违规/通过）、置信度区间
 *   （min/max）、文本哈希模糊；查询 / 重置
 * - DataTable + Pagination + 空态 + loading；点击行任意单元格呼出 AppModal 查看
 *   detail_json（T41：详情由 EvidencePanel（T37 组件）分层渲染，原始 JSON 折叠
 *   由 EvidencePanel 内部承载；detail 为 null 时说明 standard 渠道不返回明细）
 * - T41「⬇️ 导出 CSV」：按当前筛选调用既有导出端点 GET /admin/logs/export
 *   （v0.1 T11：StreamingResponse + utf-8-sig BOM，过滤参数与 /admin/logs 一致，
 *   无分页参数、全量流式）；响应为 CSV 文本，前端组装 blob 下载；
 *   上限 10,000 行语义说明：后端不强制截断，前端在结果条数超过 1 万行时二次
 *   确认并提示缩小范围（详见 exportHint 常量与 askExport）
 * - T41「⟳ 自动刷新（10s）」：开关默认关，localStorage sf_audit_autorefresh 记忆；
 *   开启后每 10 秒重拉当前页（保留筛选与页码），离开页面自动清除定时器
 *
 * 字段对齐（依据 src/safefusion/api/admin.py query_logs + _normalize_log、
 * storage/database.py audit_logs 表）：
 *   GET /admin/logs → { total, page, page_size, items:[{ request_id, ts,
 *   text_hash, has_violation(bool), confidence(number|null), category, source,
 *   detail(dict|null，由 detail_json 解析而来), key_tier }] }
 *   降级标记列取 detail.degraded（orchestrator.py 写入 "semantic:<原因码>"）。
 *
 * T41 EvidencePanel 对接（T37 组件，真实契约见 web/src/api/types.ts + EvidencePanel.vue）：
 *   props: { result: AuditResult | null, loading?: boolean, durationMs?: number }
 *   AuditView 把审计日志行映射为 AuditResult（auditResult computed）：
 *   request_id←row.request_id、timestamp←row.ts、has_violation←row.has_violation、
 *   confidence←row.confidence（空→0）、category←row.category、source←row.source
 *   （窄化为 AuditSource 五值，未知兜底 semantic）、cache_hit←row.cache_hit（日志
 *   无此列→false）、detail←row.detail（解析后的 AuditDetail 或 null）。
 *   原始 JSON <details> 折叠由 EvidencePanel 内部承载（复用 JsonTree）。
 *
 * 已知后端缺口（写入报告 TODO，由主模型集成阶段处理，本页不做后端改动）：
 * - 置信度区间 / 文本哈希过滤：/admin/logs 无对应查询参数（仅 start/end/
 *   has_violation/source/category/key_tier/basic 分页）→ 本页在启用这两类筛选
 *   时进入「客户端过滤模式」：循环拉取筛选窗口内全部记录（最多 40 页 × 500 =
 *   2 万条，超限截断近似），内存过滤后前端分页；未启用时走服务端分页。
 *   导出端点同样不支持这两类参数 → 导出时忽略它们并在按钮 title 注明。
 * - 结论「需人工」：audit_logs 无该状态（仅 has_violation 0/1）→ 筛选栏仅
 *   提供 全部/违规/通过，第三态留 TODO。
 * - 错误响应体为 {error}（admin.py 全局异常处理器），而 api/client.ts 的
 *   readableError 只解析 {detail} → 错误 Toast 显示通用文案（对齐问题记录
 *   TODO，本页不修改 client.ts；导出为直连 http 请求，本页内置双错误体解析）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import AppModal from '../components/AppModal.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import { apiGet, http } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { AuditDetail, AuditResult, AuditSource } from '../api/types'

interface LogsPage {
  total: number
  page: number
  page_size: number
  items: Array<Record<string, unknown>>
}

/** 审核记录行（字段与 /admin/logs items 对齐） */
type LogRow = Record<string, unknown>

const PAGE_SIZE = 20 // 分页每页条数（服务端模式与客户端模式一致）
const ALL_PAGE_SIZE = 500 // 客户端过滤模式分批拉取页大小
const ALL_MAX_PAGES = 40 // 客户端过滤模式页数上限（2 万条，超限截断近似）

// ---------- 筛选状态 ----------
const startTime = ref('') // datetime-local 原文（本地时区）
const endTime = ref('')
const conclusion = ref('') // '' 全部 | 'true' 违规 | 'false' 通过
const confMin = ref('')
const confMax = ref('')
const textHash = ref('')

// ---------- 列表状态 ----------
const rows = ref<LogRow[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
// 客户端过滤模式的中间结果（供内存分页与总条数）
const clientRows = ref<LogRow[]>([])
const inClientMode = ref(false)

// ---------- 明细弹窗 ----------
const detailOpen = ref(false)
const detailRow = ref<LogRow | null>(null)

/** detail 对象（null → 说明 standard 渠道无明细） */
const detailObj = computed<Record<string, unknown> | null>(() => {
  const d = detailRow.value?.detail
  return d && typeof d === 'object' ? (d as Record<string, unknown>) : null
})

/** AuditSource 合法值（schemas.Source 五值，用于 row.source 窄化） */
const SOURCE_VALUES: readonly AuditSource[] = [
  'semantic',
  'llm',
  'basic_rules_pass',
  'cache',
  'permanent_list',
]

/**
 * 审计日志行 → EvidencePanel 可渲染的 AuditResult（T41 接线；
 * 字段映射见文件头注释；面板内部再取 result.detail 分层渲染）
 */
const auditResult = computed<AuditResult | null>(() => {
  const row = detailRow.value
  if (!row) return null
  const rawSource = textOf(row.source)
  const source: AuditSource = (SOURCE_VALUES as readonly string[]).includes(rawSource)
    ? (rawSource as AuditSource)
    : 'semantic'
  return {
    request_id: textOf(row.request_id),
    timestamp: textOf(row.ts),
    has_violation: isViolation(row),
    confidence: typeof row.confidence === 'number' ? row.confidence : 0,
    category: typeof row.category === 'string' && row.category ? row.category : null,
    source,
    cache_hit: row.cache_hit === true,
    detail: detailObj.value as AuditDetail | null,
  }
})

// ---------- T41：自动刷新（10s，默认关，localStorage 记忆） ----------
const AUTO_REFRESH_KEY = 'sf_audit_autorefresh'
const AUTO_REFRESH_MS = 10_000

const autoRefresh = ref(localStorage.getItem(AUTO_REFRESH_KEY) === '1')
let refreshTimer: number | undefined
const toast = useToastStore()

function applyAutoRefresh(enable: boolean): void {
  if (enable) {
    if (refreshTimer === undefined) {
      refreshTimer = window.setInterval(() => {
        void loadData() // 定时重拉当前页（保留筛选与页码）
      }, AUTO_REFRESH_MS)
    }
    localStorage.setItem(AUTO_REFRESH_KEY, '1')
  } else {
    if (refreshTimer !== undefined) {
      window.clearInterval(refreshTimer)
      refreshTimer = undefined
    }
    localStorage.removeItem(AUTO_REFRESH_KEY)
  }
}

watch(autoRefresh, (enabled) => {
  applyAutoRefresh(enabled)
  toast.info(enabled ? '已开启自动刷新（每 10 秒）' : '已关闭自动刷新')
})

// ---------- T41：CSV 导出 ----------
/** 导出行数上限（PRD §M9 G5 语义说明；后端 /logs/export 不强制截断，前端提示性约定） */
const EXPORT_MAX_ROWS = 10_000
/** 导出按钮 hover 提示：说明端点契约与上限语义 */
const exportHint =
  '按当前筛选（时间 / 结论；置信度与文本哈希为客户端过滤，导出不支持）下载 CSV。' +
  '导出端点 GET /admin/logs/export 为全量流式（utf-8-sig BOM，Excel 兼容），' +
  `建议单次 ≤ ${EXPORT_MAX_ROWS.toLocaleString()} 行，超出时先缩小时间范围。`

const exporting = ref(false)
/** 结果条数超过上限时的二次确认弹窗 */
const exportWarnOpen = ref(false)

/** 当前筛选对应的导出/查询参数（与 loadData 服务端参数一致） */
function exportParams(): Record<string, unknown> {
  const hasV = conclusion.value === '' ? undefined : conclusion.value === 'true'
  return {
    start: toIso(startTime.value),
    end: toIso(endTime.value),
    has_violation: hasV,
  }
}

/** 当前已知结果条数（服务端模式 = total；客户端过滤模式 = 内存累计） */
function currentFilteredCount(): number {
  return inClientMode.value ? clientRows.value.length : total.value
}

function askExport(): void {
  if (currentFilteredCount() > EXPORT_MAX_ROWS) {
    exportWarnOpen.value = true
  } else {
    void runExport()
  }
}

/** 下载 CSV 文本为本地文件（blob + 临时 <a>；CSV 已含 BOM，无需补） */
function downloadCsv(text: string, filename: string): void {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/** 直连 http 的导出错误文案（镜像 client.readableError 的 {detail}/{error} 双错误体） */
function exportErrorText(error: unknown): string {
  const data = (error as { response?: { data?: unknown } } | undefined)?.response?.data
  if (data && typeof data === 'object') {
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === 'string') return `导出失败：${detail}`
    const errMsg = (data as { error?: unknown }).error
    if (typeof errMsg === 'string') return `导出失败：${errMsg}`
  }
  return `导出失败：${error instanceof Error ? error.message : '请稍后重试'}`
}

async function runExport(): Promise<void> {
  exporting.value = true
  try {
    const res = await http.get<string>('/logs/export', {
      params: exportParams(),
      responseType: 'text',
      timeout: 60_000, // 大结果集导出放宽超时
    })
    const csv = res.data
    // 行数统计：去 BOM 后按行切分，第 1 行表头，其余为数据行
    // （detail_json 为 json.dumps 输出，字符串内换行已转义，按 \n 切分安全）
    const dataRows = csv.replace(/^\uFEFF/, '').split('\n').filter((line) => line.trim() !== '').length - 1
    const now = new Date()
    const stamp = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, '0'),
      String(now.getDate()).padStart(2, '0'),
    ].join('') + '_' + [String(now.getHours()).padStart(2, '0'), String(now.getMinutes()).padStart(2, '0'), String(now.getSeconds()).padStart(2, '0')].join('')
    downloadCsv(csv, `audit_logs_${stamp}.csv`)
    toast.success(
      dataRows > EXPORT_MAX_ROWS
        ? `已导出 ${dataRows} 条（超过 ${EXPORT_MAX_ROWS.toLocaleString()} 行建议，请缩小筛选范围分次导出）`
        : `已导出 ${dataRows} 条记录`,
    )
  } catch (error) {
    toast.error(exportErrorText(error))
    console.warn('[AuditView] 导出 CSV 失败：', error)
  } finally {
    exporting.value = false
  }
}

// ---------- 工具 ----------
/** datetime-local（本地时区）→ UTC ISO；空值返回 undefined（axios 会省略该参数） */
function toIso(value: string): string | undefined {
  const v = value.trim()
  if (!v) return undefined
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}

/** 数值字符串 → number；空/非法返回 undefined */
function toNum(value: string): number | undefined {
  const v = value.trim()
  if (!v) return undefined
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** ISO 时间 → 本地可读时间文本 */
function fmtTime(ts: unknown): string {
  const s = textOf(ts)
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

/** 置信度 0~1 → 百分数文本 */
function fmtConfidence(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

/** 文本哈希截断（title 显示全文） */
function shortHash(value: unknown, len = 12): string {
  const s = textOf(value)
  return s ? (s.length > len ? `${s.slice(0, len)}…` : s) : '—'
}

/** 降级标记：detail.degraded（orchestrator 写入 "semantic:<原因码>"），无则 '—' */
function degradedOf(row: LogRow): string {
  const detail = row.detail
  if (detail && typeof detail === 'object') {
    const degraded = (detail as Record<string, unknown>).degraded
    return textOf(degraded) || '—'
  }
  return '—'
}

function isViolation(row: LogRow): boolean {
  return row.has_violation === true || row.has_violation === 1
}

// ---------- 数据加载 ----------
/** 服务端分页模式 */
async function fetchServerPage(params: Record<string, unknown>): Promise<void> {
  const res = await apiGet<LogsPage>('/logs', { ...params, page: page.value, page_size: PAGE_SIZE })
  rows.value = res.items
  total.value = res.total
}

/** 客户端过滤模式：循环拉取窗口内全部记录（上限 40 页，超限截断近似） */
async function fetchAllWindow(params: Record<string, unknown>): Promise<LogRow[]> {
  const all: LogRow[] = []
  for (let p = 1; p <= ALL_MAX_PAGES; p++) {
    const res = await apiGet<LogsPage>('/logs', { ...params, page: p, page_size: ALL_PAGE_SIZE })
    all.push(...res.items)
    if (all.length >= res.total) break
    if (res.items.length === 0) break
  }
  return all
}

/** 客户端过滤模式的内存分页切片 */
function applyClientPagination(): void {
  const start = (page.value - 1) * PAGE_SIZE
  rows.value = clientRows.value.slice(start, start + PAGE_SIZE)
}

async function loadData(): Promise<void> {
  loading.value = true
  try {
    const hasV = conclusion.value === '' ? undefined : conclusion.value === 'true'
    const params: Record<string, unknown> = {
      start: toIso(startTime.value),
      end: toIso(endTime.value),
      has_violation: hasV,
    }
    const min = toNum(confMin.value)
    const max = toNum(confMax.value)
    const hash = textHash.value.trim()
    inClientMode.value = min !== undefined || max !== undefined || hash !== ''
    if (inClientMode.value) {
      // 后端无置信度/文本哈希过滤参数 → 拉全量后内存过滤（口径注释见文件头）
      let all = await fetchAllWindow(params)
      if (min !== undefined) {
        all = all.filter((r) => typeof r.confidence === 'number' && r.confidence >= min)
      }
      if (max !== undefined) {
        all = all.filter((r) => typeof r.confidence === 'number' && r.confidence <= max)
      }
      if (hash !== '') {
        all = all.filter((r) => textOf(r.text_hash).toLowerCase().includes(hash.toLowerCase()))
      }
      clientRows.value = all
      total.value = all.length
      applyClientPagination()
    } else {
      await fetchServerPage(params)
    }
  } catch (error) {
    // 错误提示已由 api 层统一 Toast（401 除外）；此处仅记录调试信息
    console.warn('[AuditView] 加载审核记录失败：', error)
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  page.value = 1
  void loadData()
}

function resetFilters(): void {
  startTime.value = ''
  endTime.value = ''
  conclusion.value = ''
  confMin.value = ''
  confMax.value = ''
  textHash.value = ''
  page.value = 1
  clientRows.value = []
  void loadData()
}

function onPageChange(nextPage: number): void {
  page.value = nextPage
  if (inClientMode.value) {
    applyClientPagination()
  } else {
    void loadData()
  }
}

// ---------- 明细弹窗 ----------
function openDetail(row: LogRow): void {
  detailRow.value = row
  detailOpen.value = true
}

function closeDetail(): void {
  detailOpen.value = false
  detailRow.value = null
}

onMounted(() => {
  void loadData()
  // 页面加载时若 localStorage 记忆为开启状态 → 直接启动定时器（watch 不会对初始值触发）
  if (autoRefresh.value) applyAutoRefresh(true)
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) {
    window.clearInterval(refreshTimer)
    refreshTimer = undefined
  }
})

// ---------- 列定义（DataTable 结构类型传入；degraded 为派生列） ----------
const columns = [
  { key: 'ts', label: '时间', width: 176 },
  { key: 'has_violation', label: '结论', width: 76 },
  { key: 'confidence', label: '置信度', width: 96 },
  { key: 'text_hash', label: '文本哈希', width: 150 },
  { key: 'key_tier', label: '渠道', width: 84 },
  { key: 'degraded', label: '降级', width: 170 },
]
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">🔍 审核记录</h2>
    <p class="page-hint">
      查看历史审核结论与命中证据链（点击任一单元格展开明细）；上方筛选栏可组合时间 / 结论 / 置信度 / 文本哈希查询，
      记录来自审核 API（POST /v1/audit）的每次调用。
    </p>

    <!-- 筛选栏 -->
    <div class="card filter-card">
      <div class="filter-row">
        <label class="filter-item">
          <span class="filter-label">开始时间</span>
          <input v-model="startTime" type="datetime-local" class="input" />
        </label>
        <label class="filter-item">
          <span class="filter-label">结束时间</span>
          <input v-model="endTime" type="datetime-local" class="input" />
        </label>
        <label class="filter-item">
          <span class="filter-label">结论</span>
          <select v-model="conclusion" class="input">
            <option value="">全部</option>
            <option value="true">违规</option>
            <option value="false">通过</option>
          </select>
        </label>
        <label class="filter-item">
          <span class="filter-label">置信度 ≥</span>
          <input v-model="confMin" type="number" min="0" max="1" step="0.01" class="input" placeholder="0~1" />
        </label>
        <label class="filter-item">
          <span class="filter-label">置信度 ≤</span>
          <input v-model="confMax" type="number" min="0" max="1" step="0.01" class="input" placeholder="0~1" />
        </label>
        <label class="filter-item">
          <span class="filter-label">文本哈希</span>
          <input v-model="textHash" type="text" class="input" placeholder="模糊匹配" />
        </label>
        <div class="filter-actions">
          <button type="button" class="btn btn-primary btn-sm" @click="applyFilters">🔍 查询</button>
          <button type="button" class="btn btn-ghost btn-sm" @click="resetFilters">重置</button>
        </div>
      </div>
      <p class="filter-note">
        注：置信度区间与文本哈希为客户端过滤（后端 /admin/logs 无对应参数，TODO），
        启用时最多拉取 2 万条窗口记录近似统计；「需人工」结论后端暂不存在（TODO）。
      </p>
    </div>

    <!-- 记录表格 -->
    <div class="card">
      <div class="card-title">
        <span>🕘 审核记录（共 {{ total }} 条）</span>
        <span class="title-right">
          <span v-if="inClientMode" class="mode-note">客户端过滤模式（内存分页）</span>
          <label class="auto-toggle" :title="'开启后每 10 秒自动重拉当前页（保留筛选与页码），状态记忆于本地'">
            <input v-model="autoRefresh" type="checkbox" />
            <span>⟳ 自动刷新（10s）</span>
          </label>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="exporting || loading"
            :title="exportHint"
            @click="askExport"
          >
            {{ exporting ? '导出中…' : '⬇️ 导出 CSV' }}
          </button>
        </span>
      </div>
      <DataTable :columns="columns" :rows="rows" :loading="loading">
        <template #empty>
          <EmptyState
            icon="📭"
            title="暂无审核记录"
            :hint="'审核记录来自审核 API 的调用，产生记录后会按筛选条件显示在这里。\n可以先到「试运行」一键示例文本、当场看到分层结果，或参考顶栏「指南」中的 API 对接示例发起第一条审核请求。'"
            action-text="去试运行看看"
            to="trial"
          />
        </template>
        <template #cell="{ row, column }">
          <!-- 点击任意单元格 = 查看该行明细（DataTable 无行点击插槽，以全单元格委托等效实现） -->
          <div class="cell-click" @click="openDetail(row)">
            <template v-if="column.key === 'ts'">{{ fmtTime(row.ts) }}</template>
            <template v-else-if="column.key === 'has_violation'">
              <span :class="isViolation(row) ? 'tag tag-danger' : 'tag tag-success'">
                {{ isViolation(row) ? '违规' : '通过' }}
              </span>
            </template>
            <template v-else-if="column.key === 'confidence'">{{ fmtConfidence(row.confidence) }}</template>
            <template v-else-if="column.key === 'text_hash'">
              <span class="hash-cell" :title="textOf(row.text_hash)">{{ shortHash(row.text_hash) }}</span>
            </template>
            <template v-else-if="column.key === 'key_tier'">
              <span :class="row.key_tier === 'full' ? 'tag tag-blue' : 'tag tag-gray'">
                {{ row.key_tier ? String(row.key_tier) : '—' }}
              </span>
            </template>
            <template v-else-if="column.key === 'degraded'">
              <span v-if="degradedOf(row) !== '—'" class="tag tag-danger" :title="'降级：' + degradedOf(row)">
                {{ degradedOf(row) }}
              </span>
              <span v-else>—</span>
            </template>
          </div>
        </template>
      </DataTable>
      <Pagination :page="page" :page-size="PAGE_SIZE" :total="total" @change="onPageChange" />
    </div>

    <!-- 明细弹窗：T41 由 EvidencePanel（T37 共享组件）分层渲染，原始 JSON 折叠由组件承载 -->
    <AppModal :show="detailOpen" title="审核明细（证据面板）" @close="closeDetail">
      <div v-if="detailRow" class="detail-meta">
        <div><span class="dl">请求 ID：</span>{{ textOf(detailRow.request_id) }}</div>
        <div><span class="dl">时间：</span>{{ fmtTime(detailRow.ts) }}</div>
        <div>
          <span class="dl">结论：</span>
          {{ isViolation(detailRow) ? '违规' : '通过' }}（来源 {{ textOf(detailRow.source) }}）
        </div>
        <div v-if="degradedOf(detailRow) !== '—'">
          <span class="dl">降级：</span><span class="tag tag-danger">{{ degradedOf(detailRow) }}</span>
        </div>
      </div>
      <p v-if="detailObj === null" class="detail-null">
        detail 为 null —— 请求使用 standard 渠道 Key 时后端不返回明细（仅 full 组返回）。
      </p>
      <!-- 面板传 AuditResult（行映射见 auditResult）；detail=null 时面板内部渲染
           「无分层证据」分支兜底，仍展示判定总览与原始 JSON -->
      <EvidencePanel :result="auditResult" />
      <template #actions>
        <button type="button" class="btn btn-primary" @click="closeDetail">关闭</button>
      </template>
    </AppModal>

    <!-- 导出超 1 万行的二次确认（上限语义说明） -->
    <ConfirmDialog
      :show="exportWarnOpen"
      title="⚠️ 导出结果较大"
      :message="`当前筛选结果约 ${currentFilteredCount().toLocaleString()} 行，超过建议上限 ${EXPORT_MAX_ROWS.toLocaleString()} 行（PRD G5）。导出端点按后端实现为全量流式，文件可能较大；建议先缩小时间范围/结论筛选。仍要继续导出吗？`"
      @confirm="exportWarnOpen = false; void runExport()"
      @cancel="exportWarnOpen = false"
    />
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

.filter-card {
  margin-bottom: 16px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 14px;
  align-items: flex-end;
}

.filter-item {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 150px;
  flex: 1 1 150px;
}

.filter-label {
  font-size: 0.72rem;
  color: var(--text-3);
  font-weight: 600;
}

.filter-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.filter-note {
  font-size: 0.72rem;
  color: var(--text-3);
  margin-top: 10px;
}

.mode-note {
  font-size: 0.72rem;
  color: var(--text-3);
  font-weight: 400;
}

/* T41：卡片标题右侧操作区（自动刷新开关 + 导出按钮） */
.title-right {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.auto-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.76rem;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.auto-toggle input {
  accent-color: var(--primary);
  cursor: pointer;
}

/* 整行可点击（等效「点击行 → 查看明细」） */
.cell-click {
  display: block;
  cursor: pointer;
  min-height: 18px;
}

.hash-cell {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.76rem;
}

/* 结论 / 渠道 / 降级标签 */
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

/* 明细弹窗 */
.detail-meta {
  font-size: 0.8rem;
  color: var(--text-2);
  padding: 8px 0 10px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 10px;
  line-height: 1.7;
}

.dl {
  color: var(--text-3);
  font-weight: 600;
}

.detail-null {
  font-size: 0.78rem;
  color: var(--text-3);
  line-height: 1.6;
}
</style>