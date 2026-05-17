import { defineStore } from 'pinia'
import { ref } from 'vue'
import { researchApi } from '../api'

const REPORT_INDEX_KEY = 'research_report_index'
const REPORT_PREFIX = 'research_report_'

function storage() {
  return typeof window === 'undefined' ? null : window.sessionStorage
}

function readJson(key, fallback) {
  try {
    const raw = storage()?.getItem(key)
    return raw ? JSON.parse(raw) : fallback
  } catch (e) {
    return fallback
  }
}

function firstHeading(markdown) {
  const line = (markdown || '').split('\n').find(item => item.trim())
  return line?.replace(/^#+\s*/, '').slice(0, 120) || '竞品分析报告'
}

function normalizeReport(payload, request = {}) {
  const markdown = payload.markdown || payload.report || payload.competitive_report || ''
  const id = String(payload.task_id || payload.id || Date.now())
  const competitors = Array.isArray(payload.competitors)
    ? payload.competitors.length
    : (payload.competitors ?? 0)

  return {
    id,
    title: payload.title || payload.query || request.query || firstHeading(markdown),
    query: payload.query || request.query || payload.title || '',
    markdown,
    score: payload.quality_score ?? payload.score ?? null,
    grade: payload.quality_grade || payload.grade || 'B',
    competitors,
    missing_dimensions: payload.missing_dimensions || [],
    created_at: payload.created_at || new Date().toLocaleString(),
    output_files: payload.output_files || {},
    raw: payload
  }
}

function cachedReports() {
  const ids = readJson(REPORT_INDEX_KEY, [])
  return ids
    .map(id => readJson(`${REPORT_PREFIX}${id}`, null))
    .filter(Boolean)
}

export const useResearchStore = defineStore('research', () => {
  const currentTask = ref(null)
  const taskHistory = ref(cachedReports())
  const isLoading = ref(false)

  function cacheReport(report) {
    const s = storage()
    if (!s) return

    s.setItem(`${REPORT_PREFIX}${report.id}`, JSON.stringify(report))
    const ids = readJson(REPORT_INDEX_KEY, [])
    const nextIds = [report.id, ...ids.filter(id => id !== report.id)].slice(0, 50)
    s.setItem(REPORT_INDEX_KEY, JSON.stringify(nextIds))
    taskHistory.value = [report, ...taskHistory.value.filter(item => item.id !== report.id)]
  }

  async function createTask(taskParams) {
    isLoading.value = true
    try {
      const result = await researchApi.competitive(taskParams)
      const report = normalizeReport(result, taskParams)
      currentTask.value = {
        id: report.id,
        status: result.status === 'success' ? 'completed' : 'failed',
        result: report,
        createdAt: new Date()
      }
      cacheReport(report)
      return currentTask.value
    } finally {
      isLoading.value = false
    }
  }

  async function getReport(id) {
    const cached = readJson(`${REPORT_PREFIX}${id}`, null)
    if (cached) return cached

    const result = await researchApi.getReport(id)
    const report = normalizeReport(result)
    cacheReport(report)
    return report
  }

  function updateTaskStatus(status, result) {
    if (currentTask.value) {
      currentTask.value.status = status
      currentTask.value.result = result
    }
  }

  async function fetchHistory() {
    const remoteHistory = await researchApi.history()
    const remoteReports = (remoteHistory || []).map(item => normalizeReport(item))
    const merged = [...cachedReports(), ...remoteReports]
    const seen = new Set()
    taskHistory.value = merged.filter(item => {
      if (seen.has(item.id)) return false
      seen.add(item.id)
      return true
    })
    return taskHistory.value
  }

  async function deleteReport(id) {
    if (id.startsWith('research_report_')) {
      await researchApi.deleteReport(id)
    }

    const s = storage()
    if (s) {
      s.removeItem(`${REPORT_PREFIX}${id}`)
      const ids = readJson(REPORT_INDEX_KEY, []).filter(item => item !== id)
      s.setItem(REPORT_INDEX_KEY, JSON.stringify(ids))
    }
    taskHistory.value = taskHistory.value.filter(item => item.id !== id)
  }

  return {
    currentTask,
    taskHistory,
    isLoading,
    createTask,
    getReport,
    updateTaskStatus,
    fetchHistory,
    deleteReport
  }
})
