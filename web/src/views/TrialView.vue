<script setup lang="ts">
/**
 * 试运行页（PRD v0.3.0 §M2，T37）：现场验证全链路的上手主入口。
 * - 文本输入（textarea）+ 模式说明 + 「🚀 发送」按钮 → POST /admin/test-audit
 *   （管理端 full 权限，等效 full 组 Key 调用 POST /v1/audit，返回完整 detail）；
 * - 「🎲 随机示例」chips：GET /admin/test-examples（≤20 条，黑/白池标注），
 *   点击填入文本、可选自动发送（默认开，对齐旧版「点击即填入并自动发送」）；
 * - 结果为 EvidencePanel 分层证据 + 前端实测耗时（ms）；
 * - 空态 EmptyState（首次使用提示 + 加载示例）。
 *
 * 字段契约（读后端源码）：schemas.AuditRequest（仅提交 text）/
 * AuditResult（model_dump 返回）；admin.py test-examples → {items,total}。
 *
 * T57a/F2：test-audit 请求走 SLOW_TIMEOUT_MS（120s）——首图懒装配
 * （语义模型按需装配）场景可能远超默认 15s，原全局超时会造成假失败
 * 而后端已写库；超时专属 Toast「请勿重复提交」。其余本页改动归 T57c/F8。
 *
 * T57c/F8（S1）资源与口径：
 * - 图片预览 objectURL 四时机统一 revoke：换选（onImagePick 重建前先释放旧的）、
 *   删图（removeImage）、发送成功后（send 结束释放预览，避免预览图驻留内存——
 *   结果区只展示文本/命中，不再引用预览图）、组件卸载（onBeforeUnmount 全量 revoke）。
 * - 「耗时」口径：durationMs = 发送（含前端 base64 编码）→ 响应全程，
 *   前端实测值与后端日志 duration 属同数量级但**含前端编码/上传**；
 *   面板明示「含前端编码/上传」（见 EvidencePanel durationMs 注释）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import EmptyState from '../components/EmptyState.vue'
import { apiGet, apiPost, errorText, SLOW_TIMEOUT_MS } from '../api/client'
import type { AuditResult, ExamplesResponse, TrialExample } from '../api/types'

const EXAMPLES_MAX = 20 // 后端抽样上限（PRD M2；前端同 20 条展示窗口）

// ---------- 输入/示例状态 ----------
const inputText = ref('')
const selectedImages = ref<File[]>([])
const imagePreviews = ref<string[]>([])
const examples = ref<TrialExample[]>([])
const examplesLoading = ref(false)
const autoSend = ref(true) // 点击示例后自动发送

// ---------- 结果状态 ----------
const sending = ref(false)
const result = ref<AuditResult | null>(null)
const error = ref('')
const durationMs = ref(0)
const hasSent = ref(false) // 是否成功发送过（结果区空态判断）

const canSend = computed(() => inputText.value.trim() !== '' || selectedImages.value.length > 0)

// ---------- 图片预览 objectURL 生命周期（T57c/F8：四时机 revoke） ----------

/** 释放全部预览 objectURL（换选前 / 发送成功后 / 组件卸载共用） */
function revokeAllPreviews(): void {
  for (const url of imagePreviews.value) URL.revokeObjectURL(url)
  imagePreviews.value = []
}

/** 释放单个预览 objectURL（删图） */
function revokePreview(index: number): void {
  const url = imagePreviews.value[index]
  if (url) URL.revokeObjectURL(url)
}

// ---------- 图片选择（PRD v0.4.0 M5：试运行支持上传图片） ----------
function onImagePick(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = input.files
  if (!files || files.length === 0) return
  // F8 时机①换图：先释放上一批预览 objectURL，再重建（避免旧 URL 泄漏）
  revokeAllPreviews()
  selectedImages.value = Array.from(files)
  imagePreviews.value = selectedImages.value.map((f) => URL.createObjectURL(f))
  input.value = ''
}

function removeImage(index: number): void {
  // F8 时机②删图：释放该预览 URL 再移除引用
  revokePreview(index)
  selectedImages.value.splice(index, 1)
  imagePreviews.value.splice(index, 1)
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result as string
      // 保留 data URI 中的 base64 部分（AuditRequest.images[].base64 契约）
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

// ---------- 随机示例 ----------
async function loadExamples(): Promise<void> {
  if (examplesLoading.value) return
  examplesLoading.value = true
  try {
    const res = await apiGet<ExamplesResponse>('/test-examples')
    examples.value = res.items.slice(0, EXAMPLES_MAX)
  } catch (err) {
    // 错误 Toast 已由 api 层统一弹出；此处仅记录，chips 区显示提示文案
    console.warn('[TrialView] 随机示例加载失败：', err)
  } finally {
    examplesLoading.value = false
  }
}

/** 点击示例：填入文本框；（autoSend 开时）立即发送 */
function pickExample(ex: TrialExample): void {
  inputText.value = ex.text
  if (autoSend.value) void send()
}

// ---------- 发送 ----------
// 错误文案收敛自 client.ts 导出的 errorText（F4①：detail/error 双响应体 + 超时
// 专属文案 + 401 兜底，原本页本地实现删除；api 层已 Toast，此处仅内联展示）
async function send(): Promise<void> {
  if (!canSend.value || sending.value) return
  sending.value = true
  error.value = ''
  // 耗时口径（F8）：t0 起于发送前（含前端 base64 编码/上传），止于响应返回；
  // 与后端日志 duration 同数量级但含前端开销——见 EvidencePanel durationMs 注释
  const t0 = performance.now()
  try {
    const images = []
    for (const file of selectedImages.value) {
      images.push({ base64: await fileToBase64(file) })
    }
    const payload: Record<string, unknown> = { text: inputText.value.trim() || null }
    if (images.length > 0) payload.images = images
    // 首图懒装配场景可能远超 15s → 120s 慢超时（T57a/F2）
    result.value = await apiPost<AuditResult>('/test-audit', payload, undefined, {
      timeoutMs: SLOW_TIMEOUT_MS,
    })
    hasSent.value = true
    // F8 时机③发送成功后：释放预览 objectURL 并清空图片队列（结果区不再引用
    // 预览图，避免驻留内存；图片文件本体随队列一并清掉，界面与状态一致）
    revokeAllPreviews()
    selectedImages.value = []
  } catch (err) {
    // api 层已 Toast（401 除外）；此处内联展示错误卡片便于结果区自解释
    error.value = errorText(err)
  } finally {
    durationMs.value = Math.round(performance.now() - t0)
    sending.value = false
  }
}

onMounted(() => {
  void loadExamples()
})

// F8 时机④组件卸载：全量释放预览 objectURL（路由切走时不泄漏）
onBeforeUnmount(() => {
  revokeAllPreviews()
})
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">🧪 试运行</h2>
    <p class="page-hint">
      现场验证全链路：输入文本或上传图片（或点一个随机示例）→ 发送 → 分层证据逐层展示，
      系统判定「为什么违规 / 为什么通过」当场可见。
    </p>

    <!-- 文本输入 -->
    <div class="card">
      <div class="card-title">
        <span>📝 输入待审核文本</span>
        <span v-if="sending" class="trial-note">审核中…</span>
      </div>
      <p class="trial-mode-note">
        执行方式：管理端 full 权限（等效 full 组 Key 调用 POST /v1/audit），返回完整证据明细；
        standard / full 的分组差异说明见顶栏「指南」。
      </p>
      <textarea
        v-model="inputText"
        class="input trial-textarea"
        rows="5"
        placeholder="粘贴或输入文本，Ctrl+Enter 快捷发送……"
        @keydown.ctrl.enter.prevent="send"
      ></textarea>
      <div class="trial-images">
        <input
          type="file"
          class="input add-file"
          accept="image/*"
          multiple
          @change="onImagePick"
        />
        <div v-if="imagePreviews.length" class="trial-preview-list">
          <div v-for="(src, i) in imagePreviews" :key="i" class="trial-preview-item">
            <img :src="src" alt="待审核图片预览" />
            <button type="button" class="trial-preview-remove" @click="removeImage(i)">✕</button>
          </div>
        </div>
      </div>
      <div class="trial-actions">
        <button
          type="button"
          class="btn btn-primary"
          :disabled="!canSend || sending"
          @click="send"
        >
          {{ sending ? '⏳ 审核中…' : '🚀 发送' }}
        </button>
        <label class="trial-opt">
          <input v-model="autoSend" type="checkbox" />
          点击示例后自动发送
        </label>
      </div>
    </div>

    <!-- 随机示例 -->
    <div class="card">
      <div class="card-title">
        <span>🎲 随机示例（{{ examples.length }}）</span>
        <button
          v-if="examples.length"
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="examplesLoading"
          @click="loadExamples"
        >
          🔄 换一批
        </button>
      </div>
      <p v-if="examplesLoading" class="trial-note">示例加载中…</p>
      <p v-else-if="examples.length === 0" class="trial-note">
        未读取到示例：请确认 data/corpus/black.csv 与 white.csv 语料存在
        （第一列为文本，UTF-8 / 带 BOM；每条 ≤200 字符才会被抽样）。
      </p>
      <div v-else class="trial-examples">
        <button
          v-for="(ex, i) in examples"
          :key="i"
          type="button"
          class="trial-chip"
          :title="ex.text"
          @click="pickExample(ex)"
        >
          <span class="trial-pool" :class="ex.pool === 'black' ? 'pool-black' : 'pool-white'">
            {{ ex.pool === 'black' ? '黑' : '白' }}
          </span>
          <span class="trial-chip-text">{{ ex.text }}</span>
        </button>
      </div>
    </div>

    <!-- 首次使用空态（为什么空 + 现在干什么 + 动作按钮） -->
    <div v-if="!hasSent && !sending && !error" class="card empty-banner">
      <EmptyState
        icon="🧪"
        title="还没有试运行结果"
        :hint="'输入一段文本或上传图片，或点一个随机示例立即发送，判定与分层证据（关键词 / 正则 / 语义 / 白名单 / LLM）会显示在下方。\n试运行以管理端 full 权限执行，返回完整证据明细。'"
        action-text="加载随机示例"
        @action="loadExamples"
      />
    </div>

    <!-- 失败卡片（内联展示；api 层另有 Toast） -->
    <div v-if="error" class="card trial-error">
      <span class="tag tag-danger">❌ 请求失败</span>
      <span class="trial-error-text">{{ error }}</span>
    </div>

    <!-- 结果区：分层证据面板 + 耗时 -->
    <EvidencePanel
      v-if="hasSent || sending"
      :result="result"
      :loading="sending"
      :duration-ms="durationMs"
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

.trial-mode-note {
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: -6px 0 10px;
}

.trial-textarea {
  resize: vertical;
  min-height: 96px;
  line-height: 1.7;
}

.trial-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  margin-top: 12px;
}

.trial-opt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.78rem;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
}

.trial-note {
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.7;
}

/* 图片选择与预览（PRD v0.4.0 M5） */
.trial-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.add-file {
  flex: 1 1 260px;
  padding: 6px 10px;
}

.trial-preview-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.trial-preview-item {
  position: relative;
  width: 64px;
  height: 64px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.trial-preview-item img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.trial-preview-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: none;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 0.66rem;
  line-height: 1;
  cursor: pointer;
}

/* 示例 chips 滚动区 */
.trial-examples {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 220px;
  overflow-y: auto;
  padding: 2px;
}

.trial-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 4px 10px 4px 4px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-hover);
  color: var(--text-2);
  font-size: 0.76rem;
  cursor: pointer;
  transition: var(--transition);
}

.trial-chip:hover {
  border-color: var(--primary);
  background: var(--primary-light);
  color: var(--text);
}

.trial-pool {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 0.66rem;
  font-weight: 700;
}

.pool-black {
  background: var(--text);
  color: var(--surface);
}

.pool-white {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-3);
}

.trial-chip-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}

/* 空态与错误 */
.empty-banner {
  padding: 8px 20px;
  margin-bottom: 14px;
}

.trial-error {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.trial-error-text {
  font-size: 0.82rem;
  color: var(--text-2);
  word-break: break-word;
}
</style>