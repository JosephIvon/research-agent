import { defineStore } from 'pinia'
import { ref } from 'vue'
import { researchApi } from '../api'
import {
  buildTaskTimeline,
  makeResearchRunId,
  normalizeDeliverables
} from './researchTaskHelpers'

const REPORT_INDEX_KEY = 'research_report_index'
const REPORT_PREFIX = 'research_report_'
const RUN_INDEX_KEY = 'research_run_index'
const RUN_PREFIX = 'research_run_'

function storage() {
  return typeof window === 'undefined' ? null : window.localStorage
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

function cachedRuns() {
  const ids = readJson(RUN_INDEX_KEY, [])
  return ids
    .map(id => readJson(`${RUN_PREFIX}${id}`, null))
    .filter(Boolean)
}

function createRunEvent(stage, type, message) {
  return {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    stage,
    type,
    message,
    time: new Date().toLocaleTimeString()
  }
}

// Backend SSE event stage → frontend timeline stage mapping
const STAGE_MAP = {
  decompose: 'understand',
  search: 'search',
  crawl_start: 'crawl',
  crawl_progress: 'crawl',
  crawl_complete: 'extract',
  extract: 'extract',
  verify: null,          // no frontend step
  report_generate: 'report',
  prd_generate: 'prd',
  completed: 'finish',
  artifact_ready: null,  // handled via report_generate / prd_generate
  error: 'finish'
}

function mapBackendStage(backendStage) {
  return STAGE_MAP[backendStage] || null
}

function getEventType(backendEvent, status) {
  if (status === 'failed' || status === 'error') return 'error'
  if (status === 'completed' || backendEvent === 'completed') return 'success'
  if (status === 'running') return 'running'
  return 'running'
}

function connectTaskSSE(runId, taskId, callbacks = {}) {
  const eventSource = researchApi.getTaskEvents(taskId)
  let reportId = null
  let prdContent = null

  function handleMessage(event) {
    try {
      const data = JSON.parse(event.data)
      const { event: backendEvent, stage, status, message, payload } = data

      const frontendStage = mapBackendStage(backendEvent)
      if (!frontendStage) return

      const eventType = getEventType(backendEvent, status)
      emitRunEvent(runId, frontendStage, eventType, message, callbacks)

      if (backendEvent === 'artifact_ready' && payload) {
        if (payload.artifact_type === 'report' && payload.report_id) {
          reportId = payload.report_id
        }
        if (payload.artifact_type === 'prd' && payload.content) {
          prdContent = payload.content
        }
      }

      if (backendEvent === 'completed' || status === 'failed') {
        cleanup()
        finalizeRun(runId, reportId, prdContent, status === 'failed' ? 'failed' : 'completed', callbacks)
      }
    } catch (e) {
      // ignore parse errors
    }
  }

  function finalizeRun(runId, reportId, prdContent, status, callbacks) {
    if (reportId) {
      researchApi.getTask(taskId).then(task => {
        const artifact = task?.artifacts?.report
        if (artifact) {
          const report = {
            id: reportId,
            markdown: artifact.content || '',
            query: task.user_query,
            competitors: task.crawl_results?.competitors || [],
            quality_score: task.quality_score,
            quality_grade: task.quality_grade,
            missing_dimensions: task.missing_dimensions || [],
            created_at: artifact.created_at || new Date().toLocaleString(),
          }
          if (prdContent) {
            report.prd = prdContent
            report.prd_created_at = new Date().toISOString()
          }
          cacheReport(report)
          updateResearchRun(runId, { status: 'completed', report_id: report.id, completed_at: new Date().toISOString() })
          currentTask.value = { id: report.id, status: 'completed', result: report }
        }
      }).catch(() => {})
    } else {
      updateResearchRun(runId, { status, completed_at: new Date().toISOString() })
    }
  }

  function cleanup() {
    eventSource.close()
  }

  eventSource.addEventListener('message', (e) => handleMessage(e))
  eventSource.addEventListener('error', () => {
    cleanup()
  })

  return cleanup
}

export const useResearchStore = defineStore('research', () => {
  const currentTask = ref(null)
  const taskHistory = ref(cachedReports())
  const taskRuns = ref(cachedRuns())
  const isLoading = ref(false)

  function cacheReport(report) {
    const s = storage()
    if (!s) return

    s.setItem(`${REPORT_PREFIX}${report.id}`, JSON.stringify(report))
    const ids = readJson(REPORT_INDEX_KEY, [])
    const nextIds = [report.id, ...ids.filter(id => id !== report.id)].slice(0, 50)
    s.setItem(REPORT_INDEX_KEY, JSON.stringify(nextIds))
    taskHistory.value = [report, ...taskHistory.value.filter(item => item.id !== report.id)]
    return report
  }

  function cacheRun(run) {
    const s = storage()
    if (!s) return run

    s.setItem(`${RUN_PREFIX}${run.id}`, JSON.stringify(run))
    const ids = readJson(RUN_INDEX_KEY, [])
    const nextIds = [run.id, ...ids.filter(id => id !== run.id)].slice(0, 50)
    s.setItem(RUN_INDEX_KEY, JSON.stringify(nextIds))
    taskRuns.value = [run, ...taskRuns.value.filter(item => item.id !== run.id)]
    return run
  }

  function getResearchRun(id) {
    return readJson(`${RUN_PREFIX}${id}`, null) || taskRuns.value.find(item => item.id === id) || null
  }

  function updateResearchRun(id, patch) {
    const existing = getResearchRun(id)
    if (!existing) return null
    const next = {
      ...existing,
      ...patch,
      updated_at: new Date().toISOString()
    }
    return cacheRun(next)
  }

  function appendRunEvent(id, stage, type, message) {
    const existing = getResearchRun(id)
    if (!existing) return null
    const event = createRunEvent(stage, type, message)
    const events = [...(existing.events || []), event].slice(-80)
    const next = updateResearchRun(id, { current_stage: stage, events })
    return { event, run: next }
  }

  function createResearchRun(taskParams, options = {}) {
    const deliverables = normalizeDeliverables(options.deliverables)
    const run = {
      id: makeResearchRunId(),
      status: 'pending',
      params: taskParams,
      deliverables,
      timeline: buildTaskTimeline({ params: taskParams, deliverables }),
      events: [],
      report_id: null,
      error: null,
      task_id: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    }
    return cacheRun(run)
  }

  async function createAsyncTask(taskParams, options = {}) {
    const deliverables = normalizeDeliverables(options.deliverables)
    const run = createResearchRun(taskParams, { deliverables })

    const result = await researchApi.createTask({
      ...taskParams,
      deliverables
    })

    updateResearchRun(run.id, { task_id: result.task_id, backend_status: 'pending' })
    return run
  }

  function emitRunEvent(id, stage, type, message, callbacks = {}) {
    const payload = appendRunEvent(id, stage, type, message)
    if (payload?.event && callbacks.onEvent) {
      callbacks.onEvent(payload.event, payload.run)
    }
    return payload?.run
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

  async function runResearchRun(id, callbacks = {}) {
    const run = getResearchRun(id)
    if (!run) {
      throw new Error('任务不存在')
    }

    if (run.task_id) {
      isLoading.value = true
      updateResearchRun(id, {
        status: 'running',
        error: null,
        started_at: run.started_at || new Date().toISOString()
      })

      connectTaskSSE(id, run.task_id, callbacks)
      return { sseConnected: true }
    }

    isLoading.value = true
    updateResearchRun(id, {
      status: 'running',
      error: null,
      started_at: run.started_at || new Date().toISOString()
    })

    try {
      emitRunEvent(id, 'understand', 'running', '正在理解你的调研目标，并拆解要比较的关键维度。', callbacks)
      emitRunEvent(id, 'scope', 'running', '正在确认竞品范围、登录配置和自动搜索策略。', callbacks)
      if (run.params?.enable_search !== false) {
        emitRunEvent(id, 'search', 'running', '正在检索公开资料，补充定价、文档和评价来源。', callbacks)
      }
      emitRunEvent(id, 'crawl', 'running', '正在登录并抓取竞品页面，可能需要等待网站响应。', callbacks)

      const result = await researchApi.competitive(run.params)
      let report = normalizeReport(result, run.params)
      report.run_id = id
      cacheReport(report)

      const successCount = Array.isArray(result.competitors)
        ? result.competitors.filter(item => item.status === 'success').length
        : 0
      emitRunEvent(id, 'extract', 'success', `已整理 ${successCount} 个可用竞品来源，正在沉淀关键维度。`, callbacks)
      emitRunEvent(id, 'report', result.status === 'success' ? 'success' : 'warning', result.status === 'success'
        ? '竞品分析报告已生成。'
        : '已生成数据质量提示，需要补充资料后报告会更完整。', callbacks)

      if (run.deliverables?.prd) {
        if (result.status === 'success' && report.markdown) {
          emitRunEvent(id, 'prd', 'running', '正在把竞品洞察转成产品需求文档。', callbacks)
          const prdResult = await researchApi.prd({
            report_content: report.markdown,
            query: report.query || run.params.query || '竞品分析'
          })
          report = {
            ...report,
            prd: prdResult.prd || prdResult.content || '',
            prd_created_at: new Date().toISOString()
          }
          cacheReport(report)
          emitRunEvent(id, 'prd', 'success', 'PRD 已生成，并已归档到结果中心。', callbacks)
        } else {
          emitRunEvent(id, 'prd', 'warning', '当前数据质量不足，PRD 已暂缓生成。补充竞品资料后可重新生成。', callbacks)
        }
      }

      emitRunEvent(id, 'finish', 'success', '交付物已整理完成，正在进入结果中心。', callbacks)
      const finalRun = updateResearchRun(id, {
        status: result.status === 'success' ? 'completed' : 'needs_input',
        report_id: report.id,
        completed_at: new Date().toISOString()
      })
      currentTask.value = {
        id: report.id,
        status: finalRun.status,
        result: report,
        createdAt: new Date(run.created_at)
      }
      return finalRun
    } catch (error) {
      emitRunEvent(id, 'finish', 'error', '任务执行失败，请检查设置或稍后重试。', callbacks)
      updateResearchRun(id, {
        status: 'failed',
        error: error?.message || '任务执行失败',
        completed_at: new Date().toISOString()
      })
      throw error
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

  function updateReport(id, patch) {
    const existing = readJson(`${REPORT_PREFIX}${id}`, null)
    if (!existing) return null
    return cacheReport({
      ...existing,
      ...patch,
      updated_at: new Date().toISOString()
    })
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
    taskRuns,
    isLoading,
    createTask,
    createResearchRun,
    createAsyncTask,
    getResearchRun,
    updateResearchRun,
    runResearchRun,
    getReport,
    updateReport,
    updateTaskStatus,
    fetchHistory,
    deleteReport
  }
})
