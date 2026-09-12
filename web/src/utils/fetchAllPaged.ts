import { apiGet } from '../api/client'

/**
 * fetchAllPaged —— 并发受限的全量分页抓取器（T57a/F3 共享工具）。
 *
 * 为什么需要：审计/概览/词库三处原为「串行 for 循环逐页拉取」，
 * 大库（2 万条 / 5 万条）下页面转圈数秒到数十秒；本工具改为
 * 「先拉第 1 页拿 total → 剩余页并发（≤4）拉取 → 按页序归并」，
 * total 收敛即停，避免多拉空页。
 *
 * 口径（与后端对齐）：
 * - page_size 上限 500：管理端分页依赖 `_MAX_PAGE_SIZE = 500`
 *   （src/safefusion/api/dependencies.py），传更大值会被 FastAPI 422；
 *   本工具对 pageSize 做 min(pageSize, 500) 收敛，防御性对齐。
 * - truncated 语义：到达 maxPages 页但累计条数仍未达到 total → 结果被
 *   maxPages 截断（近似值，非全量）；调用方应据此在 UI 注明口径
 *   （如「最多拉取 N 页 × M 条，超限截断近似」）。
 * - total 收敛即停：任一时刻累计条数 ≥ 最新响应里的 total 即停止
 *   （total 可能随并发写入增长，收敛条件是充分条件）。
 *
 * 取消：signal 透传给底层 axios（client 的 RequestOptions.signal），
 * 中止时在飞并发一并失败并抛出（由调用方静默处理）。
 */

/** 每页拉取的分页参数（与后端 Page 结构对齐：page / page_size） */
export interface FetchPageParams {
  page: number
  page_size: number
}

/** fetchAllPaged 调用配置 */
export interface FetchAllPagedOptions<T> {
  /** 每页附加查询参数（如 start/end/has_violation），分页参数由本工具注入 */
  params?: Record<string, unknown>
  /** 每页条数：后端上限 500；不传默认 500 */
  pageSize?: number
  /** 最大拉取页数：防御性上限，防止 total 异常/缺失时无限拉取 */
  maxPages: number
  /** 外部 AbortSignal：中止在飞并发（组件卸载/切页/筛选时） */
  signal?: AbortSignal
  /** 单页拉取器（测试注入用；缺省走统一 api 客户端：axios + X-Admin-Token） */
  fetcher?: (params: FetchPageParams) => Promise<{ total: number; items: T[] }>
}

/** 抓取结果 */
export interface FetchAllResult<T> {
  /** 归并后的全量条目（按页序 1..N 排列） */
  items: T[]
  /** 是否被 maxPages 截断（累计条数未达 total） */
  truncated: boolean
}

/** 并发上限：4 路并行拉页（背压，避免打爆服务端；PRD §M5 F3 要求） */
const CONCURRENCY = 4
/** 后端分页单页上限（src/safefusion/api/dependencies.py `_MAX_PAGE_SIZE`） */
const DEFAULT_PAGE_SIZE = 500

/**
 * 分页批量执行器：把 [1..count] 拆成 ≤CONCURRENCY 的并发批次，按序收集结果。
 * @param load 单页加载函数（页码 → 响应）；page_size 由调用方闭包固定
 */
async function runBatches<T>(
  count: number,
  signal: AbortSignal | undefined,
  load: (page: number) => Promise<{ total: number; items: T[] }>,
): Promise<Array<{ total: number; items: T[] }>> {
  const out: Array<{ total: number; items: T[] }> = new Array(count)
  for (let start = 1; start <= count; start += CONCURRENCY) {
    const batch: number[] = []
    for (let p = start; p <= Math.min(count, start + CONCURRENCY - 1); p++) batch.push(p)
    const settled = await Promise.all(
      batch.map(async (p) => {
        // 中止信号已在途：直接抛错让 Promise.all 快速失败，不再发请求
        if (signal?.aborted) throw signal.reason ?? new Error('aborted')
        return load(p)
      }),
    )
    settled.forEach((res, i) => {
      out[start - 1 + i] = res
    })
  }
  return out
}

/**
 * 全量分页抓取：第 1 页拿 total → 剩余页并发（≤4）→ 按页序归并。
 * @param url 分页端点（如 '/logs'、'/keywords'），默认 fetcher 在此追加分页参数
 * @returns 全量条目 + truncated 标志（maxPages 截断语义见文件头注释）
 */
export async function fetchAllPaged<T>(
  url: string,
  options: FetchAllPagedOptions<T>,
): Promise<FetchAllResult<T>> {
  // pageSize 上限 500 对齐后端（dependencies.py `_MAX_PAGE_SIZE`），超出收敛防 422
  const pageSize = Math.min(options.pageSize ?? DEFAULT_PAGE_SIZE, DEFAULT_PAGE_SIZE)
  const { maxPages, signal, params } = options
  // 缺省单页拉取器：统一 api 客户端（自动带头 X-Admin-Token），分页参数与附加参数合并
  const fetcher =
    options.fetcher ??
    (async (p: FetchPageParams): Promise<{ total: number; items: T[] }> =>
      apiGet<{ total: number; items: T[] }>(url, { ...params, ...p }, { signal }))

  // 第 1 页：既取数据又拿 total（并发调度的前提）
  const first = await fetcher({ page: 1, page_size: pageSize })
  const firstTotal = first.total
  const firstItems = first.items

  // 防御：total 为 0 或首页即空 → 无后续页
  if (firstTotal <= firstItems.length || firstItems.length === 0) {
    return { items: firstItems, truncated: false }
  }

  // 剩余页数：ceil((total - 已拉条数) / pageSize)，但不超过 maxPages 上限
  const restCount = Math.min(Math.ceil((firstTotal - firstItems.length) / pageSize), maxPages - 1)
  if (restCount <= 0) {
    return { items: firstItems, truncated: firstTotal > firstItems.length }
  }

  // 并发拉剩余页：批次序号 p 为 1..restCount，实际页码需偏移 +1（第 1 页已在上一步拉过）
  const pages = await runBatches(restCount, signal, (p) =>
    fetcher({ page: p + 1, page_size: pageSize }),
  )

  // 按页序归并（runBatches 已保序），total 收敛即停：
  // 累计条数 ≥ 最新响应 total 说明已拉全（total 可能随并发写入增长，收敛条件是充分条件）
  const items = [...firstItems]
  let truncated = false
  for (const res of pages) {
    if (items.length >= res.total) break
    items.push(...res.items)
    if (res.items.length === 0) break // 防御：空页即止（total 异常时防死循环）
  }
  // 收敛判定：若已拉满 maxPages 页仍未达到最后已知 total → 截断
  if (items.length < (pages.length > 0 ? pages[pages.length - 1].total : firstTotal)) {
    truncated = true
  }
  return { items, truncated }
}
