<script setup lang="ts">
/**
 * 空态即下一步组件（PRD v0.3.0 §M1）：
 * 「为什么空 + 现在该干什么 + 跳转按钮」三段式空态，对齐旧版 §2.8 文案范式。
 * - 用法一（页面内独立使用）：<EmptyState icon="📭" title="..." hint="..." action-text="..." to="/overview" />
 * - 用法二（DataTable 空态插槽）：在 DataTable 上提供 <template #empty><EmptyState ... /></template>，
 *   此时外层 .empty-state 由 DataTable 提供居中与留白，本组件只渲染内容。
 * - 跳转：to 以 "/" 开头按路径跳转，否则按路由名跳转；不传 to 时点击按钮触发 action 事件
 *   （同一页面的下一步操作，如滚动到导入面板 / 打开新增表单），由父组件自行处理。
 */
import { useRouter } from 'vue-router'

const props = withDefaults(
  defineProps<{
    /** 空态图标（Emoji） */
    icon?: string
    /** 标题：「为什么空」（一句话） */
    title?: string
    /** 提示：「现在该干什么」（可多行，用换行分隔） */
    hint?: string
    /** 操作按钮文案；缺省则不渲染按钮 */
    actionText?: string
    /** 跳转目标：以 "/" 开头按路径、否则按路由名；缺省时按钮触发 action 事件 */
    to?: string
  }>(),
  { icon: '📭', title: '', hint: '', actionText: '', to: '' },
)

const emit = defineEmits<{
  (e: 'action'): void
}>()

const router = useRouter()

function handleAction(): void {
  const target = props.to?.trim()
  if (target) {
    void router.push(target.startsWith('/') ? target : { name: target })
    return
  }
  emit('action')
}
</script>

<template>
  <div class="empty-box">
    <span v-if="props.icon" class="empty-icon" aria-hidden="true">{{ props.icon }}</span>
    <p v-if="props.title" class="empty-title">{{ props.title }}</p>
    <p v-if="props.hint" class="empty-hint">{{ props.hint }}</p>
    <button
      v-if="props.actionText"
      type="button"
      class="btn btn-primary btn-sm empty-action"
      @click="handleAction"
    >
      {{ props.actionText }}
    </button>
  </div>
</template>

<style scoped>
.empty-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
  padding: 12px 0;
}

.empty-icon {
  font-size: 2rem;
  display: block;
}

.empty-title {
  font-size: 0.92rem;
  font-weight: 700;
  color: var(--text-2);
}

.empty-hint {
  font-size: 0.78rem;
  color: var(--text-3);
  line-height: 1.7;
  max-width: 480px;
  white-space: pre-line;
}

.empty-action {
  margin-top: 4px;
}
</style>