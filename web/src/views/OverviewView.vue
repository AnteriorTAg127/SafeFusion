<script setup lang="ts">
/**
 * 概览页（T24）：
 * - 顶部 4 张统计卡片：今日审核数 / 今日违规数 / 累计审核数 / 降级组件数
 * - 最近 7 天审核趋势：ECharts 柱状图（双序列：每日审核数 / 每日违规数）；
 *   图表初始化失败时降级为「图表暂不可用 + 数据表格」
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
 * - 降级组件数：需审核 API GET /health（:8000，api/app.py 231 行）；Vite dev
 *   proxy 仅代理 /admin（vite.config.ts），管理 API 无等价健康/降级端点 →
 *   本卡标注「—」并注释说明，缺管理侧健康/降级统计端点列入报告 TODO
 *   （由主模型集成阶段处理，前端不做跨端口直连）。
 *
 * 防 XSS：图表数据与全部动态内容均为数值/插值渲染（Vue 默认 textContent 转义）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import StatCard from '../components/StatCard.vue'
import { apiGet } from '../api/client'

/** GET /admin/logs 响应结构（admin.py query_logs） */
interface LogPage {
  total: number
  page: number
  page_size: number
  items: Array<Record<string, unknown>>
}

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
const chartRef = ref<HTMLDivElement | null>(null)
const chartOk = ref(false)
let chart: ReturnType<typeof echarts.init> | null = null

function buildOption(): EChartsOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['每日审核数', '每日违规数'] },
    grid: { left: 44, right: 16, top: 40, bottom: 28 },
    xAxis: { type: 'category', data: trendRows.value.map((r) => r.day) },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      {
        name: '每日审核数',
        type: 'bar',
        data: trendRows.value.map((r) => r.count),
        itemStyle: { color: '#165dff' },
      },
      {
        name: '每日违规数',
        type: 'bar',
        data: trendRows.value.map((r) => r.violation),
        itemStyle: { color: '#f53f3f' },
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
  void loadStats()
  // 趋势数据就绪后再初始化图表（柱状图数据非空）
  void loadTrend().then(() => initChart())
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">📊 概览</h2>
    <div class="stats-grid">
      <StatCard icon="📨" :value="todayCount" label="今日审核数" tone="blue" />
      <StatCard icon="🛡️" :value="todayViolation" label="今日违规数" tone="orange" />
      <StatCard icon="📚" :value="cumulative" label="累计审核数" tone="green" />
      <StatCard icon="⚠️" value="—" label="降级组件数" tone="purple" />
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
        2 万条（40 页 × 500）截断近似；降级组件数需审核 API /health（:8000 跨端口，
        管理侧无等价端点，暂标注 —）。
      </p>
    </div>
  </section>
</template>

<style scoped>
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