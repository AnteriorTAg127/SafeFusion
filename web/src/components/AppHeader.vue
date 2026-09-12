<script setup lang="ts">
/**
 * 顶栏组件：左侧标题（Logo + 名称），右侧 主题切换按钮（T35）+ 状态徽标（占位）+ 指南入口
 * + 「🚪 退出登录」（T57c/F10①）。
 * status 预留 online/offline 两态配色，供后续页面接入后端健康检查。
 * ❓ 指南下拉内容见 GuideMenu.vue（静态骨架，与 README 同步维护）。
 * 🌙/☀️ 主题按钮：图标按当前实际主题显示，点击三态循环（跟随系统 → 另一实际
 * 主题 → 另一显式主题 → 跟随系统），逻辑见 stores/theme.ts。
 *
 * F10① 登出入口：clearToken + 跳 /login（仅清本地，无后端调用——令牌本身即
 * 长期凭证，真正失效走设置页改密，PRD §M5 F10）；路由守卫在 token 清空后
 * 对受保护页自动重定向，此处显式 push 保证立即回登录页。
 */
import GuideMenu from './GuideMenu.vue'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'
import router from '../router'

withDefaults(
  defineProps<{
    status?: 'online' | 'offline' | 'unknown'
    statusText?: string
  }>(),
  { status: 'unknown', statusText: '服务状态检测中' },
)

// T35 主题：resolved 当前实际主题（图标）、title 当前状态 + 下一次点击落点
const theme = useThemeStore()
const auth = useAuthStore()

/** 退出登录：仅清本地令牌并回登录页（无后端调用，见文件头注释） */
function logout(): void {
  auth.clearToken()
  void router.push({ name: 'login' })
}
</script>

<template>
  <header class="app-header">
    <div class="header-left">
      <span class="header-icon" aria-hidden="true">🛡️</span>
      <h1 class="header-title">SafeFusion 管理面板</h1>
    </div>
    <div class="header-right">
      <button
        type="button"
        class="theme-toggle"
        :title="theme.title"
        :aria-label="theme.title"
        @click="theme.toggle"
      >
        <span aria-hidden="true">{{ theme.resolved === 'dark' ? '🌙' : '☀️' }}</span>
      </button>
      <span class="status-badge" :class="`status-${status}`">{{ statusText }}</span>
      <GuideMenu />
      <button type="button" class="logout-btn" title="清除本地令牌并回到登录页" @click="logout">
        🚪 退出登录
      </button>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-icon {
  font-size: 1.5rem;
}

.header-title {
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}

/* T35 主题切换按钮：无边框图标钮，悬停有底色反馈 */
.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: 10px;
  background: transparent;
  font-size: 1.05rem;
  line-height: 1;
  cursor: pointer;
  transition: var(--transition);
}

.theme-toggle:hover {
  background: var(--surface-hover);
}

/* 状态徽标：默认灰色占位，online 绿色 / offline 红色 */
.status-badge {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 20px;
  background: var(--border);
  color: var(--text-3);
  transition: var(--transition);
}

.status-online {
  background: var(--success-light);
  color: var(--success);
}

.status-offline {
  background: var(--danger-light);
  color: var(--danger);
}

/* F10① 退出登录：ghost 风格小按钮，悬停红色提示破坏性语义 */
.logout-btn {
  padding: 5px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-2);
  font-size: 0.74rem;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
}

.logout-btn:hover {
  border-color: var(--danger);
  color: var(--danger);
  background: var(--danger-light);
}
</style>
