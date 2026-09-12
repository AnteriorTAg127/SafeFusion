import { describe, expect, it } from 'vitest'

/**
 * F12 基建冒烟测试：验证 vitest 运行器可用（node 环境、TS 转译、断言）。
 * 真实用例由各任务卡（T57a/T57b/T57c）按 PRD §M5 F12 清单补齐，本文件保留作回归哨兵。
 */
describe('vitest 基建冒烟', () => {
  it('可执行 TS 与断言', () => {
    const sum = (a: number, b: number): number => a + b
    expect(sum(2, 3)).toBe(5)
  })

  it('node 环境无 window', () => {
    expect(typeof globalThis.window).toBe('undefined')
  })
})
