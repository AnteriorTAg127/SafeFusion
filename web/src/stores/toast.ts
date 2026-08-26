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

/**
 * 全局 Toast store：页面/API 层调用 success/error/info 弹出提示，
 * 默认自动消失（success/info 3s，error 4s，便于阅读错误信息）。
 */
export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])

  function push(type: ToastType, message: string, duration: number): void {
    const id = nextId++
    items.value.push({ id, type, message })
    window.setTimeout(() => remove(id), duration)
  }

  function success(message: string): void {
    push('success', message, 3000)
  }

  function error(message: string): void {
    push('error', message, 4000)
  }

  function info(message: string): void {
    push('info', message, 3000)
  }

  function remove(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  return { items, success, error, info, remove }
})