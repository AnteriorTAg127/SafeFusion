<script setup lang="ts">
/**
 * 图片白名单页（T24）：
 * - DataTable：图片（缩略图占位 + 服务端文件路径文本）、pHash 摘要（截断+title
 *   全文）、添加时间、备注（原文件名）、操作（删除 ← ConfirmDialog）
 * - 添加：多选图片文件 → POST multipart（字段名 files，对齐 admin.py）→ 刷新
 * - 服务端分页 + 空态 + loading
 *
 * 字段对齐（依据 src/safefusion/api/admin.py、storage/database.py whitelist_meta 表）：
 *   GET /admin/whitelist/images?page&page_size → { total, page, page_size,
 *   items:[{ id, md5, phash_hex, note, created_at }] }
 *   POST /admin/whitelist/images（multipart 字段名 files，可多张）→
 *   { uploaded, failed, items:[{ id, md5, phash_hex, note, file }] }
 *   DELETE /admin/whitelist/images/{entry_id} → { deleted, file_deleted, file }
 *
 * 已知后端缺口（写入报告 TODO）：
 * - 后端无图片访问/缩略图端点：白名单原图存服务端 data/whitelist/{md5}.png，
 *   GET /admin/whitelist/images 只返回 md5/phash 等元数据 → 列表无 <img> 可加载，
 *   以占位图标 + 服务端路径文本呈现（不再臆造 URL）；后端如后续加
 *   /admin/whitelist/images/{id}/file 或静态挂载再切换 <img :src>。
 * - 无 pHash 距离展示（列表仅存 phash_hex；距离是审核时动态计算的，写 TODO）。
 */
import { onMounted, ref } from 'vue'
import DataTable from '../components/DataTable.vue'
import Pagination from '../components/Pagination.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiDelete } from '../api/client'
import { useToastStore } from '../stores/toast'

interface WhitelistPage {
  total: number
  page: number
  page_size: number
  items: Array<Record<string, unknown>>
}

interface WhitelistUploadResult {
  uploaded: number
  failed: number
  items: Array<Record<string, unknown>>
}

type WhitelistRow = Record<string, unknown>

const PAGE_SIZE = 10
const toast = useToastStore()

const rows = ref<WhitelistRow[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const uploading = ref(false)
const deleting = ref<WhitelistRow | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function fmtTime(ts: unknown): string {
  const s = textOf(ts)
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

function shortPhash(value: unknown, len = 16): string {
  const s = textOf(value)
  return s ? (s.length > len ? `${s.slice(0, len)}…` : s) : '—'
}

// ---------- 数据加载（服务端分页） ----------
async function loadData(): Promise<void> {
  loading.value = true
  try {
    const res = await apiGet<WhitelistPage>('/whitelist/images', {
      page: page.value,
      page_size: PAGE_SIZE,
    })
    rows.value = res.items
    total.value = res.total
  } catch (error) {
    console.warn('[WhitelistView] 加载白名单失败：', error)
  } finally {
    loading.value = false
  }
}

function onPageChange(nextPage: number): void {
  page.value = nextPage
  void loadData()
}

// ---------- 添加（多文件 multipart，字段名 files） ----------
async function uploadImages(): Promise<void> {
  const files = fileInput.value?.files
  if (!files || files.length === 0) {
    toast.error('请先选择图片文件')
    return
  }
  uploading.value = true
  try {
    const fd = new FormData()
    for (const f of Array.from(files)) {
      fd.append('files', f)
    }
    const res = await apiPost<WhitelistUploadResult>('/whitelist/images', fd)
    if (res.uploaded > 0) toast.success(`已添加 ${res.uploaded} 张白名单图片`)
    if (res.failed > 0) toast.error(`${res.failed} 张上传失败（图片解码失败或为空文件）`)
    if (fileInput.value) fileInput.value.value = ''
    page.value = 1
    await loadData()
  } catch (error) {
    console.warn('[WhitelistView] 上传失败：', error)
  } finally {
    uploading.value = false
  }
}

// ---------- 删除（二次确认） ----------
function askDelete(row: WhitelistRow): void {
  deleting.value = row
}

async function confirmDelete(): Promise<void> {
  const row = deleting.value
  if (!row) return
  try {
    const res = await apiDelete<{ deleted: number; file_deleted: boolean }>(
      `/whitelist/images/${String(row.id)}`,
    )
    toast.success(`白名单条目已删除${res.file_deleted ? '' : '（磁盘原图缺失，未删除文件）'}`)
    await loadData()
  } catch (error) {
    console.warn('[WhitelistView] 删除失败：', error)
  } finally {
    deleting.value = null
  }
}

onMounted(() => {
  void loadData()
})

// ---------- 列定义 ----------
const columns = [
  { key: 'image', label: '图片', width: 220 },
  { key: 'phash_hex', label: 'pHash 摘要', width: 180 },
  { key: 'created_at', label: '添加时间', width: 176 },
  { key: 'note', label: '备注（原文件名）' },
  { key: 'actions', label: '操作', width: 90 },
]
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">🖼️ 图片白名单</h2>

    <!-- 添加区 -->
    <div class="card">
      <div class="card-title"><span>➕ 添加白名单图片</span></div>
      <div class="add-row">
        <input ref="fileInput" type="file" class="input add-file" accept="image/*" multiple />
        <button type="button" class="btn btn-primary btn-sm" :disabled="uploading" @click="uploadImages">
          📤 上传并入库
        </button>
      </div>
      <p class="filter-note">
        多选图片后上传：后端逐个计算 md5 + pHash，原图存服务端 data/whitelist/{md5}.png
        （重复 md5 幂等返回既有条目）。注：后端暂无图片访问端点（TODO），列表以
        占位图标 + 服务端路径文本展示，不加载真实缩略图。
      </p>
    </div>

    <!-- 列表 -->
    <div class="card">
      <div class="card-title"><span>🗂️ 白名单列表（共 {{ total }} 条）</span></div>
      <DataTable :columns="columns" :rows="rows" :loading="loading" empty-text="暂无白名单图片">
        <template #cell="{ row, column }">
          <template v-if="column.key === 'image'">
            <div class="img-cell">
              <span class="img-placeholder" aria-hidden="true">🖼️</span>
              <span class="img-info">
                <span class="img-note">{{ textOf(row.note) || '（无文件名备注）' }}</span>
                <span class="img-path">data/whitelist/{{ textOf(row.md5) }}.png</span>
              </span>
            </div>
          </template>
          <template v-else-if="column.key === 'phash_hex'">
            <span class="phash-cell" :title="textOf(row.phash_hex)">{{ shortPhash(row.phash_hex) }}</span>
          </template>
          <template v-else-if="column.key === 'created_at'">{{ fmtTime(row.created_at) }}</template>
          <template v-else-if="column.key === 'note'">{{ textOf(row.note) || '—' }}</template>
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
      title="⚠️ 删除白名单条目"
      :message="deleting ? `确定删除该白名单图片吗？\nmd5：${textOf(deleting.md5)}\n备注：${textOf(deleting.note) || '—'}\n将同时删除数据库记录与服务端原图文件。` : ''"
      danger
      @confirm="confirmDelete"
      @cancel="deleting = null"
    />
  </section>
</template>

<style scoped>
.add-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.add-file {
  flex: 2 1 260px;
  padding: 6px 10px;
}

.filter-note {
  font-size: 0.72rem;
  color: var(--text-3);
  margin-top: 10px;
}

/* 图片列：占位图标 + 备注 + 服务端路径文本（无图片访问端点，TODO） */
.img-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.img-placeholder {
  font-size: 1.4rem;
  flex-shrink: 0;
}

.img-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.img-note {
  font-size: 0.78rem;
  color: var(--text-2);
  word-break: break-all;
}

.img-path {
  font-size: 0.68rem;
  color: var(--text-3);
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  word-break: break-all;
}

.phash-cell {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.76rem;
}
</style>