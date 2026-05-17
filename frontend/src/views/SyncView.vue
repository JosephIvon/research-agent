<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">← 返回</el-button>
        <span class="page-title">同步到文档平台</span>
      </div>
    </header>

    <main class="layout-main">
      <div class="sync-container">
        <div class="platform-cards">
          <div class="platform-card card" :class="{ connected: feishuConnected }">
            <div class="platform-icon">📄</div>
            <div class="platform-name">飞书文档</div>
            <div class="platform-status">{{ feishuConnected ? '● 已连接' : '○ 未连接' }}</div>
            <el-button v-if="!feishuConnected" type="primary" @click="connectFeishu">连接</el-button>
            <el-button v-else @click="syncToFeishu">同步</el-button>
          </div>

          <div class="platform-card card" :class="{ connected: tencentConnected }">
            <div class="platform-icon">📊</div>
            <div class="platform-name">腾讯文档</div>
            <div class="platform-status">{{ tencentConnected ? '● 已连接' : '○ 未连接' }}</div>
            <el-button v-if="!tencentConnected" type="primary" @click="connectTencent">连接</el-button>
            <el-button v-else @click="syncToTencent">同步</el-button>
          </div>
        </div>

        <div class="sync-settings card">
          <div class="card-header">同步设置</div>
          <div class="setting-row">
            <el-checkbox v-model="syncFullReport">同步完整报告</el-checkbox>
          </div>
          <div class="setting-row">
            <el-checkbox v-model="syncSummaryOnly">仅同步摘要</el-checkbox>
          </div>
          <div class="setting-row">
            <span class="label">文档标题:</span>
            <el-input v-model="docTitle" placeholder="输入文档标题" class="title-input" />
          </div>
        </div>

        <div class="sync-history card">
          <div class="card-header">同步历史</div>
          <div class="history-list">
            <div v-for="(item, index) in syncHistory" :key="index" class="history-item" :class="item.status">
              <span class="history-icon">{{ item.status === 'success' ? '✓' : '✗' }}</span>
              <span class="history-date">{{ item.date }}</span>
              <span class="history-platform">{{ item.platform }}</span>
              <span class="history-status">{{ item.status === 'success' ? '成功' : '失败' }}</span>
            </div>
            <div v-if="!syncHistory.length" class="empty-history">暂无同步记录</div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { syncApi, researchApi } from '../api'

const feishuConnected = ref(false)
const tencentConnected = ref(false)
const syncFullReport = ref(true)
const syncSummaryOnly = ref(false)
const docTitle = ref('')
const syncHistory = ref([])
const reportContent = ref(null)

async function loadLatestReport() {
  try {
    const history = await researchApi.history()
    if (history?.length) {
      const latest = await researchApi.getReport(history[0].id)
      reportContent.value = latest?.markdown || null
    }
  } catch (_) {}
}

async function loadStatus() {
  try {
    const data = await syncApi.status()
    feishuConnected.value = Boolean(data.platforms?.feishu?.configured)
    tencentConnected.value = Boolean(data.platforms?.tencent?.configured)
  } catch (e) {
    feishuConnected.value = false
    tencentConnected.value = false
  }
}

function connectFeishu() {
  ElMessage.info('请在设置页面配置飞书App ID和App Secret')
}

function connectTencent() {
  ElMessage.info('请在设置页面配置腾讯文档App ID和App Secret')
}

async function syncToFeishu() {
  try {
    await syncApi.feishu({
      title: docTitle.value || '智能调研报告',
      full_report: syncFullReport.value,
      report_content: reportContent.value
    })
    syncHistory.value.unshift({
      platform: '飞书文档',
      status: 'success',
      date: new Date().toLocaleString()
    })
    ElMessage.success('已同步到飞书')
  } catch (e) {
    ElMessage.error('同步失败')
    syncHistory.value.unshift({
      platform: '飞书文档',
      status: 'failed',
      date: new Date().toLocaleString()
    })
  }
}

async function syncToTencent() {
  try {
    await syncApi.tencent({
      title: docTitle.value || '智能调研报告',
      full_report: syncFullReport.value,
      report_content: reportContent.value
    })
    syncHistory.value.unshift({
      platform: '腾讯文档',
      status: 'success',
      date: new Date().toLocaleString()
    })
    ElMessage.success('已同步到腾讯文档')
  } catch (e) {
    ElMessage.error('同步失败')
    syncHistory.value.unshift({
      platform: '腾讯文档',
      status: 'failed',
      date: new Date().toLocaleString()
    })
  }
}

onMounted(() => {
  loadStatus()
  loadLatestReport()
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
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.sync-container {
  max-width: 800px;
  margin: 0 auto;
}

.platform-cards {
  display: flex;
  gap: 24px;
  margin-bottom: 24px;
}

.platform-card {
  flex: 1;
  text-align: center;
  padding: 32px;

  &.connected {
    border: 2px solid var(--success);
  }

  .platform-icon { font-size: 48px; margin-bottom: 16px; }
  .platform-name { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
  .platform-status { font-size: 14px; color: var(--text-secondary); margin-bottom: 16px; }
}

.sync-settings {
  margin-bottom: 24px;

  .setting-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;

    .label { width: 80px; }
    .title-input { flex: 1; max-width: 400px; }
  }
}

.sync-history {
  .history-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .history-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    font-size: 14px;
    border-bottom: 1px solid var(--border);

    &.success .history-icon { color: var(--success); }
    &.failed .history-icon { color: var(--error); }

    .history-date { color: var(--text-secondary); }
    .history-platform { font-weight: 500; }
  }

  .empty-history {
    color: var(--text-secondary);
    text-align: center;
    padding: 24px;
  }
}
</style>
