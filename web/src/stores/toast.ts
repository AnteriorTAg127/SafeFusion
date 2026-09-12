import { ref } from 'vue'
import { defineStore } from 'pinia'

/** Toast 类型：success / error / info 三色 */
export type ToastType = 'success' | 'error' | 'info'

export interface ToastItem {
  id: number
  type: ToastType
  message: string
}

/** 自增 id，避免重复 key */
let nextId = 1

/** 各类型自动消失时长（F10③：error 4s→8s 便于阅读长错误文案；success/info 维持 3s） */
const DURATION_MS: Record<ToastType, number> = {
  success: 3000,
  error: 8000,
  info: 3000,
}

/**
 * 全局 Toast store：页面/API 层调用 success/error/info 弹出提示。
 * 自动消失计时在 store 内完成；error 类支持手动关闭（× 按钮，AppToast.vue），
 * success/info 维持自动消失不可手关（防误触，PRD §M5 F10③）。
 */
export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])

  function push(type: ToastType, message: string): void {
    const id = nextId++
    items.value.push({ id, type, message })
    window.setTimeout(() => remove(id), DURATION_MS[type])
  }

  function success(message: string): void {
    push('success', message)
  }

  function error(message: string): void {
    push('error', message)
  }

  function info(message: string): void {
    push('info', message)
  }

  /** 按 id 移除单条（自动消失计时到点 / error 类手动关闭共用） */
  function remove(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  /** 手动关闭单条（F10③：仅 error 类渲染 × 按钮；success/info 不暴露） */
  function close(id: number): void {
    remove(id)
  }

  return { items, success, error, info, remove, close }
})
