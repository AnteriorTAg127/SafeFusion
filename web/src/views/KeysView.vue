<script setup lang="ts">
/**
 * 密钥管理页（v0.3.0 §M7 / 任务卡 T40）：
 * - 创建：分组 standard（默认）/ full + 备注 → POST /admin/keys →
 *   成功后完整 Key 仅此一次显示（高亮卡片 + 「复制」按钮 + 警示「关闭后不再显示，
 *   请立即保存」）；列表其余时刻只显示缩略前缀（后端 GET /admin/keys 已脱敏：
 *   前 8 位 + "…"）。
 * - 列表：DataTable 列 = 分组 / 前缀 / 备注 / 最近使用 / 限流 / 创建时间 / 状态 +
 *   操作（停用/启用切换、删除）；客户端分页（GET /admin/keys 无分页参数，全量返回）。
 * - 删除 / 停用：ConfirmDialog 二次确认（提示该 Key 的 effect：相关调用方将失效）。
 * - 空态走 EmptyState（「暂无 API Key」→「创建第一个 Key」打开创建面板）。
 * - 顶部操作提示行（M1 规范：用途一句话 + 主操作）+「对接提示」卡（X-Api-Key 头 +
 *   curl/fetch 一句话示例，静态文案，风格对齐 GuideMenu）。
 *
 * 字段对齐（依据 src/safefusion/api/admin.py keys CRUD 与
 * src/safefusion/storage/database.py api_keys 表，v0.3.0 三波现状源码）：
 *   POST   /admin/keys body { tier: "standard"|"full", note?: string|null } → 201
 *          { key: <完整 Key，仅此一次>, tier, enabled, note }
 *   GET    /admin/keys → [{ key: <mask: 前 8 位 + "…">, tier, enabled, note,
 *          created_at }]（无分页参数；无 last_used/rate_limit 字段）
 *   PATCH  /admin/keys/{key} body { enabled?|note? } → 更新后行（路径需完整 Key 明文）
 *   DELETE /admin/keys/{key} → { deleted }（路径需完整 Key 明文）
 *
 * 已知后端缺口（写入报告 TODO，前端不做扩表 / 不做后端改动）：
 * 1. api_keys 表无 last_used / rate_limit 列 → 列表「最近使用」恒为「—」；按 Key
 *    独立限流不可配，当前为全局固定 60 次 / 60 秒（app.py KeyRateLimiter，环境变量
 *    SAFEFUSION_RATE_LIMIT 整体调整，非按 Key）→「限流」列亦为「—」（title 提示真实策略）。
 * 2. PATCH/DELETE 路径参数需要完整 Key 明文，而 GET 列表只返回脱敏前缀 → 前端对
 *    「非本会话新建」的 Key 无法定位。本页对「本会话新建的 Key」用内存暂存的完整明文
 *    （不落盘、不持久化、不打印、不进日志，红线遵守）完成停用/删除；其余 Key 待后端
 *    补强（建议：PATCH/DELETE 支持脱敏前缀唯一匹配，或 GET 额外返回稳定非敏感标识）
 *    后自动全量生效——统一收敛在 keyRefFor()，见文件头 TODO 注释。
 */
import { computed, onMounted, ref } from 'vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import AppModal from '../components/AppModal.vue'
import { apiGet, apiPost, apiPatch, apiDelete } from '../api/client'
import { useToastStore } from '../stores/toast'

/** POST /admin/keys 创建成功响应（完整 Key 明文仅此一次返回） */
interface CreateKeyResult {
  key: string
  tier: 'standard' | 'full'
  enabled: boolean
  note: string | null
}

/** 列表行（后端返回：key 已脱敏；last_used/rate_limit 后端暂缺） */
type KeysRow = Record<string, unknown>

const toast = useToastStore()

// ---------- 列表状态 ----------
const rows = ref<KeysRow[]>([])
const loading = ref(false)
const page = ref(1)
const PAGE_SIZE = 10
const total = ref(0)

// ---------- 创建状态 ----------
const showCreate = ref(false)
const creating = ref(false)
const createTier = ref<'standard' | 'full'>('standard')
const createNote = ref('')
/**
 * 本会话新建 Key 的完整明文（仅内存暂存：不落盘 / 不持久化 / 不打印 / 不进日志）。
 * 两个用途：① 创建成功后弹窗内一次性展示 + 复制；② 供该新行的停用/删除定位
 * （后端 PATCH/DELETE 需要完整 Key 而列表只给脱敏前缀，见文件头 TODO）。
 */
const createdKey = ref<{ full: string } | null>(null)

/** 待停用/待删除行（ConfirmDialog 确认目标） */
const disabling = ref<KeysRow | null>(null)
const deleting = ref<KeysRow | null>(null)

// ---------- 取值辅助（DataTable 单元格值为 unknown，统一收敛） ----------
function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function boolOf(value: unknown): boolean {
  return value === true || value === 1 || textOf(value).toLowerCase() === 'true'
}

/** 创建时间（ISO）→ 本地化显示 */
function fmtTime(value: unknown): string {
  const s = textOf(value)
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

/** 脱敏规则与后端 admin.py _mask_key 一致（key[:8] + "…"）：用于把本会话新建
 *  Key 的明文与列表行（脱敏前缀）匹配。 */
function maskOf(fullKey: string): string {
  return `${fullKey.slice(0, 8)}…`
}

/**
 * 解析某行可用的管理标识（PATCH/DELETE 路径参数）。
 * 后端 GET 列表仅返回脱敏前缀而 PATCH/DELETE 需要完整 Key 明文 → 本会话新建的
 * Key 走内存暂存的明文；其余行暂以脱敏前缀请求（当前后端将 404，见文件头 TODO #2，
 * 后端补强为前缀唯一匹配 / 稳定标识后此处自动全量生效，无需改动调用点）。
 */
function keyRefFor(row: KeysRow): string {
  const prefix = textOf(row.key)
  if (createdKey.value && prefix === maskOf(createdKey.value.full)) {
    return createdKey.value.full
  }
  return prefix
}

// ---------- 数据加载（GET /admin/keys 无分页参数 → 全量后客户端分页） ----------
async function loadData(): Promise<void> {
  loading.value = true
  try {
    const list = await apiGet<KeysRow[]>('/keys')
    rows.value = list
    total.value = list.length
  } catch (error) {
    console.warn('[KeysView] 加载 API Key 列表失败：', error)
  } finally {
    loading.value = false
  }
}

/** 当前页行：服务端按创建时间升序返回 → 展示倒序（最新在前） */
const pagedRows = computed<KeysRow[]>(() =>
  [...rows.value].reverse().slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)

function onPageChange(nextPage: number): void {
  page.value = nextPage
}

// ---------- 创建（完整 Key 仅显示一次） ----------
function openCreate(): void {
  // 重置，保证下一次创建重新进入「表单」态并再次一次性展示新 Key
  createdKey.value = null
  showCreate.value = true
}

function closeCreate(): void {
  showCreate.value = false
}

async function submitCreate(): Promise<void> {
  creating.value = true
  try {
    const note = createNote.value.trim() || null
    const res = await apiPost<CreateKeyResult>('/keys', { tier: createTier.value, note })
    // 响应中的完整 Key 仅此一次可获得：存入内存供弹窗展示 + 复制 + 本会话管理
    createdKey.value = { full: res.key }
    createNote.value = ''
    toast.success('API Key 已创建（完整 Key 仅此一次显示，请立即保存）')
    await loadData() // 刷出列表中的新行（脱敏前缀）
  } catch (error) {
    console.warn('[KeysView] 创建 API Key 失败：', error)
  } finally {
    creating.value = false
  }
}

/** 复制完整 Key：clipboard API 不可用/失败时降级为隐藏 textarea + execCommand */
async function copyKey(): Promise<void> {
  const full = createdKey.value?.full
  if (!full) return
  let ok = false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(full)
      ok = true
    }
  } catch {
    ok = false
  }
  if (!ok) {
    const ta = document.createElement('textarea')
    ta.value = full
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(ta)
  }
  toast.info(ok ? '完整 Key 已复制到剪贴板' : '复制失败，请手动选中复制')
}

// ---------- 停用（二次确认）/ 启用 ----------
function askDisable(row: KeysRow): void {
  disabling.value = row
}

async function confirmDisable(): Promise<void> {
  const row = disabling.value
  if (!row) return
  try {
    await apiPatch<KeysRow>(`/keys/${encodeURIComponent(keyRefFor(row))}`, { enabled: false })
    toast.success('已停用该 Key（相关调用方将失效）')
    await loadData()
  } catch (error) {
    console.warn('[KeysView] 停用 API Key 失败：', error)
  } finally {
    disabling.value = null
  }
}

/** 启用无副作用，直接执行（不停用那样需要二次确认） */
async function enableKey(row: KeysRow): Promise<void> {
  try {
    await apiPatch<KeysRow>(`/keys/${encodeURIComponent(keyRefFor(row))}`, { enabled: true })
    toast.success('已启用该 Key')
    await loadData()
  } catch (error) {
    console.warn('[KeysView] 启用 API Key 失败：', error)
  }
}

// ---------- 删除（二次确认） ----------
function askDelete(row: KeysRow): void {
  deleting.value = row
}

async function confirmDelete(): Promise<void> {
  const row = deleting.value
  if (!row) return
  try {
    await apiDelete(`/keys/${encodeURIComponent(keyRefFor(row))}`)
    toast.success('API Key 已删除')
    // 删除的正是本会话新建的 Key → 内存明文一并清除（不再需要）
    if (createdKey.value && textOf(row.key) === maskOf(createdKey.value.full)) {
      createdKey.value = null
    }
    await loadData()
  } catch (error) {
    console.warn('[KeysView] 删除 API Key 失败：', error)
  } finally {
    deleting.value = null
  }
}

onMounted(() => {
  void loadData()
})

// ---------- 列定义 ----------
const columns = [
  { key: 'tier', label: '分组', width: 90 },
  { key: 'key', label: 'Key（前缀）', width: 210 },
  { key: 'note', label: '备注', width: 150 },
  { key: 'last_used', label: '最近使用', width: 110 },
  { key: 'rate', label: '限流', width: 100 },
  { key: 'created_at', label: '创建时间', width: 150 },
  { key: 'enabled', label: '状态', width: 80 },
  { key: 'actions', label: '操作', width: 150 },
]
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">🔑 密钥管理</h2>
    <p class="page-hint">
      审核 API（POST /v1/audit）的访问凭证：创建后分发给外部调用方，调用时经 X-Api-Key 请求头携带。
      主操作 = 「➕ 创建 API Key」；完整 Key 仅在创建时显示一次，本站不保存明文。
    </p>

    <!-- 创建入口（M1：用途一句话 + 主操作） -->
    <div class="card">
      <div class="card-title">
        <span>➕ 创建 API Key</span>
        <button type="button" class="btn btn-primary btn-sm" @click="openCreate">创建 API Key</button>
      </div>
      <p class="desc-text">
        为每个调用方签发独立凭证：<b>standard</b>（仅基本判定，详情脱敏）或 <b>full</b>
        （完整细节）。按调用方拆分 Key 可独立启停 / 删除，互不影响；分组差异在对接时
        决定可见的详情字段（对齐旧版 tab-apikeys 的 standard/full 语义）。
      </p>
    </div>

    <!-- 对接提示（静态文案，风格对齐 GuideMenu「审核 API 对接」章节） -->
    <div class="card">
      <div class="card-title"><span>🔌 审核 API 对接</span></div>
      <p class="desc-text">
        调用 <code class="mono">POST http://127.0.0.1:8000/v1/audit</code> 时把 Key 放在
        <code class="mono">X-Api-Key</code> 请求头（也支持 <code class="mono">Authorization: Bearer</code>）；
        Key 缺失、被停用或被删除时请求返回 401，请先在此页签发并保持启用。
      </p>
      <pre class="connect-code"># curl 一句话
curl -X POST http://127.0.0.1:8000/v1/audit \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: &lt;你的 Key&gt;" \
  -d '{"text":"待审核内容","images":[]}'

# fetch 一句话
fetch('http://127.0.0.1:8000/v1/audit', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Api-Key': '&lt;你的 Key&gt;' },
  body: JSON.stringify({ text: '待审核内容', images: [] })
})</pre>
      <p class="filter-note">
        分组差异：standard 结果详情脱敏，full 返回完整细节；当前每 Key 限流为全局固定
        60 次 / 60 秒（环境变量 SAFEFUSION_RATE_LIMIT 可整体调整，非按 Key，TODO 见后端）。
        ⚠️ 完整 Key 仅创建时显示一次，忘记只能删除后重新创建（本站不保存明文）。
      </p>
    </div>

    <!-- Key 列表 -->
    <div class="card">
      <div class="card-title">
        <span>🗝️ API Key 列表（共 {{ total }} 条）</span>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="loadData">
          🔄 刷新
        </button>
      </div>

      <DataTable :columns="columns" :rows="pagedRows" :loading="loading">
        <template #empty>
          <EmptyState
            icon="🔑"
            title="暂无 API Key"
            :hint="'审核 API 尚未签发任何访问凭证，外部调用方暂时无法接入。\n点击「创建第一个 Key」生成 standard / full 分组的凭证；完整 Key 仅在创建时显示一次，请立即保存。'"
            action-text="创建第一个 Key"
            @action="openCreate"
          />
        </template>
        <template #cell="{ row, column }">
          <template v-if="column.key === 'tier'">
            <span
              class="tier-chip"
              :class="textOf(row.tier) === 'full' ? 'tier-full' : 'tier-standard'"
              :title="textOf(row.tier) === 'full' ? 'full：完整细节' : 'standard：仅基本判定（结果脱敏）'"
            >{{ textOf(row.tier) || '—' }}</span>
          </template>
          <template v-else-if="column.key === 'key'">
            <span class="mono">{{ textOf(row.key) || '—' }}</span>
            <span v-if="createdKey && textOf(row.key) === maskOf(createdKey.full)" class="chip-new">
              本次新建
            </span>
          </template>
          <template v-else-if="column.key === 'note'">{{ textOf(row.note) || '—' }}</template>
          <template v-else-if="column.key === 'last_used'">
            <!-- 后端 api_keys 表无 last_used 字段（TODO，见文件头）：缺则标注 — -->
            <span class="dim">—</span>
          </template>
          <template v-else-if="column.key === 'rate'">
            <!-- 无按 Key 限流字段：全局固定 60 次/60 秒（env 可调），非按 Key（TODO，见文件头） -->
            <span class="dim" title="当前为全局固定限流 60 次/60 秒（SAFEFUSION_RATE_LIMIT 可整体调整），按 Key 独立限流后端暂缺（TODO）">
              —
            </span>
          </template>
          <template v-else-if="column.key === 'created_at'">{{ fmtTime(row.created_at) }}</template>
          <template v-else-if="column.key === 'enabled'">
            <span class="status-tag" :class="boolOf(row.enabled) ? 'status-on' : 'status-off'">
              {{ boolOf(row.enabled) ? '启用' : '停用' }}
            </span>
          </template>
          <template v-else-if="column.key === 'actions'">
            <button
              v-if="boolOf(row.enabled)"
              type="button"
              class="btn btn-ghost btn-sm"
              @click.stop="askDisable(row)"
            >停用</button>
            <button
              v-else
              type="button"
              class="btn btn-primary btn-sm"
              @click.stop="enableKey(row)"
            >启用</button>
            <button
              type="button"
              class="btn btn-danger btn-sm"
              @click.stop="askDelete(row)"
            >删除</button>
          </template>
        </template>
      </DataTable>
      <Pagination :page="page" :page-size="PAGE_SIZE" :total="total" @change="onPageChange" />
    </div>

    <!-- 创建弹窗：表单 → 创建成功后一次性展示完整 Key -->
    <AppModal :show="showCreate" title="🔑 创建 API Key" @close="closeCreate">
      <template v-if="createdKey === null">
        <div class="create-form">
          <label class="form-item">
            <span class="form-label">权限分组</span>
            <select v-model="createTier" class="input">
              <option value="standard">standard（仅基本判定，结果脱敏）</option>
              <option value="full">full（完整细节）</option>
            </select>
          </label>
          <label class="form-item">
            <span class="form-label">备注（可选）</span>
            <input
              v-model="createNote"
              type="text"
              class="input"
              placeholder="用途 / 归属方，便于管理"
            />
          </label>
        </div>
      </template>

      <template v-else>
        <div class="key-once">
          <div class="key-once-mono">{{ createdKey.full }}</div>
          <p class="key-warn">
            ⚠️ 完整 Key 仅此一次显示，关闭后不再显示，请立即复制保存；本站不保存明文，
            忘记只能删除后重新创建。
          </p>
        </div>
      </template>

      <template #actions>
        <template v-if="createdKey === null">
          <button type="button" class="btn btn-ghost" @click="closeCreate">取消</button>
          <button type="button" class="btn btn-primary" :disabled="creating" @click="submitCreate">
            {{ creating ? '创建中...' : '创建' }}
          </button>
        </template>
        <template v-else>
          <button type="button" class="btn btn-ghost" @click="copyKey">📋 复制</button>
          <button type="button" class="btn btn-primary" @click="closeCreate">我已保存，关闭</button>
        </template>
      </template>
    </AppModal>

    <!-- 停用二次确认（提示 effect：相关调用方将失效） -->
    <ConfirmDialog
      :show="disabling !== null"
      title="⚠️ 停用 API Key"
      :message="
        disabling
          ? `确定停用 Key「${textOf(disabling.key)}」吗？停用后，使用它的调用方将立即失效（审核请求返回 401）；重新启用即可恢复。`
          : ''
      "
      danger
      @confirm="confirmDisable"
      @cancel="disabling = null"
    />

    <!-- 删除二次确认（提示 effect：相关调用方将失效，且不可恢复） -->
    <ConfirmDialog
      :show="deleting !== null"
      title="⚠️ 删除 API Key"
      :message="
        deleting
          ? `确定删除 Key「${textOf(deleting.key)}」吗？删除后，使用它的调用方将立即失效（审核请求返回 401），且无法恢复。`
          : ''
      "
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

.desc-text {
  font-size: 0.8rem;
  color: var(--text-2);
  line-height: 1.7;
}

.mono {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.76rem;
  word-break: break-all;
}

.dim {
  color: var(--text-3);
}

/* 对接 curl / fetch 代码块（风格对齐 GuideMenu .guide-code） */
.connect-code {
  margin: 10px 0 6px;
  padding: 10px 12px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 0.72rem;
  line-height: 1.7;
  overflow-x: auto;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
}

.filter-note {
  font-size: 0.72rem;
  color: var(--text-3);
  margin-top: 8px;
  line-height: 1.7;
}

/* 分组徽标 */
.tier-chip {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 700;
  white-space: nowrap;
}

.tier-full {
  background: var(--primary-light);
  color: var(--primary);
}

.tier-standard {
  background: var(--surface-hover);
  color: var(--text-2);
}

/* 「本次新建」小徽标：提示该行可用内存中的完整 Key 直接管理（见 keyRefFor） */
.chip-new {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  font-size: 0.66rem;
  font-weight: 700;
  background: var(--success-light);
  color: var(--success);
  white-space: nowrap;
}

/* 启用 / 停用状态标签 */
.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 700;
  white-space: nowrap;
}

.status-on {
  background: var(--success-light);
  color: var(--success);
}

.status-off {
  background: var(--surface-hover);
  color: var(--text-3);
}

/* 创建弹窗表单 */
.create-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-label {
  font-size: 0.74rem;
  font-weight: 600;
  color: var(--text-3);
}

/* 完整 Key 一次性展示卡片 */
.key-once {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.key-once-mono {
  padding: 14px;
  border: 1px dashed var(--primary);
  border-radius: var(--radius-sm);
  background: var(--primary-light);
  color: var(--text);
  font-size: 0.84rem;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  word-break: break-all;
  user-select: text;
}

.key-warn {
  font-size: 0.76rem;
  color: var(--danger);
  line-height: 1.7;
}
</style>