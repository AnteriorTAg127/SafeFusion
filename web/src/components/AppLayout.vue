<script setup lang="ts">
/**
 * 布局容器：顶栏 + 顶层分区切换 + 子路由出口。
 * 对应 / 路由，7 个子页面以 router-view 呈现，切换时带淡入过渡。
 *
 * T57b/F6（W2）顶栏服务状态真实化：本组件挂载即探测 GET /admin/health，
 * 之后每 30s 轮询；online「● 服务正常」/ offline「● 服务不可达」接入 AppHeader。
 * - 探测失败**静默**：不走 api 层 request()（避免自动 Toast 连环弹），
 *   直连 http.get 裸实例 + catch（http 不带响应拦截器的自动 Toast）；
 * - 重入守卫：上一轮探测未完成则跳过本轮（与 T57a 轮询模式一致，防堆积）；
 * - 登录页不挂载本组件 → 不启动探测（PRD §M5 F6 要求）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { http } from '../api/client'
import AppHeader from './AppHeader.vue'
import ScopeSwitch from './ScopeSwitch.vue'

/** 服务状态徽标：online / offline / unknown（首次探测完成前占位） */
type ServerStatus = 'online' | 'offline' | 'unknown'

/** 探测周期：30s（PRD §M5 F6） */
const HEALTH_POLL_MS = 30_000

const serverStatus = ref<ServerStatus>('unknown')
/** 状态文案（AppHeader 原 prop 恒显「服务状态检测中」的替代） */
const serverStatusText = ref('服务状态检测中')
/** 重入守卫：上一轮探测未完成则跳过本轮（防请求堆积） */
let probeInFlight = false
let healthTimer: number | undefined

/**
 * 单轮探测：成功 → online；失败（网络/非 2xx）→ offline。
 * 直连 http.get（裸 axios 实例，不经 request()），失败 catch 静默——
 * 后端宕机瞬间不弹 Toast（降噪，配合 client.ts 3s 同错误去重窗）。
 */
async function probeHealth(): Promise<void> {
  if (probeInFlight) return
  probeInFlight = true
  try {
    await http.get('/health', { timeout: 5000 }) // 5s 内无响应视为不可达
    serverStatus.value = 'online'
    serverStatusText.value = '● 服务正常'
  } catch {
    // 静默：不弹 Toast、不 console 噪音（顶栏徽标即唯一反馈）
    serverStatus.value = 'offline'
    serverStatusText.value = '● 服务不可达'
  } finally {
    probeInFlight = false
  }
}

onMounted(() => {
  void probeHealth()
  healthTimer = window.setInterval(() => {
    void probeHealth()
  }, HEALTH_POLL_MS)
})

onBeforeUnmount(() => {
  if (healthTimer !== undefined) {
    window.clearInterval(healthTimer)
    healthTimer = undefined
  }
})
</script>

<template>
  <div class="app-layout">
    <AppHeader :status="serverStatus" :status-text="serverStatusText" />
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
