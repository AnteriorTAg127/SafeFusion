<script setup lang="ts">
/**
 * 审核记录页（T24）：
 * - 筛选栏：时间范围（datetime-local）、结论（全部/违规/通过）、置信度区间
 *   （min/max）、文本哈希模糊；查询 / 重置
 * - DataTable + Pagination + 空态 + loading；点击行任意单元格呼出 AppModal 查看
 *   detail_json（JsonTree 递归渲染，Vue 插值自动转义，不用 innerHTML）
 *
 * 字段对齐（依据 src/safefusion/api/admin.py query_logs + _normalize_log、
 * storage/database.py audit_logs 表）：
 *   GET /admin/logs → { total, page, page_size, items:[{ request_id, ts,
 *   text_hash, has_violation(bool), confidence(number|null), category, source,
 *   detail(dict|null，由 detail_json 解析而来), key_tier }] }
 *   降级标记列取 detail.degraded（orchestrator.py 写入 "semantic:<原因码>"）。
 *
 * 已知后端缺口（写入报告 TODO，由主模型集成阶段处理，本页不做后端改动）：
 * - 置信度区间 / 文本哈希过滤：/admin/logs 无对应查询参数（仅 start/end/
 *   has_violation/source/category/key_tier/basic 分页）→ 本页在启用这两类筛选
 *   时进入「客户端过滤模式」：循环拉取筛选窗口内全部记录（最多 40 页 × 500 =
 *   2 万条，超限截断近似），内存过滤后前端分页；未启用时走服务端分页。
 * - 结论「需人工」：audit_logs 无该状态（仅 has_violation 0/1）→ 筛选栏仅
 *   提供 全部/违规/通过，第三态留 TODO。
 * - 错误响应体为 {error}（admin.py 全局异常处理器），而 api/client.ts 的
 *   readableError 只解析 {detail} → 错误 Toast 显示通用文案（对齐问题记录
 *   TODO，本页不修改 client.ts）。
 */
import { onMounted, ref } from 'vue'
import DataTable from '../components/DataTable.vue'
import Pagination from '../components/Pagination.vue'
import AppModal from '../components/AppModal.vue'
import JsonTree from './components/JsonTree.vue'
import { apiGet } from '../api/client'

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
        <span v-if="inClientMode" class="mode-note">客户端过滤模式（内存分页）</span>
      </div>
      <DataTable :columns="columns" :rows="rows" :loading="loading" empty-text="暂无审核记录">
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

    <!-- 明细弹窗：detail_json 树（JsonTree，textContent 安全渲染） -->
    <AppModal :show="detailOpen" title="审核明细（detail_json）" @close="closeDetail">
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
      <div class="detail-title">明细 JSON（只读，点击关闭）</div>
      <p v-if="detailRow && detailRow.detail === null" class="detail-null">
        detail 为 null —— 请求使用 standard 渠道 Key 时后端不返回明细（仅 full 组返回）。
      </p>
      <JsonTree v-else :value="detailRow ? detailRow.detail : null" />
      <template #actions>
        <button type="button" class="btn btn-primary" @click="closeDetail">关闭</button>
      </template>
    </AppModal>
  </section>
</template>

<style scoped>
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

.detail-title {
  font-size: 0.78rem;
  font-weight: 700;
  color: var(--text-2);
  margin: 6px 0 8px;
}

.detail-null {
  font-size: 0.78rem;
  color: var(--text-3);
  line-height: 1.6;
}
</style>