<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">← 返回</el-button>
        <span class="report-title">竞品分析报告</span>
      </div>
      <div class="header-actions">
        <el-button @click="copyReport">📋 复制</el-button>
        <el-button @click="exportReport">📥 导出</el-button>
        <el-button @click="shareReport">🔗 分享</el-button>
      </div>
    </header>

    <main class="layout-main">
      <div class="report-container" v-if="report">
        <div class="report-sidebar">
          <div class="report-meta card">
            <div class="grade-badge" :class="gradeClass">{{ report.grade || 'B' }}</div>
            <div class="meta-info">
              <div class="meta-score">{{ report.score || 7.2 }}/10</div>
              <div class="meta-label">质量评分</div>
            </div>
          </div>

          <div class="report-toc card">
            <div class="card-header">📑 目录</div>
            <div class="toc-list">
              <a v-for="item in toc" :key="item.id" :href="'#' + item.id" class="toc-item">
                {{ item.text }}
              </a>
            </div>
          </div>

          <div class="report-missing card" v-if="missingDimensions.length">
            <div class="card-header">⚠️ 缺失维度</div>
            <div class="missing-tags">
              <el-tag v-for="dim in missingDimensions" :key="dim" type="warning">{{ dim }}</el-tag>
            </div>
          </div>

          <div class="report-actions">
            <el-button type="primary" @click="$router.push(`/followup/${taskId}`)">💬 追问</el-button>
            <el-button @click="$router.push(`/review/${taskId}`)">🔍 多角色审查</el-button>
            <el-button @click="$router.push('/sync')">📤 同步</el-button>
          </div>
        </div>

        <div class="report-content">
          <div class="report-header card">
            <h1 class="report-title-main">{{ report.title || '竞品分析报告' }}</h1>
            <div class="report-info">
              <span>生成时间: {{ report.created_at || '未知' }}</span>
              <span v-if="report.quality">数据质量: {{ report.quality }}</span>
            </div>
          </div>

          <div class="report-body card">
            <div class="markdown-content" v-html="renderedMarkdown"></div>
          </div>

          <div class="report-chart card" v-if="chartData">
            <div class="card-header">📊 功能对比雷达图</div>
            <div ref="chartRef" class="radar-chart"></div>
          </div>
        </div>
      </div>

      <div v-else class="loading-state">
        <el-skeleton :rows="10" animated />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { RadarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, RadarComponent, TooltipComponent } from 'echarts/components'
import { init, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const router = useRouter()
const store = useResearchStore()
use([RadarChart, RadarComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const report = ref(null)
const taskId = route.params.id
const chartRef = ref(null)
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
let chartInstance = null

const gradeClass = computed(() => `grade-${(report.value?.grade || 'B').toLowerCase()}`)

const toc = computed(() => {
  if (!report.value?.markdown) return []
  const headings = []
  const regex = /^(#{1,3})\s+(.+)/gm
  let match
  while ((match = regex.exec(report.value.markdown)) !== null) {
    const level = match[1].length
    const text = match[2]
    const id = text.toLowerCase().replace(/[^\w]/g, '-')
    headings.push({ id, text, level })
  }
  return headings
})

const renderedMarkdown = computed(() => {
  return report.value?.markdown ? DOMPurify.sanitize(md.render(report.value.markdown)) : ''
})

const missingDimensions = computed(() => {
  return report.value?.missing_dimensions || []
})

const chartData = computed(() => report.value?.radar_data)

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

function shareReport() {
  navigator.clipboard.writeText(window.location.href)
  ElMessage.success('链接已复制')
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
  .report-title { font-size: 16px; font-weight: 500; }
}

.header-actions {
  display: flex;
  gap: 8px;
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.report-container {
  display: flex;
  gap: 24px;
  max-width: 1400px;
  margin: 0 auto;
}

.report-sidebar {
  width: 280px;
  flex-shrink: 0;
}

.report-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;

  .meta-info {
    .meta-score { font-size: 24px; font-weight: 600; }
    .meta-label { font-size: 12px; color: var(--text-secondary); }
  }
}

.report-toc {
  margin-bottom: 16px;
}

.toc-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toc-item {
  font-size: 14px;
  color: var(--text-secondary);
  padding: 4px 0;
  &:hover { color: var(--primary); }
}

.report-missing {
  margin-bottom: 16px;

  .missing-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
}

.report-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.report-content {
  flex: 1;
  min-width: 0;
}

.report-header {
  margin-bottom: 16px;

  .report-title-main { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
  .report-info { font-size: 12px; color: var(--text-secondary); display: flex; gap: 16px; }
}

.report-body {
  margin-bottom: 16px;
}

.radar-chart {
  width: 100%;
  height: 300px;
}

.loading-state {
  max-width: 900px;
  margin: 0 auto;
}
</style>
