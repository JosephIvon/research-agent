import { defineStore } from 'pinia'
import { ref } from 'vue'
import { researchApi } from '../api'
import {
  buildTaskTimeline,
  getTaskArtifactContent,
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

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function fetchTaskUntilArtifactsReady(taskId, deliverables = {}, fallbackReportId = null) {
  const expected = normalizeDeliverables(deliverables)
  let lastTask = null

  for (let attempt = 0; attempt < 8; attempt += 1) {
    lastTask = await researchApi.getTask(taskId)
    if (lastTask?.status === 'failed' || lastTask?.status === 'completed') {
      const hasReport = expected.report === false ||
        Boolean(getTaskArtifactContent(lastTask, 'report') || lastTask?.report_id || fallbackReportId)
      const hasPrd = expected.prd === false || Boolean(getTaskArtifactContent(lastTask, 'prd'))
      if (hasReport && (expected.prd === false || hasPrd)) {
        return lastTask
      }
    }

    await wait(400)
  }

  return lastTask
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

function connectTaskSSE(runId, taskId, callbacks = {}, handlers = {}) {
  const eventSource = researchApi.getTaskEvents(taskId)
  let reportId = null
  let prdContent = null

  function handleMessage(event) {
    try {
      const data = JSON.parse(event.data)
      const { event: backendEvent, stage, status, message, payload } = data

      // Capture artifact data before stage mapping check
      if (backendEvent === 'artifact_ready' && payload) {
        if (payload.artifact_type === 'report' && payload.report_id) {
          reportId = payload.report_id
        }
        if (payload.artifact_type === 'prd' && payload.content) {
          prdContent = payload.content
        }
      }

      const frontendStage = mapBackendStage(backendEvent)
      if (!frontendStage) return

      const eventType = getEventType(backendEvent, status)
      emitRunEvent(runId, frontendStage, eventType, message, callbacks)

      if (backendEvent === 'completed' || status === 'failed') {
        cleanup()
        handlers.onComplete?.({
          runId,
          taskId,
          reportId,
          prdContent,
          status: status === 'failed' ? 'failed' : 'completed',
          callbacks
        })
      }
    } catch (e) {
      // ignore parse errors
    }
  }

  const BACKEND_EVENTS = [
    'task_created', 'decompose', 'search', 'crawl_start',
    'crawl_progress', 'crawl_complete', 'extract', 'verify',
    'report_generate', 'prd_generate', 'artifact_ready',
    'completed', 'error'
  ]

  function cleanup() {
    eventSource.close()
  }

  for (const evt of BACKEND_EVENTS) {
    eventSource.addEventListener(evt, (e) => handleMessage(e))
  }
  eventSource.addEventListener('error', (event) => {
    cleanup()
    handlers.onError?.({ runId, taskId, event, callbacks })
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

  async function finalizeAsyncRun({ runId, taskId, reportId, prdContent, status, callbacks }) {
    const run = getResearchRun(runId)
    const finalStatus = status === 'failed' ? 'failed' : 'completed'

    if (finalStatus === 'failed') {
      const failedRun = updateResearchRun(runId, {
        status: 'failed',
        error: '调研任务执行失败，请检查输入或稍后重试。',
        completed_at: new Date().toISOString()
      })
      callbacks.onError?.(failedRun)
      return failedRun
    }

    try {
      const task = await fetchTaskUntilArtifactsReady(taskId, run?.deliverables || {}, reportId)
      if (task?.status === 'failed') {
        const failedRun = updateResearchRun(runId, {
          status: 'failed',
          error: task.current_message || '调研任务执行失败，请检查输入或稍后重试。',
          completed_at: new Date().toISOString()
        })
        callbacks.onError?.(failedRun)
        return failedRun
      }

      const reportArtifact = task?.artifacts?.report
      const prdArtifact = task?.artifacts?.prd
      const resolvedReportId = reportId || task?.report_id || reportArtifact?.report_id || `partial_${taskId}`
      const reportMarkdown = getTaskArtifactContent(task, 'report')
      const fullPrdContent = getTaskArtifactContent(task, 'prd', prdContent || '')

      if (!resolvedReportId || !reportMarkdown) {
        const failedRun = updateResearchRun(runId, {
          status: 'failed',
          error: '报告已生成，但前端未能取回完整结果，请刷新后重试。',
          completed_at: new Date().toISOString()
        })
        callbacks.onError?.(failedRun)
        return failedRun
      }

      const crawlCompetitors = Array.isArray(task?.crawl_results?.competitors)
        ? task.crawl_results.competitors
        : []

      const report = {
        id: resolvedReportId,
        title: firstHeading(reportMarkdown),
        markdown: reportMarkdown,
        query: task?.user_query || run?.params?.query || '',
        competitors: crawlCompetitors.length,
        score: task?.quality_score ?? null,
        grade: task?.quality_grade || 'B',
        quality_score: task?.quality_score,
        quality_grade: task?.quality_grade,
        missing_dimensions: task?.missing_dimensions || [],
        created_at: reportArtifact?.created_at || new Date().toLocaleString(),
        raw: {
          ...task,
          competitors: crawlCompetitors
        }
      }

      if (fullPrdContent) {
        report.prd = fullPrdContent
        report.prd_created_at = prdArtifact?.created_at || new Date().toISOString()
      }

      cacheReport(report)
      const finalRun = updateResearchRun(runId, {
        status: 'completed',
        report_id: report.id,
        completed_at: new Date().toISOString()
      })
      currentTask.value = { id: report.id, status: 'completed', result: report }
      callbacks.onComplete?.(report, finalRun)
      return finalRun
    } catch (error) {
      const failedRun = updateResearchRun(runId, {
        status: 'failed',
        error: error?.message || '报告结果获取失败，请稍后重试。',
        completed_at: new Date().toISOString()
      })
      callbacks.onError?.(failedRun)
      return failedRun
    }
  }

  function handleTaskStreamError(id, event, callbacks = {}) {
    const status = event?.status
    const isAuthError = status === 401
    const message = isAuthError
      ? 'API Token 认证失败，请在设置页重新填写后重试。'
      : '实时进度连接中断，请稍后重试。'

    const failedRun = updateResearchRun(id, {
      status: 'failed',
      error: message,
      completed_at: new Date().toISOString()
    })
    callbacks.onError?.(failedRun)

    if (isAuthError && typeof window !== 'undefined' && window.location.pathname !== '/settings') {
      window.location.assign('/settings')
    }
    return failedRun
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

  async function runResearchRun(id, callbacks = {}) {
    const run = getResearchRun(id)
    if (!run) {
      throw new Error('任务不存在')
    }

    if (!run.task_id) {
      throw new Error('任务缺少后端 task_id，无法连接 SSE。请通过 createAsyncTask 创建任务。')
    }

    isLoading.value = true
    updateResearchRun(id, {
      status: 'running',
      error: null,
      started_at: run.started_at || new Date().toISOString()
    })

    connectTaskSSE(id, run.task_id, callbacks, {
      onComplete: finalizeAsyncRun,
      onError: ({ runId, event, callbacks }) => handleTaskStreamError(runId, event, callbacks)
    })
    return { sseConnected: true }
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
