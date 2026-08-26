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
  try {
    // 用输入 token 请求配置端点完成验证；端点存在（T22）且鉴权通过即视为有效
    await apiGet<unknown>('/config')
    auth.setToken(token)
    toast.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/overview'
    void router.replace(redirect)
  } catch (error) {
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
</style>