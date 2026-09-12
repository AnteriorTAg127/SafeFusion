<script setup lang="ts">
/**
 * 系统设置页（T25 全量配置表单 + T36 操作提示 + T39 v0.3.0 改造 + T57c/F11 拆分）：
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
 * - 模型卡（v0.3.0 M6，T39 + T57c/F11）：状态/下载/装配轮询机整体迁入
 *   composables/useModelCard.ts，本页仅消费返回值（模板保持不变）。
 * - 安全卡（v0.3.0 M9 C5，T39）：🔐 改密——当前密码 + 新密码（≥10 位）+ 确认
 *   新密码 → POST /admin/config/password；成功 Toast「已修改，旧令牌立即失效」
 *   并**立即登出**（旧令牌已失效，本会话继续发请求只会 401，必须回登录页）。
 * - 422 / 400 错误文案由 api client 统一 Toast（兼容 {error} 与 {detail} 两种
 *   错误体），前端不再重复弹窗。
 *
 * T57c/F11 拆分结构：
 * - views/settings/configFields.ts：GROUP_META 及其类型（FieldMeta/SectionMeta/
 *   GroupMeta/MaskedSecret/FieldKind）整体迁出，导出类型不变；
 * - views/settings/configForm.ts：无 Vue 依赖的纯表单引擎（flattenGroup /
 *   setNested / buildPayload / synthesizeGroup / detectKind / isMaskedSecret），
 *   buildPayload 错误经 { ok:false, error } 返回，本页负责弹 Toast；
 * - composables/useModelCard.ts：模型卡状态 + 10s 轮询 + 下载 1s 轮询 +
 *   装配调用（onMounted/onUnmounted 清理内聚在组合式内）；
 * - 本地 textOf 删除，改 import utils/format（T57b 移交项）。
 * 对外行为零变化：保存/恢复默认/测试连接/改密/下载/装配/来源徽标/JSON 数组编辑原样。
 *
 * TODO(歧义-已知后端行为)：
 * 1) PUT 部分键覆盖语义：前端提交全量非密钥字段，若后端语义变化可退回仅提交
 *    改动字段（当前全量提交在两种语义下均安全）；
 * 2) logging.level / image.animated.mode / embedding.local.device 后端暂无
 *    白名单校验（仅业务规则校验 backend/fuse_mode），select 提供已知合法值，
 *    服务端返回未知取值时追加「(未知)」选项展示，不静默丢失；
 * 3) 密钥 configured 反映「当前进程环境变量是否已设置」，前端只读（决策 F）；
 *    保存即热应用生效（applied=false 仅出现在未注入容器的测试部署）。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { apiGet, apiPost, apiPut } from '../api/client'
import router from '../router'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { textOf } from '../utils/format'
import { GROUP_META } from './settings/configFields'
import type { FieldMeta, GroupMeta, MaskedSecret } from './settings/configFields'
import { buildPayload, flattenGroup, isMaskedSecret, synthesizeGroup } from './settings/configForm'
import { useModelCard } from '../composables/useModelCard'

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

/** 模型卡（T57c/F11：状态/轮询/下载/装配整体迁入 composables/useModelCard.ts） */
const {
  models,
  modelsLoading,
  downloadPolling,
  loadBusy,
  downloadProgressText,
  downloadStageText,
  vectorStoreReady,
  clipStatusText,
  fasttextStatusText,
  clipStatusClass,
  formatBytes,
  loadModels,
  startDownload,
  loadModel,
} = useModelCard()

/** 改密表单（POST /admin/config/password） */
const pwCurrent = ref('')
const pwNew = ref('')
const pwConfirm = ref('')
const pwSubmitting = ref(false)

/** T38 跳转高亮：?group=xxx 落地分组卡后短暂描边（highlightGroup === 分组名） */
const highlightGroup = ref<string | null>(null)

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

function secretOf(group: string, key: string): MaskedSecret | null {
  const value = draft.value[group]?.[key]
  return isMaskedSecret(value) ? value : null
}

function fieldValue(group: string, key: string): unknown {
  return draft.value[group]?.[key]
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

/**
 * 渲染分组清单：以 GET /admin/config 实际返回的分组为准（决策：按响应渲染），
 * 已知分组取 GROUP_META 静态元数据（label/类型/枚举/范围），未知分组走
 * synthesizeGroup 自动降级（纯函数见 configForm.ts）。后端分组白名单与其模型
 * 字段同步演进时页面不破。
 */
const renderGroups = computed<GroupMeta[]>(() => {
  return Object.keys(draft.value).map((group) => GROUP_BY_KEY.get(group) ?? synthesizeGroup(group, draft.value[group]))
})

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
  const meta = renderGroups.value.find((g) => g.group === group)
  if (!meta) return
  // buildPayload 纯化（configForm.ts）：错误经 { ok:false, error } 返回，此处弹 Toast
  // （与拆分前「toast.error 首条错误并中止」行为一致）
  const built = buildPayload(meta, draft.value[group] ?? {})
  if (!built.ok) {
    toast.error(built.error)
    return
  }
  savingGroup.value = group
  try {
    const res = await apiPut<ConfigPutResult>(`/config/${group}`, built.payload)
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

    <!-- 模型卡（v0.3.0 M6：按需下载 / 装配 / 状态，独立于分组表单；状态机迁入 useModelCard） -->
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
            <span v-if="models?.vector_store?.name">当前库：{{ models?.vector_store?.name }}</span>
            <span v-if="models?.vector_store?.path" class="dl-muted">路径 {{ models?.vector_store?.path }}</span>
            <span>黑池 {{ models?.vector_store?.black.count ?? 0 }} 条（{{ models?.vector_store?.black.dim ?? '—' }} 维）</span>
            <span>白池 {{ models?.vector_store?.white.count ?? 0 }} 条（{{ models?.vector_store?.white.dim ?? '—' }} 维）</span>
            <span v-if="models?.vector_store?.available?.length" class="dl-muted">
              可用库：{{ models?.vector_store?.available.join(' / ') }}
            </span>
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

              <!-- JSON 数组/对象编辑（providers / vector_stores 等） -->
              <template v-else-if="field.kind === 'json'">
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
                <textarea
                  class="input json-textarea"
                  rows="6"
                  :value="textOf(fieldValue(meta.group, field.key))"
                  :placeholder="field.nullable ? '留空 = null（未配置）' : ''"
                  @input="onTextInput(meta.group, field.key, $event)"
                ></textarea>
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

/* JSON 数组/对象编辑（providers / vector_stores） */
.json-textarea {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 0.74rem;
  line-height: 1.5;
  resize: vertical;
  min-height: 96px;
  white-space: pre;
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

/* F4③：.tag 系已全局化于 style.css（自本页收敛，含暗色变量适配）；
   此处仅保留本页差异覆盖——密钥徽标在纵向 flex 容器内需顶部对齐 */
.tag {
  align-self: flex-start;
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
