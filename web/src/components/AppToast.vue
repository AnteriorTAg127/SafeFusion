<script setup lang="ts">
/**
 * 全局 Toast 渲染：消费 toast store 的 items 列表。
 * 消息文案由 store 传入（组件内仅展示），显示位置底部居中，自动消失计时在 store 内完成。
 * F10③：error 类渲染 × 手动关闭按钮（驻留 8s，长错误文案可提前关掉）；
 * success/info 维持自动消失不可手关（防误触）。
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
          <span class="toast-msg">{{ item.message }}</span>
          <button
            v-if="item.type === 'error'"
            type="button"
            class="toast-close"
            aria-label="关闭提示"
            @click="toast.close(item.id)"
          >
            ✕
          </button>
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

/* 消息 + （error 的 ×）单行布局：覆盖全局 .toast 的纯文本 div 布局 */
.toast {
  display: flex;
  align-items: center;
  gap: 4px;
  text-align: left;
}

/* error 类手动关闭按钮（F10③）：悬停反色、点击即移除单条 */
.toast-close {
  flex-shrink: 0;
  margin-left: 8px;
  padding: 0 4px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  opacity: 0.75;
  font-size: 0.8rem;
  line-height: 1;
  cursor: pointer;
  transition: var(--transition);
}

.toast-close:hover {
  opacity: 1;
  background: rgba(0, 0, 0, 0.18);
}
</style>
