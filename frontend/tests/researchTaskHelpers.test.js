import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildTaskTimeline,
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
