/**
 * 共享格式化工具（PRD §M5 F4②：视图间复制的 textOf/fmtTime/shortHash/shortPhash/
 * SOURCE_LABELS 收敛点，T57b 产出）。
 *
 * 统一口径（diff 七处视图实现后选定，2026-08-29）：
 * - textOf：null/undefined → ''。AuditView/KeysView/KeywordsView/ReviewView/
 *   RulesView/WhitelistView/SettingsView 七处实现逐字一致
 *   （`value === null || value === undefined ? '' : String(value)`），无差异可取舍；
 *   EvidencePanel 的 fmtVal 返回 '—' 但函数名/语义不同（面板专用「占位值」），不并入。
 *   展示端统一以 `textOf(x) || '—'` 兜底。
 * - fmtTime：空 → '—'；非法日期 → 原文（不回退猜测）；合法 → toLocaleString()。
 *   差异点：ReviewView 对「上次运行时间为空」有页面专属文案「从未运行」——
 *   该语义留在调用方（见 ReviewView.fmtLastRun），不污染共享函数。
 * - shortHash / shortPhash：同算法、不同默认截断长度（12 / 16，分别来自
 *   AuditView / WhitelistView 的原默认值），各自保留默认，空值 → '—'。
 * - SOURCE_LABELS：schemas.Source 五值 → 中文（自 EvidencePanel 收敛）；
 *   sourceLabel() 提供「未知值回显原文、空值 → '—'」的完整语义。
 */

/** null/undefined → ''；其余 String() 原样（展示端 `|| '—'` 兜底占位） */
export function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** ISO 时间 → 本地可读文本；空 → '—'；非法日期回显原文（不猜测时区/格式） */
export function fmtTime(ts: unknown): string {
  const s = textOf(ts)
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

/** 文本哈希截断（title 显示全文；默认截断 12 位，对齐 AuditView 原实现） */
export function shortHash(value: unknown, len = 12): string {
  const s = textOf(value)
  return s ? (s.length > len ? `${s.slice(0, len)}…` : s) : '—'
}

/** pHash 摘要截断（title 显示全文；默认截断 16 位，对齐 WhitelistView 原实现） */
export function shortPhash(value: unknown, len = 16): string {
  const s = textOf(value)
  return s ? (s.length > len ? `${s.slice(0, len)}…` : s) : '—'
}

/** 判定来源 → 中文（schemas.Source 五值；自 EvidencePanel 收敛，AuditView F7 筛选共用） */
export const SOURCE_LABELS: Record<string, string> = {
  semantic: '语义层',
  llm: 'LLM 兜底',
  basic_rules_pass: '基础规则放行',
  cache: '缓存命中',
  permanent_list: '永久黑白名单',
}

/** 来源值 → 中文文案：空 → '—'；已知五值 → 中文；未知值回显原文（后端演进不破） */
export function sourceLabel(value: unknown): string {
  const s = textOf(value)
  return s ? (SOURCE_LABELS[s] ?? s) : '—'
}
