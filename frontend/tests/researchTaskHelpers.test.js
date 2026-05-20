import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildSourceRows,
  buildTaskTimeline,
  getReportCompetitorCount,
  getTaskArtifactContent,
  getTaskInputSummary,
  normalizeDeliverables
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
