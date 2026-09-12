/**
 * 系统设置页表单引擎纯函数（T57c/F11 拆分自 SettingsView.vue）：
 * 无 Vue / 无 store 依赖，可被 vitest 直接单测（F12）。
 *
 * 口径（保持与拆分前 SettingsView 行为完全一致）：
 * - flattenGroup：后端返回的分组（含嵌套对象/遮蔽密钥/数组）→ 扁平点分键草稿
 *   （遮蔽对象整体保留供徽标展示；数组转 JSON 字符串供 textarea 编辑）；
 * - setNested：点分路径写入嵌套对象（如 'cloud.base_url' → payload.cloud.base_url）；
 * - buildPayload：全量非密钥字段提交负载（secret 跳过 / nullable 空→null /
 *   必填空拦截 / int/float/json 校验分支）。**错误传递**：拆分前以 toast.error
 *   弹首条错误并返回 null；本模块不依赖 toast，改为返回
 *   `{ ok: false, error }`（首条错误文案），调用方负责 Toast——UI 行为一致。
 * - synthesizeGroup / detectKind：未知分组的按值类型探测兜底渲染元数据。
 *
 * 注：buildPayload 全量提交是 T25 时代的保守策略（后端部分键覆盖语义下全量
 * 提交均安全且已验证），本次拆分不改这个口径，仅把纯函数抽出可测。
 */
import type { FieldMeta, FieldKind, GroupMeta, MaskedSecret, SectionMeta } from './configFields'

/** 未知分组字段类型探测（按当前值推断控件：bool/number/字符串/遮蔽对象） */
export function detectKind(value: unknown): FieldKind {
  if (isMaskedSecret(value)) return 'secret'
  if (typeof value === 'boolean') return 'bool'
  if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'float'
  return 'text'
}

/** 是否密钥遮蔽对象（{api_key_env, configured} 形态，config_override.mask_secret_fields 输出） */
export function isMaskedSecret(value: unknown): value is MaskedSecret {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const obj = value as Record<string, unknown>
  return 'api_key_env' in obj && typeof obj.configured === 'boolean'
}

/** 点分路径写入嵌套对象（如 'cloud.base_url' → payload.cloud.base_url） */
export function setNested(target: Record<string, unknown>, dotted: string, value: unknown): void {
  const parts = dotted.split('.')
  let node = target
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i]
    if (typeof node[key] !== 'object' || node[key] === null || Array.isArray(node[key])) {
      node[key] = {}
    }
    node = node[key] as Record<string, unknown>
  }
  node[parts[parts.length - 1]] = value
}

/** flattenGroup 的辅助：把后端返回的分组（含嵌套对象/遮蔽密钥）平铺为点分路径 */
export function flattenGroup(raw: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const walk = (node: Record<string, unknown>, prefix: string): void => {
    for (const [key, value] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${key}` : key
      if (isMaskedSecret(value)) {
        out[path] = value // 遮蔽对象整体保留（徽标展示），不参与提交
      } else if (Array.isArray(value)) {
        // 数组字段（如 providers / vector_stores）保留为 JSON 字符串，便于 textarea 编辑
        out[path] = JSON.stringify(value, null, 2)
      } else if (value !== null && typeof value === 'object') {
        walk(value as Record<string, unknown>, path)
      } else {
        out[path] = value
      }
    }
  }
  walk(raw, '')
  return out
}

/** 构建提交负载的结果：成功携带负载；失败携带首条错误文案（调用方负责 Toast） */
export type BuildPayloadResult =
  | { ok: true; payload: Record<string, unknown> }
  | { ok: false; error: string }

/**
 * 构造提交负载：全量非密钥字段；空可空字段转 null；空必填/非法数值前端拦截。
 * 错误传递：返回 { ok:false, error }（首条错误文案），由调用方弹 Toast——
 * 与拆分前「toast.error 首条错误并中止」的 UI 行为一致。
 * @param meta 分组元数据（已知分组取 GROUP_META；未知分组取 synthesizeGroup 产物）
 * @param flat 分组草稿（flattenGroup 输出形态：点分键 → 值）
 */
export function buildPayload(meta: GroupMeta, flat: Record<string, unknown>): BuildPayloadResult {
  const payload: Record<string, unknown> = {}
  const get = (key: string): unknown => flat[key]
  const textOf = (value: unknown): string => (value === null || value === undefined ? '' : String(value))
  for (const section of meta.sections) {
    for (const field of section.fields) {
      if (field.kind === 'secret') continue // 密钥不参与提交（后端对 api_key 键一律 422）
      const value = get(field.key)
      if (value === null || value === undefined || value === '') {
        if (field.nullable) {
          setNested(payload, field.key, null)
          continue
        }
        return { ok: false, error: `字段 ${meta.group}.${field.key} 不能为空（必填项）` }
      }
      if (field.kind === 'bool') {
        setNested(payload, field.key, value === true || value === 'true')
      } else if (field.kind === 'int') {
        const num = Number(value)
        if (!Number.isInteger(num)) {
          return { ok: false, error: `字段 ${meta.group}.${field.key} 必须为整数` }
        }
        setNested(payload, field.key, num)
      } else if (field.kind === 'float') {
        const num = Number(value)
        if (Number.isNaN(num)) {
          return { ok: false, error: `字段 ${meta.group}.${field.key} 必须为数字` }
        }
        setNested(payload, field.key, num)
      } else if (field.kind === 'json') {
        const text = textOf(value).trim()
        if (!text) {
          setNested(payload, field.key, [])
          continue
        }
        try {
          const parsed = JSON.parse(text)
          if (Array.isArray(parsed) || (parsed !== null && typeof parsed === 'object')) {
            setNested(payload, field.key, parsed)
          } else {
            return { ok: false, error: `字段 ${meta.group}.${field.key} 必须是 JSON 数组或对象` }
          }
        } catch (err) {
          return {
            ok: false,
            error: `字段 ${meta.group}.${field.key} JSON 解析失败：${err instanceof Error ? err.message : String(err)}`,
          }
        }
      } else {
        setNested(payload, field.key, textOf(value))
      }
    }
  }
  return { ok: true, payload }
}

/**
 * 兜底渲染未知分组（GROUP_META 未收录，如后端后续新增分组）：
 * - 以扁平字段的点分路径前缀分组为 section（"cache.audit_cache.enabled" → cache → audit_cache...）；
 * - 字段 label 兜底为「字段名 + 中文注释」；
 * - 类型按当前值探测（secret/bool/int/float/text），未知取值不做枚举猜测。
 * 已知分组永远走 GROUP_META 静态元数据，不会命中此分支。
 */
export function synthesizeGroup(group: string, flat: Record<string, unknown>): GroupMeta {
  const sections: SectionMeta[] = []
  const sectionMap = new Map<string, FieldMeta[]>()
  const topLevel: FieldMeta[] = []
  for (const [key, value] of Object.entries(flat)) {
    const field: FieldMeta = { key, label: `（未知分组字段，类型自动识别）${key}`, kind: detectKind(value) }
    const first = key.split('.')[0] ?? ''
    if (key.includes('.')) {
      if (!sectionMap.has(first)) sectionMap.set(first, [])
      sectionMap.get(first)?.push(field)
    } else {
      topLevel.push(field)
    }
  }
  if (topLevel.length > 0) sections.push({ title: '顶层字段', fields: topLevel })
  for (const [title, fields] of sectionMap) {
    sections.push({ title, fields })
  }
  return {
    group,
    title: group,
    icon: '🧩',
    desc: '后端返回了未收录到 GROUP_META 的分组（配置源码可能已更新）——按响应值自动获取类型渲染；请同步补充字段映射表',
    sections,
    synthetic: true,
  }
}
