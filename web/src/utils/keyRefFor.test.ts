import { describe, expect, it } from 'vitest'

// keyRef.ts 纯函数单测（T57c/F12，node 环境）：验证前端脱敏/定位口径与后端
// admin.py _mask_key / _resolve_key_ref（admin.py:537-563）一致——
// - maskOf：key[:8] + "…"（后端列表回显形态）
// - keyRefFor：本会话新建（fullKey 提供且与脱敏前缀匹配）→ 完整明文（精确匹配优先）；
//   其余 → 原样返回脱敏前缀（后端前缀唯一匹配兜底，多条 400 / 零条 404 由后端判定）
import { keyRefFor, maskOf } from './keyRef'

describe('keyRef（F12：脱敏 + 行定位，对齐后端 admin.py _mask_key/_resolve_key_ref）', () => {
  it('maskOf 与前 8 位 + "…" 一致（后端 _mask_key 同形态）', () => {
    expect(maskOf('abcdef12ghijklmn')).toBe('abcdef12…')
    expect(maskOf('12345678')).toBe('12345678…')
  })

  it('短 Key：不足 8 位仍按前 8 位截断（与后端 slice 语义一致）', () => {
    expect(maskOf('abc')).toBe('abc…')
  })

  it('keyRefFor：行前缀与本会话新建 Key 脱敏形态一致 → 返回完整明文', () => {
    const full = 'abcdef12ghijklmnop'
    expect(keyRefFor('abcdef12…', full)).toBe(full)
  })

  it('keyRefFor：行前缀不匹配任何明文 → 原样返回脱敏前缀（后端前缀唯一匹配兜底）', () => {
    expect(keyRefFor('zzzz9999…', null)).toBe('zzzz9999…')
    expect(keyRefFor('zzzz9999…', 'abcdef12ghijklmnop')).toBe('zzzz9999…')
  })

  it('keyRefFor：未提供 fullKey（非本会话新建）→ 前缀原样返回', () => {
    expect(keyRefFor('abcdef12…', null)).toBe('abcdef12…')
  })

  it('keyRefFor：边界——行前缀恰好为明文前 8 位但无 "…"（后端完整明文形态）', () => {
    // 完整明文路径：行 key 本身即完整明文时原样透传（不参与前缀匹配）
    expect(keyRefFor('abcdef12ghijklmnop', null)).toBe('abcdef12ghijklmnop')
  })
})
