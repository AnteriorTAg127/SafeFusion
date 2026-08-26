<script setup lang="ts">
// 应用根组件：路由出口 + 全局 Toast
import AppToast from './components/AppToast.vue'
</script>

<template>
  <router-view />
  <AppToast />
</template>