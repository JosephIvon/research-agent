<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push(`/report/${taskId}`)">← 返回报告</el-button>
        <span class="page-title">多角色审查 & PRD</span>
      </div>
      <div class="header-tabs">
        <span :class="{ active: activeTab === 'review' }" @click="activeTab = 'review'">审查</span>
        <span :class="{ active: activeTab === 'prd' }" @click="activeTab = 'prd'">PRD</span>
      </div>
    </header>

    <main class="layout-main">
      <div class="review-container" v-if="activeTab === 'review'">
        <div class="role-cards">
          <div v-for="role in roles" :key="role.id" class="role-card card">
            <div class="role-header">
              <span class="role-icon">{{ role.icon }}</span>
              <span class="role-name">{{ role.name }}</span>
            </div>
            <div class="role-score" v-if="role.score">
              <span class="score-value">{{ role.score }}</span>
              <span class="score-label">评分</span>
            </div>
            <div class="role-opinion" v-if="role.opinion">{{ role.opinion }}</div>
            <div class="role-status" :class="role.status">{{ role.status_text }}</div>
            <el-button v-if="!role.score" type="primary" :loading="role.loading" @click="generateReview(role.id)">
              生成{{ role.name }}审查
            </el-button>
          </div>
        </div>

        <div class="review-actions">
          <el-button type="primary" @click="generateAllReview" :loading="isGeneratingAll">
            生成全角色审查
          </el-button>
        </div>

        <div class="action-items card" v-if="actionItems.length">
          <div class="card-header">📋 行动项清单</div>
          <div class="items-list">
            <div v-for="(item, index) in actionItems" :key="index" class="action-item">
              <el-checkbox v-model="item.checked">{{ item.text }}</el-checkbox>
            </div>
          </div>
        </div>
      </div>

      <div class="prd-container" v-if="activeTab === 'prd'">
        <div class="prd-actions">
          <el-button type="primary" :loading="isGeneratingPRD" @click="generatePRD">
            🚀 一键生成PRD
          </el-button>
        </div>

        <div class="prd-content card" v-if="prdContent">
          <div class="markdown-content" v-html="renderedPRD"></div>
        </div>

        <div class="prd-empty" v-else-if="!isGeneratingPRD">
          <p>点击上方按钮生成PRD文档</p>
        </div>

        <div class="prd-loading" v-else>
          <el-skeleton :rows="10" animated />
        </div>

        <div class="prd-footer-actions" v-if="prdContent">
          <el-button @click="exportPRD">📥 导出PRD</el-button>
          <el-button type="primary" @click="syncPRD">📤 同步到飞书</el-button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { researchApi, syncApi } from '../api'
import MarkdownIt from 'markdown-it'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const taskId = route.params.id
const store = useResearchStore()

const activeTab = ref('review')
const roles = ref([
  { id: 'developer', name: '开发', icon: '👨‍💻', score: null, opinion: null, status: 'pending', status_text: '待生成', loading: false },
  { id: 'tester', name: '测试', icon: '👩‍🔬', score: null, opinion: null, status: 'pending', status_text: '待生成', loading: false },
  { id: 'operations', name: '运营', icon: '👨‍🎤', score: null, opinion: null, status: 'pending', status_text: '待生成', loading: false }
])
const actionItems = ref([])
const isGeneratingAll = ref(false)
const isGeneratingPRD = ref(false)
const prdContent = ref(null)
const reportContent = ref('')
const reportTitle = ref('')
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const renderedPRD = computed(() => prdContent.value ? md.render(prdContent.value) : '')

async function generateReview(roleId) {
  if (!reportContent.value) {
    ElMessage.warning('报告内容尚未加载完成')
    return
  }
  const role = roles.value.find(r => r.id === roleId)
  if (role) {
    role.loading = true
    try {
      const data = await researchApi.multiRoleReview({
        report_content: reportContent.value,
        role: roleId
      })
      role.score = data.score || 8.0
      role.opinion = data.review || data.opinion || '审查完成'
      role.status = data.passed === false ? 'warn' : 'pass'
      role.status_text = data.passed === false ? '⚠️ 建议' : '✓ 已生成'
    } catch (e) {
      ElMessage.error(`生成${role.name}审查失败`)
    } finally {
      role.loading = false
    }
  }
}

async function generateAllReview() {
  if (!reportContent.value) {
    ElMessage.warning('报告内容尚未加载完成')
    return
  }
  isGeneratingAll.value = true
  try {
    const data = await researchApi.multiRoleReview({
      report_content: reportContent.value
    })
    if (data.reviews) {
      Object.entries(data.reviews).forEach(([roleId, review]) => {
        const role = roles.value.find(r => r.id === roleId)
        if (role) {
          role.score = review.score || 8.0
          role.opinion = review.review || review.opinion || '审查完成'
          role.status = review.status === 'failed' ? 'warn' : 'pass'
          role.status_text = review.status === 'failed' ? '⚠️ 失败' : '✓ 已生成'
        }
      })
    }
    if (data.action_items) {
      actionItems.value = data.action_items.map(item => ({ text: item, checked: false }))
    }
  } catch (e) {
    ElMessage.error('生成全角色审查失败')
  } finally {
    isGeneratingAll.value = false
  }
}

async function generatePRD() {
  if (!reportContent.value) {
    ElMessage.warning('报告内容尚未加载完成')
    return
  }
  isGeneratingPRD.value = true
  try {
    const data = await researchApi.prd({
      report_content: reportContent.value,
      query: reportTitle.value || '竞品分析'
    })
    prdContent.value = data.prd || data.content || ''
  } catch (e) {
    ElMessage.error('生成PRD失败')
  } finally {
    isGeneratingPRD.value = false
  }
}

function exportPRD() {
  const blob = new Blob([prdContent.value], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'prd.md'
  a.click()
  URL.revokeObjectURL(url)
}

async function syncPRD() {
  try {
    await syncApi.feishu({
      report_content: prdContent.value,
      title: 'PRD文档'
    })
    ElMessage.success('已同步到飞书')
  } catch (e) {
    ElMessage.error('同步失败')
  }
}

onMounted(async () => {
  try {
    const report = await store.getReport(taskId)
    reportContent.value = report.markdown || ''
    reportTitle.value = report.query || report.title || '竞品分析'
  } catch (e) {
    ElMessage.error('加载报告失败')
  }
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

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-tabs {
  display: flex;
  gap: 24px;
  font-size: 14px;

  span {
    padding: 4px 0;
    cursor: pointer;
    color: var(--text-secondary);
    border-bottom: 2px solid transparent;

    &.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
    }
  }
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.review-container, .prd-container {
  max-width: 900px;
  margin: 0 auto;
}

.role-cards {
  display: flex;
  gap: 16px;
  margin-bottom: 24px;
}

.role-card {
  flex: 1;
  text-align: center;
  padding: 24px;

  .role-header {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-bottom: 16px;
    .role-icon { font-size: 32px; }
    .role-name { font-size: 18px; font-weight: 600; }
  }

  .role-score {
    margin-bottom: 12px;
    .score-value { font-size: 32px; font-weight: 600; color: var(--primary); }
    .score-label { font-size: 12px; color: var(--text-secondary); }
  }

  .role-opinion {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 12px;
    text-align: left;
  }

  .role-status {
    font-size: 14px;
    font-weight: 500;
    &.pass { color: var(--success); }
    &.warn { color: var(--warning); }
  }
}

.review-actions, .prd-actions {
  text-align: center;
  margin-bottom: 24px;
}

.action-items {
  .items-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
}

.prd-content {
  margin-bottom: 24px;
}

.prd-empty, .prd-loading {
  background: #fff;
  border-radius: 12px;
  padding: 48px;
  text-align: center;
  color: var(--text-secondary);
}

.prd-footer-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
}
</style>
