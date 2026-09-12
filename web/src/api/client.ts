import axios, { isAxiosError } from 'axios'
import type { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'

/**
 * 统一 API 客户端（axios 实例）：
 * - baseURL '/admin'：dev 走 vite proxy → :8001，生产同源 mount，全程免 CORS（PRD 决策 H）
 * - 请求拦截器：自动注入 X-Admin-Token（PRD 决策 G）
 * - 响应拦截器：401 → 清 token 并通知「未授权处理器」（F5 解耦：本模块不再依赖
 *   router 单例，跳转逻辑由 main.ts（composition root）经 onUnauthorized 注入；
 *   未注入时仅清 token 不跳转——API 层可脱离路由单测）
 * - 导出泛型辅助函数 apiGet/apiPut/apiPost/apiPatch/apiDelete：非 401 错误自动弹
 *   Toast 后继续抛出，页面可再 catch 做额外处理（如关闭 loading）
 * - T57a/F2 分级超时：每调用可传 options { timeoutMs, signal }；超时（ECONNABORTED）
 *   弹「请勿重复提交」专属文案；用户主动取消（AbortController → ERR_CANCELED）静默
 * - T57b/F6 降噪：相同错误文案 3s 内只弹一条 Toast（后端宕机瞬间多请求并发失败
 *   不再连环弹；模块级 Map<text, lastTs> 记录最近弹出时间）
 */
export const http = axios.create({
  baseURL: '/admin',
  timeout: 15000,
})

/** 慢操作超时：导入/上传/试运行/复核等可能超过默认 15s 的操作（PRD §M5 F2 分级超时） */
export const SLOW_TIMEOUT_MS = 120_000

/** 每次调用可覆盖的请求选项（向后兼容：缺省沿用全局 15s 默认） */
export interface RequestOptions {
  /** 覆盖 axios 全局 timeout（ms） */
  timeoutMs?: number
  /** 外部 AbortSignal：组件卸载/切页/筛选时取消在途请求（F3） */
  signal?: AbortSignal
}

// ---------- F5：401 未授权处理钩子（解耦 router 单例） ----------
/**
 * 401 处理钩子（可为空）：由 main.ts（composition root）注册
 * 「clearToken + 跳登录（带 redirect）」；未注册时 401 仅清 token。
 * 模块级 let + 单槽注册，避免 API 层 import router 形成
 * client → router → views → client 的隐性依赖环（PRD §M5 F5/W1）。
 */
let unauthorizedHandler: (() => void) | null = null

/** 注册 401 未授权处理器（覆盖式单槽；传 null 可注销）。 */
export function onUnauthorized(cb: (() => void) | null): void {
  unauthorizedHandler = cb
}

// ---------- F6：同错误 3s 去重窗（降噪） ----------
/** 相同错误文案最近一次 Toast 的时间戳（模块级，跨请求共享） */
const lastToastTs = new Map<string, number>()
/** 降噪窗：相同文案 3s 内只弹一次（PRD §M5 F6/W2） */
const TOAST_DEDUP_MS = 3_000

/** 弹错误 Toast（带去重窗）：命中窗口内 → 静默跳过；否则记录并弹出 */
function toastErrorDedup(text: string): void {
  const now = Date.now()
  const last = lastToastTs.get(text)
  if (last !== undefined && now - last < TOAST_DEDUP_MS) return
  lastToastTs.set(text, now)
  useToastStore().error(text)
}

/** 测试钩子：清空错误 Toast 去重窗（仅单测 beforeEach 使用，生产不调用）。
 * 需要它的原因：同文件多个用例若在 3s 内产生相同文案（如超时文案），
 * 去重窗会吞掉后续用例的 Toast 断言；测试间重置保证用例隔离。 */
export function __resetErrorDedup(): void {
  lastToastTs.clear()
}

// 请求拦截器：注入管理 Token
http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.set('X-Admin-Token', auth.token)
  }
  return config
})

// 响应拦截器：401 → 清凭证并通知未授权处理器（跳转逻辑在 main.ts）
http.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      const auth = useAuthStore()
      auth.clearToken()
      unauthorizedHandler?.()
    }
    return Promise.reject(error)
  },
)

/** 是否用户主动取消（AbortController 中止）：此类失败静默处理，不弹任何 Toast */
function isUserAbort(error: unknown): boolean {
  return isAxiosError(error) && (error.code === 'ERR_CANCELED' || axios.isCancel(error))
}

/**
 * 从错误对象提取可读信息（F4① 收敛点：client.ts 私有 readableError 改名导出，
 * 兼容 FastAPI 认证的 detail 字段与管理侧自定义 {error} 响应体）：
 * - 超时（ECONNABORTED / message 含 timeout）→ 「请勿重复提交」专属文案
 * - 响应体 {detail}（string）优先；{detail} 数组（422 校验错误）→ 通用参数文案；
 *   {error}（string）其次
 * - 401 → 「Token 无效或已过期」；其余 → axios message / Error.message / 兜底
 */
export function errorText(error: unknown): string {
  if (isAxiosError(error)) {
    // 超时专属文案：axios 超时抛 ECONNABORTED（message 含 'timeout of ... ms'）；
    // 与用户主动取消（ERR_CANCELED）严格区分——取消由 isUserAbort 拦截，不弹 Toast
    if (error.code === 'ECONNABORTED' || /timeout/i.test(error.message ?? '')) {
      return '请求超时：后端可能仍在后台执行，请勿重复提交，可稍后到审核记录/列表确认结果'
    }
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

/** 统一请求入口：成功返回响应体；失败弹 Toast（401 与用户取消除外）并继续抛出 */
async function request<T>(config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await http.request<T>(config)
    return response.data
  } catch (error) {
    // 用户主动取消（组件卸载/切页 AbortController）：静默，避免取消产生噪音 Toast
    if (isUserAbort(error)) throw error
    // 401 已由响应拦截器统一处理（清 token + 通知跳登录），此处不再重复弹 Toast
    const status = isAxiosError(error) ? error.response?.status : undefined
    if (status !== 401) {
      // F6 降噪：相同文案 3s 去重（宕机瞬间并发失败只弹一条）
      toastErrorDedup(errorText(error))
    }
    throw error
  }
}

export function apiGet<T>(
  url: string,
  params?: Record<string, unknown>,
  options?: RequestOptions,
): Promise<T> {
  return request<T>({ url, method: 'GET', params, timeout: options?.timeoutMs, signal: options?.signal })
}

export function apiPost<T>(
  url: string,
  data?: unknown,
  params?: Record<string, unknown>,
  options?: RequestOptions,
): Promise<T> {
  return request<T>({ url, method: 'POST', data, params, timeout: options?.timeoutMs, signal: options?.signal })
}

export function apiPut<T>(url: string, data?: unknown, options?: RequestOptions): Promise<T> {
  return request<T>({ url, method: 'PUT', data, timeout: options?.timeoutMs, signal: options?.signal })
}

export function apiPatch<T>(url: string, data?: unknown, options?: RequestOptions): Promise<T> {
  return request<T>({ url, method: 'PATCH', data, timeout: options?.timeoutMs, signal: options?.signal })
}

export function apiDelete<T>(
  url: string,
  params?: Record<string, unknown>,
  options?: RequestOptions,
): Promise<T> {
  return request<T>({ url, method: 'DELETE', params, timeout: options?.timeoutMs, signal: options?.signal })
}
