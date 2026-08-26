<script setup lang="ts">
/**
 * 只读 JSON 树渲染（T24 新增子组件，供 AuditView 详情弹窗使用）：
 * - 对象 → 键值对逐层递归；数组 → 带序号 [i]；标量 → 文本；null → "null"
 * - 安全：全部内容经 Vue 插值（textContent 语义，自动转义），**绝不使用 v-html**
 * - 递归引用自身（Vue SFC 隐式自引用，以文件名 JsonTree 解析）
 */
defineProps<{
  value: unknown
}>()

/** 标量 / null 判断（模板分支用，避免 unknown 类型告警） */
function isScalar(value: unknown): boolean {
  return value === null || typeof value !== 'object'
}

/** 标量文本化（null → "null"） */
function scalarText(value: unknown): string {
  if (value === null) return 'null'
  return typeof value === 'string' ? value : String(value)
}

/** 对象 → 条目数组（非对象返回空，防御性转换以通过模板类型检查） */
function entries(value: unknown): Array<[string, unknown]> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return []
  return Object.entries(value as Record<string, unknown>)
}

/** 数组 → 数组（非数组返回空，供模板 v-for 使用） */
function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}
</script>

<template>
  <div class="json-tree">
    <!-- 标量 / null -->
    <span v-if="isScalar(value)" class="json-scalar">{{ scalarText(value) }}</span>
    <!-- 数组：带序号逐项递归 -->
    <div v-else-if="Array.isArray(value)" class="json-node">
      <span v-if="value.length === 0" class="json-empty">[]（空数组）</span>
      <div v-for="(item, i) in asArray(value)" :key="i" class="json-row">
        <span class="json-key">[{{ i }}]</span>
        <div class="json-val">
          <JsonTree :value="item" />
        </div>
      </div>
    </div>
    <!-- 对象：键值对逐层递归 -->
    <div v-else class="json-node">
      <span v-if="entries(value).length === 0" class="json-empty">{ }（空对象）</span>
      <div v-for="[k, v] in entries(value)" :key="k" class="json-row">
        <span class="json-key">{{ k }}</span>
        <div class="json-val">
          <JsonTree :value="v" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.json-tree {
  font-size: 0.8rem;
  line-height: 1.55;
}

.json-row {
  display: flex;
  gap: 8px;
  padding: 1px 0;
  align-items: flex-start;
}

.json-row:hover {
  background: var(--surface-hover);
}

.json-key {
  flex-shrink: 0;
  min-width: 100px;
  max-width: 45%;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  color: var(--primary);
  word-break: break-all;
  user-select: text;
}

.json-val {
  min-width: 0;
  flex: 1;
  word-break: break-word;
  user-select: text;
}

.json-scalar {
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.json-node {
  width: 100%;
}

.json-empty {
  color: var(--text-3);
  font-style: italic;
}
</style>