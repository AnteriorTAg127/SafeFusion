<script setup lang="ts">
/**
 * 通用数据表格：列定义 + 行数据 + loading + 空态。
 * - 单元格默认按列 key 取值渲染（Vue 插值自动转义，防 XSS）；
 * - 对象/数组值自动 JSON 序列化展示；
 * - 提供 cell 具名插槽（作用域：row / column / value）供自定义渲染（按钮、徽标等）。
 */

/** 列定义（由父组件以结构类型传入，无需显式导入） */
interface ColumnDef {
  /** 行数据中的取值键 */
  key: string
  /** 表头文案 */
  label: string
  /** 可选列宽：数字（px）或 CSS 宽度字符串 */
  width?: string | number
}

const props = withDefaults(
  defineProps<{
    columns: ColumnDef[]
    rows?: Array<Record<string, unknown>>
    loading?: boolean
    emptyText?: string
  }>(),
  {
    rows: () => [],
    loading: false,
    emptyText: '暂无数据',
  },
)

/** 单元格取值：null/undefined 显示占位符，对象/数组转 JSON 文本 */
function cellValue(row: Record<string, unknown>, key: string): unknown {
  const value = row[key]
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return '[不可序列化]'
    }
  }
  return value
}

function columnStyle(column: ColumnDef): Record<string, string | number> | undefined {
  return column.width !== undefined ? { width: column.width } : undefined
}
</script>

<template>
  <div class="table-wrap">
    <!-- loading 态 -->
    <div v-if="props.loading" class="loading-row">
      <div class="loading">加载中...</div>
    </div>
    <!-- 空态 -->
    <div v-else-if="props.rows.length === 0" class="empty-state">
      <span class="empty-icon">📭</span>
      <p>{{ props.emptyText }}</p>
    </div>
    <!-- 数据表格 -->
    <table v-else class="table">
      <thead>
        <tr>
          <th v-for="col in props.columns" :key="col.key" :style="columnStyle(col)">
            {{ col.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in props.rows" :key="rowIndex">
          <td v-for="col in props.columns" :key="col.key" :style="columnStyle(col)">
            <slot name="cell" :row="row" :column="col" :value="cellValue(row, col.key)">
              {{ cellValue(row, col.key) }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.loading-row {
  padding: 20px 0;
}
</style>