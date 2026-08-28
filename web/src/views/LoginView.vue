<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { isAxiosError } from 'axios'
import { apiGet } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'

/**
 * 登录页：输入管理 Token，调 GET /admin/config（带 X-Admin-Token）验证。
 * 成功 → 存 token 并跳转（优先跳回来源页）；401 → 错误 Toast。
 * 底部含令牌说明块（PRD v0.3.0 §M1）：令牌来源 = 配置 admin_token / 环境变量
 * ADMIN_PASSWORD；两者皆缺时启动日志打印一次性临时令牌（仅此一次）。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const toast = useToastStore()

const tokenInput = ref('')
const loading = ref(false)

async function handleLogin(): Promise<void> {
  const token = tokenInput.value.trim()
  if (!token) {
    toast.error('请输入管理 Token')
    return
  }
  loading.value = true
  // 先注入令牌（请求拦截器从 store 取 X-Admin-Token），再调配置端点验证；
  // 顺序颠倒会导致验证请求不带任何头而必然 401（v0.2.1 登录页缺陷修复）
  auth.setToken(token)
  try {
    await apiGet<unknown>('/config')
    toast.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/overview'
    void router.replace(redirect)
  } catch (error) {
    // 验证失败：回滚本地令牌，避免残留无效凭证
    auth.clearToken()
    // 401：token 无效；其余：后端未就绪 / 网络问题
    if (isAxiosError(error) && error.response?.status === 401) {
      toast.error('Token 无效，请检查后重试')
    } else {
      toast.error('无法连接管理服务，请确认后端已启动')
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-logo" aria-hidden="true">🛡️</div>
      <h1 class="login-title">SafeFusion 管理面板</h1>
      <p class="login-desc">请输入管理 Token（X-Admin-Token）以登录</p>
      <input
        v-model="tokenInput"
        type="password"
        class="input login-input"
        placeholder="管理 Token"
        autocomplete="off"
        @keyup.enter="handleLogin"
      />
      <button type="button" class="btn btn-primary login-btn" :disabled="loading" @click="handleLogin">
        {{ loading ? '验证中...' : '登 录' }}
      </button>

      <!-- 令牌说明（PRD v0.3.0 §M1；依据 api/admin.py _resolve_admin_token 实际逻辑） -->
      <div class="login-token-block">
        <div class="token-title">💡 管理令牌从哪来？</div>
        <ul class="token-list">
          <li>管理令牌取自配置 <code>admin_token</code> 或环境变量 <code>ADMIN_PASSWORD</code>；登录时原样输入即可。</li>
          <li>两者都未设置时，<b>服务启动日志会打印一次随机生成的临时令牌（仅此一次输出）</b>，
            请在启动终端中查找「自动生成管理令牌」字样；重启后令牌会变化，需重新获取。</li>
        </ul>
        <div class="token-title token-faq">常见问题</div>
        <ul class="token-list">
          <li><b>「Token 无效」</b>：令牌错误或已过期（如改了 ADMIN_PASSWORD 后仍用旧令牌），换新令牌重试。</li>
          <li><b>「无法连接管理服务」</b>：后端未启动 / 端口不通（管理 API 默认 :8001），与服务无关。</li>
          <li>令牌仅保存在本浏览器 localStorage；不会在任何页面回显明文。</li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.login-card {
  width: min(380px, 100%);
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  padding: 36px 32px;
  text-align: center;
  animation: fadeIn 0.3s ease;
}

.login-logo {
  font-size: 2.4rem;
  line-height: 1;
  margin-bottom: 14px;
}

.login-title {
  font-size: 1.3rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin-bottom: 6px;
}

.login-desc {
  font-size: 0.78rem;
  color: var(--text-3);
  margin-bottom: 22px;
}

.login-input {
  text-align: center;
  margin-bottom: 16px;
}

.login-btn {
  width: 100%;
  justify-content: center;
  padding: 10px 0;
  font-size: 0.92rem;
}

/* 令牌说明块（登录页底部） */
.login-token-block {
  margin-top: 22px;
  padding-top: 14px;
  border-top: 1px dashed var(--border);
  text-align: left;
}

.token-title {
  font-size: 0.76rem;
  font-weight: 700;
  color: var(--text-2);
  margin-bottom: 6px;
}

.token-faq {
  margin-top: 12px;
}

.token-list {
  margin: 0;
  padding-left: 16px;
  font-size: 0.7rem;
  color: var(--text-3);
  line-height: 1.8;
}

.token-list code {
  background: var(--surface-hover);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 0.66rem;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}
</style>