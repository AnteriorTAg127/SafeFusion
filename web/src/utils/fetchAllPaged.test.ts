import { describe, expect, it, vi } from 'vitest'

// fetchAllPaged.ts 顶部 import '../api/client'（默认 fetcher 走 api 客户端）。
// 本测试全部注入假 fetcher，不触发 client 网络路径；但模块加载链仍会执行
// client.ts 顶层的 auth/toast import → 这里与 client.test.ts 同样 mock，
// 避免 node 环境加载 pinia 真实实例（F5 解耦后 client 不再 import router，
// 此处的 router mock 已删除）。
vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ token: 'test-token', clearToken: vi.fn() }),
}))

vi.mock('../stores/toast', () => ({
  useToastStore: () => ({ error: vi.fn() }),
}))

import { fetchAllPaged } from './fetchAllPaged'

/**
 * fetchAllPaged 单测（T57a/F3，纯逻辑 node 环境，注入假 fetcher 驱动，
 * 不经过 axios/网络——与 vitest.config node 环境一致）：
 * - 并发归并顺序：结果按页序 1..N 排列（并发不破坏顺序）
 * - total 收敛即停：累计 ≥ total 后不再拉后续页
 * - maxPages 截断：达到上限仍未拉全 → truncated=true
 * - signal 中止：abort 后失败并停止在飞并发
 * - page_size 上限对齐后端（≤500）
 */

/** 造一页响应：第 p 页产出连续 id 的条目（便于断言归并顺序） */
function makePage(p: number, pageSize: number, total: number) {
  const start = (p - 1) * pageSize
  const count = Math.max(0, Math.min(pageSize, total - start))
  const items = Array.from({ length: count }, (_, i) => ({ id: start + i }))
  return { total, items }
}

/** 记录 fetcher 被调用过的页码（断言收敛即停） */
function makeTracker() {
  const calls: number[] = []
  return { calls, track: (p: number): void => { calls.push(p) } }
}

describe('fetchAllPaged', () => {
  it('拉满全量：多页按页序归并，total 收敛后不再拉多余页', async () => {
    const pageSize = 100
    const total = 350
    const { calls, track } = makeTracker()
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 10,
      fetcher: async ({ page }) => {
        track(page)
        return makePage(page, pageSize, total)
      },
    })
    // 350 条 → 4 页（100+100+100+50）；第 4 页累计 350 ≥ total 后停止，不应拉第 5 页
    expect(calls).toEqual([1, 2, 3, 4])
    expect(res.items.length).toBe(350)
    expect(res.truncated).toBe(false)
    // 顺序断言：按页序归并（id 连续）
    expect(res.items[0].id).toBe(0)
    expect(res.items[99].id).toBe(99)
    expect(res.items[100].id).toBe(100)
    expect(res.items[349].id).toBe(349)
  })

  it('并发批次：第 2~4 页同批发出（≤4 并发），不影响归并顺序', async () => {
    const pageSize = 100
    const total = 1000 // 10 页：第 1 页 + 9 页 → 3 个批次（4+4+1）
    const { calls, track } = makeTracker()
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 10,
      fetcher: async ({ page }) => {
        track(page)
        return makePage(page, pageSize, total)
      },
    })
    expect(calls).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    expect(res.items.length).toBe(1000)
  })

  it('total 收敛即停：尾页未满但累计已达 total 不拉下一页', async () => {
    const pageSize = 500
    const total = 1200 // 3 页（500+500+200）
    const { calls, track } = makeTracker()
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 5,
      fetcher: async ({ page }) => {
        track(page)
        return makePage(page, pageSize, total)
      },
    })
    expect(calls).toEqual([1, 2, 3])
    expect(res.items.length).toBe(1200)
    expect(res.truncated).toBe(false)
  })

  it('maxPages 截断：达到页数上限仍未拉全 → truncated=true', async () => {
    const pageSize = 100
    const total = 10_000 // 远大于 maxPages 能拉到的量
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 3, // 最多 3 页 = 300 条
      fetcher: async ({ page }) => makePage(page, pageSize, total),
    })
    expect(res.items.length).toBe(300)
    expect(res.truncated).toBe(true)
  })

  it('恰好在 maxPages 页内拉全 → truncated=false', async () => {
    const pageSize = 100
    const total = 300 // 恰好 3 页
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 3,
      fetcher: async ({ page }) => makePage(page, pageSize, total),
    })
    expect(res.items.length).toBe(300)
    expect(res.truncated).toBe(false)
  })

  it('单页即全量（total ≤ 首页条数）：不拉后续页', async () => {
    const pageSize = 500
    const total = 200
    const { calls, track } = makeTracker()
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize,
      maxPages: 5,
      fetcher: async ({ page }) => {
        track(page)
        return makePage(page, pageSize, total)
      },
    })
    expect(calls).toEqual([1])
    expect(res.items.length).toBe(200)
    expect(res.truncated).toBe(false)
  })

  it('空数据集：total=0 首页空 → 返回空且 truncated=false', async () => {
    const res = await fetchAllPaged<{ id: number }>('/logs', {
      pageSize: 500,
      maxPages: 5,
      fetcher: async () => ({ total: 0, items: [] as Array<{ id: number }> }),
    })
    expect(res.items).toEqual([])
    expect(res.truncated).toBe(false)
  })

  it('page_size 上限对齐后端（≤500）：超限收敛到 500 防 422', async () => {
    const seen: number[] = []
    await fetchAllPaged<{ id: number }>('/logs', {
      pageSize: 9999, // 超出后端 _MAX_PAGE_SIZE
      maxPages: 2,
      fetcher: async ({ page_size }) => {
        seen.push(page_size)
        return { total: 0, items: [] as Array<{ id: number }> }
      },
    })
    expect(seen).toEqual([500])
  })

  it('page_size 透传：自定义 pageSize（100）在全部页生效（不硬编码 500）', async () => {
    const seen: number[] = []
    const total = 350
    await fetchAllPaged<{ id: number }>('/logs', {
      pageSize: 100,
      maxPages: 10,
      fetcher: async ({ page, page_size }) => {
        seen.push(page_size)
        return makePage(page, page_size, total)
      },
    })
    expect(seen).toEqual([100, 100, 100, 100])
  })

  it('signal 中止：abort 后在飞并发一并失败并抛出（不再发请求）', async () => {
    const controller = new AbortController()
    let afterAbortCalls = 0
    const p = fetchAllPaged<{ id: number }>('/logs', {
      pageSize: 100,
      maxPages: 10,
      signal: controller.signal,
      fetcher: async ({ page }) => {
        // 第 1 页先完成拿 total；第 2~4 页在途时 abort
        if (page === 1) return { total: 1000, items: Array.from({ length: 100 }, (_, i) => ({ id: i })) }
        afterAbortCalls += 1
        controller.abort() // 模拟外部在请求进行中中止
        throw new Error('should not resolve after abort')
      },
    })
    await expect(p).rejects.toThrow()
    expect(afterAbortCalls).toBeGreaterThan(0) // 中止发生在并发批次内
  })
})
