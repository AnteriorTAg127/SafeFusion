import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 认证状态 store：管理 Token 在 localStorage 的读写。
 * 键名 sf_admin_token（与 PRD 决策 G「登录页 + localStorage 存 X-Admin-Token」一致）。
 */
const TOKEN_KEY = 'sf_admin_token'

export const useAuthStore = defineStore('auth', () => {
  // 初始值直接读 localStorage（刷新页面后保持登录态）
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) ?? '')

  /** 保存 token（内存 + localStorage 双写） */
  function setToken(value: string): void {
    token.value = value
    localStorage.setItem(TOKEN_KEY, value)
  }

  /** 清除 token（登出 / 401 失效时调用） */
  function clearToken(): void {
    token.value = ''
    localStorage.removeItem(TOKEN_KEY)
  }

  return { token, setToken, clearToken }
})