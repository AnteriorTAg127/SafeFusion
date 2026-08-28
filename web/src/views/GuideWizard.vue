<script lang="ts">
/**
 * 普通 script 块：承载供外部（GuideMenu）引用的具名导出。
 * <script setup> 不允许 ES module 导出，常量放这里共享。
 */
export const WIZARD_DONE_KEY = 'sf_guide_wizard_done'
</script>

<script setup lang="ts">
/**
 * 首次使用三步向导（PRD v0.3.0 §M9 A1，T41）：
 * ① 令牌与环境 → ② 数据准备 → ③ 模型与向量库
 * - 完成度判定只基于后端真实计数（/admin/health 数据概况 + /admin/models 状态），
 *   与 PRD 风险表「首次向导误判完成度 → 完成度只基于后端真实计数」一致；
 *   展示触发条件按 PRD A1「词库=0 或 模型未就绪」，本卡判定可简化：
 *   入口挂 GuideMenu（打开即见），不做登录自动弹窗。
 * - 数据源（契约已核对 src/safefusion/api/admin.py）：
 *   GET /admin/health → data: { keywords, vector_black, vector_white,
 *   whitelist_images, rules }（admin_health 端点）
 *   GET /admin/models → chinese_clip.status / fasttext.status /
 *   vector_store.{black,white}.count / semantic.ready
 * - 完成：三步全部就绪（② 词库 > 0 且 ③ semantic.ready）→ 自动标记完成并写入
 *   localStorage sf_guide_wizard_done=1（与 GuideMenu 约定），GuideMenu 隐藏主入口；
 *   「跳过」与末步未就绪时的「完成」仅关闭、不写完成标记（完成度只基于真实计数，
 *   PRD A1 可再次从指南进入）。
 * - 弹窗形态独立浮层（自定义遮罩，宽度不受 AppModal 420px 限制）；
 *   不注册路由（避免与 T37 的 router 改动冲突）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { apiGet } from '../api/client'

const props = defineProps<{ show: boolean }>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

/** GET /admin/health 数据概况（只看本向导用到的字段） */
interface HealthData {
  data?: {
    keywords?: number
    vector_black?: number
    vector_white?: number
    whitelist_images?: number
    rules?: number
  }
  components?: Record<string, Record<string, unknown>>
}

/** GET /admin/models 状态（只看本向导用到的字段） */
interface ModelsData {
  chinese_clip?: { status?: string; message?: string | null }
  fasttext?: { status?: string }
  vector_store?: {
    black?: { count?: number; dim?: number | null }
    white?: { count?: number; dim?: number | null }
  }
  semantic?: { ready?: boolean; status?: string; reason?: string | null }
}

const steps = ['令牌与环境', '数据准备', '模型与向量库'] as const
const current = ref(0)

const loading = ref(false)
const health = ref<HealthData | null>(null)
const models = ref<ModelsData | null>(null)
const router = useRouter()

// ---------- 工具 ----------
function numOf(value: number | undefined): number {
  return typeof value === 'number' ? value : 0
}

/** 模型状态码 → 中文短文案（chinese_clip.status / fasttext.status） */
function modelStatusText(status: string | undefined): string {
  const map: Record<string, string> = {
    ready: '已就绪',
    cloud: '云端后端',
    downloading: '下载中',
    not_downloaded: '未下载',
    not_configured: '未配置',
    missing: '文件缺失',
    error: '异常',
  }
  return status ? (map[status] ?? status) : '—'
}

/** 就绪/未就绪 徽标（绿/橙红） */
function badgeClass(ok: boolean): string {
  return ok ? 'badge badge-ok' : 'badge badge-no'
}

// ---------- 完成度判定（只基于后端真实计数） ----------
/** ② 数据准备：词库 > 0 即就绪（PRD A1 触发条件「词库=0」的否定） */
const dataReady = computed(() => numOf(health.value?.data?.keywords) > 0)
/** ③ 模型与向量库：语义引擎已装配即就绪（/admin/models semantic.ready） */
const modelReady = computed(() => models.value?.semantic?.ready === true)
const allReady = computed(() => dataReady.value && modelReady.value)

// ---------- 加载 ----------
async function refresh(): Promise<void> {
  loading.value = true
  try {
    const [h, m] = await Promise.all([
      apiGet<HealthData>('/health'),
      apiGet<ModelsData>('/models'),
    ])
    health.value = h
    models.value = m
    if (allReady.value) {
      // 全部就绪：立即标记完成 → GuideMenu 隐藏「🚀 首次使用向导」主入口
      localStorage.setItem(WIZARD_DONE_KEY, '1')
    }
  } catch (error) {
    // 失败由 api 层统一 Toast（401 除外）；本页继续展示空状态与重试入口
    console.warn('[GuideWizard] 完成度检测失败：', error)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      current.value = 0
      void refresh()
    }
  },
)

onMounted(() => {
  if (props.show) void refresh()
})

// ---------- 操作 ----------
/** 跳转对应页面（② 数据准备 / ③ 模型卡）并收起向导 */
function go(name: string): void {
  // ③「跳转设置页模型卡」：模型卡为 T39 产物，先落路由一级；T39 落地后若提供
  // 锚点定位再追加 query（TODO，见报告）
  void router.push({ name })
  emit('close')
}

function next(): void {
  if (current.value < steps.length - 1) current.value += 1
}

/** 三步全部就绪后的「完成」：写完成标记并关闭（入口随之隐藏，GuideMenu 提供「重新打开」） */
function finish(): void {
  localStorage.setItem(WIZARD_DONE_KEY, '1')
  emit('close')
}

/** 未全部就绪时在末步点「完成/结束」：仅关闭不写标记（PRD A1 完成度只基于真实计数，入口保留可再进） */
function close(): void {
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="wizard-fade">
      <div v-if="props.show" class="wizard-mask">
        <div class="wizard-panel" role="dialog" aria-label="首次使用向导">
          <div class="wizard-head">
            <div class="wizard-title">🚀 首次使用向导（三步）</div>
            <button type="button" class="wizard-close" aria-label="关闭" @click="emit('close')">
              ✕
            </button>
          </div>
          <div class="wizard-steps">
            <span
              v-for="(label, index) in steps"
              :key="label"
              :class="['wizard-step', { active: index === current, done: index < current }]"
            >
              {{ index + 1 }}. {{ label }}
            </span>
          </div>

          <div class="wizard-body">
            <!-- 检测中 -->
            <div v-if="loading" class="wizard-loading">⏳ 正在检测系统状态…</div>

            <!-- 全部就绪 -->
            <div v-else-if="allReady" class="wizard-done-view">
              <div class="done-icon">🎉</div>
              <p class="done-title">全部就绪，可以开始使用了！</p>
              <p class="done-desc">
                关键词词库已有 {{ numOf(health?.data?.keywords) }} 条，语义引擎已装配。
                向导入口将自动收起；后续需要时可在「指南」面板顶部点击「🧙 重新打开」。
              </p>
            </div>

            <!-- 步骤内容 -->
            <template v-else>
              <!-- ① 令牌与环境 -->
              <div v-if="current === 0" class="wizard-step-content">
                <p class="step-desc">
                  管理端所有请求通过 <code>X-Admin-Token</code> 请求头鉴权，令牌来源如下
                  （登录页底部已做相同说明）：
                </p>
                <ul class="step-list">
                  <li>配置文件 <code>admin_token</code>（config.yaml，DB 化后为「系统设置 → server 组」）；</li>
                  <li>环境变量 <code>ADMIN_PASSWORD</code>；</li>
                  <li>
                    两者均未设置时，服务启动日志打印一次随机令牌（仅此一次，请立即保存）。
                  </li>
                </ul>
                <div class="step-status">
                  <span :class="badgeClass(true)">✅ 就绪</span>
                  <span class="status-note">已登录即视为完成（登录页已说明令牌来源）</span>
                </div>
              </div>

              <!-- ② 数据准备 -->
              <div v-else-if="current === 1" class="wizard-step-content">
                <p class="step-desc">
                  数据直接决定审核质量；以下计数来自 <code>GET /admin/health</code> 数据概况，
                  词库为就绪门槛，其余项按需准备。
                </p>
                <table class="wizard-table">
                  <tbody>
                    <tr>
                      <td>关键词词库</td>
                      <td>{{ numOf(health?.data?.keywords) }} 条</td>
                      <td>
                        <span :class="badgeClass(dataReady)">
                          {{ dataReady ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td>
                        <button type="button" class="link-btn" @click="go('keywords')">→ 去词库管理</button>
                      </td>
                    </tr>
                    <tr>
                      <td>白名单图片</td>
                      <td>{{ numOf(health?.data?.whitelist_images) }} 张</td>
                      <td>
                        <span :class="badgeClass(numOf(health?.data?.whitelist_images) > 0)">
                          {{ numOf(health?.data?.whitelist_images) > 0 ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td>
                        <button type="button" class="link-btn" @click="go('whitelist')">→ 去图片白名单</button>
                      </td>
                    </tr>
                    <tr>
                      <td>正则消歧规则</td>
                      <td>{{ numOf(health?.data?.rules) }} 条</td>
                      <td>
                        <span :class="badgeClass(numOf(health?.data?.rules) > 0)">
                          {{ numOf(health?.data?.rules) > 0 ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td>
                        <button type="button" class="link-btn" @click="go('rules')">→ 去规则管理</button>
                      </td>
                    </tr>
                    <tr>
                      <td>向量库（黑 / 白池）</td>
                      <td>{{ numOf(health?.data?.vector_black) }} / {{ numOf(health?.data?.vector_white) }} 条</td>
                      <td>
                        <span :class="badgeClass(numOf(health?.data?.vector_black) > 0)">
                          {{ numOf(health?.data?.vector_black) > 0 ? '就绪' : '未就绪' }}
                        </span>
                        <span class="status-note">（黑池为语义通道门槛）</span>
                      </td>
                      <td><span class="td-note">脚本构建（见 README）</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <!-- ③ 模型与向量库 -->
              <div v-else class="wizard-step-content">
                <p class="step-desc">
                  模型状态来自 <code>GET /admin/models</code>；语义引擎装配为就绪门槛。
                </p>
                <table class="wizard-table">
                  <tbody>
                    <tr>
                      <td>语义引擎（Chinese-CLIP）</td>
                      <td>{{ modelStatusText(models?.chinese_clip?.status) }}</td>
                      <td>
                        <span :class="badgeClass(modelReady)">
                          {{ modelReady ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td>
                        <button type="button" class="link-btn" @click="go('settings')">→ 去设置页模型卡</button>
                      </td>
                    </tr>
                    <tr>
                      <td>轻量文本模型（fasttext）</td>
                      <td>{{ modelStatusText(models?.fasttext?.status) }}</td>
                      <td>
                        <span :class="badgeClass(models?.fasttext?.status === 'ready')">
                          {{ models?.fasttext?.status === 'ready' ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td><span class="td-note">配置 light_model 路径</span></td>
                    </tr>
                    <tr>
                      <td>向量库（黑 / 白池）</td>
                      <td>
                        {{ numOf(models?.vector_store?.black?.count) }} /
                        {{ numOf(models?.vector_store?.white?.count) }} 条
                      </td>
                      <td>
                        <span :class="badgeClass(numOf(models?.vector_store?.black?.count) > 0)">
                          {{ numOf(models?.vector_store?.black?.count) > 0 ? '就绪' : '未就绪' }}
                        </span>
                      </td>
                      <td><span class="td-note">脚本构建</span></td>
                    </tr>
                  </tbody>
                </table>
                <p v-if="models?.semantic?.reason" class="step-reason">
                  语义降级原因：<code>{{ models.semantic.reason }}</code>
                  （详见设置页模型卡指引 / README 模型部署排查表）
                </p>
              </div>
            </template>
          </div>

          <div class="wizard-foot">
            <button v-if="current > 0" type="button" class="btn btn-ghost btn-sm" @click="current -= 1">
              ← 上一步
            </button>
            <span class="foot-spacer" />
            <template v-if="!loading">
              <button
                v-if="allReady"
                type="button"
                class="btn btn-primary btn-sm"
                @click="finish"
              >
                🎉 完成
              </button>
              <button v-else-if="current < steps.length - 1" type="button" class="btn btn-primary btn-sm" @click="next">
                下一步 →
              </button>
              <!-- 末步且未全部就绪：结束浏览（仅关闭，不写完成标记，入口保留可再进） -->
              <button v-else type="button" class="btn btn-primary btn-sm" @click="close">
                完成
              </button>
            </template>
            <button type="button" class="btn btn-ghost btn-sm" @click="emit('close')">跳过</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 浮层遮罩：不响应遮罩点击关闭（对齐 AppModal 约定，避免误触丢失操作） */
.wizard-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  z-index: 998;
  padding: 20px;
}

.wizard-panel {
  width: min(680px, 100%);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  padding: 22px 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
}

/* 头部 */
.wizard-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}

.wizard-title {
  font-size: 1.02rem;
  font-weight: 700;
  color: var(--text);
}

.wizard-close {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3);
  font-size: 0.9rem;
  line-height: 1;
  cursor: pointer;
  transition: var(--transition);
}

.wizard-close:hover {
  background: var(--surface-hover);
  color: var(--text);
}

/* 步骤条 */
.wizard-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}

.wizard-step {
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 0.74rem;
  font-weight: 600;
  background: var(--surface-hover);
  color: var(--text-3);
}

.wizard-step.active {
  background: var(--primary-light);
  color: var(--primary);
}

.wizard-step.done {
  background: var(--success-light);
  color: var(--success);
}

/* 主体 */
.wizard-body {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  font-size: 0.84rem;
  color: var(--text-2);
  line-height: 1.8;
}

.wizard-loading {
  padding: 40px 0;
  text-align: center;
  color: var(--text-3);
}

.step-desc {
  margin-bottom: 10px;
}

.step-desc code,
.step-reason code {
  background: var(--surface-hover);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.76rem;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}

.step-list {
  margin: 0 0 12px 18px;
}

.step-status {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.status-note {
  font-size: 0.72rem;
  color: var(--text-3);
}

/* 状态表 */
.wizard-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.wizard-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.wizard-table td:first-child {
  font-weight: 600;
  color: var(--text-2);
  white-space: nowrap;
}

.td-note {
  font-size: 0.74rem;
  color: var(--text-3);
  white-space: nowrap;
}

/* 就绪/未就绪徽标 */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}

.badge-ok {
  background: var(--success-light);
  color: var(--success);
}

.badge-no {
  background: var(--orange-light);
  color: var(--orange);
}

.step-reason {
  margin-top: 10px;
  font-size: 0.76rem;
  color: var(--text-3);
}

/* 完成视图 */
.wizard-done-view {
  text-align: center;
  padding: 26px 10px;
}

.done-icon {
  font-size: 2.4rem;
  margin-bottom: 10px;
}

.done-title {
  font-size: 1rem;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 8px;
}

.done-desc {
  font-size: 0.8rem;
  color: var(--text-3);
  max-width: 460px;
  margin: 0 auto;
}

/* 底部按钮 */
.wizard-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

.foot-spacer {
  flex: 1;
}

/* 面板内跳转按钮 */
.link-btn {
  background: none;
  border: none;
  padding: 0;
  font-size: 0.76rem;
  color: var(--primary);
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.link-btn:hover {
  text-decoration: underline;
}

/* 遮罩淡入 / 面板弹出过渡 */
.wizard-fade-enter-active,
.wizard-fade-leave-active {
  transition: opacity 0.2s ease;
}

.wizard-fade-enter-active .wizard-panel,
.wizard-fade-leave-active .wizard-panel {
  transition:
    transform 0.22s cubic-bezier(0.34, 1.4, 0.64, 1),
    opacity 0.2s ease;
}

.wizard-fade-enter-from,
.wizard-fade-leave-to {
  opacity: 0;
}

.wizard-fade-enter-from .wizard-panel,
.wizard-fade-leave-to .wizard-panel {
  opacity: 0;
  transform: translateY(12px) scale(0.96);
}
</style>