<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <span class="page-title">历史报告</span>
      </div>
      <div class="header-actions">
        <el-input v-model="searchKeyword" placeholder="搜索..." :prefix-icon="Search" clearable class="search-input" />
      </div>
    </header>

    <main class="layout-main">
      <div class="history-container">
        <div v-if="filteredReports.length === 0" class="empty-state">
          <p>暂无历史报告</p>
          <el-button type="primary" @click="$router.push('/')">开始调研</el-button>
        </div>

        <div v-else class="report-list">
          <div v-for="group in groupedReports" :key="group.date" class="report-group">
            <div class="group-header">{{ group.label }}</div>
            <div class="group-cards">
              <div v-for="report in group.reports" :key="report.id" class="report-card card" @click="viewReport(report.id)">
                <div class="report-card-header">
                  <span class="report-icon">📊</span>
                  <span class="report-title">{{ report.title }}</span>
                </div>
                <div class="report-card-meta">
                  <span class="grade-badge" :class="'grade-' + (report.grade || 'b').toLowerCase()">
                    {{ report.grade || 'B' }}
                  </span>
                  <span class="meta-item">{{ report.competitors || 0 }}个竞品</span>
                  <span class="meta-item">{{ report.created_at || report.date }}</span>
                </div>
                <div class="report-card-actions">
                  <el-button size="small" @click.stop="viewReport(report.id)">查看</el-button>
                  <el-button size="small" @click.stop="reExecute(report)">重新执行</el-button>
                  <el-button size="small" type="danger" @click.stop="deleteReport(report.id)">删除</el-button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { useResearchStore } from '../stores/research'

const router = useRouter()
const store = useResearchStore()

const reports = ref([])
const searchKeyword = ref('')

const filteredReports = computed(() => {
  if (!searchKeyword.value) return reports.value
  const keyword = searchKeyword.value.toLowerCase()
  return reports.value.filter(r =>
    (r.title || '').toLowerCase().includes(keyword)
  )
})

const groupedReports = computed(() => {
  const groups = {}
  const today = new Date().toDateString()
  const yesterday = new Date(Date.now() - 86400000).toDateString()

  filteredReports.value.forEach(report => {
    const dateStr = new Date(report.date || report.created_at).toDateString()
    let label
    if (dateStr === today) label = '🕐 今天'
    else if (dateStr === yesterday) label = '🕐 昨天'
    else label = dateStr

    if (!groups[label]) groups[label] = { label, reports: [] }
    groups[label].reports.push(report)
  })

  return Object.values(groups)
})

async function loadHistory() {
  try {
    reports.value = await store.fetchHistory()
  } catch (e) {
    reports.value = store.taskHistory
  }
}

function viewReport(id) {
  router.push(`/report/${id}`)
}

function reExecute(report) {
  router.push(`/?query=${encodeURIComponent(report.query || report.title)}`)
}

async function deleteReport(id) {
  try {
    await ElMessageBox.confirm('确定要删除这个报告吗？', '提示', {
      type: 'warning'
    })
    await store.deleteReport(id)
    reports.value = reports.value.filter(r => r.id !== id)
    ElMessage.success('已删除')
  } catch (e) {
  }
}

onMounted(() => {
  loadHistory()
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
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: #fff;
  border-bottom: 1px solid var(--border);
}

.header-actions {
  .search-input { width: 200px; }
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.history-container {
  max-width: 900px;
  margin: 0 auto;
}

.empty-state {
  text-align: center;
  padding: 48px;
  color: var(--text-secondary);
}

.report-group {
  margin-bottom: 24px;
}

.group-header {
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}

.group-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.report-card {
  padding: 16px;
  cursor: pointer;
  transition: all 0.2s;
  &:hover { box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1); }

  .report-card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    .report-icon { font-size: 20px; }
    .report-title { font-size: 16px; font-weight: 500; }
  }

  .report-card-meta {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .report-card-actions {
    display: flex;
    gap: 8px;
  }
}
</style>
