import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
// 全局样式（主题变量 + 基础组件样式）
import './style.css'
import { useThemeStore } from './stores/theme'

const pinia = createPinia()
const app = createApp(App)

app.use(pinia)
app.use(router)

// T35 主题：预先实例化 store，激活「跟随系统」的 matchMedia 实时监听与响应式状态
// （防闪烁的初始 data-theme 由 index.html 内联脚本先行设置，此处为应用内接管）
useThemeStore()

app.mount('#app')