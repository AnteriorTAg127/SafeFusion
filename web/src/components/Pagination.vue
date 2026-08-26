<script setup lang="ts">
import { computed } from 'vue'

/**
 * 分页控件：上一页 / 页码信息 / 下一页。
 * props: page（当前页，从 1 起）、pageSize、total；emit change(page)。
 */
const props = defineProps<{
  page: number
  pageSize: number
  total: number
}>()

const emit = defineEmits<{
  (e: 'change', page: number): void
}>()

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))

function go(target: number): void {
  if (target >= 1 && target <= totalPages.value && target !== props.page) {
    emit('change', target)
  }
}
</script>

<template>
  <div class="pagination">
    <button type="button" class="btn btn-ghost btn-sm" :disabled="page <= 1" @click="go(page - 1)">
      ← 上一页
    </button>
    <span class="page-info">{{ page }} / {{ totalPages }}</span>
    <button
      type="button"
      class="btn btn-ghost btn-sm"
      :disabled="page >= totalPages"
      @click="go(page + 1)"
    >
      下一页 →
    </button>
  </div>
</template>