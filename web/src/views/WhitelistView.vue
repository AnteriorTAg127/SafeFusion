<script setup lang="ts">
/**
 * 图片白名单页（T24 + T57a/F1 修复）：
 * - DataTable：图片（真实缩略图）、pHash 摘要（截断+title 全文）、添加时间、
 *   备注（原文件名）、操作（删除 ← ConfirmDialog）
 * - 添加：多选图片文件 → POST multipart（字段名 files，对齐 admin.py）→ 刷新
 * - 服务端分页 + 空态 + loading
 *
 * T57a/F1（C1）缩略图 401 破图修复 —— blob 拉图：
 * - 根因：后端鉴权仅认请求头 X-Admin-Token（admin.py 路由级 Depends +
 *   dependencies.py:89 Header 只读、无 query/cookie 回退），浏览器 <img src>
 *   直连 GET /admin/whitelist/images/{id}/file 无法带头 → 必 401 碎图。
 * - 修复：axios 带 X-Admin-Token 以 responseType:'blob' 拉 /file → URL.createObjectURL
 *   给 <img>（令牌绝不出现在 URL/query，安全红线 PRD 决策 #9）。
 * - 懒加载：IntersectionObserver 观察缩略图容器，进入视口才拉图，避免一次性全量拉取。
 * - 生命周期：objectURL 缓存复用（同 id 不重复拉）+ 卸载/换图 revoke 释放。
 * - 兜底：拉取失败（404 原图缺失/401/网络）→ 占位图标 + 路径文本。
 *
 * 字段对齐（依据 src/safefusion/api/admin.py、storage/database.py whitelist_meta 表）：
 *   GET /admin/whitelist/images?page&page_size → { total, page, page_size,
 *   items:[{ id, md5, phash_hex, note, created_at }] }
 *   POST /admin/whitelist/images（multipart 字段名 files，可多张）→
 *   { uploaded, failed, items:[{ id, md5, phash_hex, note, file }] }
 *   DELETE /admin/whitelist/images/{entry_id} → { deleted, file_deleted, file }
 *   GET /admin/whitelist/images/{id}/file → FileResponse(image/png)（404 条目/文件缺失）
 *
 * 已知后端缺口（写入报告 TODO）：
 * - 无 pHash 距离展示（列表仅存 phash_hex；距离是审核时动态计算的，写 TODO）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiDelete, http, SLOW_TIMEOUT_MS } from '../api/client'
import { fmtTime, shortPhash, textOf } from '../utils/format'
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
/** 添加上传面板引用：空态「去上传图片」滚动到此处（PRD §M1） */
const addPanelRef = ref<HTMLElement | null>(null)

function scrollToAddPanel(): void {
  addPanelRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// ---------- T57a/F1：blob 缩略图（objectURL 缓存 + 懒加载 + 生命周期 revoke） ----------
/**
 * 缓存 key：白名单条目 id → { url, promise }。
 * url 已生成 → 直接复用（换页/重渲染不重复拉图）；
 * promise 在途 → 同 id 并发请求合并（防重复触发）。
 * 组件卸载时全部 revoke（URL.revokeObjectURL 释放 blob 引用）。
 */
const thumbCache = new Map<string, { url: string; promise: Promise<string> }>()
/** IntersectionObserver：缩略图进入视口才拉图（懒加载，避免一次性全量拉图） */
let thumbObserver: IntersectionObserver | null = null
/** 组件是否已卸载：卸载后不再创建新 objectURL（防泄漏） */
let unmounted = false

/**
 * 拉取白名单原图 → objectURL（经统一 axios 实例带头 X-Admin-Token；
 * 令牌只在请求头，绝不出现在 URL/query —— 安全红线 PRD 决策 #9）。
 * 失败（404 原图缺失 / 401 / 网络）→ 返回 ''，模板回退占位图。
 * 缓存命中（含在途 promise）直接返回 promise：调用方 .then 始终拿到最终
 * url（并发合并的健壮性保证——不返回半成品 ''）。
 */
async function loadThumbUrl(id: unknown): Promise<string> {
  const key = String(id)
  const cached = thumbCache.get(key)
  if (cached) return cached.promise
  const promise = (async (): Promise<string> => {
    try {
      const res = await http.get(`/whitelist/images/${key}/file`, {
        responseType: 'blob',
        timeout: SLOW_TIMEOUT_MS, // 原图较大/慢盘，放宽超时防假失败
      })
      if (unmounted) return '' // 已卸载：不创建新 objectURL（防泄漏）
      const url = URL.createObjectURL(res.data as Blob)
      // 缓存更新为「已就绪」形态（promise 保持同一引用，resolve 为最终 url）
      thumbCache.set(key, { url, promise: Promise.resolve(url) })
      return url
    } catch (error) {
      // 失败不弹 Toast（缩略图静默降级；http.get 不经过 request()，无自动 Toast）
      console.warn(`[WhitelistView] 缩略图拉取失败 id=${key}：`, error)
      thumbCache.set(key, { url: '', promise: Promise.resolve('') })
      return ''
    }
  })()
  thumbCache.set(key, { url: '', promise })
  return promise
}

/**
 * 行级缩略图 src（响应式对象：key=条目 id → objectURL；键写入走整体替换触发更新）。
 * 模板中按 key 取值的函数无法被 Vue 追踪对象属性变化 → 用 computed 派生 + 整体替换
 * 引用（thumbUrl.value = {...thumbUrl.value, [key]: url}）保证依赖收集正确（F1 渲染基石）。
 */
const thumbUrl = ref<Record<string, string>>({})
/** 已触发过拉图的条目（IntersectionObserver 每行只触发一次） */
const thumbTriggered = new Set<string>()

/** 写入一行缩略图 url（整体替换引用触发响应更新） */
function setThumbUrl(key: string, url: string): void {
  thumbUrl.value = { ...thumbUrl.value, [key]: url }
}

/** 缩略图 src（纯查询，无副作用）：缓存就绪直接返回；未就绪返回行级 url 或 '' */
function thumbSrcOf(row: WhitelistRow): string {
  const key = String(row.id)
  const cached = thumbCache.get(key)
  if (cached && cached.url !== '') return cached.url
  return thumbUrl.value[key] ?? ''
}

/**
 * IntersectionObserver 回调：进入视口的行触发拉图（每行只触发一次）。
 * 元素上挂 data-thumb-key 供查询。
 */
function onThumbIntersect(entries: IntersectionObserverEntry[]): void {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue
    const key = (entry.target as HTMLElement).dataset.thumbKey
    if (!key || thumbTriggered.has(key)) continue
    thumbTriggered.add(key)
    void loadThumbUrl(key).then((url) => {
      setThumbUrl(key, url)
    })
  }
}

/**
 * 观察当前页所有缩略图容器（IntersectionObserver 幂等：已触发的不再触发；
 * 数据更新时先 disconnect 重建观察器，保证新渲染的行也会被观察）。
 */
function observeThumbs(): void {
  const els = document.querySelectorAll<HTMLElement>('[data-thumb-key]')
  if (!els.length) return
  if (thumbObserver) thumbObserver.disconnect()
  // 不支持 IntersectionObserver 的浏览器：退化为主列表加载即拉图（见下方兜底逻辑）
  if (typeof IntersectionObserver === 'undefined') {
    for (const el of els) {
      const key = el.dataset.thumbKey
      if (key && !thumbTriggered.has(key)) {
        thumbTriggered.add(key)
        void loadThumbUrl(key).then((url) => {
          setThumbUrl(key, url)
        })
      }
    }
    return
  }
  thumbObserver = new IntersectionObserver(onThumbIntersect, { rootMargin: '120px' })
  els.forEach((el) => thumbObserver?.observe(el))
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
    // 新页数据就绪后观察缩略图容器（触发懒加载；等待 DOM 渲染完成）
    await nextTick()
    observeThumbs()
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
    const res = await apiPost<WhitelistUploadResult>('/whitelist/images', fd, undefined, {
      timeoutMs: SLOW_TIMEOUT_MS, // 批量上传放宽超时（F2：慢操作不假失败）
    })
    if (res.uploaded > 0) toast.success(`已添加 ${res.uploaded} 张白名单图片`)
    if (res.failed > 0) toast.error(`${res.failed} 张上传失败（图片解码失败或为空文件）`)
    if (fileInput.value) fileInput.value.value = ''
    page.value = 1
    // 新条目可能复用旧 objectURL（同 id 缓存）：仅清空待拉集合与行级 url，缓存保留复用
    thumbTriggered.clear()
    thumbUrl.value = {}
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
    // 删除后释放该条目 objectURL，避免内存滞留
    const key = String(row.id)
    const cached = thumbCache.get(key)
    if (cached?.url) URL.revokeObjectURL(cached.url)
    thumbCache.delete(key)
    delete thumbUrl.value[key]
    thumbTriggered.delete(key)
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

onBeforeUnmount(() => {
  // 组件卸载：停止观察 + 释放全部 objectURL（防内存泄漏，F1 生命周期要求）
  unmounted = true
  thumbObserver?.disconnect()
  thumbObserver = null
  for (const { url } of thumbCache.values()) {
    if (url) URL.revokeObjectURL(url)
  }
  thumbCache.clear()
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
    <p class="page-hint">
      登记确定安全、应在审核中快速放行的图片（如品牌 Logo、官方配图）：上传后按 md5 + pHash 入库，
      审核时命中白名单的图片将走快速放行通道。主操作 = 上方「📤 上传并入库」。
    </p>

    <!-- 添加区 -->
    <div ref="addPanelRef" class="card">
      <div class="card-title"><span>➕ 添加白名单图片</span></div>
      <div class="add-row">
        <input ref="fileInput" type="file" class="input add-file" accept="image/*" multiple />
        <button type="button" class="btn btn-primary btn-sm" :disabled="uploading" @click="uploadImages">
          {{ uploading ? '上传中…' : '📤 上传并入库' }}
        </button>
      </div>
      <p class="filter-note">
        多选图片后上传：后端逐个计算 md5 + pHash，原图存服务端 data/whitelist/{md5}.png
        （重复 md5 幂等返回既有条目）。列表经 blob 拉取展示真实缩略图
        （v0.4.0 T57a：/admin/whitelist/images/{id}/file + X-Admin-Token 请求头）。
      </p>
    </div>

    <!-- 列表 -->
    <div class="card">
      <div class="card-title"><span>🗂️ 白名单列表（共 {{ total }} 条）</span></div>
      <DataTable :columns="columns" :rows="rows" :loading="loading">
        <template #empty>
          <EmptyState
            icon="🖼️"
            title="暂无白名单图片"
            :hint="'白名单用于在审核时快速放行确定安全的图片（如品牌 Logo、官方配图）。\n在上方选择一张或多张图片上传：后端逐个计算 md5 + pHash 入库，之后相同或近似图片（汉明距离在阈值内）在审核中会命中白名单快速放行。'"
            action-text="去上传图片"
            @action="scrollToAddPanel"
          />
        </template>
        <template #cell="{ row, column }">
          <template v-if="column.key === 'image'">
            <div class="img-cell">
              <!-- 缩略图：blob objectURL（经 axios 带头拉取，令牌不入 URL）；
                   未就绪/失败时回退占位图标（@error 兜底 + src='' 占位双保险） -->
              <div class="img-thumb-wrap" :data-thumb-key="String(row.id)">
                <img
                  v-if="thumbSrcOf(row)"
                  class="img-thumb"
                  :src="thumbSrcOf(row)"
                  :alt="textOf(row.note) || '白名单图片'"
                  @error="setThumbUrl(String(row.id), '')"
                />
                <span v-else class="img-placeholder">🖼️</span>
              </div>
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
/* 标题下操作提示行（PRD v0.3.0 §M1：每页用途 + 主操作） */
.page-hint {
  font-size: 0.76rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: -8px 0 14px;
}

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

/* 图片列：真实缩略图（blob objectURL）+ 备注 + 服务端路径文本（T57a/F1） */
.img-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 缩略图容器：作为 IntersectionObserver 观察目标（data-thumb-key 关联条目 id） */
.img-thumb-wrap {
  width: 56px;
  height: 56px;
  flex-shrink: 0;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--surface-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.img-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
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
