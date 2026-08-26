<script setup lang="ts">
/**
 * 全局 Toast 渲染：消费 toast store 的 items 列表。
 * 消息文案由 store 传入（组件内仅展示），显示位置底部居中，自动消失计时在 store 内完成。
 */
import { useToastStore } from '../stores/toast'

const toast = useToastStore()
</script>

<template>
  <Teleport to="body">
    <div class="toast-stack" aria-live="polite">
      <TransitionGroup name="toast">
        <div
          v-for="item in toast.items"
          :key="item.id"
          class="toast"
          :class="`toast-${item.type}`"
        >
          {{ item.message }}
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
/* toast 进出过渡（基础样式见 src/style.css） */
.toast-enter-active,
.toast-leave-active {
  transition:
    opacity 0.25s ease,
    transform 0.25s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>