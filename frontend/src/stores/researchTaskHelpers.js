export const DEFAULT_DELIVERABLES = {
  report: true,
  prd: true
}

export function normalizeDeliverables(deliverables = {}) {
  return {
    report: deliverables.report !== false,
    prd: deliverables.prd !== false
  }
}

export function getTaskInputSummary(params = {}) {
  const targetSites = Array.isArray(params.target_sites) ? params.target_sites : []
  const legacyUrls = Array.isArray(params.urls) ? params.urls : []
  const siteCount = targetSites.length || legacyUrls.length
  const loginCount = targetSites.filter(site => Boolean(site?.auth_credentials)).length

  return {
    siteCount,
    loginCount,
    autoSearchText: params.enable_search === false ? '关闭' : '开启',
    passwordsVisible: false
  }
}

export function getTaskArtifactContent(task = {}, key, fallback = '') {
  const content = task?.artifacts?.[key]?.content
  return typeof content === 'string' && content ? content : fallback
}

// --------------------------------------------------------------------------
// 任务终态（M6 PARTIAL）
//
// 这些原本内联在 ProgressView.vue 的 computed 里——于是「partial 忘了加，
// 标签静默回退成『准备中』」这种 bug 根本测不到：Vue 单测在这套仓库里没有
// 位置。挪成纯函数后才能被 node --test 覆盖。
// --------------------------------------------------------------------------

// 后端任务终态。partial = 预算耗尽收束：报告已产出但不完整。
// 任何"任务是否已经跑完"的判断都必须走这个集合——漏掉 partial 的后果不是
// 判断错，而是永远等不到：在取报告的轮询里它会白转 8 轮 ×400ms。
export const TERMINAL_RUN_STATUSES = Object.freeze(['completed', 'partial', 'failed'])

export function isTerminalRunStatus(status) {
  return TERMINAL_RUN_STATUSES.includes(status)
}

export function getRunStatusLabel(status) {
  return {
    pending: '等待开始',
    running: '执行中',
    completed: '已完成',
    // partial 不写「已完成」：预算收束的报告是半截的，标签必须说真话
    partial: '已交付 · 不完整',
    needs_input: '需要补充资料',
    failed: '执行失败'
  }[status] || '准备中'
}

export function getRunStatusTagType(status) {
  return {
    completed: 'success',
    partial: 'warning',
    needs_input: 'warning',
    failed: 'danger'
  }[status] || 'primary'
}

export function getRunStatusKicker(status) {
  return {
    completed: '已完成',
    partial: '预算收束',
    needs_input: '需要补充资料',
    failed: '执行失败'
  }[status] || '正在处理'
}

export function getRunHeadline(run = {}) {
  const status = run?.status
  if (status === 'completed') return '报告和交付物已整理完成。'
  if (status === 'partial') {
    return '任务触发了预算上限并提前收束。报告已交付，但可能未覆盖全部竞品或维度——建议提高预算后重跑。'
  }
  if (status === 'needs_input') return '已生成质量提示，建议补充竞品资料后继续。'
  if (status === 'failed') return run.error || '任务执行失败。'
  return '你可以先离开这个页面，但刷新可能会重新执行当前任务。'
}

/**
 * 把后端任务/报告载荷里的可观测元数据并到报告对象上。
 *
 * 四个字段（trace_url / injection_findings_count / cost_report / incomplete）都只
 * 在后端有权威来源，前端缓存只是副本。任何新增字段都必须同时落在这里，
 * 否则冷加载（换浏览器、清缓存）就会把它整个丢掉——成本卡和"内容不完整"
 * 标记都栽过这一回。
 */
export function mergeReportMeta(target, payload = {}) {
  if (payload.trace_url) target.trace_url = payload.trace_url
  if (Number.isFinite(Number(payload.injection_findings_count))) {
    target.injection_findings_count = Number(payload.injection_findings_count)
  }
  if (payload.cost_report && typeof payload.cost_report === 'object') {
    target.cost_report = payload.cost_report
  }
  // 缺失时保持 false 而不是 undefined——否则"后端没给这个字段"与"不是 partial"
  // 在模板里分不出来。显式 false 才是可依赖的默认值。
  if (payload.incomplete !== undefined) {
    target.incomplete = Boolean(payload.incomplete)
  }
  return target
}

export function getReportCompetitors(report = {}) {
  if (Array.isArray(report?.raw?.competitors)) return report.raw.competitors
  if (Array.isArray(report?.raw?.crawl_results?.competitors)) return report.raw.crawl_results.competitors
  if (Array.isArray(report?.competitor_details)) return report.competitor_details
  if (Array.isArray(report?.competitors)) return report.competitors
  return []
}

export function getReportCompetitorCount(report = {}) {
  const competitors = getReportCompetitors(report)
  if (competitors.length) return competitors.length
  return Number.isFinite(Number(report?.competitors)) ? Number(report.competitors) : 0
}

export function buildSourceRows(report = {}) {
  return getReportCompetitors(report)
    .map((item, index) => {
      const rawPages = Array.isArray(item.raw_pages) ? item.raw_pages : []
      const discoveredUrls = Array.isArray(item.discovered_urls) ? item.discovered_urls : []
      return {
        name: item.name || `竞品 ${index + 1}`,
        url: item.url,
        status: item.status || 'failed',
        dataCount: Array.isArray(item.extracted_data) ? item.extracted_data.length : 0,
        pageCount: rawPages.length || (item.page_count || 0),
        pageUrls: rawPages.map(page => page.url).filter(Boolean),
        discoveredUrls,
        error: item.error_message || item.error || ''
      }
    })
    .filter(item => item.url)
}

export function buildTaskTimeline({ params = {}, deliverables = {} } = {}) {
  const normalizedDeliverables = normalizeDeliverables(deliverables)
  const summary = getTaskInputSummary(params)
  const steps = [
    {
      id: 'understand',
      title: '理解调研目标',
      detail: '拆解问题、识别要比较的产品维度'
    },
    {
      id: 'scope',
      title: '确认竞品范围',
      detail: summary.siteCount
        ? `已指定 ${summary.siteCount} 个竞品网站`
        : '未指定网站，将依靠自动搜索补充竞品'
    }
  ]

  if (params.enable_search !== false) {
    steps.push({
      id: 'search',
      title: '搜索公开资料',
      detail: summary.siteCount ? '补充定价、文档、公开评价等外部信息' : '搜索候选竞品和公开资料'
    })
  }

  steps.push(
    {
      id: 'crawl',
      title: '登录和抓取网站',
      detail: `${summary.siteCount} 个指定网站，其中 ${summary.loginCount} 个需要登录`
    },
    {
      id: 'extract',
      title: '提取关键信息',
      detail: '整理功能、模型、定价、集成、目标用户和限制'
    }
  )

  if (normalizedDeliverables.report) {
    steps.push({
      id: 'report',
      title: '生成竞品报告',
      detail: '输出对比结论、机会点和数据质量提示'
    })
  }

  if (normalizedDeliverables.prd) {
    steps.push({
      id: 'prd',
      title: '生成 PRD',
      detail: '把竞品洞察转成产品需求文档'
    })
  }

  steps.push({
    id: 'finish',
    title: '整理交付物',
    detail: '准备报告、PRD、来源状态和下一步建议'
  })

  return steps
}

export function makeResearchRunId() {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `run_${random}`
}

export const AGENT_STEP_LIMIT = 50
export const AGENT_THOUGHT_TRUNCATE = 60

export function normalizeAgentStep(payload = {}) {
  return {
    step: payload.step,
    thought: payload.thought ?? null,
    tool_calls: Array.isArray(payload.tool_calls) ? payload.tool_calls : [],
    stop_reason: payload.stop_reason ?? null
  }
}

export function appendAgentStep(steps = [], payload) {
  return [...steps, normalizeAgentStep(payload)].slice(-AGENT_STEP_LIMIT)
}

export function buildAgentStepMessage(payload = {}) {
  const tools = (Array.isArray(payload.tool_calls) ? payload.tool_calls : [])
    .map(tool => tool?.name)
    .filter(Boolean)
    .join('、')
  return `第${payload.step}轮：${tools || '思考'}`
}

function summarizeArguments(toolCall = {}) {
  try {
    return JSON.stringify(toolCall.arguments ?? {}) ?? ''
  } catch (e) {
    return ''
  }
}

export function formatAgentStepSummary(step = {}) {
  const tools = (Array.isArray(step.tool_calls) ? step.tool_calls : [])
    .map(tool => `${tool?.name || '未知工具'}（${summarizeArguments(tool).slice(0, 40)}）`)
    .join('、')
  const thought = String(step.thought || '').trim().slice(0, AGENT_THOUGHT_TRUNCATE)
  return [`第${step.step}轮`, tools || '思考', thought].filter(Boolean).join(' · ')
}

// --------------------------------------------------------------------------
// SSE 事件 → 时间轴阶段
//
// 这张表此前内联在 research.js 里，而 research.js 是 Pinia store——node --test
// 碰不到它。后果很具体：citation_check 在后端发了 4 次，前端压根没订阅；
// 而就算订阅了、忘了在这里配 stage，事件也会被 `if (!frontendStage) return`
// 静默丢掉，界面上什么都不会发生，也没有任何报错。
// --------------------------------------------------------------------------

/** 后端会发、且前端**必须**订阅的事件名。与 server.py 的 TaskEventType 对应。 */
export const BACKEND_EVENTS = Object.freeze([
  'task_created', 'decompose', 'search', 'crawl_start',
  'crawl_progress', 'agent_step', 'crawl_complete', 'extract', 'verify',
  'report_generate', 'citation_check', 'prd_generate', 'artifact_ready',
  'injection_detected', 'partial', 'completed', 'error'
])

export const STAGE_MAP = Object.freeze({
  decompose: 'understand',
  search: 'search',
  crawl_start: 'crawl',
  crawl_progress: 'crawl',
  agent_step: 'crawl',
  crawl_complete: 'extract',
  extract: 'extract',
  verify: null,          // no frontend step
  report_generate: 'report',
  // 引用校验闭环（M2）：开始 / 未通过待重生成 / 通过 / 终判未过。
  // 落在 report 这一步而不是独立成步——它在时间轴上就发生在「生成竞品报告」
  // 期间，单开一步会让进度条凭空多出一段用户看不懂的工序。
  // 事件是追加进 run.events 的（上限 80 条），同一阶段多条不会互相覆盖。
  citation_check: 'report',
  prd_generate: 'prd',
  completed: 'finish',
  // 阶段取事件自带的 stage（crawl 或 report_generate），见 resolveFrontendStage
  injection_detected: null,
  partial: 'finish',
  artifact_ready: null,  // handled via report_generate / prd_generate
  error: 'finish'
})

export function mapBackendStage(backendEvent) {
  return STAGE_MAP[backendEvent] || null
}

/**
 * 后端 `stage` 字段 → 时间轴阶段。
 *
 * 这是**另一套词表**，不能拿 STAGE_MAP 顶替：STAGE_MAP 的键是事件名
 * （crawl_start / crawl_progress / agent_step → 'crawl'），而后端 stage 字段
 * 直接就是 'crawl'。拿事件名的表去查 stage 值，只会得到 undefined，然后事件
 * 被 `if (!frontendStage) return` 静默丢掉——这正是 injection_detected 的时间轴
 * 条目一直没出现的原因（提示框是对的，因为数据在早退之前就收集了）。
 */
export const BACKEND_STAGE_MAP = Object.freeze({
  crawl: 'crawl',
  crawl_start: 'crawl',
  crawl_progress: 'crawl',
  crawl_complete: 'extract',
  extract: 'extract',
  report_generate: 'report',
  completed: 'finish'
})

export function mapBackendStageField(backendStage) {
  return BACKEND_STAGE_MAP[backendStage] || null
}

/**
 * 解析事件该落在时间轴的哪一步。
 *
 * injection_detected 特判：它在爬取和成文两个阶段都会发，阶段得看事件自己带的
 * stage，而不是硬编码成一个值——否则成文阶段发现的注入会被记到"抓取"那一步上。
 * 返回 null 表示这条事件不进时间轴（调用方仍应先处理它的 payload）。
 */
export function resolveFrontendStage(backendEvent, eventStage) {
  const mapped = mapBackendStage(backendEvent)
  if (mapped) return mapped
  if (backendEvent === 'injection_detected') return mapBackendStageField(eventStage)
  return null
}

export function getEventType(backendEvent, status) {
  if (status === 'failed' || status === 'error') return 'error'
  // partial 走 success：报告确实产出了，只是被预算截断。不完整的细节由 run.status 承载。
  if (status === 'completed' || backendEvent === 'completed' || backendEvent === 'partial') return 'success'
  return 'running'
}
