import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AGENT_STEP_LIMIT,
  AGENT_THOUGHT_TRUNCATE,
  appendAgentStep,
  buildAgentStepMessage,
  formatAgentStepSummary,
  normalizeAgentStep
} from '../src/stores/researchTaskHelpers.js'

test('normalizeAgentStep keeps payload shape with safe defaults', () => {
  assert.deepEqual(normalizeAgentStep({
    step: 3,
    thought: '先查定价页',
    tool_calls: [{ name: 'web_search', arguments: { query: '价格' } }],
    stop_reason: 'tool_calls'
  }), {
    step: 3,
    thought: '先查定价页',
    tool_calls: [{ name: 'web_search', arguments: { query: '价格' } }],
    stop_reason: 'tool_calls'
  })

  assert.deepEqual(normalizeAgentStep({ step: 1 }), {
    step: 1,
    thought: null,
    tool_calls: [],
    stop_reason: null
  })
  assert.equal(normalizeAgentStep({ step: 2, stop_reason: null }).stop_reason, null)
})

test('appendAgentStep merges payloads and keeps only the latest 50 steps', () => {
  let steps = []
  for (let i = 1; i <= AGENT_STEP_LIMIT + 7; i += 1) {
    steps = appendAgentStep(steps, { step: i, thought: null, tool_calls: [] })
  }

  assert.equal(steps.length, AGENT_STEP_LIMIT)
  assert.equal(steps[0].step, 8)
  assert.equal(steps[steps.length - 1].step, AGENT_STEP_LIMIT + 7)
})

test('buildAgentStepMessage joins tool names and falls back to 思考', () => {
  assert.equal(
    buildAgentStepMessage({
      step: 2,
      tool_calls: [
        { name: 'web_search', arguments: { query: 'a' } },
        { name: 'fetch_url', arguments: { url: 'https://x.example' } }
      ]
    }),
    '第2轮：web_search、fetch_url'
  )
  assert.equal(buildAgentStepMessage({ step: 1, tool_calls: [] }), '第1轮：思考')
  assert.equal(buildAgentStepMessage({ step: 4, thought: '只有思考' }), '第4轮：思考')
})

test('formatAgentStepSummary renders round, tool names with args and truncated thought', () => {
  const summary = formatAgentStepSummary({
    step: 5,
    thought: '对比两家定价，需要先抓取价格页面再抽取表格数据',
    tool_calls: [
      { name: 'fetch_url', arguments: { url: 'https://alpha.example/pricing' } }
    ]
  })

  assert.ok(summary.startsWith('第5轮 · fetch_url（{"url":"https://alpha.example/pricing"}） · '), summary)
  assert.ok(summary.includes('对比两家定价'))

  const longThought = '长'.repeat(AGENT_THOUGHT_TRUNCATE + 10)
  const truncated = formatAgentStepSummary({ step: 6, thought: longThought, tool_calls: [] })
  assert.equal(truncated, `第6轮 · 思考 · ${'长'.repeat(AGENT_THOUGHT_TRUNCATE)}`)

  assert.equal(formatAgentStepSummary({ step: 7, thought: '', tool_calls: [] }), '第7轮 · 思考')
})

test('merges an agent_step payload stream into timeline messages and steps like the store does', () => {
  // 模拟 connectTaskSSE 中 handleMessage 对 agent_step 的处理方式
  const payloads = [
    { step: 1, thought: '拆解调研目标', tool_calls: [], stop_reason: null },
    {
      step: 2,
      thought: null,
      tool_calls: [{ name: 'web_search', arguments: { query: '竞品 A 定价' } }],
      stop_reason: 'tool_calls'
    },
    {
      step: 3,
      thought: '结束调研',
      tool_calls: [{ name: 'finish', arguments: {} }],
      stop_reason: 'end_turn'
    }
  ]

  let agentSteps = []
  const messages = []
  for (const payload of payloads) {
    agentSteps = appendAgentStep(agentSteps, payload)
    messages.push(buildAgentStepMessage(payload))
  }

  assert.deepEqual(messages, [
    '第1轮：思考',
    '第2轮：web_search',
    '第3轮：finish'
  ])
  assert.equal(agentSteps.length, 3)
  assert.equal(agentSteps[2].stop_reason, 'end_turn')
  assert.equal(agentSteps[0].thought, '拆解调研目标')
  assert.equal(agentSteps[1].tool_calls[0].name, 'web_search')
})
