<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <span class="report-title">结果中心</span>
      </div>
      <div class="header-actions">
        <ThemeToggle />
        <el-button @click="copyReport">
          <el-icon><DocumentCopy /></el-icon>
          复制当前
        </el-button>
        <el-dropdown trigger="click" @command="handleExportFormat">
          <el-button type="primary">
            <el-icon><Download /></el-icon>
            导出
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="pdf">导出 PDF</el-dropdown-item>
              <el-dropdown-item command="word">导出 Word</el-dropdown-item>
              <el-dropdown-item command="html">导出 HTML</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <main class="layout-main">
      <div class="report-container" v-if="report">
        <aside class="report-sidebar">
          <section class="score-panel">
            <div class="score-head">
              <div class="grade-badge" :class="gradeClass">{{ gradeDisplay }}</div>
              <div>
                <div class="score-value">{{ scoreDisplay }}</div>
                <div class="score-label">质量评分 / 10</div>
              </div>
            </div>
            <div class="score-track" aria-hidden="true">
              <div class="score-fill" :class="scoreTone" :style="{ width: `${scorePercent}%` }"></div>
            </div>
            <p class="score-note">{{ qualityMessage }}</p>
          </section>

          <section class="side-panel">
            <h2>报告信息</h2>
            <dl class="meta-list">
              <div>
                <dt>生成时间</dt>
                <dd>{{ createdAt }}</dd>
              </div>
              <div>
                <dt>竞品数量</dt>
                <dd>{{ competitorCount }} 个</dd>
              </div>
              <div>
                <dt>自动评级</dt>
                <dd>{{ gradeDisplay }} 级</dd>
              </div>
              <div>
                <dt>PRD</dt>
                <dd>{{ hasPRD ? '已生成' : '未生成' }}</dd>
              </div>
            </dl>
          </section>

          <section class="side-panel" v-if="costReport">
            <h2>成本估算</h2>
            <dl class="meta-list">
              <div>
                <dt>Token 用量</dt>
                <dd>{{ formatCostNumber(costReport.total_tokens) }}</dd>
              </div>
              <div>
                <dt>费用估算（USD）</dt>
                <dd>{{ formatCostUsd(costReport) }}</dd>
              </div>
              <div>
                <dt>模型调用次数</dt>
                <dd>{{ formatCostNumber(costReport.calls) }}</dd>
              </div>
              <div>
                <dt>计量口径</dt>
                <dd>
                  <el-tag v-if="costReport.estimated" type="warning" size="small">估算值</el-tag>
                  <el-tag v-else type="success" size="small">API 真值</el-tag>
                </dd>
              </div>
            </dl>
          </section>

          <section class="side-panel" v-if="missingDimensions.length">
            <h2>缺失维度</h2>
            <div class="missing-tags">
              <el-tag v-for="dim in missingDimensions" :key="dim" type="warning">{{ dim }}</el-tag>
            </div>
          </section>

          <section class="side-panel feedback-panel">
            <h2>报告反馈</h2>
            <div class="feedback-buttons">
              <el-button
                :type="feedbackRating === 'up' ? 'success' : 'default'"
                :disabled="isSendingFeedback"
                @click="submitFeedback('up')"
              >
                👍 有用（{{ feedbackCounts.up }}）
              </el-button>
              <el-button
                :type="feedbackRating === 'down' ? 'danger' : 'default'"
                :disabled="isSendingFeedback"
                @click="submitFeedback('down')"
              >
                👎 需改进（{{ feedbackCounts.down }}）
              </el-button>
            </div>
            <div v-if="feedbackRating" class="feedback-comment">
              <el-input
                v-model="feedbackComment"
                maxlength="120"
                placeholder="补充一句话意见（可选）"
                @keyup.enter="submitComment"
              />
              <el-button type="primary" :loading="isSendingFeedback" @click="submitComment">提交意见</el-button>
            </div>
          </section>

          <section class="side-actions">
            <el-button type="primary" @click="$router.push(`/followup/${taskId}`)">
              <el-icon><ChatDotRound /></el-icon>
              追问
            </el-button>
            <el-button @click="$router.push(`/review/${taskId}`)">
              <el-icon><View /></el-icon>
              多角色审查
            </el-button>
            <el-button v-if="traceUrl" @click="openTrace">
              <el-icon><Link /></el-icon>
              查看 Trace
            </el-button>
            <el-button @click="$router.push('/sync')">
              <el-icon><Upload /></el-icon>
              同步
            </el-button>
          </section>
        </aside>

        <section class="report-content">
          <el-alert
            v-if="reportIncomplete"
            class="incomplete-alert"
            type="error"
            show-icon
            :closable="false"
            title="本次结果不完整：任务触发了预算上限并提前收束"
            description="下方报告由预算耗尽前已采集的证据生成，可能没有覆盖全部竞品或调研维度。请勿把它当作完整结论；如需完整报告，请提高预算后重跑。"
          />

          <el-alert
            v-if="injectionFindingsCount > 0"
            class="injection-alert"
            type="warning"
            show-icon
            :closable="false"
            :title="`已检测并净化 ${injectionFindingsCount} 处来源注入痕迹`"
          >
            <template v-if="injectionSources.length" #default>
              <ul class="injection-sources">
                <li v-for="item in injectionSources" :key="item.url + item.finding_count">
                  来源 {{ item.url || '（报告正文回声）' }}：{{ item.finding_count }} 处<template v-if="item.high_count">（高危 {{ item.high_count }}）</template>
                </li>
              </ul>
            </template>
          </el-alert>

          <div class="title-panel">
            <div>
              <div class="section-kicker">调研主题</div>
              <h1>{{ displayTitle }}</h1>
            </div>
            <div class="title-tags">
              <el-tag v-if="reportIncomplete" type="warning" effect="dark" class="partial-tag">
                预算收束 · 内容不完整
              </el-tag>
              <el-tag :type="qualityTagType" effect="light">{{ qualityTagText }}</el-tag>
            </div>
          </div>

          <div v-if="isLowQuality" class="quality-callout">
            <strong>当前报告数据不足</strong>
            <span>建议补充明确竞品 URL，若网站需要登录，请在首页为对应网站单独填写登录凭据后重新调研。</span>
          </div>

          <nav class="artifact-tabs" aria-label="交付物">
            <button :class="{ active: activeTab === 'report' }" type="button" @click="activeTab = 'report'">
              竞品报告
            </button>
            <button :class="{ active: activeTab === 'prd' }" type="button" @click="activeTab = 'prd'">
              PRD
              <span v-if="hasPRD" class="tab-dot"></span>
            </button>
            <button :class="{ active: activeTab === 'sources' }" type="button" @click="activeTab = 'sources'">
              来源状态
            </button>
          </nav>

          <article v-if="activeTab === 'report'" class="report-body">
            <div class="markdown-content" v-html="renderedMarkdown"></div>
          </article>

          <section v-else-if="activeTab === 'prd'" class="report-body prd-body">
            <div v-if="hasPRD" class="markdown-content" v-html="renderedPRD"></div>
            <div v-else class="empty-artifact">
              <h2>PRD 还没有生成</h2>
              <p>如果这份竞品报告质量足够，可以直接把报告洞察转成产品需求文档。</p>
              <el-button type="primary" :loading="isGeneratingPRD" @click="generatePRD">
                生成 PRD
              </el-button>
            </div>
          </section>

          <section v-else class="report-body sources-body">
            <div class="source-header">
              <div>
                <h2>来源和抓取状态</h2>
                <p>这里展示本次调研实际读取到的竞品来源，便于判断报告可信度。</p>
              </div>
              <el-tag :type="sourceRows.length ? 'success' : 'warning'">
                {{ sourceRows.length ? `${sourceRows.length} 个来源` : '暂无来源' }}
              </el-tag>
            </div>
            <div v-if="sourceRows.length" class="source-list">
              <article v-for="source in sourceRows" :key="source.url" class="source-item" :class="source.status">
                <div>
                  <strong>{{ source.name }}</strong>
                  <a :href="source.url" target="_blank" rel="noreferrer">{{ source.url }}</a>
                </div>
                <div class="source-meta">
                  <el-tag :type="source.status === 'success' ? 'success' : 'danger'" size="small">
                    {{ source.status === 'success' ? '成功' : '失败' }}
                  </el-tag>
                  <span>{{ source.dataCount }} 条信息</span>
                  <span>{{ source.pageCount || 0 }} 个站内页</span>
                </div>
                <div v-if="source.pageUrls.length" class="source-pages">
                  <span>已读取页面</span>
                  <a v-for="url in source.pageUrls" :key="url" :href="url" target="_blank" rel="noreferrer">
                    {{ url }}
                  </a>
                </div>
                <div v-if="source.discoveredUrls.length" class="source-pages discovered">
                  <span>发现的高价值入口</span>
                  <a v-for="url in source.discoveredUrls" :key="url" :href="url" target="_blank" rel="noreferrer">
                    {{ url }}
                  </a>
                </div>
                <p v-if="source.error">{{ source.error }}</p>
              </article>
            </div>
            <div v-else class="empty-artifact">
              <h2>没有可用来源</h2>
              <p>建议回到首页补充明确的竞品网站，或检查需要登录的网站账号密码是否可用。</p>
              <el-button type="primary" @click="$router.push('/')">补充竞品继续调研</el-button>
            </div>
          </section>

          <section class="chart-panel" v-if="activeTab === 'report' && chartData">
            <h2>功能评分雷达图</h2>
            <div ref="chartRef" class="radar-chart"></div>
          </section>
        </section>
      </div>

      <div v-else class="loading-state">
        <el-skeleton :rows="10" animated />
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowDown,
  ArrowLeft,
  ChatDotRound,
  DocumentCopy,
  Download,
  Link,
  Moon,
  Sunny,
  Upload,
  View
} from '@element-plus/icons-vue'
import ThemeToggle from '../components/ThemeToggle.vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { RadarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, RadarComponent, TooltipComponent } from 'echarts/components'
import { init, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useResearchStore } from '../stores/research'
import { buildSourceRows, getReportCompetitorCount } from '../stores/researchTaskHelpers'
import { gradeClassName, resolveGrade } from '../utils/reportGrade'
import { researchApi } from '../api'

const route = useRoute()
const store = useResearchStore()
use([RadarChart, RadarComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const report = ref(null)
const taskId = route.params.id
const requestedTab = typeof route.query.tab === 'string' ? route.query.tab : ''
const activeTab = ref(['report', 'prd', 'sources'].includes(requestedTab) ? requestedTab : 'report')
const isGeneratingPRD = ref(false)
const chartRef = ref(null)
const feedbackCounts = ref({ up: 0, down: 0 })
const feedbackRating = ref('')
const feedbackComment = ref('')
const isSendingFeedback = ref(false)
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
let chartInstance = null

const numericScore = computed(() => {
  const value = Number(report.value?.score)
  return Number.isFinite(value) ? Math.max(0, Math.min(10, value)) : null
})

const scoreDisplay = computed(() => numericScore.value === null ? '暂无' : numericScore.value.toFixed(1))
const scorePercent = computed(() => numericScore.value === null ? 0 : Math.round(numericScore.value * 10))
const gradeDisplay = computed(() => resolveGrade(numericScore.value, report.value?.grade))
const gradeClass = computed(() => gradeClassName(gradeDisplay.value))
const scoreTone = computed(() => {
  if (numericScore.value === null) return 'score-muted'
  if (numericScore.value < 3) return 'score-bad'
  if (numericScore.value < 6) return 'score-warn'
  return 'score-good'
})
const isLowQuality = computed(() => numericScore.value !== null && numericScore.value < 5)
const qualityTagType = computed(() => isLowQuality.value ? 'warning' : 'success')
const qualityTagText = computed(() => isLowQuality.value ? '需要补充数据' : '可阅读')
const qualityMessage = computed(() => {
  if (numericScore.value === null) return '暂无质量评分，建议检查报告内容完整性。'
  if (numericScore.value < 5) return '数据基础偏弱，报告更适合作为问题诊断。'
  if (numericScore.value < 7) return '信息可用，但建议补充更多竞品来源。'
  return '信息覆盖较完整，可进入追问和审查。'
})
const createdAt = computed(() => report.value?.created_at || '未知')
const displayTitle = computed(() => {
  const rawTitle = report.value?.title || ''
  const trimmedTitle = rawTitle.trim()
  if (trimmedTitle && !trimmedTitle.startsWith('>') && !trimmedTitle.includes('数据质量提示')) {
    return trimmedTitle
  }
  const heading = report.value?.markdown?.match(/^#\s+(.+)$/m)?.[1]
  return heading || report.value?.query || '竞品分析报告'
})

const renderedMarkdown = computed(() => {
  return report.value?.markdown ? DOMPurify.sanitize(md.render(report.value.markdown)) : ''
})
const renderedPRD = computed(() => {
  return report.value?.prd ? DOMPurify.sanitize(md.render(report.value.prd)) : ''
})

const missingDimensions = computed(() => report.value?.missing_dimensions || [])
const chartData = computed(() => report.value?.radar_data)
const hasPRD = computed(() => Boolean(report.value?.prd))
const competitorCount = computed(() => getReportCompetitorCount(report.value || {}))
const sourceRows = computed(() => buildSourceRows(report.value || {}))
const injectionFindingsCount = computed(() => Number(report.value?.injection_findings_count) || 0)
// SSE 实时收集的注入发现带来源 URL，终态计数只有一个总数。这里展示来源维度。
const injectionSources = computed(() => {
  const list = report.value?.injection_findings
  return Array.isArray(list) ? list : []
})
const reportIncomplete = computed(() => Boolean(report.value?.incomplete))
const traceUrl = computed(() => report.value?.trace_url || '')
const costReport = computed(() => report.value?.cost_report || null)
const activeArtifactContent = computed(() => {
  if (activeTab.value === 'prd') return report.value?.prd || ''
  if (activeTab.value === 'sources') {
    return sourceRows.value.map(item => {
      const pages = item.pageUrls.length
        ? `\n已读取页面:\n${item.pageUrls.map(url => `- ${url}`).join('\n')}`
        : ''
      const discovered = item.discoveredUrls.length
        ? `\n发现入口:\n${item.discoveredUrls.map(url => `- ${url}`).join('\n')}`
        : ''
      return `${item.name}\n${item.url}\n状态: ${item.status}\n信息数量: ${item.dataCount}\n站内页: ${item.pageCount}${pages}${discovered}${item.error ? `\n错误: ${item.error}` : ''}`
    }).join('\n\n')
  }
  return report.value?.markdown || ''
})
const activeArtifactFilename = computed(() => {
  if (activeTab.value === 'prd') return 'prd.md'
  if (activeTab.value === 'sources') return 'sources.md'
  return 'report.md'
})

async function loadReport() {
  try {
    report.value = await store.getReport(taskId)
    if (chartData.value) {
      setTimeout(initChart, 100)
    }
  } catch (e) {
    ElMessage.error('加载报告失败')
  }
}

function formatCostNumber(value) {
  const num = Number(value)
  return Number.isFinite(num) ? num.toLocaleString() : '—'
}

// 费用只渲染 estimated_usd。此前写成 `estimated_usd ?? estimated ?? '—'`，
// estimated 是个布尔标记（true = 本轮只有估算值），一旦金额缺失就会把
// `true` 直接当美元渲染出来，显示成 "$true"。口径标签单独一行说明。
function formatCostUsd(report) {
  const usd = Number(report?.estimated_usd)
  return Number.isFinite(usd) ? `≈ $${usd}` : '—'
}

function openTrace() {
  if (traceUrl.value) window.open(traceUrl.value, '_blank')
}

function applyFeedbackSummary(summary) {
  if (summary && typeof summary === 'object') {
    feedbackCounts.value = { up: summary.up || 0, down: summary.down || 0 }
  }
}

async function loadFeedbackCounts() {
  if (!report.value?.id) return
  try {
    const counts = await researchApi.getReportFeedback(report.value.id)
    applyFeedbackSummary(counts)
  } catch (e) {
    // 计数获取失败时保持 0 显示，不打断页面
  }
}

async function submitFeedback(rating) {
  if (!report.value?.id || isSendingFeedback.value) return
  feedbackRating.value = rating
  isSendingFeedback.value = true
  try {
    const result = await researchApi.addReportFeedback(report.value.id, rating)
    applyFeedbackSummary(result?.summary)
    ElMessage.success(rating === 'up' ? '感谢你的认可' : '已收到改进建议')
  } catch (e) {
    // 失败提示由 api 拦截器统一处理
  } finally {
    isSendingFeedback.value = false
  }
}

async function submitComment() {
  const comment = feedbackComment.value.trim()
  if (!comment || !feedbackRating.value || isSendingFeedback.value) return
  isSendingFeedback.value = true
  try {
    const result = await researchApi.addReportFeedback(report.value.id, feedbackRating.value, comment)
    applyFeedbackSummary(result?.summary)
    feedbackComment.value = ''
    feedbackRating.value = ''
    ElMessage.success('意见已提交，感谢反馈')
  } catch (e) {
    // 失败提示由 api 拦截器统一处理
  } finally {
    isSendingFeedback.value = false
  }
}

function initChart() {
  if (!chartRef.value || !chartData.value) return
  if (chartInstance) {
    chartInstance.dispose()
  }
  chartInstance = init(chartRef.value)
  chartInstance.setOption({
    radar: {
      indicator: chartData.value.map(item => ({
        name: item.name,
        max: 10
      }))
    },
    series: [{
      type: 'radar',
      data: [{
        value: chartData.value.map(item => item.score),
        name: '功能评分'
      }]
    }]
  })
}

function copyReport() {
  navigator.clipboard.writeText(activeArtifactContent.value || '')
  ElMessage.success('已复制到剪贴板')
}

function exportReport() {
  const content = activeArtifactContent.value || ''
  const blob = new Blob([content], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = activeArtifactFilename.value
  a.click()
  URL.revokeObjectURL(url)
}

async function handleExportFormat(format) {
  if (!report.value?.id) {
    ElMessage.warning('报告尚未加载完成')
    return
  }
  try {
    const contentType = activeTab.value === 'prd' ? 'prd' : 'report'
    const response = await fetch(`${import.meta.env.VITE_API_BASE_URL || '/api'}/research/export`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(getAuthHeaders())
      },
      body: JSON.stringify({
        report_id: report.value.id,
        format,
        content_type: contentType
      })
    })
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: '导出失败' }))
      throw new Error(err.detail || '导出失败')
    }
    const blob = await response.blob()
    const filename = response.headers.get('content-disposition')?.match(/filename=(.+)/)?.[1] || `report.${format}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename.replace(/"/g, '')
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${format.toUpperCase()}`)
  } catch (e) {
    ElMessage.error(e.message || '导出失败，请稍后重试')
  }
}

function getAuthHeaders() {
  const token = sessionStorage.getItem('auth_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function generatePRD() {
  if (!report.value?.markdown) {
    ElMessage.warning('报告内容尚未加载完成')
    return
  }

  isGeneratingPRD.value = true
  try {
    const data = await researchApi.prd({
      report_content: report.value.markdown,
      query: report.value.query || report.value.title || '竞品分析'
    })
    const updated = store.updateReport(taskId, {
      prd: data.prd || data.content || '',
      prd_created_at: new Date().toISOString()
    })
    report.value = updated || {
      ...report.value,
      prd: data.prd || data.content || '',
      prd_created_at: new Date().toISOString()
    }
    activeTab.value = 'prd'
    ElMessage.success('PRD 已生成')
  } catch (e) {
    ElMessage.error('生成 PRD 失败')
  } finally {
    isGeneratingPRD.value = false
  }
}

onMounted(async () => {
  await loadReport()
  loadFeedbackCounts()
})

watch(activeTab, tab => {
  if (tab === 'report' && chartData.value) {
    setTimeout(initChart, 100)
  }
})

onUnmounted(() => {
  if (chartInstance) {
    chartInstance.dispose()
    chartInstance = null
  }
})
</script>

<style lang="scss" scoped>
.layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f4f7fb;
}

.layout-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  min-height: 64px;
  padding: 0 28px;
  background: #fff;
  border-bottom: 1px solid #dbe3ee;
}

.header-left,
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.report-title {
  color: #172033;
  font-weight: 700;
}

.layout-main {
  flex: 1;
  padding: 28px;
}

.report-container {
  width: min(1480px, 100%);
  margin: 0 auto;
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}

.report-sidebar {
  position: sticky;
  top: 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.score-panel,
.side-panel,
.title-panel,
.report-body,
.chart-panel {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
}

.score-panel,
.side-panel,
.title-panel,
.report-body,
.chart-panel {
  padding: 22px;
}

.score-head {
  display: flex;
  align-items: center;
  gap: 14px;
}

.grade-badge {
  flex: 0 0 44px;
}

.score-value {
  color: #172033;
  font-size: 28px;
  line-height: 1;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}

.score-label {
  margin-top: 4px;
  color: #64748b;
  font-size: 12px;
}

.score-track {
  height: 8px;
  margin-top: 18px;
  overflow: hidden;
  border-radius: 999px;
  background: #e8eef6;
}

.score-fill {
  height: 100%;
  border-radius: inherit;
  background: #2563eb;
}

.score-fill.score-good {
  background: #16a34a;
}

.score-fill.score-warn {
  background: #f97316;
}

.score-fill.score-bad {
  background: #ef4444;
}

.score-fill.score-muted {
  background: #94a3b8;
}

.score-note {
  margin: 12px 0 0;
  color: #64748b;
}

.side-panel h2,
.chart-panel h2 {
  margin: 0 0 14px;
  color: #172033;
  font-size: 15px;
}

.meta-list {
  margin: 0;
}

.meta-list > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  border-bottom: 1px solid #edf2f7;
}

.meta-list > div:last-child {
  border-bottom: 0;
}

.meta-list dt {
  color: #64748b;
}

.meta-list dd {
  margin: 0;
  color: #172033;
  font-weight: 700;
  text-align: right;
}

.missing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.injection-alert {
  border-radius: 8px;
}

/* 预算收束的提示刻意比注入提示更重：注入痕迹被净化掉了，报告本身是可信的；
   而 partial 的报告是真的缺内容——两者混成同一种黄色 warning 会被一起忽略。 */
.incomplete-alert {
  border-radius: 8px;
  border-left: 4px solid var(--el-color-danger);
}

.title-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.partial-tag {
  font-weight: 700;
  letter-spacing: 0.02em;
}

.injection-sources {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
  word-break: break-all;
}

.feedback-buttons {
  display: flex;
  gap: 8px;

  .el-button {
    flex: 1;
    margin: 0;
  }
}

.feedback-comment {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.side-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.report-content {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.title-panel {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.section-kicker {
  margin-bottom: 8px;
  color: #2563eb;
  font-size: 12px;
  font-weight: 700;
}

.title-panel h1 {
  margin: 0;
  max-width: 920px;
  color: #172033;
  font-size: 26px;
  line-height: 1.35;
  letter-spacing: 0;
}

.quality-callout {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 14px 16px;
  border-radius: 8px;
  color: #713f12;
  background: #fff7ed;
  box-shadow: inset 0 0 0 1px #fed7aa;

  strong {
    flex: 0 0 auto;
  }
}

.artifact-tabs {
  display: flex;
  gap: 8px;
  padding: 6px;
  border-radius: 10px;
  background: #eaf0f7;

  button {
    min-height: 38px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 0 16px;
    border: 0;
    border-radius: 8px;
    color: #475569;
    background: transparent;
    font-weight: 700;
    cursor: pointer;

    &.active {
      color: #172033;
      background: #fff;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.10);
    }
  }
}

.tab-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: #16a34a;
}

.report-body {
  min-height: 360px;
}

.prd-body,
.sources-body {
  min-height: 520px;
}

.empty-artifact {
  min-height: 360px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;

  h2 {
    margin: 0 0 8px;
    color: #172033;
    font-size: 20px;
    letter-spacing: 0;
  }

  p {
    max-width: 460px;
    margin: 0 0 18px;
    color: #64748b;
  }
}

.source-header {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 18px;

  h2 {
    margin: 0 0 6px;
    color: #172033;
    font-size: 20px;
    letter-spacing: 0;
  }

  p {
    margin: 0;
    color: #64748b;
  }
}

.source-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.source-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  padding: 14px;
  border-radius: 8px;
  background: #f8fbff;
  box-shadow: inset 0 0 0 1px #dbe6f4;

  strong,
  a {
    display: block;
  }

  strong {
    color: #172033;
  }

  a {
    margin-top: 4px;
    overflow-wrap: anywhere;
  }

  p {
    grid-column: 1 / -1;
    margin: 0;
    color: #b91c1c;
  }
}

.source-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #64748b;
  font-size: 13px;
  white-space: nowrap;
}

.source-pages {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 6px 10px;
  padding-top: 2px;
  font-size: 13px;

  span {
    color: #64748b;
    font-weight: 700;
  }

  a {
    margin-top: 0;
    color: #2563eb;
  }
}

.radar-chart {
  width: 100%;
  height: 300px;
}

.loading-state {
  max-width: 900px;
  margin: 0 auto;
}

@media (max-width: 960px) {
  .layout-header {
    align-items: flex-start;
    flex-direction: column;
    height: auto;
    padding: 12px 18px;
  }

  .layout-main {
    padding: 18px;
  }

  .report-container {
    grid-template-columns: 1fr;
  }

  .report-sidebar {
    position: static;
  }

  .title-panel,
  .quality-callout,
  .source-header,
  .source-item {
    flex-direction: column;
  }

  .artifact-tabs {
    overflow-x: auto;
  }

  .source-item {
    display: flex;
  }
}
</style>
