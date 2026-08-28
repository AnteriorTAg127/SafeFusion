import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

/**
 * 路由表：
 * - /login           登录页（公开，无需 token）
 * - /                布局容器（AppLayout：顶栏 + 分区切换 + 子路由出口）
 *   - /trial /overview /audit /keywords /whitelist /rules /review /settings /keys  9 个子路由
 *   （keys 密钥管理归「系统设置」分区，trial 试运行独立分区，见 ScopeSwitch）
 * - 全局前置守卫：无 token 且目标非 /login → 重定向 /login（PRD 决策 G）
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('../components/AppLayout.vue'),
      children: [
        { path: '', redirect: { name: 'overview' } },
        {
          path: 'trial',
          name: 'trial',
          component: () => import('../views/TrialView.vue'),
          meta: { title: '试运行' },
        },
        {
          path: 'overview',
          name: 'overview',
          component: () => import('../views/OverviewView.vue'),
          meta: { title: '概览' },
        },
        {
          path: 'audit',
          name: 'audit',
          component: () => import('../views/AuditView.vue'),
          meta: { title: '审核记录' },
        },
        {
          path: 'keywords',
          name: 'keywords',
          component: () => import('../views/KeywordsView.vue'),
          meta: { title: '词库管理' },
        },
        {
          path: 'whitelist',
          name: 'whitelist',
          component: () => import('../views/WhitelistView.vue'),
          meta: { title: '图片白名单' },
        },
        {
          path: 'rules',
          name: 'rules',
          component: () => import('../views/RulesView.vue'),
          meta: { title: '规则管理' },
        },
        {
          path: 'review',
          name: 'review',
          component: () => import('../views/ReviewView.vue'),
          meta: { title: '定时复核' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('../views/SettingsView.vue'),
          meta: { title: '系统设置' },
        },
        {
          path: 'keys',
          name: 'keys',
          component: () => import('../views/KeysView.vue'),
          meta: { title: '密钥管理' },
        },
      ],
    },
    // 兜底：未匹配路由回到概览
    { path: '/:pathMatch(.*)*', redirect: { name: 'overview' } },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  // 目标非公开且无 token → 强制回登录页（记录来源，登录后跳回）
  if (!to.meta.public && !auth.token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 已登录访问登录页 → 直接进概览
  if (to.name === 'login' && auth.token) {
    return { name: 'overview' }
  }
  return true
})

export default router