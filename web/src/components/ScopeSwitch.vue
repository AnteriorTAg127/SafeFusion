<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/**
 * 顶层功能分区滑动高光分段控件（对齐参考项目 scope-switch）：
 * 3 个分区覆盖全部 7 个子路由：
 * - 审核管理：/overview /audit /review
 * - 词库与规则：/keywords /whitelist /rules
 * - 系统设置：/settings
 * 点击分区跳转到该分区的第一个路由；高色光块随激活分区平滑滑动。
 */
interface Partition {
  key: string
  icon: string
  label: string
  routes: string[]
}

const partitions: Partition[] = [
  { key: 'audit', icon: '🔍', label: '审核管理', routes: ['overview', 'audit', 'review'] },
  { key: 'lexicon', icon: '📚', label: '词库与规则', routes: ['keywords', 'whitelist', 'rules'] },
  { key: 'system', icon: '⚙️', label: '系统设置', routes: ['settings'] },
]

const route = useRoute()
const router = useRouter()

/** 当前路由所属分区下标（未命中时默认第 0 个） */
const activeIndex = computed(() => {
  const name = String(route.name ?? '')
  const index = partitions.findIndex((p) => p.routes.includes(name))
  return index === -1 ? 0 : index
})

/** 光块定位：宽度 = 1/分区数，transform 按索引平移（百分比相对自身宽度） */
const glowStyle = computed(() => ({
  width: `calc(100% / ${partitions.length})`,
  transform: `translateX(${activeIndex.value * 100}%)`,
}))

/** 点击分区：已在当前分区则不跳；否则进入该分区第一个路由 */
function goPartition(index: number): void {
  const target = partitions[index]
  if (target.routes.includes(String(route.name))) return
  void router.push({ name: target.routes[0] })
}
</script>

<template>
  <div class="scope-switch" role="tablist" aria-label="功能分区">
    <button
      v-for="(partition, index) in partitions"
      :key="partition.key"
      type="button"
      role="tab"
      class="scope-btn"
      :class="{ active: index === activeIndex }"
      :aria-selected="index === activeIndex"
      @click="goPartition(index)"
    >
      <span class="scope-ico" aria-hidden="true">{{ partition.icon }}</span>
      <span class="scope-label">{{ partition.label }}</span>
    </button>
    <span class="scope-glow" aria-hidden="true" :style="glowStyle"></span>
  </div>
</template>

<style scoped>
.scope-switch {
  position: relative;
  display: flex;
  gap: 4px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 5px;
  margin-bottom: 18px;
  box-shadow: var(--shadow);
}

.scope-btn {
  position: relative;
  z-index: 1;
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 11px 0;
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: 0.95rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  border-radius: 10px;
  cursor: pointer;
  transition: color var(--transition);
}

.scope-ico {
  font-size: 1.05rem;
  line-height: 1;
  transition: transform var(--transition);
}

.scope-btn:hover {
  color: var(--text);
}

.scope-btn:hover .scope-ico {
  transform: translateY(-1px) scale(1.08);
}

.scope-btn.active {
  color: #fff;
}

/* 滑动高光：随激活分区平移（left 固定，宽度/位移由 JS 计算） */
.scope-glow {
  position: absolute;
  top: 5px;
  left: 0;
  height: calc(100% - 10px);
  border-radius: 10px;
  background: linear-gradient(135deg, var(--primary), var(--primary-hover));
  box-shadow: 0 4px 14px rgba(22, 93, 255, 0.32);
  transition:
    transform 0.25s cubic-bezier(0.4, 0, 0.2, 1),
    width 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  z-index: 0;
  pointer-events: none;
}
</style>