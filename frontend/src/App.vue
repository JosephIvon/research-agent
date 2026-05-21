<template>
  <div id="app">
    <router-view />
  </div>
</template>

<script setup>
import { onMounted } from 'vue'

// Apply persisted theme on load
onMounted(() => {
  const saved = localStorage.getItem('theme')
  if (saved === 'dark') {
    document.documentElement.classList.add('dark')
  }
})
</script>

<style>
#app {
  min-height: 100vh;
  background-color: var(--bg-page);
}
</style>