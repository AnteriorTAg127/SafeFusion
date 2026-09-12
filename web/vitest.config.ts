import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 单元测试专用配置（F12 基建）：
// - 只跑纯逻辑（utils / api / stores / composables），无浏览器 DOM；
//   个别用例若需组件挂载，另行引入 @vue/test-utils + happy-dom，不在本配置预设。
// - environment: node，与「纯逻辑单测」边界一致（PRD §M5 F12）。
// - 与 vite.config.ts 分离：构建走 vite.config，测试走本文件，互不影响。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
