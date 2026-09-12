<script setup lang="ts">
/**
 * 通用弹窗（T57c/F10② 可访问性增强）：
 * - props: title（标题）、show（是否显示）
 * - 插槽：默认（正文 body）、actions（底部操作按钮区）
 * - 遮罩点击不关闭（对齐参考项目：用按钮关闭，避免误触丢失输入——设计决策，
 *   T57c 不改此行为）；右上角 ✕ 关闭按钮触发 close 事件
 * - Esc 关闭：等价点 ✕（ConfirmDialog/表单弹窗统一走 close/cancel 事件，
 *   不直接毁输入内容）；监听仅在 show=true 时挂载，关闭/卸载自动移除
 * - 打开时 body 锁滚动（overflow:hidden，关闭还原）；打开后焦点入弹窗卡片、
 *   关闭后焦点还原到打开前元素（document.activeElement 暂存）
 * - 约定：同一时刻仅一个 AppModal 处于打开态（本应用 ConfirmDialog/表单弹窗互斥），
 *   滚动锁/焦点暂存按单弹窗语义实现。
 */
import { nextTick, onBeforeUnmount, onMounted, watch } from 'vue'

const props = defineProps<{
  title: string
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

/** 打开弹窗前 body 的滚动样式（还原用） */
let prevOverflow = ''
/** 打开弹窗时的焦点元素（关闭后还原） */
let prevFocus: HTMLElement | null = null
/** 卡片根元素（焦点/监听锚点） */
let cardEl: HTMLElement | null = null

/** Esc 关闭：等价点 ✕，统一走 close 事件（弹窗内输入内容不直接销毁）。
 * 仅弹窗处于打开态时生效（show=false 时忽略，避免全局吞掉页面内 Esc） */
function onKeydown(event: KeyboardEvent): void {
  if (!props.show) return
  if (event.key === 'Escape') {
    event.stopPropagation()
    emit('close')
  }
}

/** 打开：锁 body 滚动 + 暂存焦点；关闭：还原滚动与焦点 */
function applyLock(open: boolean): void {
  if (open) {
    prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    prevFocus = document.activeElement as HTMLElement | null
    // 等 Teleport/Transition 渲染出卡片后把焦点移入（初始焦点入弹窗）
    void nextTick(() => {
      cardEl?.focus()
    })
  } else {
    document.body.style.overflow = prevOverflow
    // 焦点还原到打开前元素（若仍存在于文档中），否则退回 body
    if (prevFocus && prevFocus.isConnected) {
      prevFocus.focus()
    }
    prevFocus = null
  }
}

watch(
  () => props.show,
  (open) => {
    applyLock(open)
  },
)

onMounted(() => {
  if (props.show) applyLock(true)
  document.addEventListener('keydown', onKeydown, true)
})

onBeforeUnmount(() => {
  // 卸载兜底：若仍处于打开态则还原滚动（防滚动锁泄漏）
  if (props.show) applyLock(false)
  document.removeEventListener('keydown', onKeydown, true)
})

/** 卡片根元素引用（初始焦点落点：可聚焦元素，无碍朗读与 Tab 循环起点） */
function onCardRef(el: Element | null): void {
  cardEl = el as HTMLElement | null
}
</script>

<template>
  <Teleport to="body">
    <Transition name="modal-fade">
      <div v-if="show" class="modal-mask">
        <!-- 遮罩本身无点击处理：点击外层不会关闭弹窗 -->
        <div ref="onCardRef" class="modal-card" role="dialog" :aria-label="title" tabindex="-1">
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
  /* 卡片可聚焦（初始焦点入弹窗，F10②）；聚焦态仅保留可见 outline 便于键盘用户定位 */
  outline: none;
}

.modal-card:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
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
