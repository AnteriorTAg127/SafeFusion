<script setup lang="ts">
/**
 * 规则管理页（T24，正则消歧规则）：
 * - 筛选：category（分类，取值自全量列表 distinct）、启用状态（全部/启用/停用）
 * - DataTable：pattern（等宽字体）、类别、action（exempt 绿 / violate 红）、
 *   启用开关（switch → PATCH active）、备注、创建时间、操作（删除）
 * - 新增规则表单（AppModal）：pattern（textarea）、category、action（select）、
 *   note；提交 → POST /admin/rules（JSON 数组单条）→ 刷新
 * - 导入：CSV（category,pattern,action 三列）/ JSON（规则对象数组）文件上传
 * - 分页（客户端分页，后端 /admin/rules 无分页参数）+ 空态 + loading
 *
 * 字段对齐（依据 src/safefusion/api/admin.py、storage/database.py rules 表）：
 *   GET /admin/rules?category&active_only → { total, items:[{ id, category,
 *   pattern, action, note, is_active(bool), created_at }] }（无分页参数）
 *   POST /admin/rules → JSON 数组 [{category,pattern,action,note}] 或 multipart
 *   file（CSV）→ { inserted, skipped, total, reload }
 *   PATCH /admin/rules/{rule_id}/active  body {active} → { id, active, reload }
 *   DELETE /admin/rules/{rule_id} → { deleted, reload }
 *
 * 已知后端缺口（写入报告 TODO）：
 * - 后端无「仅停用」过滤（active_only 只有 true/false 两个语义）→ 「停用」态
 *   以 active_only=false 拉全量后客户端过滤 is_active=false。
 */
import { onMounted, ref } from 'vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import AppModal from '../components/AppModal.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiPatch, apiDelete } from '../api/client'
import { useToastStore } from '../stores/toast'

interface RulesResponse {
  total: number
  items: Array<Record<string, unknown>>
}

interface RulesWriteResult {
  inserted: number
  skipped: number
  total: number
  reload?: string
}

type RuleRow = Record<string, unknown>

const PAGE_SIZE = 15

const toast = useToastStore()

// ---------- 列表状态 ----------
const rows = ref<RuleRow[]>([])
const allRows = ref<RuleRow[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const submitting = ref(false)
const deleting = ref<RuleRow | null>(null)

// ---------- 筛选 ----------
const categoryFilter = ref('') // '' = 全部
// select v-model 绑定值类型为 string（避免严格模式下联合类型赋值告警）
const statusFilter = ref<string>('all')
const categories = ref<string[]>([])

// ---------- 新增表单 ----------
const showForm = ref(false)
const formPattern = ref('')
const formCategory = ref('')
// action 取值 exempt / violate；提交时做运行时校验
const formAction = ref<string>('exempt')
const formNote = ref('')

// ---------- 导入 ----------
const importFile = ref<HTMLInputElement | null>(null)

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function fmtTime(ts: unknown): string {
  const s = textOf(ts)
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

function isActive(row: RuleRow): boolean {
  return row.is_active === true || row.is_active === 1
}

// ---------- 数据加载 ----------
/** 拉取规则并应用筛选（服务端只支持 category + active_only；null/停用态客户端补） */
async function loadData(): Promise<void> {
  loading.value = true
  try {
    const params: Record<string, unknown> = { active_only: statusFilter.value === 'active' }
    if (categoryFilter.value) params.category = categoryFilter.value
    const res = await apiGet<RulesResponse>('/rules', params)
    let list = res.items
    // 「停用」态：后端无该过滤语义，拉全量后客户端过滤
    if (statusFilter.value === 'inactive') {
      list = list.filter((r) => !isActive(r))
    }
    allRows.value = list
    total.value = list.length
    applyPagination()
    // 分类下拉：独立拉一次全量（不随筛选收窄，保证选项完整）
    try {
      const catRes = await apiGet<RulesResponse>('/rules', { active_only: false })
      const seen = new Set<string>()
      for (const r of catRes.items) {
        const c = textOf(r.category).trim()
        if (c) seen.add(c)
      }
      categories.value = Array.from(seen).sort()
    } catch {
      // 分类下拉加载失败不影响主列表（api 层已 Toast）
    }
  } catch (error) {
    console.warn('[RulesView] 加载规则失败：', error)
  } finally {
    loading.value = false
  }
}

function applyPagination(): void {
  const start = (page.value - 1) * PAGE_SIZE
  rows.value = allRows.value.slice(start, start + PAGE_SIZE)
}

function applyFilters(): void {
  page.value = 1
  void loadData()
}

function resetFilters(): void {
  categoryFilter.value = ''
  statusFilter.value = 'all'
  page.value = 1
  void loadData()
}

function onPageChange(nextPage: number): void {
  page.value = nextPage
  applyPagination()
}

// ---------- 启用开关（PATCH active） ----------
async function toggleActive(row: RuleRow): Promise<void> {
  const target = !isActive(row)
  try {
    const res = await apiPatch<{ id: number; active: boolean }>(`/rules/${String(row.id)}/active`, {
      active: target,
    })
    row['is_active'] = res.active
    toast.success(res.active ? '规则已启用' : '规则已停用')
  } catch (error) {
    console.warn('[RulesView] 切换规则状态失败：', error)
  }
}

// ---------- 新增规则（AppModal 表单 → POST JSON 数组单条） ----------
function openForm(): void {
  formPattern.value = ''
  formCategory.value = ''
  formAction.value = 'exempt'
  formNote.value = ''
  showForm.value = true
}

async function submitForm(): Promise<void> {
  const pattern = formPattern.value.trim()
  if (!pattern) {
    toast.error('规则 pattern 不能为空')
    return
  }
  // action 运行时校验（select 值仅 exempt/violate）
  const action = formAction.value
  if (action !== 'exempt' && action !== 'violate') {
    toast.error('action 必须为 exempt 或 violate')
    return
  }
  submitting.value = true
  try {
    const res = await apiPost<RulesWriteResult>('/rules', [
      {
        category: formCategory.value.trim(),
        pattern,
        action,
        note: formNote.value.trim() || null,
      },
    ])
    toast.success(`已新增 ${res.inserted} 条规则${res.skipped ? `，跳过重复 ${res.skipped} 条` : ''}`)
    showForm.value = false
    page.value = 1
    await loadData()
  } catch (error) {
    console.warn('[RulesView] 新增规则失败：', error)
  } finally {
    submitting.value = false
  }
}

// ---------- 导入（CSV / JSON） ----------
async function importRules(): Promise<void> {
  const file = importFile.value?.files?.[0]
  if (!file) {
    toast.error('请先选择 CSV/JSON 文件')
    return
  }
  const ext = (file.name.split('.').pop() ?? '').toLowerCase()
  submitting.value = true
  try {
    let res: RulesWriteResult
    if (ext === 'json') {
      // JSON：解析为规则对象数组后以 JSON body 提交（对齐 POST /admin/rules）
      const text = await file.text()
      let payload: unknown
      try {
        payload = JSON.parse(text)
      } catch {
        toast.error('JSON 解析失败，请检查文件格式')
        return
      }
      if (!Array.isArray(payload)) {
        toast.error('JSON 必须是规则对象数组 [{category, pattern, action, note}]')
        return
      }
      res = await apiPost<RulesWriteResult>('/rules', payload)
    } else {
      // CSV/TXT：multipart file 字段（后端按 CSV category,pattern,action 解析）
      const fd = new FormData()
      fd.append('file', file)
      res = await apiPost<RulesWriteResult>('/rules', fd)
    }
    toast.success(`导入完成：新增 ${res.inserted} 条${res.skipped ? `，跳过重复 ${res.skipped} 条` : ''}`)
    if (importFile.value) importFile.value.value = ''
    page.value = 1
    await loadData()
  } catch (error) {
    console.warn('[RulesView] 导入规则失败：', error)
  } finally {
    submitting.value = false
  }
}

// ---------- 删除（二次确认） ----------
function askDelete(row: RuleRow): void {
  deleting.value = row
}

async function confirmDelete(): Promise<void> {
  const row = deleting.value
  if (!row) return
  try {
    await apiDelete(`/rules/${String(row.id)}`)
    toast.success('规则已删除')
    await loadData()
  } catch (error) {
    console.warn('[RulesView] 删除规则失败：', error)
  } finally {
    deleting.value = null
  }
}

onMounted(() => {
  void loadData()
})

// ---------- 列定义 ----------
const columns = [
  { key: 'pattern', label: '规则 pattern' },
  { key: 'category', label: '类别', width: 110 },
  { key: 'action', label: 'action', width: 90 },
  { key: 'is_active', label: '启用', width: 70 },
  { key: 'note', label: '备注', width: 150 },
  { key: 'created_at', label: '创建时间', width: 176 },
  { key: 'actions', label: '操作', width: 90 },
]
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">📏 规则管理</h2>
    <p class="page-hint">
      用正则消歧关键词命中：exempt 豁免命中（放行）、violate 追加强命中（违规）。右上角「➕ 新增规则」
      手写一条，或「📥 导入 CSV/JSON」批量建立；规则变更后热重载即时生效。
    </p>

    <!-- 筛选栏 + 操作入口 -->
    <div class="card filter-card">
      <div class="filter-row">
        <label class="filter-item">
          <span class="filter-label">类别</span>
          <select v-model="categoryFilter" class="input" @change="applyFilters">
            <option value="">全部类别</option>
            <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span class="filter-label">启用状态</span>
          <select v-model="statusFilter" class="input" @change="applyFilters">
            <option value="all">全部</option>
            <option value="active">启用</option>
            <option value="inactive">停用</option>
          </select>
        </label>
        <div class="filter-actions">
          <button type="button" class="btn btn-ghost btn-sm" @click="resetFilters">↺ 重置</button>
          <button type="button" class="btn btn-primary btn-sm" @click="openForm">➕ 新增规则</button>
          <input ref="importFile" type="file" class="input add-file" accept=".csv,.json,.txt" />
          <button type="button" class="btn btn-ghost btn-sm" :disabled="submitting" @click="importRules">
            📥 导入 CSV/JSON
          </button>
        </div>
      </div>
      <p class="filter-note">
        「停用」状态为客户端过滤（后端 active_only 仅 true/false，TODO）；新增规则
        经 POST /admin/rules（JSON 数组）；导入支持 CSV（category,pattern,action
        三列）或 JSON 数组文件；action 缺省为 exempt。
      </p>

      <!-- 支持格式示例块（PRD v0.3.0 §M1；文案依据 admin.py _parse_rules_csv 与实际端点行为） -->
      <div class="fmt-block">
        <div class="fmt-title">📋 支持格式（示例）</div>
        <ul class="fmt-list">
          <li>
            <b>CSV</b>：固定三列「category,pattern,action」（列序固定；action 可省略、缺省为
            <code>exempt</code>；类别留空 = 不限类别）。表头行（<code>category,pattern</code> /
            <code>类别,规则</code> / <code>类别,pattern</code>）自动跳过。
            <pre class="fmt-code">category,pattern,action
色情,测试\d{6},violate
,官方声明,exempt</pre>
          </li>
          <li>
            <b>JSON</b>：规则对象数组 <code>[{category, pattern, action, note}]</code>。
            <pre class="fmt-code">[{"category":"赌博","pattern":"博彩\d+","action":"violate","note":"示例"}]</pre>
          </li>
          <li>
            <b>编码</b>：仅支持 <b>UTF-8</b>（可带 BOM）；GBK 等其它编码会提示「文件编码不支持」，
            请先转存为 UTF-8 再导入。
          </li>
          <li>校验：pattern 必须是有效正则、action 仅 <code>exempt</code>/<code>violate</code>，否则整批 400 拒绝；
            重复规则（类别 + 正则 + 动作）自动跳过并计数；导入成功后规则层热重载即时生效。</li>
        </ul>
      </div>
    </div>

    <!-- 规则表格 -->
    <div class="card">
      <div class="card-title"><span>🗃️ 正则规则（共 {{ total }} 条）</span></div>
      <DataTable :columns="columns" :rows="rows" :loading="loading">
        <template #empty>
          <EmptyState
            icon="📏"
            title="暂无正则规则"
            :hint="'正则消歧规则用于修正关键词命中：exempt 豁免命中、violate 追加强命中。\n点击右上角「➕ 新增规则」手写一条（pattern + 类别 + action），或「📥 导入 CSV/JSON」批量建立；规则为空时规则层直接跳过，不影响关键词与语义判定。'"
            action-text="新增规则"
            @action="openForm"
          />
        </template>
        <template #cell="{ row, column }">
          <template v-if="column.key === 'pattern'">
            <span class="pattern-cell" :title="textOf(row.pattern)">{{ textOf(row.pattern) }}</span>
          </template>
          <template v-else-if="column.key === 'category'">
            {{ textOf(row.category) || '不限' }}
          </template>
          <template v-else-if="column.key === 'action'">
            <span :class="row.action === 'violate' ? 'tag tag-danger' : 'tag tag-success'">
              {{ textOf(row.action) || '—' }}
            </span>
          </template>
          <template v-else-if="column.key === 'is_active'">
            <button
              type="button"
              class="switch"
              :class="{ 'switch-on': isActive(row) }"
              role="switch"
              :aria-checked="isActive(row)"
              :title="isActive(row) ? '点击停用' : '点击启用'"
              @click.stop="toggleActive(row)"
            >
              <span class="switch-thumb"></span>
            </button>
          </template>
          <template v-else-if="column.key === 'note'">{{ textOf(row.note) || '—' }}</template>
          <template v-else-if="column.key === 'created_at'">{{ fmtTime(row.created_at) }}</template>
          <template v-else-if="column.key === 'actions'">
            <button type="button" class="btn btn-danger btn-sm" @click.stop="askDelete(row)">删除</button>
          </template>
        </template>
      </DataTable>
      <Pagination :page="page" :page-size="PAGE_SIZE" :total="total" @change="onPageChange" />
    </div>

    <!-- 新增规则弹窗 -->
    <AppModal :show="showForm" title="➕ 新增正则规则" @close="showForm = false">
      <div class="form-grid">
        <label class="form-item">
          <span class="form-label">规则 pattern（正则表达式，必填）</span>
          <textarea v-model="formPattern" class="input form-textarea" rows="4" placeholder="如 测试\d{6}"></textarea>
        </label>
        <label class="form-item">
          <span class="form-label">类别（可空 = 不限定类别，作用于全部命中）</span>
          <input v-model="formCategory" type="text" class="input" placeholder="如 色情 / 赌博" />
        </label>
        <label class="form-item">
          <span class="form-label">action</span>
          <select v-model="formAction" class="input">
            <option value="exempt">exempt（豁免命中）</option>
            <option value="violate">violate（追加强命中）</option>
          </select>
        </label>
        <label class="form-item">
          <span class="form-label">备注（可选）</span>
          <input v-model="formNote" type="text" class="input" placeholder="规则用途说明" />
        </label>
      </div>
      <template #actions>
        <button type="button" class="btn btn-ghost" :disabled="submitting" @click="showForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="submitting" @click="submitForm">提交</button>
      </template>
    </AppModal>

    <!-- 删除二次确认 -->
    <ConfirmDialog
      :show="deleting !== null"
      title="⚠️ 删除规则"
      :message="deleting ? `确定删除该规则吗？\npattern：${textOf(deleting.pattern)}\n类别：${textOf(deleting.category) || '不限'}，action：${textOf(deleting.action)}` : ''"
      danger
      @confirm="confirmDelete"
      @cancel="deleting = null"
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

/* 导入示例块（PRD §M1：每行一条 + 示例块 + 列名说明 + 编码提示） */
.fmt-block {
  margin-top: 12px;
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
  align-items: center;
  flex-shrink: 0;
  flex-wrap: wrap;
}

.filter-note {
  font-size: 0.72rem;
  color: var(--text-3);
  margin-top: 10px;
}

.add-file {
  width: 180px;
  padding: 6px 10px;
}

.pattern-cell {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.78rem;
  word-break: break-all;
}

/* action 标签 */
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

/* 启用开关（switch） */
.switch {
  position: relative;
  width: 40px;
  height: 22px;
  border: none;
  border-radius: 12px;
  background: var(--border);
  cursor: pointer;
  transition: background var(--transition);
  padding: 0;
}

.switch-on {
  background: var(--success);
}

.switch-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
  transition: transform var(--transition);
}

.switch-on .switch-thumb {
  transform: translateX(18px);
}

/* 新增表单 */
.form-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.form-label {
  font-size: 0.72rem;
  color: var(--text-3);
  font-weight: 600;
}

.form-textarea {
  resize: vertical;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}
</style>