<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <span class="report-title">竞品分析报告</span>
      </div>
      <div class="header-actions">
        <el-button @click="copyReport">
          <el-icon><DocumentCopy /></el-icon>
          复制
        </el-button>
        <el-button @click="exportReport">
          <el-icon><Download /></el-icon>
          导出
        </el-button>
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
                <dd>{{ report.competitors || 0 }} 个</dd>
              </div>
              <div>
                <dt>自动评级</dt>
                <dd>{{ gradeDisplay }} 级</dd>
              </div>
            </dl>
          </section>

          <section class="side-panel" v-if="missingDimensions.length">
            <h2>缺失维度</h2>
            <div class="missing-tags">
              <el-tag v-for="dim in missingDimensions" :key="dim" type="warning">{{ dim }}</el-tag>
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
            <el-button @click="$router.push('/sync')">
              <el-icon><Upload /></el-icon>
              同步
            </el-button>
          </section>
        </aside>

        <section class="report-content">
          <div class="title-panel">
            <div>
              <div class="section-kicker">调研主题</div>
              <h1>{{ displayTitle }}</h1>
            </div>
            <el-tag :type="qualityTagType" effect="light">{{ qualityTagText }}</el-tag>
          </div>

          <div v-if="isLowQuality" class="quality-callout">
            <strong>当前报告数据不足</strong>
            <span>建议补充明确竞品 URL，若网站需要登录，请在首页为对应网站单独填写登录凭据后重新调研。</span>
          </div>

          <article class="report-body">
            <div class="markdown-content" v-html="renderedMarkdown"></div>
          </article>

          <section class="chart-panel" v-if="chartData">
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
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft,
  ChatDotRound,
  DocumentCopy,
  Download,
  Upload,
  View
} from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { RadarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, RadarComponent, TooltipComponent } from 'echarts/components'
import { init, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const store = useResearchStore()
use([RadarChart, RadarComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const report = ref(null)
const taskId = route.params.id
const chartRef = ref(null)
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
let chartInstance = null

const numericScore = computed(() => {
  const value = Number(report.value?.score)
  return Number.isFinite(value) ? Math.max(0, Math.min(10, value)) : null
})

const scoreDisplay = computed(() => numericScore.value === null ? '暂无' : numericScore.value.toFixed(1))
const scorePercent = computed(() => numericScore.value === null ? 0 : Math.round(numericScore.value * 10))
const gradeDisplay = computed(() => {
  if (numericScore.value !== null) return gradeFromScore(numericScore.value)
  return report.value?.grade || 'B'
})
const gradeClass = computed(() => `grade-${gradeDisplay.value.toLowerCase()}`)
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

const missingDimensions = computed(() => report.value?.missing_dimensions || [])
const chartData = computed(() => report.value?.radar_data)

function gradeFromScore(score) {
  if (score >= 8.5) return 'A'
  if (score >= 7) return 'B'
  if (score >= 5.5) return 'C'
  if (score >= 3) return 'D'
  return 'F'
}

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
  navigator.clipboard.writeText(report.value?.markdown || '')
  ElMessage.success('已复制到剪贴板')
}

function exportReport() {
  const content = report.value?.markdown || ''
  const blob = new Blob([content], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'report.md'
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(() => {
  loadReport()
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

.report-body {
  min-height: 360px;
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
  .quality-callout {
    flex-direction: column;
  }
}
</style>
