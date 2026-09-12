import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
// 全局样式（主题变量 + 基础组件样式）
import './style.css'
import { useThemeStore } from './stores/theme'
import { onUnauthorized } from './api/client'

const pinia = createPinia()
const app = createApp(App)

app.use(pinia)
app.use(router)

// T35 主题：预先实例化 store，激活「跟随系统」的 matchMedia 实时监听与响应式状态
// （防闪烁的初始 data-theme 由 index.html 内联脚本先行设置，此处为应用内接管）
useThemeStore()

// F5（composition root）：把「401 → 跳登录（带 redirect）」注入 API 层。
// 登录页不重复跳转（保持与旧 client 内联实现一致的语义）；client.ts 不再
// 依赖 router 单例，API 层可脱离路由单测（PRD §M5 F5/W1）。
onUnauthorized(() => {
  const cur = router.currentRoute.value
  if (cur.name !== 'login') {
    void router.push({ name: 'login', query: { redirect: cur.fullPath } })
  }
})

app.mount('#app')
