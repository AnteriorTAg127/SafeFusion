import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// SafeFusion 管理面板 Vite 配置
// - dev 模式：/admin 代理到后端管理服务（:8001），前后端跨端口联调免 CORS
// - 生产模式：构建产物 web/dist 由后端 FastAPI StaticFiles 同源 mount（见 T22）
export default defineConfig({
  plugins: [vue()],
  base: '/',
  server: {
    proxy: {
      '/admin': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 默认 outDir 即为 dist（与后端 mount 路径约定一致，显式声明便于阅读）
    outDir: 'dist',
  },
})