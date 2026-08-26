import axios, { isAxiosError } from 'axios'
import type { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios'
import router from '../router'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'

/**
 * 统一 API 客户端（axios 实例）：
 * - baseURL '/admin'：dev 走 vite proxy → :8001，生产同源 mount，全程免 CORS（PRD 决策 H）
 * - 请求拦截器：自动注入 X-Admin-Token（PRD 决策 G）
 * - 响应拦截器：401 → 清 token 并跳转登录页
 * - 导出泛型辅助函数 apiGet/apiPut/apiPost/apiDelete：非 401 错误自动弹 Toast 后继续抛出，
 *   页面可再 catch 做额外处理（如关闭 loading）
 */
export const http = axios.create({
  baseURL: '/admin',
  timeout: 15000,
})

// 请求拦截器：注入管理 Token
http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.set('X-Admin-Token', auth.token)
  }
  return config
})

// 响应拦截器：401 → 清除凭证并回登录页
http.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      const auth = useAuthStore()
      auth.clearToken()
      // 已在登录页则不重复跳转（避免登录验证失败时产生无意义导航）
      if (router.currentRoute.value.name !== 'login') {
        void router.push({
          name: 'login',
          query: { redirect: router.currentRoute.value.fullPath },
        })
      }
    }
    return Promise.reject(error)
  },
)

/** 从错误对象提取可读信息：兼容 FastAPI 认证的 detail 字段与管理侧自定义 {error} 响应体 */
function readableError(error: unknown): string {
  if (isAxiosError(error)) {
    const data = error.response?.data
    if (data && typeof data === 'object') {
      const detail = (data as { detail?: unknown }).detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) return '请求参数不合法'
      const errMsg = (data as { error?: unknown }).error
      if (typeof errMsg === 'string') return errMsg
    }
    if (error.response?.status === 401) return 'Token 无效或已过期'
    return error.message || '请求失败'
  }
  return error instanceof Error ? error.message : '请求失败'
}

/** 统一请求入口：成功返回响应体；失败弹 Toast（401 除外）并继续抛出 */
async function request<T>(config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await http.request<T>(config)
    return response.data
  } catch (error) {
    // 401 已由响应拦截器统一处理（清 token + 跳登录），此处不再重复弹 Toast
    const status = isAxiosError(error) ? error.response?.status : undefined
    if (status !== 401) {
      useToastStore().error(readableError(error))
    }
    throw error
  }
}

export function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request<T>({ url, method: 'GET', params })
}

export function apiPost<T>(url: string, data?: unknown, params?: Record<string, unknown>): Promise<T> {
  return request<T>({ url, method: 'POST', data, params })
}

export function apiPut<T>(url: string, data?: unknown): Promise<T> {
  return request<T>({ url, method: 'PUT', data })
}

export function apiDelete<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return request<T>({ url, method: 'DELETE', params })
}