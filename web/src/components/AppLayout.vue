<script setup lang="ts">
/**
 * 布局容器：顶栏 + 顶层分区切换 + 子路由出口。
 * 对应 / 路由，7 个子页面以 router-view 呈现，切换时带淡入过渡。
 */
import AppHeader from './AppHeader.vue'
import ScopeSwitch from './ScopeSwitch.vue'
</script>

<template>
  <div class="app-layout">
    <AppHeader />
    <ScopeSwitch />
    <main class="app-main">
      <router-view v-slot="{ Component }">
        <Transition name="page-fade" mode="out-in">
          <component :is="Component" />
        </Transition>
      </router-view>
    </main>
  </div>
</template>

<style scoped>
.app-layout {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px 20px 60px;
  min-height: 100%;
}

/* 路由切换淡入过渡 */
.page-fade-enter-active,
.page-fade-leave-active {
  transition: opacity 0.2s ease;
}

.page-fade-enter-from,
.page-fade-leave-to {
  opacity: 0;
}
</style>