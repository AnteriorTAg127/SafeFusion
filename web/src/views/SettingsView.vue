<script setup lang="ts">
/**
 * 系统设置页（T25 全量配置表单 + T36 操作提示 + T39 v0.3.0 改造）：
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
 * - Key 遮蔽（决策 F）：GET 返回形如 {api_key_env, configured} 的 api_key 字段
 *   （llm.api_key / embedding.cloud.api_key）→ 只读徽标「环境变量: <名> ·
 *   已配置/未配置」，不允许编辑值；保存时该字段不参与提交（后端
 *   validate_group_update 对任何含 api_key 键的负载一律 422 拒绝）。
 * - 保存（v0.3.0 M4 全量热应用）：PUT /admin/config/{group} 提交**全量非密钥
 *   字段**（后端部分键覆盖语义，全量提交在两种语义下均安全）；成功以响应
 *   config 回填草稿、**内联 sources 即时刷新来源徽标**；Toast 依 applied /
 *   apply_scope 语义：applied=true 且 scope=runtime →「已保存并生效」；
 *   scope=config（server/logging 绑定类）→ 附「端口/日志类修改于下次启动生效」；
 *   applied=false（未注入容器的测试部署）→「已保存，重启后生效」。
 * - 来源标识（v0.3.0 M4，T39）：GET /admin/config/sources → {分组:{叶子点分路径:
 *   default/yaml/db/env}}；每字段旁小徽标（默认淡化 / YAML 灰 / DB 蓝 /
 *   环境变量橙），按叶子点分路径匹配；保存响应内联 sources 即时更新徽标。
 * - 测试连接（v0.3.0 M5，T39）：embedding / llm / light_model 分组卡各加
 *   「🔌 测试连接」→ POST /admin/config/test-connection {channel} → 内联结果条
 *   （✅ 通过 / ❌ 失败 + 耗时 / 维度等 detail）。
 * - 模型卡（v0.3.0 M6，T39）：页面顶部「🤖 模型」卡——GET /admin/models 渲染
 *   chinese-clip / fasttext / 语义引擎 / 向量库状态行（徽标 + 缓存路径/大小/
 *   条数/维度细节）；本地 CLIP 未下载时「⬇️ 下载模型」（POST download → 1s 轮询
 *   GET download/{task_id} 进度百分比 → 完成 Toast）与「🔄 装配 / 重新加载」
 *   （POST load）；fasttext 未就绪时展示指向 light_model 分组字段的配置指引；
 *   页面活动时每 10s 轮询模型状态（document.hidden 跳过、回前台即刷）。
 * - 安全卡（v0.3.0 M9 C5，T39）：🔐 改密——当前密码 + 新密码（≥10 位）+ 确认
 *   新密码 → POST /admin/config/password；成功 Toast「已修改，旧令牌立即失效」
 *   并**立即登出**（旧令牌已失效，本会话继续发请求只会 401，必须回登录页）。
 * - 422 / 400 错误文案由 api client 统一 Toast（兼容 {error} 与 {detail} 两种
 *   错误体），前端不再重复弹窗。
 * - TODO(歧义-已知后端行为)：
 *   1) PUT 部分键覆盖语义：前端提交全量非密钥字段，若后端语义变化可退回仅提交
 *      改动字段（当前全量提交在两种语义下均安全）；
 *   2) logging.level / image.animated.mode / embedding.local.device 后端暂无
 *      白名单校验（仅业务规则校验 backend/fuse_mode），select 提供已知合法值，
 *      服务端返回未知取值时追加「(未知)」选项展示，不静默丢失；
 *   3) 密钥 configured 反映「当前进程环境变量是否已设置」，前端只读（决策 F）；
 *      保存即热应用生效（applied=false 仅出现在未注入容器的测试部署）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiPut } from '../api/client'
import router from '../router'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'

/** 密钥遮蔽对象（config_override.mask_secret_fields 输出形态） */
interface MaskedSecret {
  api_key_env: string | null
  configured: boolean
}

/** GET /admin/config → { 分组名: 分组配置（api_key 已遮蔽） } */
type ConfigResponse = Record<string, Record<string, unknown>>

/** 字段来源（config_override.config_sources：default/yaml/db/env） */
type SourceKind = 'default' | 'yaml' | 'db' | 'env'

/** GET /admin/config/sources → { 分组: { 叶子点分路径: 来源 } }（T39 契约） */
type SourcesResponse = Record<string, Record<string, SourceKind>>

/** PUT /admin/config/{group} 响应结构（api/admin.py update_config，v0.3.0 M4） */
interface ConfigPutResult {
  group: string
  config: Record<string, unknown>
  saved: boolean
  /** 是否已热应用（未注入共享容器时为 false，仅落库） */
  applied: boolean
  /** runtime=即时生效 / config=仅配置叶子（server/logging 绑定类下次启动生效）/ none */
  apply_scope: 'runtime' | 'config' | 'none'
  /** 字段级来源映射（保存后内联，前端据此即时刷新来源徽标） */
  sources: Record<string, SourceKind>
  deleted_db_group: boolean
}

/** 测试连接结果（api/admin.py _channel_result：channel/ok/message/detail） */
interface TestConnectionResult {
  channel: 'embedding' | 'llm' | 'fasttext'
  ok: boolean
  message: string
  detail: Record<string, unknown>
}

/** GET /admin/models 响应（api/admin.py list_models） */
interface ModelsResponse {
  hf_cache_dir?: string
  chinese_clip?: {
    backend: string
    model_name?: string
    weights_path?: string | null
    cache_dir?: string
    loaded?: boolean
    load_status?: string
    load_reason?: string | null
    cached_files?: number | null
    cache_size_bytes?: number | null
    cache_partial?: boolean
    /** cloud / ready / error / downloading / not_downloaded */
    status?: string
    message?: string
  }
  fasttext?: {
    configured: boolean
    model_path?: string | null
    config_path?: string | null
    model_file_exists?: boolean
    config_file_exists?: boolean
    loadable?: boolean
    /** ready / error / missing / not_configured */
    status?: string
  }
  vector_store?: {
    black: { count: number; dim: number | null }
    white: { count: number; dim: number | null }
  }
  semantic?: {
    ready: boolean
    status?: string
    reason?: string | null
    backend?: string
  }
}

/** GET /admin/models/download/{task_id} 进度快照（model_repo.DownloadTask.snapshot） */
interface DownloadTask {
  task_id: string
  model_name?: string
  status: 'running' | 'completed' | 'failed'
  stage?: string
  progress?: number
  downloaded_bytes?: number
  total_bytes?: number
  error?: string | null
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
    desc: '审核/管理双端口监听配置（内置默认 < config.yaml < DB 配置 < 环境变量；端口变更下次启动生效）',
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

// ---------------- T39 追加状态（来源徽标 / 测试连接 / 模型卡 / 改密） ----------------

/** 字段级来源映射（GET /admin/config/sources；保存响应内联 sources 即时更新） */
const sources = ref<Record<string, Record<string, SourceKind>>>({})

/** 各分组「测试连接」最近一次结果（group → 结果条） */
const channelResults = ref<Record<string, TestConnectionResult | null>>({})
/** 正在测试的渠道（embedding / llm / fasttext），null 表示空闲 */
const testingChannel = ref<string | null>(null)

/** 模型卡状态（GET /admin/models）与下载任务（POST download → 轮询） */
const models = ref<ModelsResponse | null>(null)
const modelsLoading = ref(false)
const downloadTask = ref<DownloadTask | null>(null)
const downloadPolling = ref(false)
const loadBusy = ref(false)

/** 改密表单（POST /admin/config/password） */
const pwCurrent = ref('')
const pwNew = ref('')
const pwConfirm = ref('')
const pwSubmitting = ref(false)

/** T38 跳转高亮：?group=xxx 落地分组卡后短暂描边（highlightGroup === 分组名） */
const highlightGroup = ref<string | null>(null)

/** 轮询句柄：模型状态 10s（页面活动时）/ 下载进度 1s（任务期间） */
let modelsTimer: number | undefined
let downloadTimer: number | undefined

const GROUP_BY_KEY = new Map(GROUP_META.map((g) => [g.group, g]))

function groupTitle(group: string): string {
  return renderGroups.value.find((g) => g.group === group)?.title ?? GROUP_BY_KEY.get(group)?.title ?? group
}

// ------------------------------------------------ T39：来源标识（M4）

/** 来源徽标文案（config_sources 四层语义） */
const SOURCE_TEXT: Record<SourceKind, string> = {
  default: '默认',
  yaml: 'YAML',
  db: '数据库',
  env: '环境变量',
}

/** 取字段来源映射（sourcesValue[分组][叶子点分路径]；缺失返回 undefined → 不渲染徽标） */
function sourceOf(group: string, key: string): SourceKind | undefined {
  return sources.value[group]?.[key]
}

function sourceText(kind: SourceKind | undefined): string {
  if (!kind) return ''
  return SOURCE_TEXT[kind] ?? kind
}

/** 来源徽标样式类（db 蓝 / yaml 灰 / env 橙 / default 淡化） */
function srcBadgeClass(group: string, key: string): string {
  const kind = sourceOf(group, key)
  if (kind === 'db') return 'src-db'
  if (kind === 'yaml') return 'src-yaml'
  if (kind === 'env') return 'src-env'
  return 'src-default'
}

/** 拉取字段级来源映射（GET /admin/config/sources） */
async function loadSources(): Promise<void> {
  try {
    sources.value = await apiGet<SourcesResponse>('/config/sources')
  } catch (error) {
    console.warn('[SettingsView] 加载配置来源失败：', error)
  }
}

/**
 * 保存成功 Toast 文案（v0.3.0 M4 热应用语义，移除「重启后生效」旧文案）：
 * - applied=true 且 apply_scope=runtime → 「已保存并生效」；
 * - applied=true 且 apply_scope=config（server/logging 绑定类）→ 附「端口/日志
 *   类修改于下次启动生效」；
 * - applied=false（未注入共享容器的测试部署）→ 退化为「重启后生效」。
 */
function saveMessage(res: ConfigPutResult, restored: boolean): string {
  const verb = restored ? '已恢复默认' : '已保存'
  if (!res.applied) return `${verb}（已写入配置存储，当前部署未热应用，重启后生效）`
  if (res.apply_scope === 'config') return `${verb}并生效（端口/日志类配置于下次启动生效）`
  return `${verb}并生效`
}

// ------------------------------------------------ T39：测试连接（M5）

/** 分组 → 测试连接渠道（light_model 对应后端 fasttext） */
const CHANNEL_BY_GROUP: Record<string, 'embedding' | 'llm' | 'fasttext'> = {
  embedding: 'embedding',
  llm: 'llm',
  light_model: 'fasttext',
}

/** 「🔌 测试连接」：POST /admin/config/test-connection {channel} → 内联结果条 */
async function testConnection(group: string): Promise<void> {
  const channel = CHANNEL_BY_GROUP[group]
  if (!channel) return
  testingChannel.value = channel
  try {
    const res = await apiPost<TestConnectionResult>('/config/test-connection', { channel })
    channelResults.value[group] = res
  } catch (error) {
    console.warn(`[SettingsView] 测试连接 ${channel} 失败：`, error)
  } finally {
    testingChannel.value = null
  }
}

/** 测试结果 detail 摘要（耗时/维度/模型/路径等已知键拼接为一行，未知键忽略） */
function connDetail(group: string): string {
  const detail = channelResults.value[group]?.detail
  if (!detail) return ''
  const parts: string[] = []
  if (detail.duration_ms !== undefined) parts.push(`耗时 ${String(detail.duration_ms)} ms`)
  if (detail.dimension !== undefined) parts.push(`维度 ${String(detail.dimension)}`)
  if (detail.chars !== undefined) parts.push(`字符数 ${String(detail.chars)}`)
  if (typeof detail.model === 'string' && detail.model) parts.push(`模型 ${detail.model}`)
  if (typeof detail.label === 'string' && detail.label) parts.push(`标签 ${detail.label}`)
  if (typeof detail.score === 'number') parts.push(`分数 ${detail.score}`)
  if (typeof detail.weights_path === 'string' && detail.weights_path) parts.push(`权重 ${detail.weights_path}`)
  if (typeof detail.cache_dir === 'string' && detail.cache_dir) parts.push(`缓存 ${detail.cache_dir}`)
  return parts.join(' · ')
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

/** 保存单个分组：PUT /admin/config/{group}，成功以响应 config 回填草稿 + 内联 sources 刷新徽标 */
async function saveGroup(group: string): Promise<void> {
  const payload = buildPayload(group)
  if (!payload) return
  savingGroup.value = group
  try {
    const res = await apiPut<ConfigPutResult>(`/config/${group}`, payload)
    if (res.config) draft.value[group] = flattenGroup(res.config)
    if (res.sources) sources.value[group] = res.sources // 保存响应内联 sources → 徽标即时更新
    toast.success(saveMessage(res, false))
    // embedding / llm / light_model 变更影响模型装配状态 → 刷新模型卡
    if (group === 'embedding' || group === 'llm' || group === 'light_model') void loadModels()
  } catch (error) {
    console.warn(`[SettingsView] 保存分组 ${group} 失败：`, error)
  } finally {
    savingGroup.value = null
  }
}

/** 恢复默认：PUT 空对象 {} → 删除该分组 DB settings 并热应用回退（即时生效） */
async function confirmRestore(): Promise<void> {
  const group = restoreTarget.value
  if (!group) return
  savingGroup.value = group
  try {
    const res = await apiPut<ConfigPutResult>(`/config/${group}`, {})
    if (res.config) draft.value[group] = flattenGroup(res.config)
    if (res.sources) sources.value[group] = res.sources
    toast.success(saveMessage(res, true))
  } catch (error) {
    console.warn(`[SettingsView] 恢复默认 ${group} 失败：`, error)
  } finally {
    savingGroup.value = null
    restoreTarget.value = null
  }
}

const restoreMessage = computed(() => {
  const group = restoreTarget.value
  return group
    ? `确定将分组「${groupTitle(group)}」恢复为默认配置吗？\n将删除该分组的管理端配置（DB settings）并立即生效（热应用）；端口/日志类配置于下次启动生效。`
    : ''
})

// ------------------------------------------------ T39：模型卡（M6）

/** 字节数人类可读（null/undefined/非有限数 → ''） */
function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

/** chinese-clip 状态徽标文案（api/admin.py list_models 的 clip.status） */
const CLIP_STATUS_TEXT: Record<string, string> = {
  ready: '已就绪',
  downloading: '下载中',
  not_downloaded: '未下载',
  error: '错误',
  cloud: '云端',
}

/** fasttext 状态徽标文案（fasttext.status） */
const FASTTEXT_STATUS_TEXT: Record<string, string> = {
  ready: '已就绪',
  error: '加载错误',
  missing: '文件缺失',
  not_configured: '未配置',
}

/** chinese-clip 状态 → 徽标文案（缺省 '—'） */
function clipStatusText(status: string | undefined): string {
  if (!status) return '—'
  return CLIP_STATUS_TEXT[status] ?? status
}

/** fasttext 状态 → 徽标文案（缺省 '—'） */
function fasttextStatusText(status: string | undefined): string {
  if (!status) return '—'
  return FASTTEXT_STATUS_TEXT[status] ?? status
}

/** chinese-clip 状态 → 徽标样式类（语义色） */
function clipStatusClass(status: string | undefined): string {
  if (status === 'ready') return 'm-badge-ok'
  if (status === 'error') return 'm-badge-err'
  if (status === 'downloading') return 'm-badge-warn'
  if (status === 'cloud') return 'm-badge-alt'
  return 'm-badge-muted' // not_downloaded / 缺省
}

/** 向量库黑白池任一非空即视为已就绪 */
const vectorStoreReady = computed(() => {
  const vs = models.value?.vector_store
  return Boolean(vs && (vs.black.count > 0 || vs.white.count > 0))
})

/** 下载进度文案（running 时显示百分比，其余状态空串） */
const downloadProgressText = computed(() => {
  const task = downloadTask.value
  if (!task || task.status !== 'running') return ''
  return task.progress !== undefined ? `进度 ${task.progress}%` : '准备中...'
})

/** 下载阶段（stage），无则空串 */
const downloadStageText = computed(() => downloadTask.value?.stage ?? '')

/** 拉取模型状态（GET /admin/models）；页面活动时由 10s 轮询调用 */
async function loadModels(): Promise<void> {
  modelsLoading.value = true
  try {
    models.value = await apiGet<ModelsResponse>('/models')
  } catch (error) {
    console.warn('[SettingsView] 加载模型状态失败：', error)
  } finally {
    modelsLoading.value = false
  }
}

/** 「⬇️ 下载模型」：POST /admin/models/download → 复用/新任务 → 1s 轮询进度 */
async function startDownload(): Promise<void> {
  if (downloadPolling.value) return
  try {
    const res = await apiPost<{
      task_id: string
      status: string
      reused?: boolean
      message?: string
    }>('/models/download', {})
    if (res.status === 'completed') {
      toast.success('模型已缓存，无需下载')
      void loadModels()
      return
    }
    downloadPolling.value = true
    downloadTask.value = { task_id: res.task_id, status: 'running' }
    toast.info(res.reused ? '复用进行中的下载任务（同模型互斥）' : '下载任务已启动')
    pollDownload(res.task_id)
  } catch (error) {
    console.warn('[SettingsView] 启动模型下载失败：', error)
  }
}

/** 下载进度轮询：GET /admin/models/download/{task_id}，completed/failed 收尾 */
function pollDownload(taskId: string): void {
  if (downloadTimer !== undefined) window.clearInterval(downloadTimer)
  downloadTimer = window.setInterval(async () => {
    try {
      const task = await apiGet<DownloadTask>(`/models/download/${taskId}`)
      downloadTask.value = task
      if (task.status === 'completed') {
        finishDownload(true, '模型下载完成，可点击「装配 / 重新加载」启用')
      } else if (task.status === 'failed') {
        finishDownload(false, `模型下载失败：${task.error || '未知错误'}`)
      }
    } catch (error) {
      console.warn('[SettingsView] 轮询下载进度失败：', error)
      finishDownload(false, '下载进度查询失败，请刷新页面查看模型状态')
    }
  }, 1000)
}

/** 下载收尾：停止轮询 → Toast → 刷新模型状态 */
function finishDownload(ok: boolean, message: string): void {
  if (downloadTimer !== undefined) {
    window.clearInterval(downloadTimer)
    downloadTimer = undefined
  }
  downloadPolling.value = false
  if (ok) toast.success(message)
  else toast.error(message)
  void loadModels()
}

/** 「🔄 装配 / 重新加载」：POST /admin/models/load（同步等待装配结果） */
async function loadModel(): Promise<void> {
  if (loadBusy.value) return
  loadBusy.value = true
  try {
    const res = await apiPost<{
      status: string
      message?: string
      reason?: string | null
      semantic_ready?: boolean
      duration_s?: number | null
    }>('/models/load', {})
    if (res.status === 'ok') {
      toast.success(`语义层装配成功${res.duration_s != null ? `（${String(res.duration_s)}s）` : ''}`)
    } else {
      toast.error(res.message || res.reason || '装配失败')
    }
    void loadModels()
  } catch (error) {
    console.warn('[SettingsView] 装配模型失败：', error)
  } finally {
    loadBusy.value = false
  }
}

// ------------------------------------------------ T39：安全卡（M9 C5 改密）

/**
 * 🔐 修改管理密码：POST /admin/config/password {current_password, new_password}。
 * 成功（旧令牌立即失效）→ Toast + 立即登出：清 token 回登录页——否则当前会话
 * 继续发请求只会 401（响应拦截器也会跳登录，但主动登出体验更明确）。
 * 失败文案（400 当前密码不正确 / 长度不足等）由 api client 统一 Toast。
 */
async function changePassword(): Promise<void> {
  if (pwSubmitting.value) return
  if (pwNew.value.length < 10) {
    toast.error('新密码长度必须 ≥ 10 位')
    return
  }
  if (pwNew.value !== pwConfirm.value) {
    toast.error('两次输入的新密码不一致')
    return
  }
  pwSubmitting.value = true
  try {
    const res = await apiPost<{ ok: boolean; message?: string }>('/config/password', {
      current_password: pwCurrent.value,
      new_password: pwNew.value,
    })
    if (!res.ok) {
      toast.error(res.message || '密码修改失败')
      return
    }
    toast.success('已修改，旧令牌立即失效')
    const auth = useAuthStore()
    auth.clearToken()
    pwCurrent.value = ''
    pwNew.value = ''
    pwConfirm.value = ''
    void router.push({ name: 'login' })
  } catch (error) {
    console.warn('[SettingsView] 修改密码失败：', error) // 错误文案已由 api client Toast
  } finally {
    pwSubmitting.value = false
  }
}

/** 页面回到前台时立即刷新模型状态（配合 10s 轮询的 document.hidden 跳过） */
function onVisibilityChange(): void {
  if (!document.hidden) void loadModels()
}

/**
 * T38 协作：概览页「系统状态」徽标点击跳转带 ?group=xxx（embedding/llm/
 * light_model/semantic/keyword），本页消费 query——等配置草稿渲染完成后滚动到
 * 对应分组卡并短暂高亮（1.6s）。
 */
async function handleGroupQuery(): Promise<void> {
  const target = router.currentRoute.value.query.group
  if (!target || typeof target !== 'string') return
  await nextTick() // 等 draft 更新后 DOM 渲染出分组卡
  const el = document.getElementById(`group-${target}`)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  highlightGroup.value = target
  window.setTimeout(() => {
    if (highlightGroup.value === target) highlightGroup.value = null
  }, 1600)
}

/** 手动刷新：配置 + 来源 + 模型三合一 */
async function refreshAll(): Promise<void> {
  await Promise.all([loadConfig(), loadSources(), loadModels()])
}

onMounted(() => {
  void loadConfig().then(() => handleGroupQuery())
  void loadSources()
  void loadModels()
  // 页面活动时每 10s 轮询模型状态（隐藏时跳过，回前台即刷）
  modelsTimer = window.setInterval(() => {
    if (!document.hidden) void loadModels()
  }, 10000)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  if (modelsTimer !== undefined) window.clearInterval(modelsTimer)
  if (downloadTimer !== undefined) window.clearInterval(downloadTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <section class="page-view">
    <div class="page-head">
      <h2 class="page-title">⚙️ 系统设置</h2>
      <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="refreshAll">
        🔄 刷新
      </button>
    </div>
    <p class="page-hint">
      全部运行参数按组在线配置（服务监听 / 判定阈值 / Embedding / LLM / 缓存 / 模型 / 语义 / 复核等）；
      <strong>保存即生效</strong>（热应用，无需重启）；端口 / 日志类配置于下次启动生效；密钥仅支持
      环境变量注入（值不回显）。字段旁小徽标 = 当前生效来源。
    </p>

    <!-- 模型卡（v0.3.0 M6：按需下载 / 装配 / 状态，独立于分组表单） -->
    <div class="card model-card">
      <div class="card-title model-title">
        <span>🤖 模型</span>
        <span class="model-sub">按需下载 · 状态每 10s 自动刷新（页面活动时）</span>
      </div>
      <div v-if="modelsLoading && !models" class="loading">模型状态加载中...</div>
      <template v-else>
        <div class="model-row">
          <div class="model-name">🧬 Chinese-CLIP（Embedding）</div>
          <span class="m-badge" :class="clipStatusClass(models?.chinese_clip?.status)">
            {{ clipStatusText(models?.chinese_clip?.status) }}
          </span>
          <div class="model-detail">
            <template v-if="models?.chinese_clip?.backend === 'cloud'">
              <span>{{ models?.chinese_clip?.message || '云端 Embedding 后端（装配/使用经下方测试连接冒烟）' }}</span>
            </template>
            <template v-else>
              <span v-if="downloadProgressText" class="dl-progress">
                ⬇️ {{ downloadProgressText }}
                <span v-if="downloadStageText">（{{ downloadStageText }}）</span>
              </span>
              <span v-if="models?.chinese_clip?.message">{{ models?.chinese_clip?.message }}</span>
              <span v-if="models?.chinese_clip?.cached_files != null" class="dl-progress">
                HF 缓存 {{ models?.chinese_clip?.cached_files }} 个 blobs{{ formatBytes(models?.chinese_clip?.cache_size_bytes) ? ` · ${formatBytes(models?.chinese_clip?.cache_size_bytes)}` : '' }}
              </span>
              <span v-if="models?.chinese_clip?.model_name">模型 {{ models?.chinese_clip?.model_name }}</span>
              <span v-if="models?.chinese_clip?.cache_dir" class="dl-muted">缓存目录 {{ models?.chinese_clip?.cache_dir }}</span>
            </template>
          </div>
          <div class="model-actions">
            <button
              v-if="models?.chinese_clip?.backend === 'local' && models?.chinese_clip?.status === 'not_downloaded'"
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="downloadPolling"
              @click="startDownload"
            >
              ⬇️ 下载模型
            </button>
            <button
              v-if="models?.chinese_clip?.backend === 'local'
                && models?.chinese_clip?.status !== 'not_downloaded'
                && models?.chinese_clip?.status !== 'downloading'"
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="loadBusy || downloadPolling"
              @click="loadModel"
            >
              🔄 {{ loadBusy ? '装配中...' : '装配 / 重新加载' }}
            </button>
            <span v-if="models?.chinese_clip?.backend === 'cloud'" class="dl-muted">后端为云端：用下方 embedding 卡「🔌 测试连接」验证</span>
          </div>
        </div>

        <div class="model-row">
          <div class="model-name">⚡ fasttext（轻量文本模型）</div>
          <span
            class="m-badge"
            :class="models?.fasttext?.status === 'ready'
              ? 'm-badge-ok'
              : models?.fasttext?.status === 'error'
                ? 'm-badge-err'
                : models?.fasttext?.status === 'missing'
                  ? 'm-badge-warn'
                  : 'm-badge-muted'"
          >
            {{ fasttextStatusText(models?.fasttext?.status) }}
          </span>
          <div class="model-detail">
            <span v-if="models?.fasttext?.model_path">模型 {{ models?.fasttext?.model_path }}</span>
            <span v-if="models?.fasttext?.config_path">配置 {{ models?.fasttext?.config_path }}</span>
            <span v-if="models?.fasttext?.status && models?.fasttext?.status !== 'ready'" class="model-guide">
              配置指引：在下方「轻量文本风险模型」分组填写 fasttext.pt 与 config.json
              路径（允许留空 = 未启用）并保存，随后可点该卡「🔌 测试连接」验证。
            </span>
          </div>
          <div class="model-actions"></div>
        </div>

        <div class="model-row">
          <div class="model-name">🧠 语义引擎</div>
          <span class="m-badge" :class="models?.semantic?.ready ? 'm-badge-ok' : 'm-badge-warn'">
            {{ models?.semantic?.ready ? '已就绪' : models?.semantic?.backend === 'cloud' ? '云端' : '待装配' }}
          </span>
          <div class="model-detail">
            <span v-if="models?.semantic?.reason">{{ models?.semantic?.reason }}</span>
            <span class="dl-muted">
              {{ models?.semantic?.ready ? '语义层可参与审核判定' : '首次审核请求或点击 CLIP 行「装配 / 重新加载」时自动装配' }}
            </span>
          </div>
          <div class="model-actions"></div>
        </div>

        <div class="model-row">
          <div class="model-name">📚 向量库</div>
          <span class="m-badge" :class="vectorStoreReady ? 'm-badge-ok' : 'm-badge-muted'">
            {{ vectorStoreReady ? '已就绪' : '为空' }}
          </span>
          <div class="model-detail">
            <span>黑池 {{ models?.vector_store?.black.count ?? 0 }} 条（{{ models?.vector_store?.black.dim ?? '—' }} 维）</span>
            <span>白池 {{ models?.vector_store?.white.count ?? 0 }} 条（{{ models?.vector_store?.white.dim ?? '—' }} 维）</span>
          </div>
          <div class="model-actions"></div>
        </div>
      </template>
    </div>

    <div v-if="loading" class="loading">加载配置中...</div>

    <template v-else>
      <!-- 分组表单卡片 -->
      <div
        v-for="meta in renderGroups"
        :key="meta.group"
        :id="`group-${meta.group}`"
        class="card group-card"
        :class="{ 'group-highlight': highlightGroup === meta.group }"
      >
        <div class="card-title group-title">
          <span class="group-title-text" :title="meta.group">{{ meta.icon }} {{ meta.title }}</span>
          <div class="group-actions">
            <button
              v-if="CHANNEL_BY_GROUP[meta.group]"
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="savingGroup !== null || testingChannel !== null"
              @click="testConnection(meta.group)"
            >
              {{ testingChannel === CHANNEL_BY_GROUP[meta.group] ? '测试中...' : '🔌 测试连接' }}
            </button>
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

        <!-- 测试连接内联结果条（v0.3.0 M5：✅ 通过 / ❌ 失败） -->
        <div
          v-if="channelResults[meta.group]"
          class="conn-result"
          :class="channelResults[meta.group]?.ok ? 'conn-ok' : 'conn-fail'"
        >
          <span class="conn-icon">{{ channelResults[meta.group]?.ok ? '✅' : '❌' }}</span>
          <div class="conn-body">
            <div class="conn-msg">{{ channelResults[meta.group]?.message }}</div>
            <div v-if="connDetail(meta.group)" class="conn-detail">{{ connDetail(meta.group) }}</div>
          </div>
        </div>

        <div v-for="(section, si) in meta.sections" :key="si" class="cfg-section">
          <div class="cfg-section-title">{{ section.title }}</div>
          <p v-if="section.desc" class="cfg-section-desc">{{ section.desc }}</p>

          <div class="field-grid">
            <div v-for="field in section.fields" :key="field.key" class="field-cell">
              <!-- 密钥只读徽标 -->
              <template v-if="field.kind === 'secret'">
                <span class="field-label-row">
                  <span class="field-label">{{ field.label }}</span>
                  <span
                    class="src-badge"
                    :class="srcBadgeClass(meta.group, field.key)"
                    :title="`来源：${sourceText(sourceOf(meta.group, field.key))}`"
                  >
                    {{ sourceText(sourceOf(meta.group, field.key)) }}
                  </span>
                </span>
                <div class="secret-badge">
                  <span
                    class="tag"
                    :class="secretOf(meta.group, field.key)?.configured ? 'tag-success' : 'tag-orange'"
                  >
                    🔑 环境变量：
                    {{ textOf(secretOf(meta.group, field.key)?.api_key_env) || '—' }}
                    · {{ secretOf(meta.group, field.key)?.configured ? '已配置' : '未配置' }}
                  </span>
                  <p class="secret-note">密钥仅支持环境变量注入（SAFEFUSION_* 或上文 api_key_env 指定变量），不写入配置存储；保存时该字段不参与提交。</p>
                </div>
              </template>

              <!-- bool 开关 -->
              <template v-else-if="field.kind === 'bool'">
                <span class="field-label-row">
                  <span class="field-label">{{ field.label }}</span>
                  <span
                    class="src-badge"
                    :class="srcBadgeClass(meta.group, field.key)"
                    :title="`来源：${sourceText(sourceOf(meta.group, field.key))}`"
                  >
                    {{ sourceText(sourceOf(meta.group, field.key)) }}
                  </span>
                </span>
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
                <span class="field-label-row">
                  <span class="field-label">{{ field.label }}</span>
                  <span
                    class="src-badge"
                    :class="srcBadgeClass(meta.group, field.key)"
                    :title="`来源：${sourceText(sourceOf(meta.group, field.key))}`"
                  >
                    {{ sourceText(sourceOf(meta.group, field.key)) }}
                  </span>
                </span>
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
                <span class="field-label-row">
                  <span class="field-label">{{ field.label }}</span>
                  <span
                    class="src-badge"
                    :class="srcBadgeClass(meta.group, field.key)"
                    :title="`来源：${sourceText(sourceOf(meta.group, field.key))}`"
                  >
                    {{ sourceText(sourceOf(meta.group, field.key)) }}
                  </span>
                </span>
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
                <span class="field-label-row">
                  <span class="field-label">{{ field.label }}</span>
                  <span
                    class="src-badge"
                    :class="srcBadgeClass(meta.group, field.key)"
                    :title="`来源：${sourceText(sourceOf(meta.group, field.key))}`"
                  >
                    {{ sourceText(sourceOf(meta.group, field.key)) }}
                  </span>
                </span>
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

      <!-- 🔐 安全卡（v0.3.0 M9 C5 改密） -->
      <div class="card security-card">
        <div class="card-title">🔐 安全（管理密码）</div>
        <div class="pw-grid">
          <div class="field-cell">
            <span class="field-label">当前密码</span>
            <input
              v-model="pwCurrent"
              type="password"
              class="input"
              autocomplete="current-password"
              placeholder="请输入当前管理密码"
            />
          </div>
          <div class="field-cell">
            <span class="field-label">新密码（至少 10 位）</span>
            <input
              v-model="pwNew"
              type="password"
              class="input"
              autocomplete="new-password"
              placeholder="至少 10 位"
            />
          </div>
          <div class="field-cell">
            <span class="field-label">确认新密码</span>
            <input
              v-model="pwConfirm"
              type="password"
              class="input"
              autocomplete="new-password"
              placeholder="再次输入新密码"
            />
          </div>
        </div>
        <p class="pw-note">
          修改后新令牌立即生效、旧令牌立即失效，所有已登录会话（含本页）
          <strong>将退出并回到登录页</strong>，请用新密码重新登录。若设置了
          <code>ADMIN_PASSWORD</code> 环境变量，重启后以环境变量为准（env 只覆盖内存不写 DB）。
        </p>
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="pwSubmitting"
          @click="changePassword"
        >
          {{ pwSubmitting ? '提交中...' : '🔑 修改密码' }}
        </button>
      </div>

      <!-- 配置优先级与来源标识说明（决策 D / v0.3.0 M4） -->
      <p class="priority-note">
        配置优先级：内置默认 &lt; config.yaml &lt; 管理端配置（DB） &lt; 环境变量
        （环境变量最高优先；被 SAFEFUSION_&lt;路径&gt;_&lt;键&gt; 钉住的键不受 DB 影响）。
        字段旁徽标 = 当前生效来源：默认（淡化）/ YAML（灰）/ 数据库（蓝）/ 环境变量（橙）。
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

/* 标题下操作提示行（PRD v0.3.0 §M1：每页用途 + 主操作） */
.page-hint {
  font-size: 0.76rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: 0 0 14px;
}

/* 来源徽标（T39：default/yaml/db/env 四色语义） */
.field-label-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.src-badge {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 6px;
  font-size: 0.68rem;
  font-weight: 600;
  line-height: 1.5;
  white-space: nowrap;
}

.src-default {
  background: transparent;
  color: var(--text-3);
  border: 1px dashed var(--border);
}

.src-yaml {
  background: var(--surface-hover);
  color: var(--text-2);
}

.src-db {
  background: var(--primary-light);
  color: var(--primary);
}

.src-env {
  background: var(--orange-light);
  color: var(--orange);
}

/* 测试连接内联结果条（T39：✅ 通过 / ❌ 失败） */
.conn-result {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  font-size: 0.78rem;
  margin: 0 0 12px;
  line-height: 1.6;
}

.conn-ok {
  background: var(--success-light);
  color: var(--success);
}

.conn-fail {
  background: var(--danger-light);
  color: var(--danger);
}

.conn-icon {
  flex-shrink: 0;
  line-height: 1.6;
}

.conn-body {
  min-width: 0;
}

.conn-msg {
  font-weight: 600;
}

.conn-detail {
  opacity: 0.85;
  word-break: break-all;
}

/* 分组卡片 */
.group-card {
  margin-bottom: 16px;
}

/* T38 跳转落地高亮（1.6s 后清除） */
.group-highlight {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
  transition: outline-color 0.2s ease;
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

/* ---------------- T39：模型卡（M6） ---------------- */
.model-title {
  align-items: baseline;
  margin-bottom: 8px;
}

.model-sub {
  font-size: 0.72rem;
  font-weight: 400;
  color: var(--text-3);
}

.model-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  padding: 11px 0;
  border-top: 1px dashed var(--border);
}

.model-row:first-of-type {
  border-top: none;
  padding-top: 4px;
}

.model-name {
  font-size: 0.8rem;
  font-weight: 700;
  color: var(--text-2);
  min-width: 160px;
}

.m-badge {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}

.m-badge-ok {
  background: var(--success-light);
  color: var(--success);
}

.m-badge-err {
  background: var(--danger-light);
  color: var(--danger);
}

.m-badge-warn {
  background: var(--orange-light);
  color: var(--orange);
}

.m-badge-alt {
  background: var(--primary-light);
  color: var(--primary);
}

.m-badge-muted {
  background: var(--surface-hover);
  color: var(--text-3);
}

.model-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 14px;
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.6;
  min-width: 0;
  flex: 1;
}

.model-guide {
  color: var(--text-2);
}

.dl-progress {
  color: var(--primary);
  font-weight: 600;
}

.dl-stage {
  opacity: 0.85;
  font-weight: 400;
}

.dl-muted {
  opacity: 0.85;
}

.model-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}

/* ---------------- T39：安全卡（改密） ---------------- */
.security-card {
  margin-top: 4px;
}

.pw-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px 18px;
}

.pw-note {
  font-size: 0.74rem;
  color: var(--text-3);
  line-height: 1.7;
  margin: 12px 0;
}

.pw-note code {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.7rem;
}
</style>