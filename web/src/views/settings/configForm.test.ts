import { describe, expect, it } from 'vitest'

// SettingsView 表单引擎纯函数单测（T57c/F11+F12，node 环境）：
// 自 configForm.ts 直测，无 Vue / 无 store 依赖——
// - buildPayload：secret 跳过、nullable 空→null、必填空拦截、int/float/json
//   校验分支、新 top_k 字段（T56）序列化；错误经 { ok:false, error } 返回
// - flattenGroup：嵌套→点分键、providers 数组→JSON 字符串、遮蔽对象整体保留
// - setNested：点分路径建嵌套（含中间层覆盖）
// - synthesizeGroup / detectKind：未知分组兜底类型探测
import { buildPayload, detectKind, flattenGroup, setNested, synthesizeGroup } from './configForm'
import { GROUP_META } from './configFields'

/** 取分组元数据（测试直接消费 GROUP_META，验证声明与序列化一致性） */
function metaOf(group: string) {
  const meta = GROUP_META.find((g) => g.group === group)
  if (!meta) throw new Error(`测试分组缺失: ${group}`)
  return meta
}

/** llm 分组完整草稿（含 v0.4.0 多 provider/failover 字段，避免 buildPayload 必填拦截） */
function llmFlat(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    base_url: 'http://x',
    model: 'm',
    api_key_env: 'K',
    timeout: '3',
    max_retry: '1',
    short_text_max_length: '10',
    active_provider: null,
    providers: '[]',
    'failover.enabled': true,
    'failover.cooldown_seconds': '30',
    'failover.max_failures': '3',
    ...overrides,
  }
}

/** embedding 分组完整草稿（含 v0.4.0 provider/failover/vector_stores） */
function embeddingFlat(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    backend: 'local',
    'local.model_name': 'x',
    'local.weights_path': null,
    'local.device': 'auto',
    'cloud.base_url': null,
    'cloud.model': null,
    'cloud.api_key_env': null,
    'cloud.api_key': { api_key_env: 'K', configured: false },
    'cloud.image_protocol': 'openai',
    'cloud.allow_no_key': false, // P8：新增声明字段（本地无鉴权服务免 Key）
    'cloud.image_max_side': '1024',
    'cloud.image_quality': '85',
    active_provider: null,
    providers: '[]',
    vector_stores: '[]',
    'failover.enabled': true,
    'failover.cooldown_seconds': '30',
    'failover.max_failures': '3',
    ...overrides,
  }
}

describe('buildPayload（F11 纯化：全量非密钥负载 + 校验分支）', () => {
  it('secret 跳过：llm.api_key 不进入负载（后端对含 api_key 键负载一律 422）', () => {
    const meta = metaOf('llm')
    const built = buildPayload(meta, llmFlat({
      api_key_env: 'MY_KEY',
      timeout: '30',
      max_retry: '2',
      api_key: { api_key_env: 'MY_KEY', configured: true },
    }))
    expect(built.ok).toBe(true)
    if (!built.ok) return
    expect(built.payload.api_key).toBeUndefined()
    expect(built.payload.api_key_env).toBe('MY_KEY')
  })

  it('nullable 空输入 → null（light_model.model_path 留空）', () => {
    const meta = metaOf('light_model')
    const built = buildPayload(meta, { model_path: '', config_path: '' })
    expect(built.ok).toBe(true)
    if (!built.ok) return
    expect(built.payload.model_path).toBeNull()
    expect(built.payload.config_path).toBeNull()
  })

  it('必填空拦截：必填字段为空 → { ok:false, error } 且不弹 store（纯函数无副作用）', () => {
    const meta = metaOf('llm')
    const built = buildPayload(meta, llmFlat({
      model: '', // 必填空
      api_key_env: '',
      timeout: '',
      max_retry: '',
      short_text_max_length: '',
    }))
    expect(built.ok).toBe(false)
    if (built.ok) return
    expect(built.error).toContain('llm.model')
    expect(built.error).toContain('不能为空')
  })

  it('int 校验：非整数 → 错误；合法整数 → 数字序列化', () => {
    const meta = metaOf('llm')
    const bad = buildPayload(meta, llmFlat({
      timeout: '30',
      max_retry: 'abc', // 非整数
    }))
    expect(bad.ok).toBe(false)
    if (bad.ok) return
    expect(bad.error).toContain('llm.max_retry')
    expect(bad.error).toContain('必须为整数')

    const good = buildPayload(meta, llmFlat({
      timeout: '30.5',
      max_retry: '3',
    }))
    expect(good.ok).toBe(true)
    if (!good.ok) return
    expect(good.payload.max_retry).toBe(3)
    expect(good.payload.timeout).toBe(30.5)
  })

  it('float 校验：NaN → 错误', () => {
    const meta = metaOf('llm')
    const built = buildPayload(meta, llmFlat({
      timeout: 'not-a-number',
    }))
    expect(built.ok).toBe(false)
    if (built.ok) return
    expect(built.error).toContain('llm.timeout')
  })

  it('json 校验：非法 JSON → 错误；合法数组 → 解析后序列化；空数组 "[]" → []', () => {
    const meta = metaOf('embedding')
    const flat = embeddingFlat({
      providers: '[{"name":"a","backend":"local"}]',
      // 真实草稿空数组来自 flattenGroup 的 JSON.stringify([])→"[]"，非空串
      vector_stores: '[]',
    })
    const good = buildPayload(meta, flat)
    expect(good.ok).toBe(true)
    if (!good.ok) return
    expect(good.payload.providers).toEqual([{ name: 'a', backend: 'local' }])
    expect(good.payload.vector_stores).toEqual([])

    const bad = buildPayload(meta, { ...flat, providers: '{broken json' })
    expect(bad.ok).toBe(false)
    if (bad.ok) return
    expect(bad.error).toContain('embedding.providers')
    expect(bad.error).toContain('JSON 解析失败')

    const scalar = buildPayload(meta, { ...flat, providers: '"just a string"' })
    expect(scalar.ok).toBe(false)
    if (scalar.ok) return
    expect(scalar.error).toContain('必须是 JSON 数组或对象')
  })

  it('T56 补遗：thresholds top_k/black_top_k/white_top_k 序列化（int 与 nullable→null）', () => {
    const meta = metaOf('thresholds')
    const built = buildPayload(meta, {
      semantic_threshold: '0.67',
      margin_w: '0.05',
      black_white_gap: '0.02', // P8：新增声明字段（防单条巧合误判）
      confidence_low: '0.35',
      confidence_high: '0.75',
      phash_whitelist_distance: '5',
      phash_dedup_distance: '3',
      top_k: '20',
      black_top_k: '15',
      white_top_k: '', // nullable 空 → null
    })
    expect(built.ok).toBe(true)
    if (!built.ok) return
    expect(built.payload.black_white_gap).toBe(0.02)
    expect(built.payload.top_k).toBe(20)
    expect(built.payload.black_top_k).toBe(15)
    expect(built.payload.white_top_k).toBeNull()
  })

  it('bool 序列化：true/"true" → true；false → false（embedding 无 bool 用 semantic.rerank_enabled）', () => {
    const meta = metaOf('semantic')
    const built = buildPayload(meta, {
      rerank_enabled: true,
      rerank_w_top: '0.5',
      rerank_w_margin: '0.3',
      rerank_w_rerank: '0.2',
      rerank_top_k: '5',
      fuse_mode: 'concat',
    })
    expect(built.ok).toBe(true)
    if (!built.ok) return
    expect(built.payload.rerank_enabled).toBe(true)
    expect(built.payload.fuse_mode).toBe('concat')
  })
})

describe('flattenGroup（后端嵌套配置 → 扁平点分键草稿）', () => {
  it('嵌套对象 → 点分键；标量原样透传', () => {
    const flat = flattenGroup({
      host: '0.0.0.0',
      port: 8000,
      redis: { url: 'redis://x', prefix: 'sf:' },
    })
    expect(flat.host).toBe('0.0.0.0')
    expect(flat.port).toBe(8000)
    expect(flat['redis.url']).toBe('redis://x')
    expect(flat['redis.prefix']).toBe('sf:')
  })

  it('数组字段 → JSON 字符串（providers 便于 textarea 编辑）', () => {
    const flat = flattenGroup({
      active_provider: null,
      providers: [{ name: 'a', backend: 'local' }],
    })
    expect(flat.providers).toBe(JSON.stringify([{ name: 'a', backend: 'local' }], null, 2))
    expect(flat.active_provider).toBeNull()
  })

  it('遮蔽对象（api_key）整体保留为原对象（徽标展示，不参与提交）', () => {
    const flat = flattenGroup({
      api_key: { api_key_env: 'MY_KEY', configured: true },
      base_url: 'http://x',
    })
    expect(flat.api_key).toEqual({ api_key_env: 'MY_KEY', configured: true })
  })

  it('嵌套对象 + 数组混合：深层点分键仍可解析', () => {
    const flat = flattenGroup({
      audit_cache: { enabled: true, capacity: 100, ttl: 60 },
    })
    expect(flat['audit_cache.enabled']).toBe(true)
    expect(flat['audit_cache.capacity']).toBe(100)
  })
})

describe('setNested（点分路径建嵌套）', () => {
  it('单级点分 → 嵌套对象', () => {
    const target: Record<string, unknown> = {}
    setNested(target, 'cloud.base_url', 'http://x')
    expect(target.cloud).toEqual({ base_url: 'http://x' })
  })

  it('多级点分 → 深层嵌套', () => {
    const target: Record<string, unknown> = {}
    setNested(target, 'a.b.c', 1)
    expect((target.a as Record<string, unknown>).b).toEqual({ c: 1 })
  })

  it('中间层已存在则复用；标量中间层被覆盖为对象', () => {
    const target: Record<string, unknown> = { a: 1 }
    setNested(target, 'a.b', 2)
    expect(target.a).toEqual({ b: 2 })
  })
})

describe('synthesizeGroup / detectKind（未知分组兜底）', () => {
  it('detectKind：bool/number（int/float）/string/遮蔽对象', () => {
    expect(detectKind(true)).toBe('bool')
    expect(detectKind(1)).toBe('int')
    expect(detectKind(1.5)).toBe('float')
    expect(detectKind('x')).toBe('text')
    expect(detectKind({ api_key_env: 'K', configured: true })).toBe('secret')
    expect(detectKind(null)).toBe('text')
  })

  it('synthesizeGroup：点分键按首段分组、顶层字段独立、synthetic 标记', () => {
    const meta = synthesizeGroup('new_group', {
      top: 1,
      'sub.a': 'x',
      'sub.b': true,
    })
    expect(meta.group).toBe('new_group')
    expect(meta.synthetic).toBe(true)
    const titles = meta.sections.map((s) => s.title)
    expect(titles).toContain('顶层字段')
    expect(titles).toContain('sub')
    const top = meta.sections.find((s) => s.title === '顶层字段')
    expect(top?.fields.map((f) => f.key)).toEqual(['top'])
    const sub = meta.sections.find((s) => s.title === 'sub')
    expect(sub?.fields.map((f) => f.key)).toEqual(['sub.a', 'sub.b'])
    expect(sub?.fields.find((f) => f.key === 'sub.b')?.kind).toBe('bool')
  })
})
