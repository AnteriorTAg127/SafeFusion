import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'

/**
 * client.ts 单测（T57a/F2 + T57b/F5，node 环境，假 axios 错误对象驱动 request 失败路径）：
 * - 超时（ECONNABORTED）→ 专属「请勿重复提交」文案 Toast；普通网络错误 → 通用文案
 * - 用户主动取消（ERR_CANCELED）→ 静默（不弹 Toast），错误原样抛出
 * - 默认 15s：未显式传 timeoutMs 时透传 undefined（axios 回落全局 15s）
 * - 显式 timeoutMs 覆盖（apiGet 第三参 options）→ config.timeout 生效
 * - SLOW_TIMEOUT_MS = 120_000 常量导出
 * - signal 透传：外部 AbortController 取消在途请求
 * - T57b/F5 onUnauthorized：注册钩子后 401 → 清 token + 钩子被调；
 *   未注册时 401 仅清 token 不调钩子（router 依赖已移除，无 vi.mock router）
 * - T57b/F6：同错误文案 3s 去重窗（相同文案两次失败只弹一次 Toast）
 */

// vi.hoisted：vitest 会把 vi.mock 调用提升到文件顶部执行，外部变量引用需经
// hoisted 容器注入（否则 factory 闭包访问 toastError 时可能 TDZ 报错）
const mocks = vi.hoisted(() => ({
  toastError: vi.fn(),
  clearToken: vi.fn(),
  unauthorized: vi.fn(),
}))

// F5 解耦后 client.ts 不再 import router，仅需 mock auth/toast（router mock 已删除）
vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ token: 'test-token', clearToken: mocks.clearToken }),
}))

vi.mock('../stores/toast', () => ({
  useToastStore: () => ({ error: mocks.toastError }),
}))

// 延迟引入：等 mock 注册完再加载被测模块
import { apiGet, errorText, http, onUnauthorized, SLOW_TIMEOUT_MS, __resetErrorDedup } from './client'

/** 构造真实 axios 错误（isAxiosError=true 才能走 errorText 的超时分支） */
function makeAxiosError(code: string, message: string): AxiosError {
  return new AxiosError(message, code, undefined, undefined, undefined)
}

/** 构造 401 响应错误（error.response.status = 401） */
function makeUnauthorizedError(): AxiosError {
  const err = makeAxiosError('ERR_BAD_REQUEST', 'Request failed')
  err.response = { status: 401, data: {}, statusText: '', headers: {}, config: {} } as never
  return err
}

/** 构造带响应体 {error}/{detail} 的 axios 错误（驱动 F6 去重文案差异化） */
function makeRespBodyError(status: number, data: unknown): AxiosError {
  const err = makeAxiosError('ERR_BAD_REQUEST', 'Request failed')
  err.response = { status, data, statusText: '', headers: {}, config: {} } as never
  return err
}

describe('client.ts (F2 分级超时 + F5 onUnauthorized + F6 去重)', () => {
  beforeEach(() => {
    mocks.toastError.mockClear()
    mocks.clearToken.mockClear()
    mocks.unauthorized.mockClear()
    // F6 去重窗：清空，保证各用例相同文案的 Toast 断言互不吞掉
    __resetErrorDedup()
    onUnauthorized(null) // 每用例默认不注册钩子（F5：未注册时 401 仅清 token）
  })

  it('导出 SLOW_TIMEOUT_MS = 120_000', () => {
    expect(SLOW_TIMEOUT_MS).toBe(120_000)
  })

  it('默认只读请求未显式传 timeoutMs → config.timeout 为 undefined（回落全局 15s）', async () => {
    const spy = vi.spyOn(http, 'request').mockRejectedValueOnce(
      makeAxiosError('ECONNABORTED', 'timeout of 15000ms exceeded'),
    )
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    const config = spy.mock.calls[0][0] as { timeout?: number }
    expect(config.timeout).toBeUndefined()
    spy.mockRestore()
  })

  it('显式 timeoutMs 覆盖（apiGet 第三参 options）→ config.timeout 生效', async () => {
    const spy = vi.spyOn(http, 'request').mockRejectedValueOnce(
      makeAxiosError('ECONNABORTED', 'timeout of 120000ms exceeded'),
    )
    await expect(apiGet('/keywords/import', undefined, { timeoutMs: 120_000 })).rejects.toBeInstanceOf(AxiosError)
    const config = spy.mock.calls[0][0] as { timeout?: number }
    expect(config.timeout).toBe(120_000)
    spy.mockRestore()
  })

  it('超时（ECONNABORTED）→ 专属「请勿重复提交」文案 Toast', async () => {
    vi.spyOn(http, 'request').mockRejectedValueOnce(
      makeAxiosError('ECONNABORTED', 'timeout of 15000ms exceeded'),
    )
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    expect(mocks.toastError).toHaveBeenCalledTimes(1)
    expect(mocks.toastError.mock.calls[0][0]).toContain('请勿重复提交')
  })

  it('普通网络错误（无响应）→ 通用文案（不含超时专属词）', async () => {
    vi.spyOn(http, 'request').mockRejectedValueOnce(
      makeAxiosError('ERR_NETWORK', 'Network Error'),
    )
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    expect(mocks.toastError).toHaveBeenCalledTimes(1)
    expect(mocks.toastError.mock.calls[0][0]).not.toContain('请勿重复提交')
  })

  it('用户主动取消（ERR_CANCELED）→ 静默不弹 Toast，错误原样抛出', async () => {
    vi.spyOn(http, 'request').mockRejectedValueOnce(makeAxiosError('ERR_CANCELED', 'canceled'))
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    expect(mocks.toastError).not.toHaveBeenCalled()
  })

  it('signal 透传：外部 AbortController 的 signal 进入 axios config', async () => {
    const controller = new AbortController()
    const spy = vi.spyOn(http, 'request').mockRejectedValueOnce(
      makeAxiosError('ERR_CANCELED', 'canceled'),
    )
    await expect(apiGet('/logs', undefined, { signal: controller.signal })).rejects.toBeInstanceOf(AxiosError)
    const config = spy.mock.calls[0][0] as { signal?: AbortSignal }
    expect(config.signal).toBe(controller.signal)
    spy.mockRestore()
  })

  it('F5：注册 onUnauthorized 后 401 → 清 token 且钩子被调用', async () => {
    const handler = mocks.unauthorized
    onUnauthorized(handler)
    // 注意：mock http.request 会绕过拦截器链（401 逻辑在响应拦截器内），
    // 因此改换 adapter 使拒绝走完整拦截器管线（request → adapter → response）
    const original = http.defaults.adapter
    http.defaults.adapter = async () => {
      throw makeUnauthorizedError()
    }
    try {
      await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    } finally {
      http.defaults.adapter = original
    }
    expect(mocks.clearToken).toHaveBeenCalledTimes(1)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('F5：未注册钩子时 401 → 仅清 token，钩子不被调用', async () => {
    const original = http.defaults.adapter
    http.defaults.adapter = async () => {
      throw makeUnauthorizedError()
    }
    try {
      await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    } finally {
      http.defaults.adapter = original
    }
    expect(mocks.clearToken).toHaveBeenCalledTimes(1)
    expect(mocks.unauthorized).not.toHaveBeenCalled()
  })

  it('F6：相同错误文案 3s 内去重 —— 两次失败只弹一次 Toast', async () => {
    const spy = vi
      .spyOn(http, 'request')
      .mockRejectedValueOnce(makeAxiosError('ERR_NETWORK', 'Network Error'))
      .mockRejectedValueOnce(makeAxiosError('ERR_NETWORK', 'Network Error'))
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    // 两次相同文案在去重窗内 → 只弹一条
    expect(mocks.toastError).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })

  it('F6：不同文案不受去重影响 —— 两次失败各弹一条', async () => {
    const errA = makeRespBodyError(400, { error: '错误甲' })
    const errB = makeRespBodyError(400, { error: '错误乙' })
    const spy = vi.spyOn(http, 'request').mockRejectedValueOnce(errA).mockRejectedValueOnce(errB)
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    await expect(apiGet('/logs')).rejects.toBeInstanceOf(AxiosError)
    expect(mocks.toastError).toHaveBeenCalledTimes(2)
    spy.mockRestore()
  })

  it('errorText 导出可用（F4① 收敛点）', () => {
    const err = makeRespBodyError(400, { detail: 'detail 文案' })
    expect(errorText(err)).toBe('detail 文案')
  })
})
