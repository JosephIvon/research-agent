<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">← 返回</el-button>
        <span class="task-title">正在调研: {{ taskName }}</span>
      </div>
    </header>

    <main class="layout-main">
      <div class="progress-container">
        <div class="step-progress">
          <el-steps :active="currentStep" align-center finish-status="success">
            <el-step title="分解" :description="stepDesc(0)" />
            <el-step title="搜索" :description="stepDesc(1)" />
            <el-step title="抓取" :description="stepDesc(2)" />
            <el-step title="核查" :description="stepDesc(3)" />
            <el-step title="撰写" :description="stepDesc(4)" />
          </el-steps>
        </div>

        <div class="crawl-progress card">
          <div class="card-header">竞品抓取进度</div>
          <div class="crawl-count">已抓取 {{ completedCount }}/{{ totalCount }} 个竞品</div>
          <div class="crawl-cards">
            <div v-for="item in crawlItems" :key="item.url" class="crawl-card" :class="item.status">
              <span class="crawl-icon">{{ statusIcon(item.status) }}</span>
              <span class="crawl-name">{{ item.name }}</span>
              <span class="crawl-url">{{ item.url }}</span>
            </div>
          </div>
        </div>

        <div class="log-section card">
          <div class="card-header">📋 实时日志</div>
          <div class="log-list">
            <div v-for="(log, index) in logs" :key="index" class="log-item" :class="log.type">
              <span class="log-time">{{ log.time }}</span>
              <span class="log-content">{{ log.message }}</span>
            </div>
          </div>
        </div>

        <div class="action-btns">
          <el-button @click="cancelTask">取消调研</el-button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const router = useRouter()
const store = useResearchStore()

const taskName = ref('加载中...')
const currentStep = ref(0)
const completedCount = ref(0)
const totalCount = ref(0)
const crawlItems = ref([])
const logs = ref([])
let pollingTimer = null

const steps = ['需求分解', '搜索检索', '网页抓取', '数据核查', '报告撰写']

function stepDesc(index) {
  if (index < currentStep.value) return '已完成'
  if (index === currentStep.value) return '进行中'
  return '等待中'
}

function statusIcon(status) {
  return { waiting: '⏳', running: '🔄', success: '✅', failed: '❌' }[status] || '⏳'
}

async function fetchStatus() {
  try {
    const taskId = route.params.id
    const report = await store.getReport(taskId)
    if (report) {
      taskName.value = report.title || taskName.value
      currentStep.value = 5
      completedCount.value = report.competitors || 0
      totalCount.value = report.competitors || 0
      logs.value = [{ time: new Date().toLocaleTimeString(), type: 'success', message: '报告已生成' }]
      clearInterval(pollingTimer)
      router.push(`/report/${taskId}`)
    }
  } catch (e) {
    logs.value = [{ time: new Date().toLocaleTimeString(), type: 'error', message: '未找到任务状态' }]
    clearInterval(pollingTimer)
  }
}

function cancelTask() {
  clearInterval(pollingTimer)
  router.push('/')
}

onMounted(() => {
  fetchStatus()
  pollingTimer = setInterval(fetchStatus, 3000)
})

onUnmounted(() => {
  clearInterval(pollingTimer)
})
</script>

<style lang="scss" scoped>
.layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.layout-header {
  display: flex;
  align-items: center;
  padding: 16px 24px;
  background: #fff;
  border-bottom: 1px solid var(--border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
  .task-title { font-size: 16px; font-weight: 500; }
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.progress-container {
  max-width: 900px;
  margin: 0 auto;
}

.step-progress {
  background: #fff;
  border-radius: 12px;
  padding: 32px;
  margin-bottom: 16px;
}

.crawl-progress {
  margin-bottom: 16px;
}

.crawl-count {
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 16px;
}

.crawl-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.crawl-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: var(--bg-hover);
  border-radius: 8px;
  font-size: 14px;

  &.success .crawl-icon { color: var(--success); }
  &.failed .crawl-icon { color: var(--error); }
  &.running .crawl-icon { color: var(--primary); animation: spin 1s linear infinite; }

  .crawl-name { font-weight: 500; }
  .crawl-url { color: var(--text-secondary); font-size: 12px; }
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.log-section {
  margin-bottom: 16px;
}

.log-list {
  max-height: 300px;
  overflow-y: auto;
}

.log-item {
  display: flex;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;

  &.success .log-content { color: var(--success); }
  &.error .log-content { color: var(--error); }
  &.info .log-content { color: var(--primary); }

  .log-time { color: var(--text-muted); min-width: 70px; }
  .log-content { flex: 1; }
}

.action-btns {
  text-align: center;
}
</style>
