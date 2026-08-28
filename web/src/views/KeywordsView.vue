<script setup lang="ts">
/**
 * 词库管理页（T24）：
 * - 顶部统计条：词条总数 / 分类数（黑白池维度后端暂不存在，标注 —）
 * - 筛选：分类（下拉，取值自列表 distinct）、关键词模糊（客户端过滤）
 * - DataTable：词 / 池 / 分类 / 来源 / 操作（删除 ← ConfirmDialog 确认）
 * - 添加：词 + 分类 + 池（禁用，后端无池维度）→ 走导入端点（单条 TXT）
 * - 导入：CSV（类别,词 两列）/ TXT（每行一词 + 分类）文件上传
 * - 分页（客户端内存分页）+ 空态 + loading
 * - T41「🧹 一键去重」：列表卡顶部按钮 → ConfirmDialog 说明副作用（自动备份 zip
 *   到 data/backups/、去重后引擎重载、不可撤销）→ POST /admin/keywords/dedup →
 *   成功 Toast + AppModal 结果摘要（前后条数 / 无法去重数 / 备份文件名 / 引擎重载）。
 *   ⚠️ 后端尚无 dedup 端点（admin.py 全文核对：仅 import/list/delete 三端点，
 *   PRD §M9 G10 由主模型集成阶段补后端）→ 前端按如下契约先写好调用：
 *   POST /admin/keywords/dedup → { status, before, after, removed, failed,
 *   backup_file, reload }；未就绪时该请求会 404/405 并由 api 层 Toast 报错，
 *   不阻塞其它功能（见报告 TODO）。
 *
 * 字段对齐（依据 src/safefusion/api/admin.py、storage/database.py keywords 表）：
 *   GET /admin/keywords?category&page&page_size → { total, page, page_size,
 *   items:[{ id, category, word, source }] }（无黑白池字段！）
 *   POST /admin/keywords/import（multipart file + category 查询参数，TXT 必填）→
 *   { inserted, skipped, total }；词条无单条 POST 端点，单条添加以「TXT 一行
 *   一词 + category 参数」复用导入接口（POST /admin/keywords/import）。
 *   DELETE /admin/keywords/{keyword_id} → { deleted }；404 时响 4xx。
 *
 * 已知后端缺口（写入报告 TODO）：
 * - keywords 表无 pool 字段：任务卡「池（黑/白）筛选/黑白词库数量」无法满足 →
 *   池下拉禁用并标注，统计条仅展示总数与分类数；黑白池概念当前只在向量库
 *   （vector_store black/white 池）与永久黑白名单（内容哈希）层面，非词库表。
 * - GET /admin/keywords 不支持词条模糊搜索 → 关键词模糊为客户端过滤
 *   （先拉全量，最多 100 页 × 500 = 5 万条，超限截断近似）。
 */
import { onMounted, ref } from 'vue'
import StatCard from '../components/StatCard.vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import AppModal from '../components/AppModal.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiDelete } from '../api/client'
import { useToastStore } from '../stores/toast'

/** GET /admin/keywords 响应结构 */
interface KeywordsPage {
  total: number
  page: number
  page_size: number
  items: Array<Record<string, unknown>>
}

/** POST /admin/keywords/import 响应结构 */
interface ImportResult {
  inserted: number
  skipped: number
  total: number
}

type KeywordRow = Record<string, unknown>

const ALL_PAGE_SIZE = 500 // 分批拉取页大小
const ALL_MAX_PAGES = 100 // 上限 5 万条，超限截断近似（口径注释）

const toast = useToastStore()

// ---------- 列表状态 ----------
const rows = ref<KeywordRow[]>([]) // 当前页数据（客户端分页）
const allRows = ref<KeywordRow[]>([]) // 全量数据（内存过滤/分页基础）
const total = ref(0)
const page = ref(1)
const PAGE_SIZE = 20
const loading = ref(false)
const deleting = ref<KeywordRow | null>(null)
const submitting = ref(false)

// ---------- T41：一键去重 ----------
/**
 * POST /admin/keywords/dedup 期望响应契约（后端由主模型集成阶段补，见文件头注释）：
 * { status: "ok", before, after, removed, failed, backup_file, reload }
 * 字段缺失时前端以「—」展示，绝不臆造数值。
 */
interface DedupResult {
  status?: string
  before?: number
  after?: number
  removed?: number
  failed?: number
  backup_file?: string | null
  reload?: string
  [key: string]: unknown
}

/** 去重前的词条总数（恢复用；请求失败不更新） */
const dedupBeforeCount = ref(0)
const dedupAskOpen = ref(false) // 副作用确认弹窗
const dedupRunning = ref(false) // 请求进行中
const dedupResult = ref<DedupResult | null>(null) // 结果摘要弹窗数据

/** 「🧹 一键去重」：先确认副作用，再提交 */
function askDedup(): void {
  dedupBeforeCount.value = totalCount.value
  dedupAskOpen.value = true
}

async function confirmDedup(): Promise<void> {
  dedupAskOpen.value = false
  dedupRunning.value = true
  try {
    const res = await apiPost<DedupResult>('/keywords/dedup')
    dedupResult.value = res
    toast.success(res.status === 'ok' ? '去重完成（词库已更新）' : '去重返回（请查看结果摘要）')
    await loadData() // 去重后刷新列表/统计
  } catch (error) {
    // 后端未就绪时 404/405 已由 api 层 Toast（TODO：主模型补 POST /admin/keywords/dedup）
    console.warn('[KeywordsView] 词库去重失败：', error)
  } finally {
    dedupRunning.value = false
  }
}

/** 引擎重载状态码 → 中文（未知值回显原文） */
function reloadText(value: unknown): string {
  if (value === 'ok') return '✅ 已重载'
  if (value === 'skipped') return '⏭ 跳过（未注入重载钩子）'
  if (value === 'failed') return '❌ 重载失败'
  return value === null || value === undefined ? '—' : String(value)
}

/** 数值结果展示（未知字段回退「—」） */
function dedupNum(value: unknown): string {
  return typeof value === 'number' ? value.toLocaleString() : '—'
}

// ---------- 统计 ----------
const totalCount = ref(0)
const categoryCount = ref(0)

// ---------- 筛选 ----------
const categoryFilter = ref('') // '' = 全部
const wordQuery = ref('') // 词条模糊
// 池筛选：后端词库无 pool 字段（TODO），下拉仅「全部」且禁用
const poolFilter = ref('')
const categories = ref<string[]>([])

// ---------- 添加 / 导入表单 ----------
const addWord = ref('')
const addCategory = ref('')
/** 添加/导入面板引用：空态「去添加词条」滚动到此处（PRD §M1 空态即下一步） */
const addPanelRef = ref<HTMLElement | null>(null)

function scrollToAddPanel(): void {
  addPanelRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

// ---------- 数据加载 ----------
/** 循环拉取全部词条（统计 + 内存过滤基础） */
async function fetchAll(): Promise<void> {
  const all: KeywordRow[] = []
  for (let p = 1; p <= ALL_MAX_PAGES; p++) {
    const res = await apiGet<KeywordsPage>('/keywords', { page: p, page_size: ALL_PAGE_SIZE })
    all.push(...res.items)
    if (all.length >= res.total) break
    if (res.items.length === 0) break
  }
  allRows.value = all
  // 统计：词条总数 / 分类数（distinct，去空）
  totalCount.value = all.length
  const seen = new Set<string>()
  for (const r of all) {
    const c = textOf(r.category).trim()
    if (c) seen.add(c)
  }
  categoryCount.value = seen.size
  categories.value = Array.from(seen).sort()
}

/** 应用筛选（分类 = 客户端筛选，词条模糊 = 客户端筛选）并分页 */
function applyFiltersAndPagination(): void {
  let list = allRows.value
  if (categoryFilter.value) {
    list = list.filter((r) => textOf(r.category) === categoryFilter.value)
  }
  const q = wordQuery.value.trim().toLowerCase()
  if (q) {
    list = list.filter((r) => textOf(r.word).toLowerCase().includes(q))
  }
  total.value = list.length
  const start = (page.value - 1) * PAGE_SIZE
  rows.value = list.slice(start, start + PAGE_SIZE)
}

async function loadData(): Promise<void> {
  loading.value = true
  try {
    await fetchAll()
    applyFiltersAndPagination()
  } catch (error) {
    console.warn('[KeywordsView] 加载词库失败：', error)
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  page.value = 1
  applyFiltersAndPagination()
}

function resetFilters(): void {
  categoryFilter.value = ''
  wordQuery.value = ''
  page.value = 1
  applyFiltersAndPagination()
}

function onPageChange(nextPage: number): void {
  page.value = nextPage
  applyFiltersAndPagination()
}

// ---------- 添加（单条，复用导入端点 TXT 形态） ----------
async function addKeyword(): Promise<void> {
  const word = addWord.value.trim()
  const category = addCategory.value.trim()
  if (!word || !category) {
    toast.error('请输入词条与分类')
    return
  }
  submitting.value = true
  try {
    const fd = new FormData()
    // 单条词条编码为「TXT 一行一词」，category 走查询参数（对齐 POST /admin/keywords/import）
    const blob = new File([`${word}\n`], `kw-${Date.now()}.txt`, { type: 'text/plain' })
    fd.append('file', blob)
    const res = await apiPost<ImportResult>('/keywords/import', fd, { category })
    if (res.inserted > 0) toast.success(`已添加词条「${word}」（${category}）`)
    if (res.skipped > 0) toast.info(`跳过 ${res.skipped} 条重复词条（category+word 唯一）`)
    addWord.value = ''
    await loadData()
  } catch (error) {
    // 错误提示已由 api 层统一 Toast
    console.warn('[KeywordsView] 添加词条失败：', error)
  } finally {
    submitting.value = false
  }
}

// ---------- 导入（CSV / TXT） ----------
const importFile = ref<HTMLInputElement | null>(null)

async function importFromFile(): Promise<void> {
  const file = importFile.value?.files?.[0]
  if (!file) {
    toast.error('请先选择 CSV/TXT 文件')
    return
  }
  const ext = (file.name.split('.').pop() ?? '').toLowerCase()
  if (ext === 'txt' && !addCategory.value.trim()) {
    toast.error('TXT 导入必须填写类别（每行一词挂到该类别下）')
    return
  }
  submitting.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    // TXT（按行解析）需要 category 查询参数；CSV（类别,词两列）不需要
    const res = await apiPost<ImportResult>('/keywords/import', fd, {
      category: ext === 'txt' ? addCategory.value.trim() : undefined,
    })
    toast.success(`导入完成：新增 ${res.inserted} 条${res.skipped ? `，跳过重复 ${res.skipped} 条` : ''}`)
    if (importFile.value) importFile.value.value = ''
    await loadData()
  } catch (error) {
    console.warn('[KeywordsView] 导入词条失败：', error)
  } finally {
    submitting.value = false
  }
}

// ---------- 删除（二次确认） ----------
function askDelete(row: KeywordRow): void {
  deleting.value = row
}

async function confirmDelete(): Promise<void> {
  const row = deleting.value
  if (!row) return
  try {
    await apiDelete(`/keywords/${String(row.id)}`)
    toast.success('词条已删除')
    await loadData()
  } catch (error) {
    console.warn('[KeywordsView] 删除词条失败：', error)
  } finally {
    deleting.value = null
  }
}

onMounted(() => {
  void loadData()
})

// ---------- 列定义 ----------
const columns = [
  { key: 'word', label: '词', width: 200 },
  { key: 'pool', label: '池', width: 80 },
  { key: 'category', label: '分类', width: 120 },
  { key: 'source', label: '来源', width: 140 },
  { key: 'actions', label: '操作', width: 90 },
]
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">📚 词库管理</h2>
    <p class="page-hint">
      维护关键词词条：导入 CSV/TXT、单条添加、删除；词条进库后关键词层（Aho-Corasick + 拼音变体）
      实时生效。主操作 = 下方「➕ 添加 / 导入词条」面板。
    </p>

    <!-- 顶部统计条 -->
    <div class="stats-grid">
      <StatCard icon="📖" :value="totalCount" label="词条总数" tone="blue" />
      <StatCard icon="🗂️" :value="categoryCount" label="分类数" tone="green" />
      <StatCard icon="⚖️" value="—" label="黑白池维度（后端暂无）" tone="purple" />
    </div>

    <!-- 筛选栏 -->
    <div class="card filter-card">
      <div class="filter-row">
        <label class="filter-item">
          <span class="filter-label">池</span>
          <select v-model="poolFilter" class="input" disabled title="后端 keywords 表无黑白池字段（TODO）">
            <option value="">全部（池维度不可用）</option>
            <option value="black">黑</option>
            <option value="white">白</option>
          </select>
        </label>
        <label class="filter-item">
          <span class="filter-label">分类</span>
          <select v-model="categoryFilter" class="input" @change="applyFilters">
            <option value="">全部分类</option>
            <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span class="filter-label">关键词模糊</span>
          <input v-model="wordQuery" type="text" class="input" placeholder="输入词条片段，回车查询" @keyup.enter="applyFilters" />
        </label>
        <div class="filter-actions">
          <button type="button" class="btn btn-primary btn-sm" @click="applyFilters">🔍 查询</button>
          <button type="button" class="btn btn-ghost btn-sm" @click="resetFilters">重置</button>
        </div>
      </div>
    </div>

    <!-- 添加 / 导入 -->
    <div ref="addPanelRef" class="card">
      <div class="card-title"><span>➕ 添加 / 导入词条</span></div>
      <div class="add-row">
        <input v-model="addWord" type="text" class="input add-word" placeholder="词（单条添加）" />
        <input v-model="addCategory" type="text" class="input add-cat" placeholder="分类（单条 / TXT 导入必填）" />
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="submitting"
          @click="addKeyword"
        >
          ➕ 添加
        </button>
        <input ref="importFile" type="file" class="input add-file" accept=".csv,.txt" />
        <button type="button" class="btn btn-ghost btn-sm" :disabled="submitting" @click="importFromFile">
          📥 导入 CSV/TXT
        </button>
      </div>

      <!-- 支持格式示例块（PRD v0.3.0 §M1；文案依据 admin.py _parse_keywords_csv/_parse_keywords_txt 实际解析能力） -->
      <div class="fmt-block">
        <div class="fmt-title">📋 支持格式（示例）</div>
        <ul class="fmt-list">
          <li>
            <b>CSV</b>：固定两列「类别,词」（第 1 列类别、第 2 列词，列序不可调换）；首行恰为
            <code>类别,词</code> 时作为表头自动跳过。
            <pre class="fmt-code">类别,词
政治,敏感词
色情,another keyword</pre>
          </li>
          <li>
            <b>TXT</b>：每行一个词（自动跳过 <code>#</code> 注释行与空行），整批归入上方「分类」
            输入框填写的类别（TXT 导入前分类必填）。
            <pre class="fmt-code"># 敏感词库
赌博
刷单</pre>
          </li>
          <li>
            <b>编码</b>：仅支持 <b>UTF-8</b>（可带 BOM）；GBK 等其它编码会提示「文件编码不支持」，
            请先转存为 UTF-8 再导入。
          </li>
          <li>重复词条（分类 + 词唯一）自动跳过并计数，不覆盖已有数据；单条「添加」= 以 TXT 形态提交一个词。</li>
        </ul>
      </div>

      <p class="filter-note">
        说明：添加为单条 TXT 编码走 /admin/keywords/import（后端无单条 POST 端点，TODO）；
        CSV 表头仅识别「类别,词」（英文表头不会跳过，会被当词条导入）；重复自动跳过。
      </p>
    </div>

    <!-- 词条表格 -->
    <div class="card">
      <div class="card-title">
        <span>🗃️ 词条列表（共 {{ total }} 条）</span>
        <button
          type="button"
          class="btn btn-danger btn-sm"
          :disabled="dedupRunning || loading"
          :title="'去重前自动备份 zip 到 data/backups/；去重后词库引擎自动重载（后端端点由主模型集成阶段补，未就绪时会报错）'"
          @click="askDedup"
        >
          {{ dedupRunning ? '去重中…' : '🧹 一键去重' }}
        </button>
      </div>
      <DataTable :columns="columns" :rows="rows" :loading="loading">
        <template #empty>
          <EmptyState
            icon="📚"
            title="词库空空如也"
            :hint="'关键词层目前没有任何词可匹配，审核请求将完全不经过关键词通道。\n在上方「添加 / 导入词条」面板导入 CSV/TXT 或逐条添加即可开始生效；分类的黑白池维度后端暂未提供（TODO）。'"
            action-text="去添加词条"
            @action="scrollToAddPanel"
          />
        </template>
        <template #cell="{ row, column }">
          <template v-if="column.key === 'word'">{{ textOf(row.word) }}</template>
          <template v-else-if="column.key === 'pool'">
            <!-- 后端 keywords 表无 pool 字段，恒为占位（TODO） -->
            <span class="pool-placeholder">—</span>
          </template>
          <template v-else-if="column.key === 'category'">
            <span class="tag tag-blue">{{ textOf(row.category) || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'source'">{{ textOf(row.source) || '—' }}</template>
          <template v-else-if="column.key === 'actions'">
            <button type="button" class="btn btn-danger btn-sm" @click.stop="askDelete(row)">删除</button>
          </template>
        </template>
      </DataTable>
      <Pagination :page="page" :page-size="PAGE_SIZE" :total="total" @change="onPageChange" />
    </div>

    <!-- 删除二次确认 -->
    <ConfirmDialog
      :show="deleting !== null"
      title="⚠️ 删除词条"
      :message="deleting ? `确定删除词条「${textOf(deleting.word)}」（分类：${textOf(deleting.category) || '—'}）吗？此操作不可恢复。` : ''"
      danger
      @confirm="confirmDelete"
      @cancel="deleting = null"
    />

    <!-- T41：一键去重副作用确认（PRD §M9 G10 + D3 借鉴：危险操作前置说明） -->
    <ConfirmDialog
      :show="dedupAskOpen"
      title="🧹 一键去重词库"
      :message="`去重将执行以下操作（不可撤销）：① 自动备份当前词库（${dedupBeforeCount.toLocaleString()} 条）为 zip 存至 data/backups/；② 剥离符号/前缀等变体后按「类别+词」去重；③ 去重完成后词库引擎自动重载（无需重启服务）。继续吗？`"
      danger
      @confirm="confirmDedup"
      @cancel="dedupAskOpen = false"
    />

    <!-- T41：去重结果摘要（字段以后端实际响应为准，缺失显示 —） -->
    <AppModal :show="dedupResult !== null" title="🧹 去重结果" @close="dedupResult = null">
      <table class="result-table">
        <tbody>
          <tr><td>去重前条数</td><td>{{ dedupNum(dedupResult?.before) }}</td></tr>
          <tr><td>去重后条数</td><td>{{ dedupNum(dedupResult?.after) }}</td></tr>
          <tr><td>移除条数</td><td>{{ dedupNum(dedupResult?.removed) }}</td></tr>
          <tr><td>无法去重数</td><td>{{ dedupNum(dedupResult?.failed) }}</td></tr>
          <tr>
            <td>备份文件</td>
            <td class="mono-cell">{{ typeof dedupResult?.backup_file === 'string' && dedupResult.backup_file ? dedupResult.backup_file : '—' }}</td>
          </tr>
          <tr><td>引擎重载</td><td>{{ reloadText(dedupResult?.reload) }}</td></tr>
        </tbody>
      </table>
      <p class="result-note">
        备份位于服务端 data/backups/ 目录（与旧版 node 前端「自动备份 zip + 结果摘要」行为对齐，
        G10）；若需恢复请从备份文件重新导入。
      </p>
      <template #actions>
        <button type="button" class="btn btn-primary" @click="dedupResult = null">关闭</button>
      </template>
    </AppModal>
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

/* 导入示例块（PRD §M1：每行一条 + 示例块 + 列名说明 + 编码提示） */
.fmt-block {
  margin-top: 14px;
  padding: 12px 14px;
  background: var(--surface-hover);
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm);
}

.fmt-title {
  font-size: 0.76rem;
  font-weight: 700;
  color: var(--text-2);
  margin-bottom: 6px;
}

.fmt-list {
  margin: 0;
  padding-left: 16px;
  font-size: 0.74rem;
  color: var(--text-2);
  line-height: 1.8;
}

.fmt-list code {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.7rem;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}

.fmt-code {
  margin: 6px 0 8px;
  padding: 8px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.72rem;
  line-height: 1.6;
  overflow-x: auto;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
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

.add-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.add-word {
  flex: 2 1 180px;
}

.add-cat {
  flex: 1 1 160px;
}

.add-file {
  flex: 2 1 220px;
  padding: 6px 10px;
}

.pool-placeholder {
  color: var(--text-3);
}

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

/* T41：去重结果摘要表 */
.result-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}

.result-table td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.result-table td:first-child {
  color: var(--text-3);
  font-weight: 600;
  width: 110px;
  white-space: nowrap;
}

.mono-cell {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.76rem;
  word-break: break-all;
}

.result-note {
  margin-top: 10px;
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.6;
}
</style>