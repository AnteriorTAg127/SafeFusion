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
 */
import { computed, onMounted, ref } from 'vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import EmptyState from '../components/EmptyState.vue'
import { apiGet, apiPost } from '../api/client'
import type { AuditResult, ExamplesResponse, TrialExample } from '../api/types'

const EXAMPLES_MAX = 20 // 后端抽样上限（PRD M2；前端同 20 条展示窗口）

// ---------- 输入/示例状态 ----------
const inputText = ref('')
const examples = ref<TrialExample[]>([])
const examplesLoading = ref(false)
const autoSend = ref(true) // 点击示例后自动发送

// ---------- 结果状态 ----------
const sending = ref(false)
const result = ref<AuditResult | null>(null)
const error = ref('')
const durationMs = ref(0)
const hasSent = ref(false) // 是否成功发送过（结果区空态判断）

const canSend = computed(() => inputText.value.trim() !== '')

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
/** 从错误对象提取可读文案（对齐 client.ts readableError 的 detail/error 双响应体） */
function errorText(err: unknown): string {
  const e = err as { response?: { data?: unknown; status?: number }; message?: string }
  const data = e?.response?.data
  if (data && typeof data === 'object') {
    const body = data as { detail?: unknown; error?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (typeof body.error === 'string') return body.error
  }
  if (e?.response?.status === 401) return 'Token 无效或已过期'
  return e?.message ?? '请求失败'
}

async function send(): Promise<void> {
  const text = inputText.value.trim()
  if (!text || sending.value) return
  sending.value = true
  error.value = ''
  const t0 = performance.now()
  try {
    result.value = await apiPost<AuditResult>('/test-audit', { text })
    hasSent.value = true
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
</script>

<template>
  <section class="page-view">
    <h2 class="page-title">🧪 试运行</h2>
    <p class="page-hint">
      现场验证全链路：输入文本（或点一个随机示例）→ 发送 → 分层证据逐层展示，
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
        :hint="'输入一段文本，或点一个随机示例立即发送，判定与分层证据（关键词 / 正则 / 语义 / 白名单 / LLM）会显示在下方。\n试运行以管理端 full 权限执行，返回完整证据明细。'"
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

/* 标签语义（自含） */
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
</style>