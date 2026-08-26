<script setup lang="ts">
/**
 * 危险操作二次确认弹窗（基于 AppModal）：
 * props: show / title / message / danger（danger=true 时确认按钮为红色）
 * emit: confirm（确认）/ cancel（取消或关闭）
 */
import AppModal from './AppModal.vue'

withDefaults(
  defineProps<{
    show: boolean
    title?: string
    message?: string
    danger?: boolean
  }>(),
  { title: '⚠️ 确认操作', message: '确定执行该操作吗？', danger: false },
)

const emit = defineEmits<{
  (e: 'confirm'): void
  (e: 'cancel'): void
}>()
</script>

<template>
  <AppModal :show="show" :title="title" @close="emit('cancel')">
    <p class="confirm-message">{{ message }}</p>
    <template #actions>
      <button type="button" class="btn btn-ghost" @click="emit('cancel')">取消</button>
      <button
        type="button"
        :class="danger ? 'btn btn-danger' : 'btn btn-primary'"
        @click="emit('confirm')"
      >
        确认
      </button>
    </template>
  </AppModal>
</template>

<style scoped>
.confirm-message {
  font-size: 0.84rem;
  color: var(--text-2);
  line-height: 1.6;
  word-break: break-word;
  user-select: text;
}
</style>