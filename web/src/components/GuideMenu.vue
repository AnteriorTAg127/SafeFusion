<script setup lang="ts">
/**
 * 顶栏「❓ 指南」入口（PRD v0.3.0 §M1）：
 * 下拉面板展示静态中文要点 —— 快速开始 / 数据准备清单 / 模型部署 / 审核 API 对接
 * （curl 示例）/ 管理员操作速览，覆盖 README 核心路径，让用户「进去就会用」。
 * - 内容为静态硬编码（v0.3.0 文档定稿前的骨架 + 占位章节），各处标注「与 README 同步」；
 * - 点击面板外 / Esc 关闭；面板内 router-link 跳转对应页面后自动收起；
 * - T41：面板顶部挂「🚀 首次使用向导」入口（GuideWizard.vue，三步：令牌/数据/模型）；
 *   三步全部就绪时向导自动写完成标记 sf_guide_wizard_done=1 → 主入口隐藏，
 *   变为「🧙 重新打开」链接（PRD A1 可跳过、可再次从指南进入）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import GuideWizard, { WIZARD_DONE_KEY } from '../views/GuideWizard.vue'

const open = ref(false)
const wizardOpen = ref(false)
const wizardDone = ref(localStorage.getItem(WIZARD_DONE_KEY) === '1')
const panelRef = ref<HTMLElement | null>(null)
const router = useRouter()

/** 向导关闭后按最新完成标记刷新入口状态 */
function onWizardClose(): void {
  wizardOpen.value = false
  wizardDone.value = localStorage.getItem(WIZARD_DONE_KEY) === '1'
}

/** 「重新打开」：清除完成标记（向导将重新检测真实完成度）并以未完成态展示 */
function reopenWizard(): void {
  localStorage.removeItem(WIZARD_DONE_KEY)
  wizardDone.value = false
  wizardOpen.value = true
}

function toggle(): void {
  open.value = !open.value
}

function close(): void {
  open.value = false
}

/** 跳转并收起面板 */
function go(name: string): void {
  void router.push({ name })
  close()
}

/** 点击面板外任意区域关闭 */
function onDocClick(event: MouseEvent): void {
  const el = panelRef.value
  if (open.value && el && !el.contains(event.target as Node)) {
    close()
  }
}

/** Esc 关闭 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="panelRef" class="guide-wrap">
    <button type="button" class="btn btn-ghost btn-sm guide-btn" :class="{ active: open }" @click="toggle">
      ❓ 指南
    </button>

    <div v-if="open" class="guide-panel">
      <div class="guide-head">
        <span class="guide-title">SafeFusion 使用指南</span>
        <span class="guide-sync">骨架版 · 与 README 同步维护</span>
      </div>

      <!-- 🚀 首次使用向导入口（PRD A1；完成即隐藏，可重新打开） -->
      <div class="guide-wizard-entry">
        <button
          v-if="!wizardDone"
          type="button"
          class="btn btn-primary btn-sm guide-wizard-btn"
          @click="wizardOpen = true"
        >
          🚀 首次使用向导（三步：令牌 / 数据 / 模型）
        </button>
        <template v-else>
          <span class="guide-wizard-done">🚀 首次使用向导（全部就绪 ✓）</span>
          <button type="button" class="link-btn" @click="reopenWizard">🧙 重新打开</button>
        </template>
      </div>

      <!-- ① 快速开始 -->
      <details class="guide-section" open>
        <summary>🚀 快速开始（5 步）</summary>
        <ol class="guide-list">
          <li><b>安装</b>：<code>pip install uv && uv sync</code>（本地语义/轻模型再加 <code>--extra ml</code>）。</li>
          <li><b>配置</b>：复制 <code>config.example.yaml</code> 为 <code>config.yaml</code>；密钥走环境变量
            （<code>ADMIN_PASSWORD</code>、<code>SAFEFUSION_LLM_API_KEY</code> 等）。</li>
          <li><b>数据准备</b>：词库 / 黑白语料 / 向量库 / 白名单 / 规则 —— 见下方「数据准备清单」。</li>
          <li><b>模型部署</b>：语义与轻量模型按需启用 —— 见「模型部署」章节。</li>
          <li><b>启动</b>：<code>.venv\Scripts\python.exe -m safefusion.api</code> →
            审核 API <code>:8000</code> / 管理面板 <code>:8001</code>。
            <button type="button" class="link-btn" @click="go('settings')">→ 去系统设置</button>
          </li>
        </ol>
      </details>

      <!-- ② 数据准备清单 -->
      <details class="guide-section" open>
        <summary>📦 数据准备清单（五类数据从哪来、放哪、怎么进系统）</summary>
        <div class="guide-table" role="table">
          <div class="guide-row guide-row-head" role="row">
            <span>数据</span><span>来源 / 格式</span><span>导入入口</span>
          </div>
          <div class="guide-row" role="row">
            <span>关键词词库</span>
            <span>CSV（类别,词）或 TXT（每行一词）；UTF-8</span>
            <span><button type="button" class="link-btn" @click="go('keywords')">词库管理</button></span>
          </div>
          <div class="guide-row" role="row">
            <span>黑白语料 + 向量库</span>
            <span>归一化清单 → <code>scripts/build_vector_db.py</code> 批量编码</span>
            <span>脚本构建（见 README）</span>
          </div>
          <div class="guide-row" role="row">
            <span>白名单图片</span>
            <span>PNG/JPG 等图片；上传即算 md5 + pHash</span>
            <span><button type="button" class="link-btn" @click="go('whitelist')">图片白名单</button></span>
          </div>
          <div class="guide-row" role="row">
            <span>正则消歧规则</span>
            <span>CSV（category,pattern,action）或 JSON 数组</span>
            <span><button type="button" class="link-btn" @click="go('rules')">规则管理</button></span>
          </div>
        </div>
        <p class="guide-note">详细逐项表（来源 / 路径 / 生成命令 / 产物）见 README「数据准备指南」（v0.3.0 文档专项落地后同步到本文）。</p>
      </details>

      <!-- ③ 模型部署 -->
      <details class="guide-section">
        <summary>🧠 模型部署</summary>
        <ul class="guide-list">
          <li><b>语义（Chinese-CLIP）</b>：首次使用自动从 HuggingFace 下载
            <code>OFA-Sys/chinese-clip-vit-base-patch16</code>；离线/内网用
            <code>--model &lt;本地权重目录&gt;</code> 指向已下载权重；大文件走代理或
            <code>HF_ENDPOINT</code> 镜像。</li>
          <li><b>轻量文本模型（fasttext.pt）</b>：配置 <code>light_model.model_path / config_path</code>
            指向已训练产物；路径为 null 时该组件停用。</li>
          <li><b>云端 Embedding / LLM</b>：Key 仅环境变量注入，配置页只显示变量名与是否已配置。</li>
          <li><b>验证</b>：<code>GET /health</code> 查看组件降级清单；引擎未就绪时审核会降级而不是报错。</li>
        </ul>
        <p class="guide-note">完整排查表（依赖缺失 / 下载失败 / GPU / 离线部署）与 README 模型部署章节目录一致，v0.3.0 定稿后同步。</p>
      </details>

      <!-- ④ 审核 API 对接 -->
      <details class="guide-section">
        <summary>🔌 审核 API 对接（curl 示例）</summary>
        <pre class="guide-code"># 审核请求（文本 + 可选图片）；Key 必填：X-Api-Key 或 Authorization: Bearer
curl -X POST http://127.0.0.1:8000/v1/audit \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: &lt;你的 API Key&gt;" \
  -d '{"text":"这是一条待审核的内容","images":[],"skip_llm":false,"context":"社区帖子"}'

# 健康检查（免认证）：组件降级清单 + 指标摘要
curl http://127.0.0.1:8000/health</pre>
        <p class="guide-note">API Key 经管理 API 生成（<code>POST /admin/keys</code>，分组 standard/full），完整 Key 仅创建时显示一次；
          「系统设置」分区已提供「密钥管理」页：<button type="button" class="link-btn" @click="go('keys')">→ 去密钥管理</button>。
          管理端端点统一带 <code>X-Admin-Token: &lt;管理令牌&gt;</code> 请求头。</p>
      </details>

      <!-- ⑤ 管理员操作速览 -->
      <details class="guide-section">
        <summary>🗂️ 管理员操作速览</summary>
        <ul class="guide-list">
          <li><b>概览</b>：统计卡 + 7 天趋势（数据来自审核日志）。<button type="button" class="link-btn" @click="go('overview')">去概览</button></li>
          <li><b>审核记录</b>：多维筛选 + 明细证据链（关键词/语义/LLM）。<button type="button" class="link-btn" @click="go('audit')">去审核记录</button></li>
          <li><b>词库 / 白名单 / 规则</b>：数据准备三页，导入即热生效（规则层）。</li>
          <li><b>定时复核</b>：LLM 抽样复核一致率，产出阈值建议。<button type="button" class="link-btn" @click="go('review')">去复核</button></li>
          <li><b>系统设置</b>：11 组配置在线编辑；保存后重启生效，密钥仅环境变量。<button type="button" class="link-btn" @click="go('settings')">去设置</button></li>
          <li><b>密钥管理</b>：签发 / 停用 / 删除审核 API Key（standard/full），完整 Key 仅创建时显示一次。<button type="button" class="link-btn" @click="go('keys')">去密钥管理</button></li>
        </ul>
      </details>

      <p class="guide-foot">
        📌 本文为前端内置骨架（v0.3.0 文档定稿前占位），各章节要点与根目录 README.md 同步维护；
        升级版本后如内容不一致，以 README 为准。
      </p>
    </div>

    <!-- 首次使用向导（独立浮层，不注册路由；T41） -->
    <GuideWizard :show="wizardOpen" @close="onWizardClose" />
  </div>
</template>

<style scoped>
.guide-wrap {
  position: relative;
}

.guide-btn.active {
  background: var(--primary-light);
  color: var(--primary);
  border-color: var(--primary);
}

.guide-panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 100;
  width: min(560px, calc(100vw - 40px));
  max-height: min(70vh, 640px);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  padding: 16px;
  animation: guideIn 0.18s ease;
  text-align: left;
}

@keyframes guideIn {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.guide-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.guide-title {
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--text);
}

.guide-sync {
  font-size: 0.68rem;
  color: var(--text-3);
}

/* 章节（details/summary） */
.guide-section {
  border-top: 1px dashed var(--border);
  padding: 8px 0;
  font-size: 0.8rem;
}

.guide-section summary {
  cursor: pointer;
  font-weight: 700;
  color: var(--text-2);
  padding: 4px 0;
  user-select: none;
}

.guide-section summary:hover {
  color: var(--primary);
}

.guide-list {
  margin: 6px 0 4px 18px;
  line-height: 1.8;
  color: var(--text-2);
}

.guide-list code,
.guide-table code {
  background: var(--surface-hover);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.72rem;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}

/* 数据准备清单表 */
.guide-table {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 6px 0;
}

.guide-row {
  display: grid;
  grid-template-columns: 110px 1fr 110px;
  gap: 8px;
  align-items: center;
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--surface-hover);
  font-size: 0.74rem;
  color: var(--text-2);
}

.guide-row-head {
  background: transparent;
  border-bottom: 1px solid var(--border);
  font-weight: 700;
  color: var(--text-3);
}

.guide-note {
  font-size: 0.7rem;
  color: var(--text-3);
  line-height: 1.6;
  margin-top: 4px;
}

/* curl 代码块 */
.guide-code {
  background: #f6f8fa;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 0.72rem;
  line-height: 1.7;
  overflow-x: auto;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  color: var(--text-2);
  margin: 6px 0;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 面板内跳转链接样式按钮 */
.link-btn {
  background: none;
  border: none;
  padding: 0;
  font-size: 0.72rem;
  color: var(--primary);
  font-weight: 600;
  cursor: pointer;
}

.link-btn:hover {
  text-decoration: underline;
}

.guide-foot {
  border-top: 1px solid var(--border);
  margin-top: 8px;
  padding-top: 8px;
  font-size: 0.7rem;
  color: var(--text-3);
  line-height: 1.6;
}

/* T41：首次使用向导入口（完成前主按钮；完成后只读状态 + 重新打开链接） */
.guide-wizard-entry {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--primary-light);
  border: 1px dashed var(--primary);
  border-radius: var(--radius-sm);
}

.guide-wizard-btn {
  background: var(--primary);
  color: #fff;
}

.guide-wizard-done {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--success);
}
</style>