/**
 * 后端契约类型（T37 新增，供 EvidencePanel / TrialView 等共享）：
 * 对齐 src/safefusion/models/schemas.py（AuditResult 及其 detail 子模型）与
 * src/safefusion/api/admin.py（GET /admin/test-examples、POST /admin/test-audit）。
 *
 * 字段以实际读到的后端源码为准；后端新增字段不会破坏本类型（仅只读访问已知键）。
 */

/** 判定来源枚举（schemas.Source 五值） */
export type AuditSource =
  | 'semantic'
  | 'llm'
  | 'basic_rules_pass'
  | 'cache'
  | 'permanent_list'

/** detail.keyword.hits 元素（schemas.KeywordHitModel） */
export interface KeywordHit {
  keyword: string
  category: string
  matched: string
  start: number
  end: number
}

/** detail.keyword.regex_filtered 元素（schemas.RegexFilteredHit） */
export interface RegexFilteredHit {
  keyword: string
  category: string
  matched: string
  reason: string
}

/** detail.keyword（schemas.KeywordDetail） */
export interface KeywordDetail {
  hits: KeywordHit[]
  regex_filtered: RegexFilteredHit[]
}

/** detail.light_model（schemas.LightModelResult，fasttext 轻量文本模型） */
export interface LightModelResult {
  label: string
  score: number
  violation: boolean
}

/** detail.image_whitelist 元素（schemas.ImageWhitelistHit，逐帧） */
export interface ImageWhitelistHit {
  frame: number
  hit: boolean
  distance: number | null
}

/** detail.semantic.black_top 元素（schemas.SemanticTopHit） */
export interface SemanticTopHit {
  id: string
  score: number
  category: string | null
}

/** detail.semantic（schemas.SemanticDetail） */
export interface SemanticDetail {
  black_top: SemanticTopHit[]
  black_avg: number
  white_avg: number
  /** 黑均−白均差值；为 null 表示语义层降级（orchestrator 降级时置 None） */
  margin: number | null
  /**
   * 生效阈值：后端当前未返回（仅 black_avg/white_avg/margin，见
   * orchestrator._semantic_detail）；预留字段，若后端补齐则面板直接渲染。
   */
  threshold?: number | null
}

/** detail.llm（schemas.LLMDetail） */
export interface LLMDetail {
  is_violation: boolean
  category: string | null
  confidence: number | null
  reason: string | null
}

/** detail（schemas.AuditDetail） */
export interface AuditDetail {
  keyword?: KeywordDetail | null
  light_model?: LightModelResult | null
  image_whitelist?: ImageWhitelistHit[] | null
  semantic?: SemanticDetail | null
  llm?: LLMDetail | null
  /**
   * 降级标记字符串（如 "semantic:lazy_pending"）：仅存在于审计日志
   * detail_json（orchestrator 写入，AuditDetail 无此字段→API 响应不携带，
   * 见 tests/test_orchestrator.py TestSemanticDegradation）；面板据此 + 语义
   * margin===null 双重检测降级状态。
   */
  degraded?: string | null
}

/** 审核响应体（schemas.AuditResult，POST /admin/test-audit 直接返回 model_dump） */
export interface AuditResult {
  request_id: string
  timestamp: string
  has_violation: boolean
  confidence: number
  category: string | null
  source: AuditSource
  cache_hit: boolean
  detail: AuditDetail | null
}

/** GET /admin/test-examples 条目（含 pool 标注） */
export interface TrialExample {
  text: string
  pool: 'black' | 'white'
}

/** GET /admin/test-examples 响应 */
export interface ExamplesResponse {
  items: TrialExample[]
  total: number
}