import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BACKEND_EVENTS,
  buildSourceRows,
  buildTaskTimeline,
  getEventType,
  getReportCompetitorCount,
  getRunHeadline,
  getRunStatusKicker,
  getRunStatusLabel,
  getRunStatusTagType,
  getTaskArtifactContent,
  getTaskInputSummary,
  isTerminalRunStatus,
  mergeReportMeta,
  normalizeDeliverables,
  mapBackendStageField,
  resolveFrontendStage,
  resolveStreamTerminal,
  STAGE_MAP
} from '../src/stores/researchTaskHelpers.js'

test('normalizes deliverables with report and PRD enabled by default', () => {
  assert.deepEqual(normalizeDeliverables(), {
    report: true,
    prd: true
  })
  assert.deepEqual(normalizeDeliverables({ prd: false }), {
    report: true,
    prd: false
  })
})

test('builds a user-facing task timeline from requested inputs', () => {
  const timeline = buildTaskTimeline({
    params: {
      enable_search: true,
      target_sites: [
        {
          url: 'https://alpha.example/product',
          auth_credentials: { username: 'demo', password: 'secret' }
        },
        { url: 'https://beta.example/pricing' }
      ]
    },
    deliverables: { report: true, prd: true }
  })

  assert.deepEqual(
    timeline.map(step => step.id),
    ['understand', 'scope', 'search', 'crawl', 'extract', 'report', 'prd', 'finish']
  )
  assert.equal(timeline.find(step => step.id === 'crawl').detail, '2 个指定网站，其中 1 个需要登录')
})

test('summarizes target sites and credentials without exposing secrets', () => {
  const summary = getTaskInputSummary({
    target_sites: [
      {
        url: 'https://alpha.example/product',
        auth_credentials: { username: 'demo', password: 'secret' }
      },
      { url: 'https://beta.example/pricing' }
    ],
    enable_search: false
  })

  assert.equal(summary.siteCount, 2)
  assert.equal(summary.loginCount, 1)
  assert.equal(summary.autoSearchText, '关闭')
  assert.equal(summary.passwordsVisible, false)
})

test('prefers full PRD artifact content over SSE preview text', () => {
  const task = {
    artifacts: {
      prd: {
        content: '完整 PRD 正文'.repeat(200)
      }
    }
  }

  assert.equal(
    getTaskArtifactContent(task, 'prd', '只有 500 字的预览'),
    '完整 PRD 正文'.repeat(200)
  )
})

test('falls back to SSE preview when task artifact is missing', () => {
  assert.equal(getTaskArtifactContent({ artifacts: {} }, 'prd', '预览内容'), '预览内容')
})

test('builds source rows from async task crawl results', () => {
  const report = {
    raw: {
      crawl_results: {
        competitors: [
          {
            name: 'api000.com',
            url: 'https://api000.com',
            status: 'success',
            extracted_data: [{ key: 'models', value: 'OpenAI' }],
            raw_pages: [
              { url: 'https://api000.com', content_length: 3300 },
              { url: 'https://api000.com/pricing', content_length: 1200 }
            ],
            discovered_urls: ['https://api000.com/pricing']
          }
        ]
      }
    }
  }

  const rows = buildSourceRows(report)

  assert.equal(getReportCompetitorCount(report), 1)
  assert.equal(rows[0].pageCount, 2)
  assert.equal(rows[0].dataCount, 1)
  assert.deepEqual(rows[0].pageUrls, ['https://api000.com', 'https://api000.com/pricing'])
  assert.deepEqual(rows[0].discoveredUrls, ['https://api000.com/pricing'])
})

// ---------------------------------------------------------------------------
// SSE 事件订阅与阶段映射
//
// 这组用例盯的是一个具体的坑：后端发了事件，前端**没订阅**，或者订阅了却没在
// STAGE_MAP 里配阶段——两种情况都会走到 `if (!frontendStage) return`，事件被
// 静默丢弃，界面上什么都不会发生，也没有任何报错。
// citation_check 正好栽在这上：后端发了 4 次，前端白名单里根本没有它。
// ---------------------------------------------------------------------------

test('subscribes citation_check so citation-verification progress is not dropped', () => {
  assert.ok(
    BACKEND_EVENTS.includes('citation_check'),
    '后端 citation_check 发了 4 次，前端不订阅就等于全部丢弃'
  )
})

test('every mapped backend event is actually subscribed', () => {
  const unmapped = Object.keys(STAGE_MAP).filter(name => !BACKEND_EVENTS.includes(name))
  assert.deepEqual(unmapped, [], `配了阶段却没订阅的事件：${unmapped.join(', ')}`)
})

test('subscription list carries no event the backend never sends', () => {
  // task_created 由 server.py 的 create_task 直接发，不经过工作流，因此没有阶段
  const allowedExtras = ['task_created']
  const extras = BACKEND_EVENTS.filter(
    name => !Object.keys(STAGE_MAP).includes(name) && !allowedExtras.includes(name)
  )
  assert.deepEqual(extras, [], `订阅了后端从不发的事件：${extras.join(', ')}`)
})

test('citation_check lands on the report step instead of inventing a new one', () => {
  assert.equal(resolveFrontendStage('citation_check'), 'report')
})

test('injection_detected takes its stage from the event payload', () => {
  // 它在爬取和成文两个阶段都会发；硬编码成一个值会把成文阶段发现的注入
  // 记到"抓取"那一步上
  assert.equal(resolveFrontendStage('injection_detected', 'crawl'), 'crawl')
  assert.equal(resolveFrontendStage('injection_detected', 'report_generate'), 'report')
  assert.equal(resolveFrontendStage('injection_detected', 'anything_else'), null)
  assert.equal(resolveFrontendStage('injection_detected', 'report_generate'), 'report')
  // 拿事件名的表去查后端 stage 值只会得到 undefined——事件被静默丢掉
  assert.equal(mapBackendStageField('crawl'), 'crawl')
  assert.equal(mapBackendStageField('report_generate'), 'report')
  assert.equal(mapBackendStageField('never_sent'), null)
})

test('events without a timeline step resolve to null rather than throwing', () => {
  assert.equal(resolveFrontendStage('verify', 'verify'), null)
  assert.equal(resolveFrontendStage('artifact_ready', 'report_generate'), null)
  assert.equal(resolveFrontendStage('never_heard_of_it'), null)
})

test('partial terminal event is reported as a delivered result, not an error', () => {
  assert.equal(getEventType('partial', 'partial'), 'success')
  assert.equal(getEventType('completed', 'completed'), 'success')
  assert.equal(getEventType('error', 'failed'), 'error')
})

// ---------------------------------------------------------------------------
// PARTIAL 终态（M6）
// ---------------------------------------------------------------------------

test('partial counts as a terminal run status', () => {
  assert.equal(isTerminalRunStatus('partial'), true)
  assert.equal(isTerminalRunStatus('completed'), true)
  assert.equal(isTerminalRunStatus('failed'), true)
  assert.equal(isTerminalRunStatus('running'), false)
})

test('partial never borrows the completed label', () => {
  // 标签写成「已完成」就是撒谎：那份报告是半截的
  assert.notEqual(getRunStatusLabel('partial'), getRunStatusLabel('completed'))
  assert.notEqual(getRunStatusLabel('partial'), '准备中', 'partial 落进默认值等于没接')
  assert.equal(getRunStatusTagType('partial'), 'warning')
  assert.equal(getRunStatusKicker('partial'), '预算收束')
})

test('partial headline explains the truncation instead of claiming success', () => {
  const headline = getRunHeadline({ status: 'partial' })
  assert.ok(headline.includes('预算'), `partial 的说明必须点明预算，���际：${headline}`)
  assert.ok(headline.includes('不完整') || headline.includes('未覆盖'))
})

test('unknown status still degrades to a safe placeholder', () => {
  assert.equal(getRunStatusLabel('something_new'), '准备中')
  assert.equal(getRunStatusTagType('something_new'), 'primary')
  assert.equal(getRunStatusKicker('something_new'), '正在处理')
})

// ---------------------------------------------------------------------------
// 报告元数据合并（冷加载不能丢字段）
// ---------------------------------------------------------------------------

test('mergeReportMeta carries the incomplete flag from a cold load', () => {
  const report = mergeReportMeta({}, { incomplete: true })
  assert.equal(report.incomplete, true)
})

test('mergeReportMeta defaults incomplete to false when backend omits it', () => {
  assert.equal(mergeReportMeta({}, {}).incomplete, undefined)
  assert.equal(mergeReportMeta({}, { incomplete: false }).incomplete, false)
})

test('mergeReportMeta keeps the other observable fields together', () => {
  const report = mergeReportMeta({}, {
    trace_url: 'https://trace.test/abc',
    injection_findings_count: '3',
    cost_report: { total_tokens: 1200 },
    incomplete: true
  })
  assert.equal(report.trace_url, 'https://trace.test/abc')
  assert.equal(report.injection_findings_count, 3, '字符串数字必须被转成数值')
  assert.deepEqual(report.cost_report, { total_tokens: 1200 })
  assert.equal(report.incomplete, true)
})

test('mergeReportMeta never overwrites existing values with undefined', () => {
  const report = mergeReportMeta(
    { trace_url: 'https://trace.test/keep', cost_report: { total_tokens: 9 } },
    { trace_url: undefined, cost_report: null }
  )
  assert.equal(report.trace_url, 'https://trace.test/keep')
  assert.deepEqual(report.cost_report, { total_tokens: 9 })
})

test('resolveStreamTerminal ignores status="completed" on progress events', () => {
  // 后端把 status 当**阶段进度**用，不是终态标志：crawl_complete /
  // citation_check / artifact_ready / injection_detected 全都发 status="completed"。
  // 按 status 判终态会让 SSE 在第一次 crawl_complete 就被拆掉，报告再也取不回来。
  assert.equal(resolveStreamTerminal('agent_step', 'completed', 'agent'), null)
  assert.equal(resolveStreamTerminal('crawl_complete', 'completed', 'crawl'), null)
  assert.equal(resolveStreamTerminal('citation_check', 'completed', 'verify'), null)
  assert.equal(resolveStreamTerminal('artifact_ready', 'completed', 'report_generate'), null)
  assert.equal(resolveStreamTerminal('injection_detected', 'completed', 'report_generate'), null)
  assert.equal(resolveStreamTerminal('agent_step', 'running', 'agent'), null)
})

test('resolveStreamTerminal terminates on the real terminal events', () => {
  assert.equal(resolveStreamTerminal('completed', 'completed', 'completed'), 'completed')
  assert.equal(resolveStreamTerminal('partial', 'partial', 'completed'), 'partial')
  // 任务级失败：verify / workflow 两个阶段
  assert.equal(resolveStreamTerminal('error', 'failed', 'workflow'), 'failed')
  assert.equal(resolveStreamTerminal('error', 'failed', 'verify'), 'failed')
})

test('resolveStreamTerminal does not terminate on a per-competitor crawl error', () => {
  // 单个竞品抓取失败时后端同样发 error + stage=crawl + status=failed
  // （mvp_workflow._fetch_single_competitor），任务仍在继续。原判定只看
  // status==='failed'，于是第一次抓取失败就把前端拆停并报成 failed，后端照跑
  // 不误、真正的报告再也没人收。
  assert.equal(resolveStreamTerminal('error', 'failed', 'crawl'), null)
})
