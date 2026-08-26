<script setup lang="ts">
/**
 * 顶栏组件：左侧标题（Logo + 名称），右侧状态徽标（占位）。
 * status 预留 online/offline 两态配色，供后续页面接入后端健康检查。
 */
withDefaults(
  defineProps<{
    status?: 'online' | 'offline' | 'unknown'
    statusText?: string
  }>(),
  { status: 'unknown', statusText: '服务状态检测中' },
)
</script>

<template>
  <header class="app-header">
    <div class="header-left">
      <span class="header-icon" aria-hidden="true">🛡️</span>
      <h1 class="header-title">SafeFusion 管理面板</h1>
    </div>
    <div class="header-right">
      <span class="status-badge" :class="`status-${status}`">{{ statusText }}</span>
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

.header-icon {
  font-size: 1.5rem;
}

.header-title {
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.02em;
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