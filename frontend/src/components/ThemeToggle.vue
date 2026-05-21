<template>
  <el-button text @click="toggleTheme" :title="isDark ? '切换到浅色模式' : '切换到深色模式'">
    <el-icon size="18">
      <Moon v-if="!isDark" />
      <Sunny v-else />
    </el-icon>
  </el-button>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Moon, Sunny } from '@element-plus/icons-vue'

const isDark = ref(localStorage.getItem('theme') === 'dark')

function toggleTheme() {
  isDark.value = !isDark.value
  localStorage.setItem('theme', isDark.value ? 'dark' : 'light')
  document.documentElement.classList.toggle('dark', isDark.value)
}

onMounted(() => {
  if (isDark.value) {
    document.documentElement.classList.add('dark')
  }
})
</script>
