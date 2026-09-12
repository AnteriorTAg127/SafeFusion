/**
 * 模型卡组合式函数（T57c/F11 拆分自 SettingsView.vue）：
 * 承载「🤖 模型」卡的全部状态与轮询机——GET /admin/models 状态、页面活动时
 * 10s 轮询（document.hidden 跳过、visibilitychange 回前台即刷）、下载 POST →
 * 1s 进度轮询（completed/failed 收尾）、装配 POST /models/load。
 *
 * 接口：useModelCard() → { models, modelsLoading, downloadTask, downloadPolling,
 * loadBusy, downloadProgressText, downloadStageText, vectorStoreReady,
 * clipStatusText, fasttextStatusText, clipStatusClass, formatBytes,
 * loadModels, startDownload, loadModel }。
 * 生命周期（onMounted/onUnmounted 清理定时器与监听）内聚于本组合式，
 * SettingsView 只消费返回值；对外行为与拆分前一致。
 */

import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import { apiGet, apiPost } from '../api/client'
import { useToastStore } from '../stores/toast'

/** GET /admin/models 响应（api/admin.py list_models） */
export interface ModelsResponse {
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
    name?: string
    path?: string
    available?: string[]
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
export interface DownloadTask {
  task_id: string
  model_name?: string
  status: 'running' | 'completed' | 'failed'
  stage?: string
  progress?: number
  downloaded_bytes?: number
  total_bytes?: number
  error?: string | null
}

/** 模型状态轮询周期：10s（页面活动时；隐藏跳过，回前台即刷） */
const MODELS_POLL_MS = 10_000
/** 下载进度轮询周期：1s（下载任务进行期间） */
const DOWNLOAD_POLL_MS = 1_000

export function useModelCard(): {
  models: Ref<ModelsResponse | null>
  modelsLoading: Ref<boolean>
  downloadTask: Ref<DownloadTask | null>
  downloadPolling: Ref<boolean>
  loadBusy: Ref<boolean>
  downloadProgressText: ComputedRef<string>
  downloadStageText: ComputedRef<string>
  vectorStoreReady: ComputedRef<boolean>
  clipStatusText: (status: string | undefined) => string
  fasttextStatusText: (status: string | undefined) => string
  clipStatusClass: (status: string | undefined) => string
  formatBytes: (value: number | null | undefined) => string
  loadModels: () => Promise<void>
  startDownload: () => Promise<void>
  loadModel: () => Promise<void>
} {
  const toast = useToastStore()

  const models = ref<ModelsResponse | null>(null)
  const modelsLoading = ref(false)
  const downloadTask = ref<DownloadTask | null>(null)
  const downloadPolling = ref(false)
  const loadBusy = ref(false)

  /** 轮询句柄：模型状态 10s（页面活动时）/ 下载进度 1s（任务期间） */
  let modelsTimer: number | undefined
  let downloadTimer: number | undefined

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
      console.warn('[useModelCard] 加载模型状态失败：', error)
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
      console.warn('[useModelCard] 启动模型下载失败：', error)
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
        console.warn('[useModelCard] 轮询下载进度失败：', error)
        finishDownload(false, '下载进度查询失败，请刷新页面查看模型状态')
      }
    }, DOWNLOAD_POLL_MS)
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
      console.warn('[useModelCard] 装配模型失败：', error)
    } finally {
      loadBusy.value = false
    }
  }

  /** 页面回到前台时立即刷新模型状态（配合 10s 轮询的 document.hidden 跳过） */
  function onVisibilityChange(): void {
    if (!document.hidden) void loadModels()
  }

  onMounted(() => {
    void loadModels()
    // 页面活动时每 10s 轮询模型状态（隐藏时跳过，回前台即刷）
    modelsTimer = window.setInterval(() => {
      if (!document.hidden) void loadModels()
    }, MODELS_POLL_MS)
    document.addEventListener('visibilitychange', onVisibilityChange)
  })

  onUnmounted(() => {
    if (modelsTimer !== undefined) window.clearInterval(modelsTimer)
    if (downloadTimer !== undefined) window.clearInterval(downloadTimer)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return {
    models,
    modelsLoading,
    downloadTask,
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
  }
}
