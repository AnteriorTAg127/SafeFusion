import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

/**
 * 主题 store（T35，PRD §M1 深色主题，决策 D）：
 * - localStorage 键 `sf_theme`，取值 'light' | 'dark' | 'system'，缺省 = 'system'（跟随系统）
 * - mode（用户显式选择 / 缺省 system）：'light' | 'dark' | 'system'
 * - resolved（实际应用主题）：由 mode 解析，只可能为 'light' | 'dark'
 * - 应用方式：`document.documentElement` 设置 `data-theme="light|dark"`
 *   （配合 web/src/style.css 的 `:root[data-theme='dark']` 暗色变量集）
 * - 防闪烁：首个 data-theme 由 index.html 头部内联脚本在首帧渲染前设置
 *   （本 store 仅接管响应式状态与后续切换；apply() 幂等，不会与内联脚本冲突）
 *
 * 三态切换设计说明（顶栏 🌙/☀️ 按钮点击逻辑）：
 *   - mode='system'  → 点击切到「当前实际主题的反面」并持久化为显式选择
 *                     （暗色用户点 ☀️ 立即变亮、亮色用户点 🌙 立即变暗——一次点击即见效）
 *   - mode='dark'    → 点击切为 'light'
 *   - mode='light'   → 点击切回 'system'（回到跟随系统；若系统当前恰为浅色，
 *                      界面不变但持久化状态改为「跟随系统」，按钮 title 会说明）
 *   即循环 system → 另一实际主题 → 另一显式主题 → system，三态均可达、可持久化。
 *   图标按「当前实际主题」显示（暗色 🌙 / 浅色 ☀️），title 提示完整状态与下一次落点。
 *
 * 跟随系统：mode='system' 时监听 `prefers-color-scheme` 的 change 事件，实时切换。
 */
export type ThemeMode = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const THEME_KEY = 'sf_theme'
const MEDIA = '(prefers-color-scheme: dark)'

/** 读取持久化选择；非法/缺失一律回退 'system'（跟随系统默认） */
function loadMode(): ThemeMode {
  const raw = localStorage.getItem(THEME_KEY)
  if (raw === 'light' || raw === 'dark' || raw === 'system') return raw
  return 'system'
}

/** 系统是否偏好深色；matchMedia 不可用（极端环境）按浅色处理 */
function systemPrefersDark(): boolean {
  try {
    return window.matchMedia(MEDIA).matches
  } catch {
    return false
  }
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(loadMode())
  const resolved = ref<ResolvedTheme>(resolveTheme(mode.value))

  /** mode → 实际主题：system 解析为系统偏好 */
  function resolveTheme(m: ThemeMode): ResolvedTheme {
    if (m === 'dark') return 'dark'
    if (m === 'light') return 'light'
    return systemPrefersDark() ? 'dark' : 'light'
  }

  /** 将当前 mode 解析结果应用到 <html data-theme>（幂等，可反复调用） */
  function apply(): void {
    resolved.value = resolveTheme(mode.value)
    document.documentElement.setAttribute('data-theme', resolved.value)
  }

  // mode 外部变更（理论上）也即时应用；正常路径仅 toggle() 修改 mode
  watch(mode, () => apply())

  // 跟随系统：mode='system' 时系统主题变化实时切换（单 SPA 生命周期内常驻监听）
  const media = window.matchMedia(MEDIA)
  media.addEventListener('change', (e) => {
    if (mode.value === 'system') {
      resolved.value = e.matches ? 'dark' : 'light'
      document.documentElement.setAttribute('data-theme', resolved.value)
    }
  })

  /** 顶栏按钮三态循环（设计说明见文件头注释） */
  function toggle(): void {
    if (mode.value === 'system') {
      // 一次点击切到另一实际主题并持久化显式选择
      mode.value = resolved.value === 'dark' ? 'light' : 'dark'
    } else if (mode.value === 'dark') {
      mode.value = 'light'
    } else {
      mode.value = 'system' // 回到跟随系统
    }
    localStorage.setItem(THEME_KEY, mode.value)
    apply()
  }

  /** 按钮 title：当前状态 + 下一次点击落点（供 AppHeader 提示） */
  const title = computed(() => {
    const cur = resolved.value === 'dark' ? '深色' : '浅色'
    if (mode.value === 'system') {
      return `跟随系统（当前${cur}）· 点击切换为${resolved.value === 'dark' ? '浅色' : '深色'}`
    }
    if (mode.value === 'dark') return `当前深色主题 · 点击切换为浅色`
    return `当前浅色主题 · 点击切换为跟随系统`
  })

  // 初次应用：与 index.html 内联脚本一致（幂等），确保响应式状态与 DOM 同步
  apply()

  return { mode, resolved, toggle, title }
})