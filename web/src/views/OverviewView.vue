<script setup lang="ts">
/**
 * 概览页（T24 基础 + T35 主题适配 + T38 系统状态区/数据卡/自动刷新）：
 * - 顶部 4 张统计卡片：今日审核数 / 今日违规数 / 累计审核数 / 降级组件数
 * - 「🩺 系统状态」卡（v0.3.0 M3）：组件徽标行，逐个展示就绪/降级（绿/黄/红）
 *   + 中文原因（title 提示）；点击徽标跳转设置页对应分组（带 query，见 goSettings）
 * - 「📦 数据状态」卡（v0.3.0 M8）：词库/向量库黑白/白名单/规则计数（来自
 *   /admin/health 的 data）；数字点击跳转对应管理页；全空时 EmptyState 提示
 * - 「⟳ 自动刷新（10s）」开关（v0.3.0 G11）：默认关，localStorage
 *   `sf_overview_autorefresh` 记忆；开启后每 10s 重拉 stats 与 /admin/health
 * - 最近 7 天审核趋势：ECharts 柱状图（双序列：每日审核数 / 每日违规数）；
 *   图表初始化失败时降级为「图表暂不可用 + 数据表格」；T35 暗色主题适配保留
 *
 * 数据口径（字段对齐依据 src/safefusion/api/admin.py，逐条核对）：
 * - 后端**没有** GET /admin/stats 端点（admin.py 全文核对结论），按任务卡约定
 *   用 /admin/logs 聚合：
 *   「累计审核数」 = GET /admin/logs?page=1&page_size=1 响应的 total
 *   （服务端 COUNT，快且准确）；
 *   「今日审核数 / 今日违规数」 = 在 page_size=1 查询上追加
 *   start=<今日零点>（违规数再加 has_violation=true），total 即当日计数。
 *   audit_logs.ts 为 UTC ISO 字符串（storage/database.py _utc_now），前端把
 *   「本地时区今天零点」换算为 UTC ISO 交给 start 参数，语义 = 本地时区的一天。
 * - 最近 7 天趋势：循环拉 /admin/logs（start=6 天前本地零点，page_size=500，
 *   最多 40 页 ≈ 2 万条，超出部分截断为近似值——口径注释），前端按 ts 的
 *   本地日期分组统计每日总数与违规数。
 * - 降级组件数 / 系统状态 / 数据概况（v0.3.0 M3/M8）：GET /admin/health
 *   （admin.py admin_health，管理侧端口）→ components{ready,reason,...}、
 *   degraded 清单、data{keywords,vector_black,vector_white,whitelist_images,
 *   rules}、cache、uptime_s。reason 中文映射见下方 REASON_TEXT（组件内维护，
 *   未知原因码兜底显示原文）。
 *
 * 防 XSS：图表数据与全部动态内容均为数值/插值渲染（Vue 默认 textContent 转义）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { apiGet } from '../api/client'
import { useThemeStore } from '../stores/theme'

/** GET /admin/logs 响应结构（admin.py query_logs） */
interface LogPage {
  total: number
  page: number
  page_size: number
  items: Array<Record<string, unknown>>
}

// ============================= /admin/health 契约（admin.py admin_health） =============================

/** 徽标行组件键（对应 /admin/health components 字段） */
type CompKey =
  | 'light_model'
  | 'embedding'
  | 'semantic'
  | 'llm'
  | 'keyword_engine'
  | 'rules'
  | 'vector_black'
  | 'vector_white'

/** components[key] 形态（后端实际含 count/dim/status 等扩展字段，仅取所需） */
interface HealthComponent {
  ready?: boolean
  reason?: string | null
  count?: number
  [key: string]: unknown
}

/** GET /admin/health 响应结构（admin.py admin_health 逐字段核对） */
interface AdminHealth {
  status: string
  version?: string
  components: Partial<Record<CompKey, HealthComponent>>
  degraded: string[]
  data: {
    keywords: number
    vector_black: number
    vector_white: number
    whitelist_images: number
    rules: number
  }
  cache: unknown
  uptime_s: number
}

/** 降级原因码 → 中文（组件内维护；原因码清单核对 context.py REASON_* 与 admin.py 内联串） */
const REASON_TEXT: Record<string, string> = {
  lazy_pending: '语义层待首次装配（懒加载）——首次审核请求或「设置页模型卡 /admin/models/load」会触发装配',
  embedding_error: 'Embedding 引擎装配失败，详见服务日志',
  embedding_assets_missing: 'Embedding 模型文件缺失（本地权重路径不存在或未下载）',
  embedding_credential_error: 'Embedding 云端密钥缺失或无效（密钥仅环境变量注入）',
  embedding_config_error: 'Embedding 配置错误（参数组合不被后端接受）',
  embedding_unconfigured: '未配置 Embedding 后端',
  semantic_engine_error: '语义引擎初始化/装配失败',
  llm_unavailable: 'LLM 兜底不可用（未配置密钥或客户端初始化失败）',
  keyword_engine_unavailable: '关键词引擎装配失败',
  regex_rules_disabled: '正则消歧规则已关闭（设置页「关键词层」可开启）',
  light_model_disabled: '轻量文本风险模型未启用（路径未配置 / 模型文件缺失 / 缺 torch）',
  empty_pool: '向量池为空——由黑白语料经 scripts/build_vector_db.py 生成入库',
  unknown: '健康数据未提供原因码（后端未知状态）',
}

/** 标黄（可配置修复类，非硬错误）的原因码；其余非就绪原因标红 */
const WARN_REASONS = new Set<string>([
  'lazy_pending',
  'embedding_unconfigured',
  'light_model_disabled',
  'regex_rules_disabled',
  'empty_pool',
])

/** 徽标行元数据：key = /admin/health 组件键；group = 设置页对应分组（query 参数） */
const BADGES: Array<{ key: CompKey; label: string; icon: string; group: string }> = [
  { key: 'light_model', label: '轻量模型', icon: '⚡', group: 'light_model' },
  { key: 'embedding', label: 'Embedding', icon: '🧬', group: 'embedding' },
  { key: 'semantic', label: '语义层', icon: '🧠', group: 'semantic' },
  { key: 'llm', label: 'LLM 兜底', icon: '🤖', group: 'llm' },
  { key: 'keyword_engine', label: '关键词引擎', icon: '🔑', group: 'keyword' },
  { key: 'rules', label: '正则规则', icon: '📐', group: 'keyword' },
  { key: 'vector_black', label: '黑池向量库', icon: '⬛', group: 'embedding' },
  { key: 'vector_white', label: '白池向量库', icon: '⬜', group: 'embedding' },
]

type BadgeTone = 'ok' | 'warn' | 'error'

interface BadgeState {
  tone: BadgeTone
  text: string
  tip: string // title 提示（完整中文原因）
}

/**
 * 徽标状态计算：
 * - ready=true → 就绪（绿）；tip = 「组件 就绪」
 * - ready=false/missing → 降级：reason 取后端 reason，缺失时对空计数池合成
 *   empty_pool，再按 WARN_REASONS 分黄/红；tip = 完整中文原因
 */
function badgeState(badge: (typeof BADGES)[number]): BadgeState {
  const comp = health.value?.components?.[badge.key]
  if (!comp) {
    return { tone: 'error', text: '降级', tip: `${badge.label}：${REASON_TEXT.unknown}` }
  }
  if (comp.ready === true) {
    return { tone: 'ok', text: '就绪', tip: `${badge.label} 就绪` }
  }
  const reason =
    comp.reason ??
    (typeof comp.count === 'number' && comp.count <= 0 ? 'empty_pool' : 'unknown')
  const tip = `${badge.label}：${REASON_TEXT[reason] ?? REASON_TEXT.unknown}`
  return { tone: WARN_REASONS.has(reason) ? 'warn' : 'error', text: '降级', tip }
}

/** 数据状态卡单元：to 缺省 = 当前无对应管理页（灰置不可点，title 说明去向） */
interface DataCell {
  key: string
  label: string
  unit: string
  count: number
  to?: string
  note: string
}

const dataCells = computed<DataCell[]>(() => {
  const d = health.value?.data
  if (!d) return []
  return [
    {
      key: 'kw',
      label: '词库',
      unit: '条',
      count: d.keywords,
      to: '/keywords',
      note: '关键词词条；前往词库管理页导入 CSV/TXT',
    },
    {
      key: 'vb',
      label: '向量库·黑池',
      unit: '条',
      count: d.vector_black,
      note: '由黑白语料经 scripts/build_vector_db.py 生成；暂无独立管理页（详见顶栏「指南」数据准备清单）',
    },
    {
      key: 'vw',
      label: '向量库·白池',
      unit: '条',
      count: d.vector_white,
      note: '由黑白语料经 scripts/build_vector_db.py 生成；暂无独立管理页（详见顶栏「指南」数据准备清单）',
    },
    {
      key: 'wl',
      label: '白名单图片',
      unit: '张',
      count: d.whitelist_images,
      to: '/whitelist',
      note: '图片白名单；前往白名单页管理',
    },
    {
      key: 'rules',
      label: '正则规则',
      unit: '条',
      count: d.rules,
      to: '/rules',
      note: '正则消歧规则；前往规则页管理',
    },
  ]
})

/** 数据概况合计（全空判定用） */
const dataTotal = computed(() => dataCells.value.reduce((sum, c) => sum + c.count, 0))

// ---------- 系统状态 / 数据概况（GET /admin/health） ----------
const router = useRouter()
const health = ref<AdminHealth | null>(null)
const healthLoading = ref(false)

async function loadHealth(): Promise<void> {
  healthLoading.value = true
  try {
    health.value = await apiGet<AdminHealth>('/health')
  } catch (error) {
    // 失败由 api 层统一 Toast（401 除外）；保留上次健康数据（首次失败徽标呈「未知」态）
    console.warn('[OverviewView] 健康状态加载失败：', error)
  } finally {
    healthLoading.value = false
  }
}

/** 降级组件数卡：读取 /admin/health degraded 清单（v0.3.0 M3；0 显示 0 绿色） */
const degradedDisplay = computed(() => (health.value ? String(health.value.degraded.length) : '…'))
const degradedTone = computed<'blue' | 'green' | 'orange' | 'purple'>(() => {
  if (!health.value) return 'purple'
  return health.value.degraded.length === 0 ? 'green' : 'orange'
})

/** 徽标点击 → 设置页对应分组（带 query 定位；见 goSettings 注释） */
function goSettings(badge: (typeof BADGES)[number]): void {
  void router.push({ name: 'settings', query: { group: badge.group } })
}

/** 数据卡数字点击 → 对应管理页（无 to 的灰置） */
function goCell(cell: DataCell): void {
  if (cell.to) void router.push(cell.to)
}

// ---------- 自动刷新开关（v0.3.0 G11；localStorage sf_overview_autorefresh） ----------
const AUTO_REFRESH_KEY = 'sf_overview_autorefresh'
const AUTO_REFRESH_MS = 10_000
const autoRefresh = ref(localStorage.getItem(AUTO_REFRESH_KEY) === '1')
let refreshTimer: number | null = null

/** 开启后每 10s 重拉 stats（计数卡）与 /admin/health（状态/数据卡）；
 *  趋势图表数据量大（最多 2 万条，见 loadTrend）且 10s 周期收益低，不纳入轮询 */
function startAutoRefresh(): void {
  if (refreshTimer !== null) return // 防重复起定时器
  refreshTimer = window.setInterval(() => {
    void loadStats()
    void loadHealth()
  }, AUTO_REFRESH_MS)
}

function stopAutoRefresh(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
}

// 初始值在 setup 阶段从 localStorage 恢复（不触发 watch 写回），切换时才持久化
watch(autoRefresh, (on) => {
  localStorage.setItem(AUTO_REFRESH_KEY, on ? '1' : '0')
  if (on) startAutoRefresh()
  else stopAutoRefresh()
})

// ---------- 统计状态 ----------
const todayCount = ref(0) // 今日审核数
const todayViolation = ref(0) // 今日违规数
const cumulative = ref(0) // 累计审核数
const loading = ref(false)
const trendRows = ref<Array<{ day: string; count: number; violation: number }>>([])

// ---------- 工具函数 ----------
/** ISO 时间 → 本地日期键（YYYY-MM-DD），用于按日分组 */
function localDateKey(ts: unknown): string {
  const d = new Date(String(ts ?? ''))
  if (Number.isNaN(d.getTime())) return 'unknown'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** 「本地时区今天零点」的 UTC ISO 字符串（与后端 UTC ts 比较的基准） */
function todayStartIso(): string {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString()
}

/** 最近 n 天（含今天）的日期键 + 展示标签 */
function lastNDays(n: number): Array<{ key: string; label: string }> {
  const today = new Date()
  const out: Array<{ key: string; label: string }> = []
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i)
    out.push({
      key: localDateKey(d.toISOString()),
      label: `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`,
    })
  }
  return out
}

// ---------- 数据加载（聚合口径见文件头注释） ----------
/** 拉取一页审核日志 */
async function fetchLogs(params: Record<string, unknown>): Promise<LogPage> {
  return apiGet<LogPage>('/logs', params)
}

/** 拉取 start 之后窗口内的全部日志（分页循环，上限截断近似） */
async function fetchTrendWindow(startIso: string): Promise<Array<Record<string, unknown>>> {
  const TREND_PAGE_SIZE = 500
  const TREND_MAX_PAGES = 40 // 上限 2 万条，超出截断为近似值（口径注释）
  const rows: Array<Record<string, unknown>> = []
  for (let page = 1; page <= TREND_MAX_PAGES; page++) {
    const res = await fetchLogs({ start: startIso, page, page_size: TREND_PAGE_SIZE })
    rows.push(...res.items)
    if (rows.length >= res.total) break
    if (res.items.length === 0) break // 防御：空页即止
  }
  return rows
}

/** 统计卡片：累计 / 今日 / 今日违规（三次 page_size=1 的 COUNT 查询） */
async function loadStats(): Promise<void> {
  loading.value = true
  try {
    const start = todayStartIso()
    const [cum, today, todayViol] = await Promise.all([
      fetchLogs({ page: 1, page_size: 1 }),
      fetchLogs({ start, page: 1, page_size: 1 }),
      fetchLogs({ start, has_violation: true, page: 1, page_size: 1 }),
    ])
    cumulative.value = cum.total
    todayCount.value = today.total
    todayViolation.value = todayViol.total
  } catch (error) {
    // 失败由 api 层统一 Toast（401 除外）；数值保持 0，趋势可能随后失败
    console.warn('[OverviewView] 统计加载失败：', error)
  } finally {
    loading.value = false
  }
}

/** 最近 7 天趋势：拉窗口内日志后按本地日期分组 */
async function loadTrend(): Promise<void> {
  const days = lastNDays(7)
  // start = 6 天前本地零点（本地时区换算 UTC ISO）
  const trendStart = new Date(
    new Date().getFullYear(),
    new Date().getMonth(),
    new Date().getDate() - 6,
  ).toISOString()
  try {
    const all = await fetchTrendWindow(trendStart)
    const map = new Map<string, { count: number; violation: number }>()
    for (const row of all) {
      const key = localDateKey(row.ts)
      let item = map.get(key)
      if (!item) {
        item = { count: 0, violation: 0 }
        map.set(key, item)
      }
      item.count += 1
      if (row.has_violation === true || row.has_violation === 1) item.violation += 1
    }
    trendRows.value = days.map((d) => ({
      day: d.label,
      count: map.get(d.key)?.count ?? 0,
      violation: map.get(d.key)?.violation ?? 0,
    }))
  } catch (error) {
    console.warn('[OverviewView] 趋势加载失败：', error)
    trendRows.value = days.map((d) => ({ day: d.label, count: 0, violation: 0 }))
  }
}

// ---------- ECharts 图表 ----------
// T35 暗色适配：图表文字/网格/提示框颜色动态读取 CSS 变量（随 data-theme 变化），
// 主题切换时由下方 watch(themeStore.resolved) 触发 setOption 重绘（notMerge 全量替换）。
const themeStore = useThemeStore()

const chartRef = ref<HTMLDivElement | null>(null)
const chartOk = ref(false)
let chart: ReturnType<typeof echarts.init> | null = null

/** 读取 CSS 变量（浅色 fallback 与 style.css 设计值一致） */
function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function buildOption(): EChartsOption {
  const primary = cssVar('--primary', '#165dff')
  const danger = cssVar('--danger', '#f53f3f')
  const text = cssVar('--text', '#1d2129')
  const text2 = cssVar('--text-2', '#4e5969')
  const border = cssVar('--border', '#e5e6eb')
  const surface = cssVar('--surface', '#ffffff')
  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: surface,
      borderColor: border,
      textStyle: { color: text },
    },
    legend: { data: ['每日审核数', '每日违规数'], textStyle: { color: text2 } },
    grid: { left: 44, right: 16, top: 40, bottom: 28 },
    xAxis: {
      type: 'category',
      data: trendRows.value.map((r) => r.day),
      axisLabel: { color: text2 },
      axisLine: { lineStyle: { color: border } },
      axisTick: { lineStyle: { color: border } },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: text2 },
      splitLine: { lineStyle: { color: border } },
    },
    series: [
      {
        name: '每日审核数',
        type: 'bar',
        data: trendRows.value.map((r) => r.count),
        itemStyle: { color: primary },
      },
      {
        name: '每日违规数',
        type: 'bar',
        data: trendRows.value.map((r) => r.violation),
        itemStyle: { color: danger },
      },
    ],
  }
}

function handleResize(): void {
  chart?.resize()
}

async function initChart(): Promise<void> {
  // 先置 chartOk=true 让 v-if 容器渲染进 DOM（否则 chartRef 为 null 无法初始化），
  // nextTick 等待容器就绪后再 echarts.init（此时容器已有实际尺寸，避免 0 尺寸告警）
  chartOk.value = true
  await nextTick()
  const el = chartRef.value
  if (!el) return
  try {
    chart = echarts.init(el)
    chart.setOption(buildOption())
    chart.resize() // 布局稳定后校准一次尺寸
    window.addEventListener('resize', handleResize)
  } catch (error) {
    // 初始化失败降级：文本「图表暂不可用」+ 数据表格（模板 v-else 分支）
    chartOk.value = false
    console.warn('[OverviewView] ECharts 初始化失败，降级为数据表格：', error)
  }
}

onMounted(() => {
  // 自动刷新开关状态已在 setup 从 localStorage 恢复；开启则启动定时器
  if (autoRefresh.value) startAutoRefresh()
  void loadStats()
  void loadHealth()
  // 趋势数据就绪后再初始化图表（柱状图数据非空）
  void loadTrend().then(() => initChart())
})

// T35 暗色适配：实际主题变化 → 全量重绘图表（颜色重新读取 CSS 变量）
watch(
  () => themeStore.resolved,
  () => {
    if (chart && chartOk.value) {
      chart.setOption(buildOption(), true)
    }
  },
)

onBeforeUnmount(() => {
  stopAutoRefresh()
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <section class="page-view">
    <div class="page-head">
      <h2 class="page-title">📊 概览</h2>
      <!-- 自动刷新开关（G11）：localStorage sf_overview_autorefresh 记忆 -->
      <button
        type="button"
        class="refresh-toggle"
        :class="{ 'refresh-on': autoRefresh }"
        role="switch"
        :aria-checked="autoRefresh"
        :title="autoRefresh ? '已开启：每 10 秒自动刷新统计与系统状态' : '已关闭：点击开启每 10 秒自动刷新'"
        @click="autoRefresh = !autoRefresh"
      >
        <span class="refresh-icon" aria-hidden="true">⟳</span>
        <span>自动刷新</span>
        <span class="refresh-freq">（10s）</span>
      </button>
    </div>
    <p class="page-hint">
      系统运行概况与最近 7 天审核趋势；数据来自审核日志聚合（记录越多越有参考价值）。
      无审核数据时下方会给出上手路径。系统状态与数据概况来自 <code>/admin/health</code>。
    </p>
    <div class="stats-grid">
      <StatCard icon="📨" :value="todayCount" label="今日审核数" tone="blue" />
      <StatCard icon="🛡️" :value="todayViolation" label="今日违规数" tone="orange" />
      <StatCard icon="📚" :value="cumulative" label="累计审核数" tone="green" />
      <!-- 降级组件数：读 /admin/health degraded 清单；0 显示 0 绿色（v0.3.0 M3） -->
      <StatCard icon="⚠️" :value="degradedDisplay" label="降级组件数" :tone="degradedTone" />
    </div>

    <!-- 🩺 系统状态卡（PRD M3）：组件徽标 + 降级原因（title 中文提示） -->
    <div class="card status-card">
      <div class="card-title">
        <span>🩺 系统状态</span>
        <span v-if="healthLoading" class="trend-note">检测中...</span>
      </div>
      <p class="status-desc">组件级就绪状态（GET /admin/health）；点击徽标前往设置页对应分组。</p>
      <div class="badge-row">
        <button
          v-for="badge in BADGES"
          :key="badge.key"
          type="button"
          class="comp-badge"
          :title="badgeState(badge).tip"
          @click="goSettings(badge)"
        >
          <span class="badge-icon" aria-hidden="true">{{ badge.icon }}</span>
          <span class="badge-name">{{ badge.label }}</span>
          <span class="badge-dot" :class="`dot-${badgeState(badge).tone}`" aria-hidden="true"></span>
          <span class="badge-state" :class="`state-${badgeState(badge).tone}`">{{ badgeState(badge).text }}</span>
        </button>
      </div>
    </div>

    <!-- 📦 数据状态卡（PRD M8）：词库/向量库/白名单/规则计数 + 跳转；全空给 EmptyState -->
    <div class="card data-card">
      <div class="card-title">
        <span>📦 数据状态</span>
        <span v-if="healthLoading" class="trend-note">检测中...</span>
      </div>

      <!-- 全空：EmptyState 式提示（为什么空 + 下一步 + 跳转词库页） -->
      <EmptyState
        v-if="health && dataTotal === 0"
        icon="📦"
        title="数据尚未就绪"
        :hint="'词库 / 向量库 / 白名单 / 规则目前均为空（数据概况来自 /admin/health）。\n参考顶栏「❓ 指南」中的数据准备清单，按五类数据逐项准备；最快上手：先导入词库。'"
        action-text="前往词库页"
        to="/keywords"
      />

      <!-- 数字格：点击跳转对应管理页（向量库暂无管理页 → 灰置+title 说明去向） -->
      <div v-else-if="dataCells.length > 0" class="data-grid">
        <button
          v-for="cell in dataCells"
          :key="cell.key"
          type="button"
          class="data-cell"
          :class="{ 'cell-disabled': !cell.to }"
          :aria-disabled="!cell.to"
          :title="cell.note"
          @click="goCell(cell)"
        >
          <span class="data-num">{{ cell.count }}</span>
          <span class="data-label">{{ cell.label }}（{{ cell.unit }}）</span>
        </button>
      </div>
      <p v-else class="data-note">健康数据未返回，暂无法展示数据概况。</p>
    </div>

    <!-- 无数据统计空态（PRD §M1：为什么空 + 下一步） -->
    <div v-if="cumulative === 0 && !loading" class="card empty-banner">
      <EmptyState
        icon="📊"
        title="还没有审核数据"
        :hint="'统计与趋势全部来自审核日志：调用审核 API（POST /v1/audit）产生第一条记录后，这里会显示今日 / 累计数字与 7 天趋势。\n若尚未接入业务流，可参考顶栏「指南」中的 API 对接示例与「数据准备」清单。\n（TODO：v0.3.0 试运行页（T37）上线后，可在此直接一键发文本验证全链路。）'"
        action-text="查看审核记录"
        to="/audit"
      />
    </div>

    <div class="card">
      <div class="card-title">
        <span>📈 最近 7 天审核趋势</span>
        <span v-if="loading" class="trend-note">统计中...</span>
      </div>

      <!-- 图表正常：ECharts 柱状图容器 -->
      <div v-if="chartOk" ref="chartRef" class="trend-chart" aria-label="最近 7 天审核趋势柱状图"></div>

      <!-- 图表失败降级：文本 + 数据表格（对齐 PRD 图表失败降级表格的可用性原则） -->
      <template v-else>
        <p class="chart-fallback">⚠️ 图表暂不可用（ECharts 初始化失败），以下为最近 7 天数据表格：</p>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>日期</th>
                <th>审核数</th>
                <th>违规数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in trendRows" :key="row.day">
                <td>{{ row.day }}</td>
                <td>{{ row.count }}</td>
                <td>{{ row.violation }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <p class="trend-note">
        口径：后端无 /admin/stats 端点（TODO），累计/今日由 /admin/logs 服务端 COUNT
        （page_size=1 的 total）聚合；趋势按 ts（UTC）的本地时区日期分组，窗口内超过
        2 万条（40 页 × 500）截断近似；降级组件数 / 系统状态 / 数据概况来自
        GET /admin/health（v0.3.0 M3/M8 管理侧端点）。
      </p>
    </div>
  </section>
</template>

<style scoped>
/* 页面标题 + 自动刷新开关行 */
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.page-title {
  margin-bottom: 0;
}

/* 自动刷新开关（pill 按钮；开启 = 绿色 + 图标旋转） */
.refresh-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  font-size: 0.76rem;
  cursor: pointer;
  transition: var(--transition);
  flex-shrink: 0;
}

.refresh-toggle:hover {
  box-shadow: var(--shadow);
}

.refresh-on {
  background: var(--success-light);
  border-color: transparent;
  color: var(--success);
  font-weight: 600;
}

.refresh-on .refresh-icon {
  animation: spin 2.4s linear infinite;
}

.refresh-freq {
  opacity: 0.8;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* 标题下操作提示行（PRD v0.3.0 §M1：每页用途 + 主操作） */
.page-hint {
  font-size: 0.76rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: 0 0 14px;
}

.page-hint code {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.72rem;
}

/* 无数据统计空态（复用 EmptyState；卡片容器包裹） */
.empty-banner {
  padding: 8px 20px;
  margin-bottom: 16px;
}

/* ---------- 🩺 系统状态卡 ---------- */
.status-card {
  margin-bottom: 16px;
}

.status-desc {
  font-size: 0.74rem;
  color: var(--text-3);
  margin: -6px 0 10px;
  line-height: 1.6;
}

.badge-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.comp-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface-hover);
  color: var(--text-2);
  font-size: 0.78rem;
  cursor: pointer;
  transition: var(--transition);
}

.comp-badge:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow);
}

.badge-icon {
  font-size: 0.9rem;
}

.badge-name {
  font-weight: 600;
}

.badge-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-ok {
  background: var(--success);
}

.dot-warn {
  background: var(--orange);
}

.dot-error {
  background: var(--danger);
}

.badge-state {
  font-weight: 700;
}

.state-ok {
  color: var(--success);
}

.state-warn {
  color: var(--orange);
}

.state-error {
  color: var(--danger);
}

/* ---------- 📦 数据状态卡 ---------- */
.data-card {
  margin-bottom: 16px;
}

.data-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.data-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 14px 10px;
  border-radius: var(--radius-sm);
  border: 1px dashed var(--border);
  background: var(--surface-hover);
  font-size: 0.74rem;
  color: var(--text-2);
  transition: var(--transition);
}

/* 有跳转目标 = 可点（实线 + hover 高亮）；无 = 灰置（title 提示去向） */
.data-cell:not(.cell-disabled) {
  cursor: pointer;
  border-style: solid;
}

.data-cell:not(.cell-disabled):hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow);
  border-color: var(--primary);
}

.cell-disabled {
  cursor: not-allowed;
  opacity: 0.62;
}

.data-num {
  font-size: 1.25rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--primary);
}

.cell-disabled .data-num {
  color: var(--text-3);
}

.data-label {
  color: var(--text-3);
  line-height: 1.4;
}

.data-note {
  font-size: 0.76rem;
  color: var(--text-3);
  margin-top: 10px;
}

.trend-chart {
  width: 100%;
  height: 320px;
}

.chart-fallback {
  font-size: 0.82rem;
  color: var(--text-3);
  margin-bottom: 10px;
}

.trend-note {
  font-size: 0.74rem;
  color: var(--text-3);
  margin-top: 12px;
  display: block;
}
</style>