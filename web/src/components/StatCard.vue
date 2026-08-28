<script setup lang="ts">
/**
 * 统计卡片（对齐参考项目 stat-card）：Emoji 图标 + 数值 + 标签。
 * tone 控制图标底色：blue / green / orange / purple。
 */
withDefaults(
  defineProps<{
    icon: string
    value: string | number
    label: string
    tone?: 'blue' | 'green' | 'orange' | 'purple'
  }>(),
  { tone: 'blue' },
)
</script>

<template>
  <div class="stat-card">
    <div class="stat-icon" :class="`stat-${tone}`" aria-hidden="true">{{ icon }}</div>
    <div class="stat-body">
      <div class="stat-value">{{ value }}</div>
      <div class="stat-label">{{ label }}</div>
    </div>
  </div>
</template>

<style scoped>
.stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 16px;
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
  transition: var(--transition);
}

.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}

.stat-icon {
  width: 42px;
  height: 42px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  font-size: 1.2rem;
  flex-shrink: 0;
}

.stat-blue {
  background: var(--primary-light);
}

.stat-purple {
  background: var(--purple-light);
}

.stat-green {
  background: var(--success-light);
}

.stat-orange {
  background: var(--orange-light);
}

.stat-body {
  min-width: 0;
}

.stat-value {
  font-size: 1.4rem;
  font-weight: 700;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: 0.75rem;
  color: var(--text-3);
  margin-top: 2px;
}
</style>