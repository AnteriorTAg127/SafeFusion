/**
 * API Key 脱敏与行定位纯函数（T57c/F12：自 KeysView 本地实现纯化，供单测直测）。
 *
 * 对齐后端 src/safefusion/api/admin.py：
 * - _mask_key（admin.py:537-540）：`key[:8] + "…"`——列表脱敏前缀展示形态；
 * - _resolve_key_ref（admin.py:543-563）：PATCH/DELETE 路径参数支持完整明文或
 *   列表脱敏前缀唯一匹配——精确匹配优先；未命中时清理尾部非字母数字字符
 *   （如 "…"）后做前缀唯一匹配（多条命中 400 / 零命中 404 由后端判定）。
 *
 * 前端收敛点：KeysView 的 keyRefFor 调用本模块，保证「本会话新建 Key 走内存
 * 明文、其余行走脱敏前缀」的口径可单测、与后端脱敏规则保持一致。
 */

/** Key 脱敏（对齐后端 admin.py _mask_key）：仅显示前 8 位 + "…" */
export function maskOf(fullKey: string): string {
  return `${fullKey.slice(0, 8)}…`
}

/**
 * 解析某行可用的管理标识（PATCH/DELETE 路径参数）：
 * - rowKey：列表行脱敏前缀（后端 GET /admin/keys 返回形态，如 "abc12345…"）；
 * - fullKey：可选的本会话新建 Key 明文（内存暂存，未提供时为 null）；
 * 当 rowKey 与 fullKey 的脱敏形态一致时，返回完整明文（后端精确匹配优先），
 * 否则原样返回脱敏前缀（后端 _resolve_key_ref 前缀唯一匹配兜底）。
 */
export function keyRefFor(rowKey: string, fullKey: string | null): string {
  if (fullKey && rowKey === maskOf(fullKey)) {
    return fullKey
  }
  return rowKey
}
