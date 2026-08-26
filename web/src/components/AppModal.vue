<script setup lang="ts">
/**
 * 通用弹窗：
 * - props: title（标题）、show（是否显示）
 * - 插槽：默认（正文 body）、actions（底部操作按钮区）
 * - 遮罩点击不关闭（对齐参考项目：用按钮关闭，避免误触丢失输入）；
 *   右上角 ✕ 关闭按钮触发 close 事件
 */
defineProps<{
  title: string
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()
</script>

<template>
  <Teleport to="body">
    <Transition name="modal-fade">
      <div v-if="show" class="modal-mask">
        <!-- 遮罩本身无点击处理：点击外层不会关闭弹窗 -->
        <div class="modal-card" role="dialog" :aria-label="title">
          <div class="modal-title">
            <span class="modal-title-text">{{ title }}</span>
            <button type="button" class="modal-close" aria-label="关闭" @click="emit('close')">
              ✕
            </button>
          </div>
          <div class="modal-body">
            <slot />
          </div>
          <div class="modal-actions">
            <slot name="actions" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  z-index: 998; /* 低于 toast(999)，保证提示浮在弹窗之上 */
  padding: 20px;
}

.modal-card {
  width: min(420px, 100%);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  padding: 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
}

.modal-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 1.05rem;
  font-weight: 700;
  margin-bottom: 8px;
}

.modal-title-text {
  min-width: 0;
  word-break: break-word;
}

.modal-close {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  margin-left: 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3);
  font-size: 0.9rem;
  line-height: 1;
  cursor: pointer;
  transition: var(--transition);
}

.modal-close:hover {
  background: var(--surface-hover);
  color: var(--text);
}

.modal-body {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  font-size: 0.84rem;
  color: var(--text-2);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}

/* 遮罩淡入 / 卡片弹出过渡 */
.modal-fade-enter-active,
.modal-fade-leave-active {
  transition: opacity 0.2s ease;
}

.modal-fade-enter-active .modal-card,
.modal-fade-leave-active .modal-card {
  transition: transform 0.22s cubic-bezier(0.34, 1.4, 0.64, 1), opacity 0.2s ease;
}

.modal-fade-enter-from,
.modal-fade-leave-to {
  opacity: 0;
}

.modal-fade-enter-from .modal-card,
.modal-fade-leave-to .modal-card {
  opacity: 0;
  transform: translateY(12px) scale(0.96);
}
</style>