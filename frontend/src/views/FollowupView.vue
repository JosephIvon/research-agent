<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push(`/report/${taskId}`)">← 返回报告</el-button>
        <span class="page-title">追问: {{ reportTitle }}</span>
      </div>
    </header>

    <main class="layout-main">
      <div class="followup-container">
        <div class="followup-sidebar">
          <div class="report-summary card">
            <div class="card-header">📋 报告摘要</div>
            <div class="summary-content" v-if="reportSummary">
              <div class="summary-item">
                <span class="label">质量:</span>
                <span class="value">{{ reportSummary.grade || 'B' }}</span>
              </div>
              <div class="summary-item">
                <span class="label">竞品:</span>
                <span class="value">{{ reportSummary.competitors || 0 }}个</span>
              </div>
              <div class="summary-item">
                <span class="label">缺失:</span>
                <span class="value">{{ reportSummary.missing_dims?.join(', ') || '无' }}</span>
              </div>
            </div>
            <el-button text type="primary" @click="$router.push(`/report/${taskId}`)">
              查看完整报告 →
            </el-button>
          </div>
        </div>

        <div class="followup-main">
          <div class="suggestions card">
            <div class="card-header">💡 推荐追问</div>
            <div class="suggestion-cards">
              <div
                v-for="item in suggestedQuestions"
                :key="item.dimension"
                class="suggestion-card"
                @click="sendQuestion(item.question)"
              >
                <span class="dimension-icon">{{ dimensionIcon(item.dimension) }}</span>
                <span class="question-text">{{ item.question }}</span>
              </div>
            </div>
          </div>

          <div class="chat-section card">
            <div class="chat-messages" ref="chatListRef">
              <div v-for="(msg, index) in chatHistory" :key="index" class="chat-message" :class="msg.role">
                <div class="message-avatar">{{ msg.role === 'user' ? '👤' : '🤖' }}</div>
                <div class="message-content">
                  <div class="message-text">{{ msg.content }}</div>
                  <div class="message-time">{{ msg.time }}</div>
                </div>
              </div>
            </div>

            <div class="chat-input">
              <el-input
                v-model="userQuestion"
                placeholder="请输入您的问题..."
                @keyup.enter="sendCustomQuestion"
              />
              <el-button type="primary" @click="sendCustomQuestion" :disabled="!userQuestion.trim()">
                发送
              </el-button>
            </div>

            <div v-if="chatHistory.length > 0" class="add-to-report">
              <el-button @click="addLastAnswerToReport">添加到报告</el-button>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { researchApi } from '../api'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const taskId = route.params.id
const store = useResearchStore()

const reportTitle = ref('加载中...')
const reportSummary = ref(null)
const reportContent = ref('')
const suggestedQuestions = ref([])
const chatHistory = ref([])
const userQuestion = ref('')
const chatListRef = ref(null)

function dimensionIcon(dimension) {
  const icons = {
    pricing: '💰',
    features: '⚙️',
    models: '🔧',
    integrations: '🔗',
    target_users: '👥',
    limitations: '⚠️',
    comparison: '📊',
    recommendations: '✅',
    technical: '🔬',
    market: '📈'
  }
  return icons[dimension] || '💬'
}

async function loadSuggestions() {
  try {
    const data = await researchApi.suggestFollowup({
      original_query: reportTitle.value,
      report_content: reportContent.value
    })
    suggestedQuestions.value = data.suggested_questions || []
  } catch (e) {
    suggestedQuestions.value = [
      { dimension: 'pricing', question: '定价策略有什么共同点和差异？' },
      { dimension: 'features', question: '核心功能的技术实现有什么特点？' },
      { dimension: 'recommendations', question: '针对中小企业应该如何选择？' }
    ]
  }
}

async function sendQuestion(question) {
  userQuestion.value = question
  await sendCustomQuestion()
}

async function sendCustomQuestion() {
  const question = userQuestion.value.trim()
  if (!question) return

  chatHistory.value.push({
    role: 'user',
    content: question,
    time: new Date().toLocaleTimeString()
  })

  userQuestion.value = ''
  await scrollToBottom()

  try {
    const data = await researchApi.followup({
      original_query: reportTitle.value,
      report_content: reportContent.value,
      question: question
    })
    chatHistory.value.push({
      role: 'assistant',
      content: data.answer || data.response || '抱歉，暂时无法回答这个问题。',
      time: new Date().toLocaleTimeString()
    })
  } catch (e) {
    chatHistory.value.push({
      role: 'assistant',
      content: '抱歉，发生了错误。',
      time: new Date().toLocaleTimeString()
    })
  }

  await scrollToBottom()
}

function addLastAnswerToReport() {
  const lastAnswer = chatHistory.value.filter(m => m.role === 'assistant').pop()
  if (lastAnswer) {
    ElMessage.success('已添加到报告')
  }
}

async function scrollToBottom() {
  await nextTick()
  if (chatListRef.value) {
    chatListRef.value.scrollTop = chatListRef.value.scrollHeight
  }
}

onMounted(async () => {
  try {
    const report = await store.getReport(taskId)
    reportTitle.value = report.title || route.query.title || '竞品分析'
    reportContent.value = report.markdown || ''
    reportSummary.value = report
  } catch (e) {
    reportTitle.value = route.query.title || '竞品分析'
    ElMessage.warning('未能读取报告内容，追问将使用默认建议')
  }
  await loadSuggestions()
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

.followup-container {
  display: flex;
  gap: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.followup-sidebar {
  width: 280px;
  flex-shrink: 0;
}

.report-summary {
  .summary-content {
    margin-bottom: 12px;
  }
  .summary-item {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    font-size: 14px;
    .label { color: var(--text-secondary); }
    .value { font-weight: 500; }
  }
}

.followup-main {
  flex: 1;
  min-width: 0;
}

.suggestions {
  margin-bottom: 16px;
}

.suggestion-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.suggestion-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--bg-hover);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
  &:hover {
    background: var(--primary);
    color: #fff;
    .question-text { color: #fff; }
  }

  .dimension-icon { font-size: 18px; }
  .question-text { font-size: 14px; color: var(--text-primary); }
}

.chat-section {
  min-height: 400px;
  display: flex;
  flex-direction: column;
}

.chat-messages {
  flex: 1;
  max-height: 400px;
  overflow-y: auto;
  padding: 16px 0;
}

.chat-message {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;

  &.user {
    flex-direction: row-reverse;
    .message-content { align-items: flex-end; }
  }

  .message-avatar { font-size: 24px; }

  .message-content {
    display: flex;
    flex-direction: column;
    gap: 4px;

    .message-text {
      padding: 12px 16px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.5;
    }
  }

  &.user .message-text {
    background: var(--primary);
    color: #fff;
  }

  &.assistant .message-text {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .message-time {
    font-size: 11px;
    color: var(--text-muted);
  }
}

.chat-input {
  display: flex;
  gap: 12px;
  padding-top: 16px;
  border-top: 1px solid var(--border);

  .el-input { flex: 1; }
}

.add-to-report {
  padding-top: 12px;
  text-align: right;
}
</style>
