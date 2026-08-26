<script setup lang="ts">
/**
 * 系统设置页（T25，全量配置自定义表单）：
 * - 按 GET /admin/config 实际返回的分组渲染「group-card」表单卡片：已知分组
 *   （server / thresholds / embedding / llm / cache / light_model / logging /
 *   image / keyword / semantic / review，对应 config_override.get_config_groups()
 *   白名单）取 GROUP_META 静态元数据；后端若返回未收录分组则 synthesizeGroup
 *   按值自动探测字段类型渲染（分组白名单会随配置模型演进，页面不破）。
 * - 字段控件类型映射（依据 src/safefusion/config.py 各分组模型字段注解）：
 *   bool → 开关(switch)；int/float → number input；枚举取值 → select 下拉
 *   （embedding.backend local|cloud、cache.backend memory|redis、
 *   semantic.fuse_mode pool|concat|weighted_avg、image.animated.mode
 *   uniform|first、logging.level DEBUG/INFO/WARNING/ERROR、device auto|cpu|cuda）；
 *   其余字符串 → text input。
 * - 字段 label 静态映射自 config.py 的 pydantic Field(description=...)（GET 响应
 *   不含描述，故以配置源码为准前置于本文件 FIELD_META）；未收录字段兜底用
 *   「字段名 + 中文注释」。
 * - Key 遮蔽（决策 F）：GET 返回形如 {api_key_env, configured} 的 api_key 字段
 *   （llm.api_key / embedding.cloud.api_key）→ 只读徽标「环境变量: <名> ·
 *   已配置/未配置」，不允许编辑值；保存时该字段不参与提交（后端
 *   validate_group_update 对任何含 api_key 键的负载一律 422 拒绝）。
 * - 保存：PUT /admin/config/{group}，body 为分组表单值；后端 save_overrides
 *   是「整体替换该分组覆盖层」而非深度合并（见 config_override.save_overrides），
 *   故前端提交**全量非密钥字段**（含未改动字段当前有效值），避免部分键提交
 *   导致同组其余覆盖项被覆盖层替换丢失；空对象 {}（恢复默认按钮）删除覆盖层。
 * - 422 错误文案由 api client 统一 Toast（兼容 {error} 与 {detail} 两种错误体），
 *   前端不再重复弹窗。
 * - TODO(歧义-已知后端行为)：
 *   1) save_overrides 整组替换：若后端后续改为深度合并部分键，本页可改回仅提交
 *      改动字段（当前全量提交在两种语义下均安全）；
 *   2) logging.level / image.animated.mode / embedding.local.device 后端暂无
 *      白名单校验（仅业务规则校验 backend/fuse_mode），select 提供已知合法值，
 *      若服务端返回未知取值则追加「(未知)」选项展示，不静默丢失；
 *   3) 密钥 configured 布尔反映「当前进程环境变量是否已设置」，前端无法也无需
 *      修改（决策 F 只读），配置保存后需重启服务生效（决策 E）。
 */
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPut } from '../api/client'
import { useToastStore } from '../stores/toast'

/** 密钥遮蔽对象（config_override.mask_secret_fields 输出形态） */
interface MaskedSecret {
  api_key_env: string | null
  configured: boolean
}

/** GET /admin/config → { 分组名: 分组配置（api_key 已遮蔽） } */
type ConfigResponse = Record<string, Record<string, unknown>>

/** PUT /admin/config/{group} 响应结构（api/admin.py update_config） */
interface ConfigPutResult {
  group: string
  config: Record<string, unknown>
  saved: boolean
  restart_required: boolean
  deleted_override?: boolean
}

type FieldKind = 'bool' | 'int' | 'float' | 'text' | 'select' | 'secret'

/** 字段元数据（label 来自 config.py description，类型来自模型注解） */
interface FieldMeta {
  key: string
  label: string
  kind: FieldKind
  /** select 取值白名单（对齐 config.py / config_override.py 合法值） */
  options?: string[]
  /** 后端为 str | None 可空字段：空输入提交 null（未配置） */
  nullable?: boolean
  /** 数值输入 min/max 提示（对齐 config_override._RANGE_RULES [0,1] 组） */
  min?: number
  max?: number
  hint?: string
}

interface SectionMeta {
  title: string
  desc?: string
  fields: FieldMeta[]
}

interface GroupMeta {
  group: string
  title: string
  icon: string
  desc?: string
  sections: SectionMeta[]
  /** 由 synthesizeGroup 生成的兜底分组（GROUP_META 未收录）标记 */
  synthetic?: boolean
}

/**
 * 分组字段清单（依据 src/safefusion/config.py 各模型字段与 description）。
 * 注意：所有 str 字段（除 null 可空外）后端 validate_group_update 会做必填
 * 非空校验，故 label 里同步标注「必填」。
 */
const GROUP_META: GroupMeta[] = [
  {
    group: 'server',
    title: '服务监听',
    icon: '🖥️',
    desc: '审核/管理双端口监听配置（内置默认 < config.yaml < 覆盖层 < 环境变量）',
    sections: [
      {
        title: '服务监听',
        fields: [
          { key: 'host', label: 'host：审核 API 监听地址（必填）', kind: 'text' },
          { key: 'port', label: 'port：审核 API 端口（:8000，必填）', kind: 'int', min: 1, max: 65535 },
          { key: 'admin_port', label: 'admin_port：管理 API 端口（:8001，必填）', kind: 'int', min: 1, max: 65535 },
        ],
      },
    ],
  },
  {
    group: 'thresholds',
    title: '判定阈值',
    icon: '🎯',
    desc: '语义层判定阈值与置信度分档（范围 [0,1]，对数轴脱敏后由用户校准）',
    sections: [
      {
        title: '判定阈值',
        fields: [
          { key: 'semantic_threshold', label: '语义层判定违规的相似度阈值（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'margin_w', label: '黑均分−白均分差值与 margin 的比较基准（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'confidence_low', label: '置信度低档上界，低于则判定安全（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'confidence_high', label: '置信度高档下界，高于则判定违规（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'phash_whitelist_distance', label: '图片白名单 pHash 汉明距离阈值（必填）', kind: 'int', min: 0, max: 64 },
          { key: 'phash_dedup_distance', label: '图片去重缓存近似命中 pHash 阈值（必填）', kind: 'int', min: 0, max: 64 },
        ],
      },
    ],
  },
  {
    group: 'embedding',
    title: 'Embedding 双后端',
    icon: '🧬',
    desc: 'backend 切换 local/cloud；云端 Key 仅环境变量注入，此处只显示变量名',
    sections: [
      {
        title: '后端选择',
        fields: [
          {
            key: 'backend',
            label: 'backend（必填）',
            kind: 'select',
            options: ['local', 'cloud'],
            hint: '切换至 cloud 时需填齐云端 base_url/model（后端必填校验），且 fuse_mode 需为 concat',
          },
        ],
      },
      {
        title: '本地后端（local）',
        fields: [
          { key: 'local.model_name', label: 'HF 模型名或本地权重标识（必填）', kind: 'text' },
          { key: 'local.weights_path', label: '本地权重目录；null 使用 HF 缓存', kind: 'text', nullable: true },
          {
            key: 'local.device',
            label: 'device（必填）',
            kind: 'select',
            options: ['auto', 'cpu', 'cuda'],
            hint: 'auto（GPU 可用则用）| cpu | cuda',
          },
        ],
      },
      {
        title: '云端后端（cloud）',
        fields: [
          { key: 'cloud.base_url', label: '云端 Embedding API base_url', kind: 'text', nullable: true },
          { key: 'cloud.model', label: '云端 embedding 模型名', kind: 'text', nullable: true },
          {
            key: 'cloud.api_key_env',
            label: '云端 Key 环境变量名',
            kind: 'text',
            nullable: true,
            hint: 'null 时仅认 SAFEFUSION_EMBEDDING_API_KEY',
          },
          { key: 'cloud.api_key', label: '云端 Key（密钥，仅显示变量名）', kind: 'secret' },
        ],
      },
    ],
  },
  {
    group: 'llm',
    title: 'LLM 兜底',
    icon: '🤖',
    desc: 'OpenAI 兼容 LLM 兜底；api_key 仅环境变量注入，此处只显示变量名',
    sections: [
      {
        title: 'LLM 兜底',
        fields: [
          { key: 'base_url', label: 'OpenAI 兼容服务地址（必填）', kind: 'text' },
          { key: 'model', label: '兜底模型名（必填）', kind: 'text' },
          {
            key: 'api_key_env',
            label: 'Key 环境变量名（必填）',
            kind: 'text',
            hint: '其实也认 SAFEFUSION_LLM_API_KEY（优先级更高）',
          },
          { key: 'timeout', label: '单次调用超时（秒，必填）', kind: 'float', min: 0 },
          { key: 'max_retry', label: 'JSON 输出解析失败重试次数（必填）', kind: 'int', min: 0 },
          { key: 'short_text_max_length', label: '短文本 LLM 缓存判定的文本长度上限（必填）', kind: 'int', min: 1 },
          { key: 'api_key', label: 'LLM Key（密钥，仅显示变量名）', kind: 'secret' },
        ],
      },
    ],
  },
  {
    group: 'cache',
    title: '五级缓存',
    icon: '🗃️',
    desc: 'backend 切换 memory/redis；每级缓存可独立开关、容量、TTL',
    sections: [
      {
        title: '缓存后端',
        fields: [
          {
            key: 'backend',
            label: 'backend（必填）',
            kind: 'select',
            options: ['memory', 'redis'],
            hint: 'memory（进程内，默认）| redis（需提供下方 Redis 连接）',
          },
          { key: 'redis.url', label: 'Redis 连接 URL（必填）', kind: 'text' },
          { key: 'redis.prefix', label: '缓存键统一前缀（必填）', kind: 'text' },
        ],
      },
      {
        title: '① 审核缓存',
        desc: '完整键（文本哈希+帧哈希+关键参数）',
        fields: [
          { key: 'audit_cache.enabled', label: '关卡：关闭时该级缓存直通', kind: 'bool' },
          { key: 'audit_cache.capacity', label: '容量上限（条目数，必填）', kind: 'int', min: 0 },
          { key: 'audit_cache.ttl', label: 'TTL（秒，必填）', kind: 'float', min: 0 },
        ],
      },
      {
        title: '② 高频缓存',
        desc: '无上下文请求（LRU+TTL）',
        fields: [
          { key: 'high_freq_cache.enabled', label: '关卡：关闭时该级缓存直通', kind: 'bool' },
          { key: 'high_freq_cache.capacity', label: '容量上限（条目数，必填）', kind: 'int', min: 0 },
          { key: 'high_freq_cache.ttl', label: 'TTL（秒，必填）', kind: 'float', min: 0 },
        ],
      },
      {
        title: '③ 图片去重缓存',
        desc: '仅单图无文本请求',
        fields: [
          { key: 'dedup_cache.enabled', label: '关卡：关闭时该级缓存直通', kind: 'bool' },
          { key: 'dedup_cache.capacity', label: '容量上限（条目数，必填）', kind: 'int', min: 0 },
          { key: 'dedup_cache.ttl', label: 'TTL（秒，必填）', kind: 'float', min: 0 },
        ],
      },
      {
        title: '④ 短文本 LLM 缓存',
        fields: [
          { key: 'short_text_llm_cache.enabled', label: '关卡：关闭时该级缓存直通', kind: 'bool' },
          { key: 'short_text_llm_cache.capacity', label: '容量上限（条目数，必填）', kind: 'int', min: 0 },
          { key: 'short_text_llm_cache.ttl', label: 'TTL（秒，必填）', kind: 'float', min: 0 },
        ],
      },
      {
        title: '⑤ 永久黑白名单',
        fields: [
          { key: 'permanent_lists', label: '启动加载，管理端写入即失效', kind: 'bool' },
        ],
      },
    ],
  },
  {
    group: 'light_model',
    title: '轻量文本风险模型',
    icon: '⚡',
    desc: '复用已训 fasttext.pt；路径为 null 时组件 disabled',
    sections: [
      {
        title: '轻量模型',
        fields: [
          { key: 'model_path', label: 'fasttext.pt 路径', kind: 'text', nullable: true, hint: 'null = 未启用（组件 disabled）' },
          { key: 'config_path', label: '模型配套 config.json 路径', kind: 'text', nullable: true, hint: 'null = 未启用' },
        ],
      },
    ],
  },
  {
    group: 'logging',
    title: '日志配置',
    icon: '📝',
    sections: [
      {
        title: '日志',
        fields: [
          {
            key: 'level',
            label: '日志级别（必填）',
            kind: 'select',
            options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
          },
          { key: 'json_lines', label: 'true = JSON 行；false = 标准文本格式', kind: 'bool' },
        ],
      },
    ],
  },
  {
    group: 'image',
    title: '图片处理（动图抽帧）',
    icon: '🖼️',
    sections: [
      {
        title: '动图抽帧',
        fields: [
          { key: 'animated.enabled', label: 'false 时退回 v0.1 首帧降级行为', kind: 'bool' },
          { key: 'animated.frames', label: '均匀抽帧数（3~5，可配，必填）', kind: 'int', min: 1 },
          {
            key: 'animated.mode',
            label: '抽帧模式（必填）',
            kind: 'select',
            options: ['uniform', 'first'],
            hint: 'uniform 均匀 | first 首帧',
          },
        ],
      },
    ],
  },
  {
    group: 'keyword',
    title: '关键词层',
    icon: '🔑',
    sections: [
      {
        title: '正则消歧规则库',
        fields: [
          { key: 'regex_rules_enabled', label: '开关；false 时规则层跳过', kind: 'bool' },
        ],
      },
    ],
  },
  {
    group: 'semantic',
    title: '语义层（Rerank 四信号）',
    icon: '🧠',
    desc: 'v0.2.1 新增 fuse_mode 图文融合模式（虚拟键，默认 pool）',
    sections: [
      {
        title: '语义层',
        fields: [
          { key: 'rerank_enabled', label: 'Rerank 开关（默认关）', kind: 'bool' },
          { key: 'rerank_w_top', label: '黑库最高相似度权重（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'rerank_w_margin', label: '黑白均值差权重（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'rerank_w_rerank', label: 'Rerank 分数权重（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'rerank_top_k', label: 'Rerank 候选数（必填）', kind: 'int', min: 1 },
          {
            key: 'fuse_mode',
            label: '图文融合模式（必填）',
            kind: 'select',
            options: ['pool', 'concat', 'weighted_avg'],
            hint: 'weighted_avg 要求文本与图像向量同维；在线 API 与本地 CLIP 维度不一致时请用 concat（后端 422 校验提示）',
          },
        ],
      },
    ],
  },
  {
    group: 'review',
    title: '定时复核',
    icon: '⏱️',
    sections: [
      {
        title: '定时复核',
        fields: [
          { key: 'interval_min', label: '复核周期（分钟）；0 禁用自动调度（必填）', kind: 'int', min: 0 },
          { key: 'band_low', label: '采样下界（置信度中带，必填）', kind: 'float', min: 0, max: 1 },
          { key: 'band_high', label: '采样上界（置信度中带，必填）', kind: 'float', min: 0, max: 1 },
          { key: 'sample_size', label: '每轮采样上限（必填）', kind: 'int', min: 1 },
          { key: 'auto_tune', label: '是否自动采纳阈值建议（默认仅出报告）', kind: 'bool' },
        ],
      },
    ],
  },
]

const toast = useToastStore()

/** 分组 → 扁平字段值的编辑草稿（分组的二级对象以点分路径平铺，如 cache.redis.url） */
const draft = ref<Record<string, Record<string, unknown>>>({})
const loading = ref(false)
const savingGroup = ref<string | null>(null)
const restoreTarget = ref<string | null>(null)

const GROUP_BY_KEY = new Map(GROUP_META.map((g) => [g.group, g]))

function groupTitle(group: string): string {
  return renderGroups.value.find((g) => g.group === group)?.title ?? GROUP_BY_KEY.get(group)?.title ?? group
}

/** 是否密钥遮蔽对象（{api_key_env, configured} 形态，config_override.mask_secret_fields 输出） */
function isMaskedSecret(value: unknown): value is MaskedSecret {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const obj = value as Record<string, unknown>
  return 'api_key_env' in obj && typeof obj.configured === 'boolean'
}

function secretOf(group: string, key: string): MaskedSecret | null {
  const value = draft.value[group]?.[key]
  return isMaskedSecret(value) ? value : null
}

function fieldValue(group: string, key: string): unknown {
  return draft.value[group]?.[key]
}

function textOf(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function isOn(group: string, key: string): boolean {
  return fieldValue(group, key) === true
}

function toggleBool(group: string, key: string): void {
  if (!draft.value[group]) return
  draft.value[group][key] = !isOn(group, key)
}

function onTextInput(group: string, key: string, event: Event): void {
  if (!draft.value[group]) return
  const target = event.target as HTMLInputElement
  draft.value[group][key] = target.value
}

function onNumberInput(group: string, key: string, event: Event): void {
  if (!draft.value[group]) return
  const target = event.target as HTMLInputElement
  // 空输入保留为 ''，提交时若必填则前端拦截，若可空则转 null
  draft.value[group][key] = target.value
}

function onSelectInput(group: string, key: string, event: Event): void {
  if (!draft.value[group]) return
  const target = event.target as HTMLSelectElement
  draft.value[group][key] = target.value === '' ? null : target.value
}

/** 数值字段 min/max 属性（number input 原生校验，不阻塞提交） */
function numberAttrs(meta: FieldMeta): Record<string, number | string> {
  const attrs: Record<string, number | string> = { step: meta.kind === 'int' ? '1' : 'any' }
  if (meta.min !== undefined) attrs.min = meta.min
  if (meta.max !== undefined) attrs.max = meta.max
  return attrs
}

/** 未知分组的兜底字段类型探测：按当前值推断控件（bool/number/字符串/遮蔽对象） */
function detectKind(value: unknown): FieldKind {
  if (isMaskedSecret(value)) return 'secret'
  if (typeof value === 'boolean') return 'bool'
  if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'float'
  return 'text'
}

/**
 * 兜底渲染未知分组（GROUP_META 未收录，如后端后续新增分组）：
 * - 以扁平字段的点分路径前缀分组为 section（"cache.audit_cache.enabled" → cache → audit_cache...）；
 * - 字段 label 兜底为「字段名 + 中文注释」；
 * - 类型按当前值探测（secret/bool/int/float/text），未知取值不做枚举猜测。
 * 已知分组永远走 GROUP_META 静态元数据，不会命中此分支。
 */
function synthesizeGroup(group: string, flat: Record<string, unknown>): GroupMeta {
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

/**
 * 渲染分组清单：以 GET /admin/config 实际返回的分组为准（决策：按响应渲染），
 * 已知分组取 GROUP_META 静态元数据（label/类型/枚举/范围），未知分组走
 * synthesizeGroup 自动降级。后端分组白名单与其模型字段同步演进时页面不破。
 */
const renderGroups = computed<GroupMeta[]>(() => {
  return Object.keys(draft.value).map((group) => GROUP_BY_KEY.get(group) ?? synthesizeGroup(group, draft.value[group]))
})

/** 构造提交负载：全量非密钥字段；空可空字段转 null；空必填/非法数值前端拦截 */
function buildPayload(group: string): Record<string, unknown> | null {
  const meta = renderGroups.value.find((g) => g.group === group)
  if (!meta) return null
  const payload: Record<string, unknown> = {}
  for (const section of meta.sections) {
    for (const field of section.fields) {
      if (field.kind === 'secret') continue // 密钥不参与提交（后端对 api_key 键一律 422）
      const value = fieldValue(group, field.key)
      if (value === null || value === undefined || value === '') {
        if (field.nullable) {
          setNested(payload, field.key, null)
          continue
        }
        toast.error(`字段 ${group}.${field.key} 不能为空（必填项）`)
        return null
      }
      if (field.kind === 'bool') {
        setNested(payload, field.key, value === true || value === 'true')
      } else if (field.kind === 'int') {
        const num = Number(value)
        if (!Number.isInteger(num)) {
          toast.error(`字段 ${group}.${field.key} 必须为整数`)
          return null
        }
        setNested(payload, field.key, num)
      } else if (field.kind === 'float') {
        const num = Number(value)
        if (Number.isNaN(num)) {
          toast.error(`字段 ${group}.${field.key} 必须为数字`)
          return null
        }
        setNested(payload, field.key, num)
      } else {
        setNested(payload, field.key, textOf(value))
      }
    }
  }
  return payload
}

/** 点分路径写入嵌套对象（如 'cloud.base_url' → payload.cloud.base_url） */
function setNested(target: Record<string, unknown>, dotted: string, value: unknown): void {
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
function flattenGroup(raw: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const walk = (node: Record<string, unknown>, prefix: string): void => {
    for (const [key, value] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${key}` : key
      if (isMaskedSecret(value)) {
        out[path] = value // 遮蔽对象整体保留（徽标展示），不参与提交
      } else if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        walk(value as Record<string, unknown>, path)
      } else {
        out[path] = value
      }
    }
  }
  walk(raw, '')
  return out
}

/** 拉取全量配置并重建草稿（刷新按钮/初始加载共用） */
async function loadConfig(): Promise<void> {
  loading.value = true
  try {
    const res = await apiGet<ConfigResponse>('/config')
    const next: Record<string, Record<string, unknown>> = {}
    for (const [group, raw] of Object.entries(res)) {
      // 防御：分组必须是对象字典（后端契约保证，此处兜底跳过非对象分组）
      if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
        next[group] = flattenGroup(raw)
      } else {
        console.warn(`[SettingsView] 忽略非对象配置分组: ${group}`)
      }
    }
    draft.value = next
  } catch (error) {
    console.warn('[SettingsView] 加载配置失败：', error)
  } finally {
    loading.value = false
  }
}

/** 保存单个分组：PUT /admin/config/{group}，成功以响应 config 回填草稿（重启生效） */
async function saveGroup(group: string): Promise<void> {
  const payload = buildPayload(group)
  if (!payload) return
  savingGroup.value = group
  try {
    const res = await apiPut<ConfigPutResult>(`/config/${group}`, payload)
    if (res.config) draft.value[group] = flattenGroup(res.config)
    toast.success('已保存（重启后生效）')
  } catch (error) {
    console.warn(`[SettingsView] 保存分组 ${group} 失败：`, error)
  } finally {
    savingGroup.value = null
  }
}

/** 恢复默认：PUT 空对象 {} → 删除覆盖层该分组（决策 E：重启生效） */
async function confirmRestore(): Promise<void> {
  const group = restoreTarget.value
  if (!group) return
  savingGroup.value = group
  try {
    const res = await apiPut<ConfigPutResult>(`/config/${group}`, {})
    if (res.config) draft.value[group] = flattenGroup(res.config)
    toast.success('已恢复默认（重启后生效）')
  } catch (error) {
    console.warn(`[SettingsView] 恢复默认 ${group} 失败：`, error)
  } finally {
    savingGroup.value = null
    restoreTarget.value = null
  }
}

const restoreMessage = computed(() => {
  const group = restoreTarget.value
  return group ? `确定将分组「${groupTitle(group)}」恢复为默认配置吗？\n将删除该分组的管理端覆盖层设置（重启后生效）。` : ''
})

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <section class="page-view">
    <div class="page-head">
      <h2 class="page-title">⚙️ 系统设置</h2>
      <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="loadConfig">
        🔄 刷新
      </button>
    </div>

    <!-- 重启生效提示（决策 E） -->
    <div class="card restart-banner">
      <span class="restart-icon">⚡</span>
      <div>
        <strong>保存 / 恢复默认后需重启服务生效</strong>
        <p class="restart-desc">
          配置写入覆盖层文件 <code>data/config_overrides.json</code>，服务仅在启动时合并（决策 E：
          不热应用）。密钥（api_key）仅支持环境变量注入（决策 F），保存时自动剔除、不参与提交。
        </p>
      </div>
    </div>

    <div v-if="loading" class="loading">加载配置中...</div>

    <template v-else>
      <!-- 分组表单卡片 -->
      <div v-for="meta in renderGroups" :key="meta.group" class="card group-card">
        <div class="card-title group-title">
          <span class="group-title-text" :title="meta.group">{{ meta.icon }} {{ meta.title }}</span>
          <div class="group-actions">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="savingGroup !== null"
              @click="restoreTarget = meta.group"
            >
              ↺ 恢复默认
            </button>
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="savingGroup !== null"
              @click="saveGroup(meta.group)"
            >
              {{ savingGroup === meta.group ? '保存中...' : '💾 保存' }}
            </button>
          </div>
        </div>

        <p v-if="meta.desc" class="group-desc">{{ meta.desc }}</p>

        <div v-for="(section, si) in meta.sections" :key="si" class="cfg-section">
          <div class="cfg-section-title">{{ section.title }}</div>
          <p v-if="section.desc" class="cfg-section-desc">{{ section.desc }}</p>

          <div class="field-grid">
            <div v-for="field in section.fields" :key="field.key" class="field-cell">
              <!-- 密钥只读徽标 -->
              <template v-if="field.kind === 'secret'">
                <span class="field-label">{{ field.label }}</span>
                <div class="secret-badge">
                  <span
                    class="tag"
                    :class="secretOf(meta.group, field.key)?.configured ? 'tag-success' : 'tag-orange'"
                  >
                    🔑 环境变量：
                    {{ textOf(secretOf(meta.group, field.key)?.api_key_env) || '—' }}
                    · {{ secretOf(meta.group, field.key)?.configured ? '已配置' : '未配置' }}
                  </span>
                  <p class="secret-note">密钥仅支持环境变量注入（SAFEFUSION_* 或上文 api_key_env 指定变量），不写入覆盖层；保存时该字段不参与提交。</p>
                </div>
              </template>

              <!-- bool 开关 -->
              <template v-else-if="field.kind === 'bool'">
                <span class="field-label">{{ field.label }}</span>
                <div class="bool-row">
                  <button
                    type="button"
                    class="switch"
                    :class="{ 'switch-on': isOn(meta.group, field.key) }"
                    role="switch"
                    :aria-checked="isOn(meta.group, field.key)"
                    :title="isOn(meta.group, field.key) ? '点击关闭' : '点击开启'"
                    @click="toggleBool(meta.group, field.key)"
                  >
                    <span class="switch-thumb"></span>
                  </button>
                  <span class="bool-text">{{ isOn(meta.group, field.key) ? '开' : '关' }}</span>
                </div>
              </template>

              <!-- 数字输入 -->
              <template v-else-if="field.kind === 'int' || field.kind === 'float'">
                <span class="field-label">{{ field.label }}</span>
                <input
                  type="number"
                  class="input"
                  :value="textOf(fieldValue(meta.group, field.key))"
                  v-bind="numberAttrs(field)"
                  @input="onNumberInput(meta.group, field.key, $event)"
                />
              </template>

              <!-- 枚举下拉 -->
              <template v-else-if="field.kind === 'select'">
                <span class="field-label">{{ field.label }}</span>
                <select
                  class="input"
                  :value="textOf(fieldValue(meta.group, field.key))"
                  @change="onSelectInput(meta.group, field.key, $event)"
                >
                  <option
                    v-for="opt in field.options"
                    :key="opt"
                    :value="opt"
                  >
                    {{ opt }}
                  </option>
                  <option
                    v-if="!field.options?.includes(textOf(fieldValue(meta.group, field.key)))"
                    :value="textOf(fieldValue(meta.group, field.key))"
                    disabled
                  >
                    {{ textOf(fieldValue(meta.group, field.key)) || '(未知取值)' }}
                  </option>
                </select>
              </template>

              <!-- 文本输入 -->
              <template v-else>
                <span class="field-label">{{ field.label }}</span>
                <input
                  type="text"
                  class="input"
                  :value="textOf(fieldValue(meta.group, field.key))"
                  :placeholder="field.nullable ? '留空 = null（未配置）' : ''"
                  @input="onTextInput(meta.group, field.key, $event)"
                />
              </template>

              <p v-if="field.hint && field.kind !== 'secret'" class="field-hint">{{ field.hint }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- 配置优先级说明（决策 D） -->
      <p class="priority-note">
        配置优先级：内置默认 &lt; config.yaml &lt; 管理端覆盖层 &lt; 环境变量
        （环境变量最高优先；被 SAFEFUSION_&lt;路径&gt;_&lt;键&gt; 钉住的键不受覆盖层影响）
      </p>
    </template>

    <!-- 恢复默认二次确认 -->
    <ConfirmDialog
      :show="restoreTarget !== null"
      title="↺ 恢复默认配置"
      :message="restoreMessage"
      :danger="true"
      @confirm="confirmRestore"
      @cancel="restoreTarget = null"
    />
  </section>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.page-title {
  margin-bottom: 0;
}

/* 重启生效横幅 */
.restart-banner {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  background: var(--primary-light);
  border: 1px solid rgba(22, 93, 255, 0.18);
}

.restart-icon {
  font-size: 1.3rem;
  line-height: 1.4;
}

.restart-desc {
  font-size: 0.76rem;
  color: var(--text-2);
  margin-top: 2px;
  line-height: 1.7;
}

.restart-desc code {
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.72rem;
}

/* 分组卡片 */
.group-card {
  margin-bottom: 16px;
}

.group-title {
  align-items: center;
}

.group-title-text {
  min-width: 0;
  word-break: break-word;
}

.group-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.group-desc {
  font-size: 0.76rem;
  color: var(--text-3);
  margin: -8px 0 12px;
  line-height: 1.6;
}

/* 分组内小节 */
.cfg-section {
  border-top: 1px dashed var(--border);
  padding-top: 12px;
  margin-top: 12px;
}

.cfg-section:first-of-type {
  border-top: none;
  padding-top: 0;
  margin-top: 0;
}

.cfg-section-title {
  font-size: 0.78rem;
  font-weight: 700;
  color: var(--text-2);
}

.cfg-section-desc {
  font-size: 0.74rem;
  color: var(--text-3);
  margin: 2px 0 10px;
}

/* 字段栅格 */
.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px 18px;
  margin-top: 10px;
}

.field-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.field-label {
  font-size: 0.74rem;
  color: var(--text-2);
  font-weight: 600;
  line-height: 1.5;
}

.field-hint {
  font-size: 0.7rem;
  color: var(--text-3);
  line-height: 1.5;
}

/* 开关（switch，风格对齐 RulesView） */
.bool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
}

.bool-text {
  font-size: 0.76rem;
  color: var(--text-2);
}

.switch {
  position: relative;
  width: 40px;
  height: 22px;
  border: none;
  border-radius: 12px;
  background: var(--border);
  cursor: pointer;
  transition: background var(--transition);
  padding: 0;
  flex-shrink: 0;
}

.switch-on {
  background: var(--success);
}

.switch-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
  transition: transform var(--transition);
}

.switch-on .switch-thumb {
  transform: translateX(18px);
}

/* 密钥徽标 */
.secret-badge {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 38px;
  justify-content: center;
}

.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
  align-self: flex-start;
}

.tag-success {
  background: var(--success-light);
  color: var(--success);
}

.tag-orange {
  background: #fff7e8;
  color: #b5711a;
}

.secret-note {
  font-size: 0.7rem;
  color: var(--text-3);
  line-height: 1.5;
}

/* 页面底部优先级说明 */
.priority-note {
  text-align: center;
  font-size: 0.74rem;
  color: var(--text-3);
  padding: 10px 0 6px;
  line-height: 1.7;
}
</style>