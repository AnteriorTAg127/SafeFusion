import { beforeEach, describe, expect, it } from 'vitest'
import { AxiosError } from 'axios'

/**
 * errorText 单测（T57b/F4①，node 环境，假 axios 错误对象驱动）：
 * - 双错误体：{detail}（string）优先；{error}（string）其次
 * - {detail} 数组（422 校验错误）→ 通用「请求参数不合法」
 * - 超时（ECONNABORTED / message 含 timeout）→ 「请勿重复提交」专属文案
 * - 401 → 「Token 无效或已过期」；普通网络错误 → message 原文
 * - 非 axios 错误：Error.message / 兜底文案
 */
import { errorText, __resetErrorDedup } from '../api/client'

/** 构造带响应体的 axios 错误（isAxiosError=true，error.response.data 可注入） */
function makeRespError(status: number, data: unknown): AxiosError {
  const err = new AxiosError('Request failed', 'ERR_BAD_REQUEST', undefined, undefined, {
    status,
    data,
    statusText: '',
    headers: {},
    config: {},
  } as never)
  return err
}

/** 构造无响应体的 axios 错误（网络层失败：code/message 可指定） */
function makeNetError(code: string, message: string): AxiosError {
  return new AxiosError(message, code, undefined, undefined, undefined)
}

describe('errorText（F4① 双错误体 / 超时 / 取消文案）', () => {
  beforeEach(() => {
    // 去重窗仅影响 request() 的 Toast，不参与 errorText 纯函数断言；重置保证隔离
    __resetErrorDedup()
  })

  it('{detail} 字符串优先（FastAPI 认证错误体）', () => {
    const err = makeRespError(422, { detail: 'Token 无效' })
    expect(errorText(err)).toBe('Token 无效')
  })

  it('{error} 字符串（管理侧自定义错误体）', () => {
    const err = makeRespError(400, { error: '词条不存在: id=1' })
    expect(errorText(err)).toBe('词条不存在: id=1')
  })

  it('{detail} 数组（422 校验错误）→ 通用参数文案', () => {
    const err = makeRespError(422, { detail: [{ loc: ['query', 'page'], msg: 'int 解析失败' }] })
    expect(errorText(err)).toBe('请求参数不合法')
  })

  it('detail 与 error 并存时 detail 优先', () => {
    const err = makeRespError(400, { detail: 'detail 文案', error: 'error 文案' })
    expect(errorText(err)).toBe('detail 文案')
  })

  it('超时（ECONNABORTED）→ 「请勿重复提交」专属文案', () => {
    const err = makeNetError('ECONNABORTED', 'timeout of 15000ms exceeded')
    expect(errorText(err)).toContain('请勿重复提交')
  })

  it('message 含 timeout 字样的网络错误也走超时文案（axios 兼容路径）', () => {
    const err = makeNetError('ERR_NETWORK', 'timeout of 120000ms exceeded')
    expect(errorText(err)).toContain('请勿重复提交')
  })

  it('401 无响应体 → 「Token 无效或已过期」', () => {
    const err = makeNetError('ERR_BAD_REQUEST', 'Request failed')
    err.response = { status: 401 } as never
    expect(errorText(err)).toBe('Token 无效或已过期')
  })

  it('普通网络错误 → axios message 原文', () => {
    const err = makeNetError('ERR_NETWORK', 'Network Error')
    expect(errorText(err)).toBe('Network Error')
  })

  it('非 axios 错误：Error.message', () => {
    expect(errorText(new Error('自定义异常'))).toBe('自定义异常')
  })

  it('非 axios 错误：非 Error 兜底文案', () => {
    expect(errorText('字符串错误')).toBe('请求失败')
  })
})
