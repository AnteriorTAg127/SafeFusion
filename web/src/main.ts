import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
// 全局样式（主题变量 + 基础组件样式）
import './style.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

app.mount('#app')