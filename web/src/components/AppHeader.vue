<script setup lang="ts">
/**
 * 顶栏组件：左侧标题（Logo + 名称），右侧 主题切换按钮（T35）+ 状态徽标（占位）+ 指南入口。
 * status 预留 online/offline 两态配色，供后续页面接入后端健康检查。
 * ❓ 指南下拉内容见 GuideMenu.vue（静态骨架，与 README 同步维护）。
 * 🌙/☀️ 主题按钮：图标按当前实际主题显示，点击三态循环（跟随系统 → 另一实际
 * 主题 → 另一显式主题 → 跟随系统），逻辑见 stores/theme.ts。
 */
import GuideMenu from './GuideMenu.vue'
import { useThemeStore } from '../stores/theme'

withDefaults(
  defineProps<{
    status?: 'online' | 'offline' | 'unknown'
    statusText?: string
  }>(),
  { status: 'unknown', statusText: '服务状态检测中' },
)

// T35 主题：resolved 当前实际主题（图标）、title 当前状态 + 下一次点击落点
const theme = useThemeStore()
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
</style>