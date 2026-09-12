/**
 * 系统设置页字段元数据（T57c/F11 拆分自 SettingsView.vue：纯数据模块，无 Vue 依赖）。
 * 导出类型 FieldKind / FieldMeta / SectionMeta / GroupMeta / MaskedSecret 与
 * 分组字段清单 GROUP_META（依据 src/safefusion/config.py 各模型字段与 description）。
 *
 * 注意：所有 str 字段（除 null 可空外）后端 validate_group_update 会做必填
 * 非空校验，故 label 里同步标注「必填」。
 *
 * T56 补遗（T57c 追加）：thresholds 组补 top_k / black_top_k / white_top_k 三字段
 * （语义 Top-K 可配置，buildPayload 按 GROUP_META 遍历，缺声明即无法保存）。
 */

/** 字段控件类型映射（依据 config.py 各分组模型字段注解） */
export type FieldKind = 'bool' | 'int' | 'float' | 'text' | 'select' | 'secret' | 'json'

/** 字段元数据（label 来自 config.py description，类型来自模型注解） */
export interface FieldMeta {
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

export interface SectionMeta {
  title: string
  desc?: string
  fields: FieldMeta[]
}

export interface GroupMeta {
  group: string
  title: string
  icon: string
  desc?: string
  sections: SectionMeta[]
  /** 由 synthesizeGroup 生成的兜底分组（GROUP_META 未收录）标记 */
  synthetic?: boolean
}

/** 密钥遮蔽对象（config_override.mask_secret_fields 输出形态） */
export interface MaskedSecret {
  api_key_env: string | null
  configured: boolean
}

/**
 * 分组字段清单（依据 src/safefusion/config.py 各模型字段与 description）。
 */
export const GROUP_META: GroupMeta[] = [
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
          // P8 漂移修复：后端 config.py black_white_gap 此前面板未声明（无法改）
          {
            key: 'black_white_gap',
            label: 'black_white_gap：黑顶分−白顶分最小差距（防单条巧合误判，必填）',
            kind: 'float',
            min: 0,
            max: 1,
            hint: '单条黑相似度再高，与白顶分差距不足此值不判违规',
          },
          { key: 'confidence_low', label: '置信度低档上界，低于则判定安全（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'confidence_high', label: '置信度高档下界，高于则判定违规（必填）', kind: 'float', min: 0, max: 1 },
          { key: 'phash_whitelist_distance', label: '图片白名单 pHash 汉明距离阈值（必填）', kind: 'int', min: 0, max: 64 },
          { key: 'phash_dedup_distance', label: '图片去重缓存近似命中 pHash 阈值（必填）', kind: 'int', min: 0, max: 64 },
          // T56：语义 Top-K 可配置（src/safefusion/config.py:57-59 description 对齐）
          {
            key: 'top_k',
            label: 'top_k：语义检索 Top-K（黑/白库默认召回数，范围 1~50，必填）',
            kind: 'int',
            min: 1,
            max: 50,
          },
          {
            key: 'black_top_k',
            label: 'black_top_k：黑库独立 Top-K；null 跟随 top_k（可空）',
            kind: 'int',
            nullable: true,
            min: 1,
            max: 50,
            hint: '留空 = null（跟随 top_k）',
          },
          {
            key: 'white_top_k',
            label: 'white_top_k：白库独立 Top-K；null 跟随 top_k（可空）',
            kind: 'int',
            nullable: true,
            min: 1,
            max: 50,
            hint: '留空 = null（跟随 top_k）',
          },
        ],
      },
    ],
  },
  {
    group: 'embedding',
    title: 'Embedding 双后端',
    icon: '🧬',
    desc: 'backend 切换 local/cloud；支持多 provider（多模型/渠道）与多向量库映射；云端 Key 仅环境变量注入，此处只显示变量名',
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
          {
            key: 'cloud.image_protocol',
            label: 'cloud.image_protocol（云端图片协议）',
            kind: 'select',
            options: ['openai', 'llamacpp'],
            hint: 'openai=OpenAI 兼容 /v1/embeddings+input；llamacpp=llama.cpp 多模态 /embeddings+content+multimodal_data',
          },
          { key: 'cloud.image_max_side', label: 'cloud.image_max_side：云端图片发送前最长边缩放上限（px，0=不缩放）', kind: 'int', min: 0 },
          { key: 'cloud.image_quality', label: 'cloud.image_quality：云端图片 JPEG 压缩质量（1~95）', kind: 'int', min: 1, max: 95 },
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
          // P8 漂移修复：后端 config.py cloud.allow_no_key 此前面板未声明（无法改）
          {
            key: 'cloud.allow_no_key',
            label: 'cloud.allow_no_key：本地无鉴权服务允许无 Key（如 llama.cpp --embeddings）',
            kind: 'bool',
            hint: '默认 false 强制 Key；本地 llama.cpp 无鉴权服务可置 true',
          },
        ],
      },
      {
        title: '多 Provider（多模型/渠道）',
        desc: '每个 provider 可配置 backend/local/cloud/vector_store/priority；active_provider 手动指定首选。留空数组表示使用上方单后端兼容模式。',
        fields: [
          { key: 'active_provider', label: 'active_provider：手动指定首选 provider 名称', kind: 'text', nullable: true },
          {
            key: 'providers',
            label: 'providers：JSON 数组（编辑后点保存生效）',
            kind: 'json',
            hint: '示例：[{"name":"local-clip","backend":"local","local":{"model_name":"OFA-Sys/chinese-clip-vit-base-patch16","device":"auto"},"vector_store":"clip","priority":1}]',
          },
          { key: 'failover.enabled', label: 'failover.enabled：熔断开关', kind: 'bool' },
          { key: 'failover.cooldown_seconds', label: 'failover.cooldown_seconds：冷却时长（秒，必填）', kind: 'float', min: 0 },
          { key: 'failover.max_failures', label: 'failover.max_failures：连续失败阈值（必填）', kind: 'int', min: 1 },
        ],
      },
      {
        title: '多向量库（vector_stores）',
        desc: '每个向量库有唯一 name + 相对 data_dir 的 path；provider 通过 vector_store 引用。启动只登记路径，按需懒加载。',
        fields: [
          {
            key: 'vector_stores',
            label: 'vector_stores：JSON 数组（编辑后点保存生效）',
            kind: 'json',
            hint: '示例：[{"name":"clip","path":"vectors/clip"},{"name":"wemm","path":"vectors/wemm"}]',
          },
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
      {
        title: '多 Provider（多模型/渠道）',
        desc: '每个 provider 可配置 base_url/model/api_key_env/priority；active_provider 手动指定首选。留空数组表示使用上方单后端兼容模式。',
        fields: [
          { key: 'active_provider', label: 'active_provider：手动指定首选 provider 名称', kind: 'text', nullable: true },
          {
            key: 'providers',
            label: 'providers：JSON 数组（编辑后点保存生效）',
            kind: 'json',
            hint: '示例：[{"name":"openai","base_url":"https://api.openai.com/v1","model":"gpt-4o-mini","priority":1},{"name":"deepseek","base_url":"https://api.deepseek.com/v1","model":"deepseek-chat","priority":2}]',
          },
          { key: 'failover.enabled', label: 'failover.enabled：熔断开关', kind: 'bool' },
          { key: 'failover.cooldown_seconds', label: 'failover.cooldown_seconds：冷却时长（秒，必填）', kind: 'float', min: 0 },
          { key: 'failover.max_failures', label: 'failover.max_failures：连续失败阈值（必填）', kind: 'int', min: 1 },
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
    group: 'rerank',
    title: 'Rerank 多提供者',
    icon: '🔁',
    desc: 'PRD v0.4.0 M2：远程 ReRanker 配置。开关仍在「语义层」的 rerank_enabled；providers 为空且开关打开时回退本地 CLIP 重排。',
    sections: [
      {
        title: '多 Provider（本地/远程）',
        desc: '每个 provider 可配置 type=local/cloud；cloud 需 base_url/model/api_key_env。active_provider 手动指定首选。',
        fields: [
          { key: 'active_provider', label: 'active_provider：手动指定首选 provider 名称', kind: 'text', nullable: true },
          {
            key: 'providers',
            label: 'providers：JSON 数组（编辑后点保存生效）',
            kind: 'json',
            hint: '示例：[{"name":"local-clip","type":"local","priority":1},{"name":"cloud-jina","type":"cloud","base_url":"https://api.example.com/v1","model":"jina-reranker-v2-base-multilingual","api_key_env":"JINA_API_KEY","priority":2}]',
          },
          { key: 'failover.enabled', label: 'failover.enabled：熔断开关', kind: 'bool' },
          { key: 'failover.cooldown_seconds', label: 'failover.cooldown_seconds：冷却时长（秒，必填）', kind: 'float', min: 0 },
          { key: 'failover.max_failures', label: 'failover.max_failures：连续失败阈值（必填）', kind: 'int', min: 1 },
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
