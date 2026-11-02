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

// SSE 流是否已到终态；返回应上报的终态名，未到则返回 null。
//
// **终态只看事件名，不看 status。** 后端的 `status` 是**阶段进度**而不是终态标志：
// crawl_complete / citation_check / artifact_ready / injection_detected 全都发
// status="completed"，那是"这一步做完了"；把它们当终态会让 SSE 在第一次
// crawl_complete 就被拆掉，报告再也取不回来。
//
// `error` 要额外排除 crawl 阶段：单个竞品抓取失败时后端同样发
// error + stage="crawl" + status="failed"（mvp_workflow._fetch_single_competitor），
// 任务还在继续。原判定只看 `status === 'failed'`，于是第一次抓取失败就把前端
// 拆停并报成 failed，后端照跑不误、真正的报告再也没人收。
export function resolveStreamTerminal(backendEvent, status, eventStage) {
  if (backendEvent === 'completed') return 'completed'
  if (backendEvent === 'partial') return 'partial'
  if (eventStage === 'crawl') return null
  if (backendEvent === 'error' || status === 'failed') return 'failed'
  return null
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

/**
 * 某个竞品「发现出来但没抓到」的站内页。
 *
 * 爬虫一直把这份留痕记在 `failed_pages`（url / 异常类型 / 耗时），但它此前
 * 只停在爬虫的返回值里，报告页看不到——少了几页却无从查证。
 */
export function getCompetitorFailedPages(competitor = {}) {
  const list = competitor?.failed_pages
  return Array.isArray(list) ? list : []
}

/**
 * 报告 id 必须是**报告 id**，不是任务 id。
 *
 * 历史列表的载荷同时带 id（报告）与 task_id（任务）。早前取 id 时把 task_id
 * 排在前面，于是卡片上的「查看 / 删除」拿着任务 id 去要报告——接口按报告 id
 * 查，任务 id 查不到，库里那十几份真实报告点开全是空白页。
 */
export function resolveReportId(payload = {}) {
  const raw = payload.id || payload.task_id || Date.now()
  return String(raw)
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
        failedPages: getCompetitorFailedPages(item),
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
  // 引用校验发生在「生成竞品报告」期间，与 citation_check 同落在 report 这一步
  verify: 'report',
  // ReAct 的工具调用阶段与 crawl 同段
  agent: 'crawl',
  completed: 'finish'
})

export function mapBackendStageField(backendStage) {
  return BACKEND_STAGE_MAP[backendStage] || null
}

/**
 * 解析事件该落在时间轴的哪一步。
 *
 * error 特判：失败要落在它**实际发生**的阶段。后端发 error 时自带 stage
 * （crawl / verify / agent / workflow），此前这里硬编码成 'finish'——于是
 * 「抓取失败 acme.com」被记到时间轴最后一步上，用户看到的是「完成」那一步
 * 红了，而不是「登录和抓取网站」红了。stage 认不出来时才回落到 'finish'。
 *
 * injection_detected 同样特判：它在爬取和成文两个阶段都会发，阶段得看事件
 * 自己带的 stage，而不是硬编码成一个值——否则成文阶段发现的注入会被记到
 * "抓取"那一步上。
 * 返回 null 表示这条事件不进时间轴（调用方仍应先处理它的 payload）。
 */
export function resolveFrontendStage(backendEvent, eventStage) {
  if (backendEvent === 'error') {
    return mapBackendStageField(eventStage) || 'finish'
  }
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

// --------------------------------------------------------------------------
// 研究计划 / 入选轨迹（报告页「调研口径」tab）
//
// 后端在任务终态落库了两份**结构化凭据**：
//   research_plan —— 主体是谁、竞品怎么定义、四个角度的权重与优先级
//   discovery     —— 候选池、每家的站点类型、打分与理由、入选/剔除清单
// 它们回答的是"这轮为什么选了这些竞品"。此前前端只有报告 markdown 里的一段话，
// 候选池与打分明细完全不可见——用户看不到口径，也就无从判断这轮结果可不可信。
//
// 取数与格式化全放在这里，.vue 只做渲染：字段缺失的兜底、旧报告的空态、权重
// 归一、站点类型中文名都能被 node --test 覆盖（Vue 组件在这套仓库里没有单测位置）。
// --------------------------------------------------------------------------

/** 站点类型中文名——与后端 src/workflow/site_types.py 的 SITE_TYPE_LABELS 对齐。 */
export const SITE_TYPE_LABELS = Object.freeze({
  product_site: '产品官网',
  media: '媒体/内容站',
  ugc_platform: '开发者社区/UGC 博客',
  app_store: '应用商店/下载站',
  directory: '产品收录/比价目录',
  unknown: '未知'
})

/** 主体类型中文名——与后端 research_plan.ENTITY_TYPES 对齐。 */
export const ENTITY_TYPE_LABELS = Object.freeze({
  app: '应用',
  platform: '平台',
  software: '软件',
  hardware: '硬件',
  service: '服务',
  brand: '品牌',
  company: '公司',
  organization: '组织',
  concept: '概念',
  person: '人物',
  place: '地点',
  unknown: '未知'
})

/** 分析角度中文名——与后端 research_plan.ANGLE_LABELS 对齐。 */
export const ANGLE_LABELS = Object.freeze({
  feature: '功能',
  business: '业务价值',
  engineering: '研发与实现',
  product: '产品与体验'
})

/** 竞品层级中文名——与后端 research_plan.COMPETITOR_LAYERS 对齐。 */
export const COMPETITOR_LAYER_LABELS = Object.freeze({
  direct: '直接竞品',
  alternative: '替代方案',
  adjacent: '相邻竞品'
})

/** 角度优先级中文名——与后端 research_plan.PRIORITY_MULTIPLIER 的键对齐。 */
export const PRIORITY_LABELS = Object.freeze({
  primary: '重点',
  secondary: '次要',
  ignore: '忽略'
})

const PLAN_SOURCES = Object.freeze(['model', 'default', 'fallback'])
const PRIORITY_KEYS = Object.freeze(['primary', 'secondary', 'ignore'])

/** 准入规则的解析来源中文名——与后端 admission_rules.AdmissionRule.source 对齐。 */
export const RULE_SOURCE_LABELS = Object.freeze({
  model: '按计划口径执行',
  partial: '部分口径可执行',
  unparsed: '口径无法解析，已退回默认口径',
  default: '默认口径'
})

function round4(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return 0
  return Math.round(num * 10000) / 10000
}

function asText(value) {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function firstObject(...values) {
  for (const value of values) {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value
  }
  return {}
}

function stringList(value) {
  if (!Array.isArray(value)) return []
  return value.map(item => asText(item)).filter(Boolean)
}

/** 站点类型 → 中文名。认不出来的类型一律显示"未知"，不把 slug 漏到界面上。 */
export function siteTypeLabel(siteType) {
  const key = asText(siteType)
  return SITE_TYPE_LABELS[key] || SITE_TYPE_LABELS.unknown
}

/**
 * 角度权重归一化。
 *
 * 后端给的权重本来就是"按优先级算完再归一"的和为 1 的小数，但历史计划、模型
 * 直接吐出的权重、字段缺失都可能不是 1。这里统一再归一一次：
 *   - 有正权重：按比例缩放到和为 1（比例关系不变）
 *   - 全是 0 / 全缺：按角度数均分，而不是全 0——权重条全空等于没信息
 *
 * 权重条按"相对最重的那条"画（barPercent），否则 0.35 与 0.30 在 0–1 的刻度上
 * 几乎看不出差别。
 */
export function normalizeAngleWeights(angles = []) {
  const items = (Array.isArray(angles) ? angles : []).filter(item => item && typeof item === 'object')
  const rawSum = items.reduce((acc, item) => {
    const value = Number(item.weight)
    return acc + (Number.isFinite(value) && value > 0 ? value : 0)
  }, 0)
  const each = items.length ? 1 / items.length : 0
  const normalized = items.map(item => {
    const raw = Number(item.weight)
    const weight = rawSum > 0
      ? (Number.isFinite(raw) && raw > 0 ? raw : 0) / rawSum
      : each
    return {
      ...item,
      rawWeight: Number.isFinite(raw) ? raw : 0,
      weight: round4(weight)
    }
  })
  const max = normalized.reduce((acc, item) => Math.max(acc, item.weight), 0)
  return normalized.map(item => ({
    ...item,
    percent: round4(item.weight * 100),
    barPercent: max > 0 ? Math.round((item.weight / max) * 100) : 0
  }))
}

/** 消歧候选 → 一行人能读的文本（后端已拍平过字符串，这里再兜一次对象形态）。 */
function formatDisambiguationCandidate(item) {
  if (typeof item === 'string') return item.trim()
  if (!item || typeof item !== 'object') return ''
  const name = asText(item.name || item.candidate)
  const description = asText(item.description)
  let label = name && description ? `${name}（${description}）` : (name || description)
  const confidence = Number(item.confidence ?? item.likelihood)
  if (label && Number.isFinite(confidence)) {
    label += `（置信度 ${confidence.toFixed(2)}）`
  }
  return label
}

/**
 * 把后端 research_plan 归一化成可直接渲染的结构。
 *
 * 兜底原则：**任何字段缺失都不能抛异常**。旧报告没有 research_plan、调用的
 * 任务走了降级路径、字段被裁剪过——都要退化成"空态"而不是白屏。
 */
export function normalizeResearchPlan(raw) {
  const plan = firstObject(raw)
  const entity = firstObject(plan.entity)
  // 后端字段名是 competitor_criteria；早期方案文档里写的是 criteria。两个都认，
  // 免得字段一改名就把整块口径打空。
  const criteria = firstObject(plan.competitor_criteria, plan.criteria)

  const source = PLAN_SOURCES.includes(asText(plan.source)) ? asText(plan.source) : ''
  const isDefault = source === 'default' || source === 'fallback'
  const sourceLabel = source === 'model'
    ? '模型生成'
    : (source === 'fallback' ? '默认口径（降级）' : (source === 'default' ? '默认口径' : '来源未知'))
  const sourceNote = source === 'model'
    ? '本次研究计划由模型生成，权重由代码按优先级算。'
    : (source === 'fallback'
      ? '模型生成失败，本轮已降级到默认口径（权重取默认优先级表）。'
      : '本次未生成研究计划，按系统默认口径执行。')

  const entityType = asText(entity.entity_type)
  const confidenceRaw = Number(entity.resolved_confidence)
  const confidence = Number.isFinite(confidenceRaw) ? Math.max(0, Math.min(1, confidenceRaw)) : null

  const layerKeys = Array.isArray(criteria.layers) ? criteria.layers.map(item => asText(item)).filter(Boolean) : []
  const layers = layerKeys.map(key => ({ key, label: COMPETITOR_LAYER_LABELS[key] || key }))

  const minRaw = Number(criteria.min_count)
  const maxRaw = Number(criteria.max_count)
  const minCount = Number.isFinite(minRaw) && minRaw > 0 ? minRaw : null
  const maxCount = Number.isFinite(maxRaw) && maxRaw > 0 ? maxRaw : null
  const countText = minCount !== null && maxCount !== null
    ? `${minCount}–${maxCount} 家`
    : (minCount !== null ? `至少 ${minCount} 家` : (maxCount !== null ? `最多 ${maxCount} 家` : '未限定'))

  const poolRaw = Number(criteria.candidate_pool)
  const candidatePool = Number.isFinite(poolRaw) && poolRaw > 0 ? poolRaw : null

  const rawAngles = Array.isArray(plan.angles) ? plan.angles : []
  const angleItems = rawAngles
    .filter(item => item && typeof item === 'object')
    .map((item, index) => {
      const angle = asText(item.angle)
      const priority = PRIORITY_KEYS.includes(asText(item.priority)) ? asText(item.priority) : 'secondary'
      return {
        angle,
        label: asText(item.label) || ANGLE_LABELS[angle] || (angle || `角度 ${index + 1}`),
        priority,
        priorityLabel: PRIORITY_LABELS[priority],
        reason: asText(item.reason),
        pageHints: stringList(item.page_hints),
        weight: Number(item.weight)
      }
    })
  const angles = normalizeAngleWeights(angleItems).map(item => ({
    ...item,
    weightText: item.weight.toFixed(2),
    percentText: `${item.percent.toFixed(1)}%`
  }))

  const openQuestions = (Array.isArray(plan.open_questions) ? plan.open_questions : [])
    .filter(item => item && typeof item === 'object')
    .map(item => ({ question: asText(item.question), default: asText(item.default) }))
    .filter(item => item.question)

  const disambiguationCandidates = (Array.isArray(entity.disambiguation_candidates)
    ? entity.disambiguation_candidates
    : [])
    .map(formatDisambiguationCandidate)
    .filter(Boolean)

  // 可执行形态的准入规则：后端把计划里那句自然语言拆成了"代码真的判了的几条"
  // 与"判不了的几条"。只显示原文会让人以为那句话被完整执行了。
  const rawRule = firstObject(plan.admission_rule)
  const ruleExecutable = stringList(rawRule.executable)
  const ruleNotExecutable = stringList(rawRule.not_executable)
  const ruleSource = asText(rawRule.source)
  const ruleSourceLabel = RULE_SOURCE_LABELS[ruleSource] || ''

  const available = Boolean(
    asText(entity.name) ||
    asText(entity.what_it_is) ||
    layers.length ||
    angles.length ||
    openQuestions.length ||
    asText(criteria.admission_rule)
  )

  return {
    available,
    source,
    sourceLabel,
    isDefault,
    sourceNote,
    error: asText(plan.error),
    entity: {
      name: asText(entity.name),
      type: entityType,
      typeLabel: ENTITY_TYPE_LABELS[entityType] || ENTITY_TYPE_LABELS.unknown,
      whatItIs: asText(entity.what_it_is),
      confidence,
      confidenceText: confidence === null ? '未评估' : confidence.toFixed(2),
      candidates: disambiguationCandidates,
      officialDomains: stringList(entity.official_domains)
    },
    criteria: {
      layers,
      layerText: layers.length ? layers.map(item => item.label).join(' / ') : '未定义',
      minCount,
      maxCount,
      countText,
      candidatePool,
      mustInclude: stringList(criteria.must_include),
      mustExclude: stringList(criteria.must_exclude),
      admissionRule: asText(criteria.admission_rule)
    },
    rule: {
      source: ruleSource,
      sourceLabel: ruleSourceLabel,
      executable: ruleExecutable,
      notExecutable: ruleNotExecutable,
      hasRule: ruleExecutable.length > 0 || ruleNotExecutable.length > 0
    },
    angles,
    weightNote: '权重由代码按优先级算（primary×1.5 / secondary×1.0 / ignore×0.5）后归一化，不是模型给的数字',
    openQuestions,
    competitorCandidates: stringList(plan.competitor_candidates),
    emptyText: '本次未生成研究计划（按默认口径执行）'
  }
}

/** 候选匹配键：有 URL 用 URL，否则退回名称。 */
function candidateKey(item) {
  const url = asText(item?.url)
  if (url) return `url:${url}`
  const name = asText(item?.name)
  return name ? `name:${name}` : ''
}

/**
 * 把后端 discovery 归一化成候选池表格。
 *
 * 前端只用 candidates / selected / scored / notes；entity_pages 与 queries_run
 * 不展示（体积大、价值低），但字段本身不丢——这里只是不取。
 */
export function normalizeDiscovery(raw, rawRejected, rawGate) {
  const discovery = firstObject(raw)
  const candidates = Array.isArray(discovery.candidates) ? discovery.candidates : []
  const selectedRaw = Array.isArray(discovery.selected) ? discovery.selected : []
  const scoredRaw = Array.isArray(discovery.scored) ? discovery.scored : []
  const notes = stringList(discovery.notes)

  const selectedKeys = new Set()
  selectedRaw.forEach(item => {
    const key = candidateKey(item)
    if (key) selectedKeys.add(key)
  })

  // scored 带打分明细，是表格的权威来源；没有它时退回候选池（只有名字和类型）
  const poolSource = scoredRaw.length ? scoredRaw : candidates
  const rows = poolSource.map(item => {
    const key = candidateKey(item)
    const scoreRaw = Number(item?.score)
    const score = Number.isFinite(scoreRaw) ? scoreRaw : 0
    const dimensionsRaw = Number(item?.dimensions ?? item?.dimension_count)
    return {
      name: asText(item?.name) || '（未命名候选）',
      url: asText(item?.url),
      siteType: asText(item?.site_type),
      siteTypeLabel: siteTypeLabel(item?.site_type),
      score,
      scoreText: Number.isFinite(scoreRaw) ? scoreRaw.toFixed(2) : '—',
      dimensions: Number.isFinite(dimensionsRaw) ? dimensionsRaw : 0,
      reasons: stringList(item?.reasons),
      selected: key ? selectedKeys.has(key) : false
    }
  })

  // 入选的排前面：用户先看"选了什么"，再看"其余为什么被剔"
  rows.sort((a, b) => {
    if (a.selected !== b.selected) return a.selected ? -1 : 1
    if (b.score !== a.score) return b.score - a.score
    return a.name.localeCompare(b.name, 'zh-Hans-CN')
  })

  const poolSize = candidates.length || scoredRaw.length || rows.length
  const selectedCount = selectedRaw.length

  // 未采信清单：抓完后数据不足被淘汰的竞品。它们**不在矩阵里**（不污染结论），
  // 但必须让用户看得见——"我们试过它，它没有数据"和"它没被考虑过"是两回事。
  const gate = firstObject(rawGate)
  const rejectedSource = Array.isArray(rawRejected) && rawRejected.length
    ? rawRejected
    : (Array.isArray(gate.rejected) ? gate.rejected : [])
  const rejected = rejectedSource
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      name: asText(item.name) || asText(item.url) || '（未命名）',
      url: asText(item.url),
      status: asText(item.status),
      dimensionCount: Number.isFinite(Number(item.dimension_count)) ? Number(item.dimension_count) : null,
      reasons: stringList(item.reasons)
    }))

  const backfilled = (Array.isArray(gate.backfilled) ? gate.backfilled : [])
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      name: asText(item.name) || asText(item.url) || '（未命名）',
      url: asText(item.url),
      reasons: stringList(item.reasons)
    }))

  return {
    available: Boolean(poolSource.length || notes.length),
    poolSize,
    selectedCount,
    rejectedCount: Math.max(0, poolSize - selectedCount),
    rows,
    selected: rows.filter(row => row.selected),
    rejected: rows.filter(row => !row.selected),
    unverified: rejected,
    backfilled,
    notes,
    emptyText: '本轮未走候选发现流程（没有候选池与打分明细）'
  }
}

/** 从报告对象里取 research_plan——旧报告没有这个字段时返回 null，不抛异常。 */
export function getReportResearchPlan(report = {}) {
  const candidates = [report?.research_plan, report?.raw?.research_plan, report?.raw?.researchPlan]
  for (const item of candidates) {
    if (item && typeof item === 'object') return item
  }
  return null
}

/** 从报告对象里取 discovery——旧报告没有这个字段时返回 null，不抛异常。 */
export function getReportDiscovery(report = {}) {
  const candidates = [report?.discovery, report?.raw?.discovery]
  for (const item of candidates) {
    if (item && typeof item === 'object') return item
  }
  return null
}

/** 「调研口径」tab 的纯文本版——用于"复制当前"。 */
export function buildResearchPlanText(rawPlan, rawDiscovery) {
  const plan = normalizeResearchPlan(rawPlan)
  const discovery = normalizeDiscovery(rawDiscovery)
  const lines = ['# 调研口径', '']

  if (!plan.available) {
    lines.push(plan.emptyText, '')
  } else {
    lines.push(`- 计划来源：${plan.sourceLabel}`)
    if (plan.entity.name) lines.push(`- 主体：${plan.entity.name}（${plan.entity.typeLabel}，置信度 ${plan.entity.confidenceText}）`)
    if (plan.entity.whatItIs) lines.push(`- 主体释义：${plan.entity.whatItIs}`)
    plan.entity.candidates.forEach(item => lines.push(`  - 备选：${item}`))
    lines.push(`- 竞品层级：${plan.criteria.layerText}`)
    lines.push(`- 竞品数量：${plan.criteria.countText}`)
    if (plan.criteria.admissionRule) lines.push(`- 入选标准：${plan.criteria.admissionRule}`)
    plan.criteria.mustInclude.forEach(item => lines.push(`  - 必须包含：${item}`))
    plan.criteria.mustExclude.forEach(item => lines.push(`  - 必须排除：${item}`))
    plan.openQuestions.forEach(item => {
      lines.push(`- 开放问题：${item.question}${item.default ? `（默认：${item.default}）` : ''}`)
    })
    if (plan.angles.length) {
      lines.push('', '## 重点角度', '')
      plan.angles.forEach(item => {
        lines.push(`- ${item.label} ${item.weightText}（${item.priorityLabel}）${item.reason ? ` —— ${item.reason}` : ''}`)
      })
      lines.push('', `注：${plan.weightNote}`)
    }
  }

  if (!discovery.available) {
    lines.push('', discovery.emptyText)
  } else {
    lines.push('', '## 候选池与入选', '')
    lines.push(`- 候选 ${discovery.poolSize} · 入选 ${discovery.selectedCount} · 剔除 ${discovery.rejectedCount}`)
    discovery.rows.forEach(row => {
      const reasons = row.reasons.length ? ` —— ${row.reasons.join('；')}` : ''
      lines.push(`- ${row.selected ? '✓' : '✗'} ${row.name}｜${row.siteTypeLabel}｜${row.scoreText}${reasons}`)
    })
    discovery.notes.forEach(note => lines.push(`- ${note}`))
  }

  return lines.join('\n')
}
