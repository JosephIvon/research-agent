import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ANGLE_LABELS,
  COMPETITOR_LAYER_LABELS,
  ENTITY_TYPE_LABELS,
  PRIORITY_LABELS,
  SITE_TYPE_LABELS,
  buildResearchPlanText,
  getReportDiscovery,
  getReportResearchPlan,
  normalizeAngleWeights,
  normalizeDiscovery,
  normalizeResearchPlan,
  siteTypeLabel
} from '../src/stores/researchTaskHelpers.js'

// ---------------------------------------------------------------------------
// 研究计划（research_plan）归一化
// ---------------------------------------------------------------------------

test('a missing research plan degrades to an empty state instead of throwing', () => {
  const plan = normalizeResearchPlan(undefined)
  assert.equal(plan.available, false)
  assert.equal(plan.entity.name, '')
  assert.equal(plan.entity.confidence, null)
  assert.equal(plan.entity.confidenceText, '未评估')
  assert.deepEqual(plan.entity.candidates, [])
  assert.deepEqual(plan.angles, [])
  assert.deepEqual(plan.openQuestions, [])
  assert.equal(plan.criteria.layerText, '未定义')
  assert.equal(plan.criteria.countText, '未限定')
  assert.ok(plan.emptyText.includes('未生成研究计划'))
})

test('normalizeResearchPlan survives a half-empty payload', () => {
  const plan = normalizeResearchPlan({ entity: {}, competitor_criteria: null, angles: 'not-a-list' })
  assert.equal(plan.available, false)
  assert.deepEqual(plan.angles, [])
  assert.equal(plan.criteria.minCount, null)
  assert.equal(plan.criteria.maxCount, null)
})

test('the entity type and confidence are read from the plan', () => {
  const plan = normalizeResearchPlan({
    entity: {
      name: '百度搭子',
      entity_type: 'app',
      what_it_is: '面向个人用户的 AI 原生社交陪伴应用',
      resolved_confidence: 0.72
    }
  })
  assert.equal(plan.available, true)
  assert.equal(plan.entity.name, '百度搭子')
  assert.equal(plan.entity.typeLabel, '应用')
  assert.equal(plan.entity.confidence, 0.72)
  assert.equal(plan.entity.confidenceText, '0.72')
  assert.equal(plan.entity.whatItIs, '面向个人用户的 AI 原生社交陪伴应用')
})

test('an unknown entity type falls back to a readable label, never the raw slug', () => {
  const plan = normalizeResearchPlan({ entity: { entity_type: 'something_new' } })
  assert.equal(plan.entity.typeLabel, '未知')
  assert.notEqual(plan.entity.typeLabel, 'something_new')
})

test('a confidence outside 0..1 is clamped instead of shown raw', () => {
  assert.equal(normalizeResearchPlan({ entity: { resolved_confidence: 1.8 } }).entity.confidence, 1)
  assert.equal(normalizeResearchPlan({ entity: { resolved_confidence: -0.4 } }).entity.confidence, 0)
})

test('the competitor criteria are read from competitor_criteria and from criteria', () => {
  const fromBackend = normalizeResearchPlan({
    competitor_criteria: {
      layers: ['direct', 'alternative', 'adjacent'],
      min_count: 3,
      max_count: 5,
      candidate_pool: 10,
      admission_rule: '必须有官网 + 至少 2 个维度有数据'
    }
  })
  assert.equal(fromBackend.criteria.layerText, '直接竞品 / 替代方案 / 相邻竞品')
  assert.equal(fromBackend.criteria.countText, '3–5 家')
  assert.equal(fromBackend.criteria.candidatePool, 10)
  assert.equal(fromBackend.criteria.admissionRule, '必须有官网 + 至少 2 个维度有数据')

  // 早期方案文档里的字段名是 criteria——两个都要认，否则口径整块打空
  const fromDoc = normalizeResearchPlan({
    criteria: { layers: ['direct'], min_count: 2, max_count: 4 }
  })
  assert.equal(fromDoc.criteria.layerText, '直接竞品')
  assert.equal(fromDoc.criteria.countText, '2–4 家')
})

test('open questions keep their default answer', () => {
  const plan = normalizeResearchPlan({
    open_questions: [{ question: '要不要含替代方案？', default: '否' }]
  })
  assert.equal(plan.openQuestions.length, 1)
  assert.equal(plan.openQuestions[0].question, '要不要含替代方案？')
  assert.equal(plan.openQuestions[0].default, '否')
})

// ---------------------------------------------------------------------------
// 角度权重：归一化 + 相对刻度
// ---------------------------------------------------------------------------

test('angle weights are normalized to sum up to one', () => {
  const weights = normalizeAngleWeights([
    { angle: 'feature', weight: 0.7 },
    { angle: 'business', weight: 0.3 }
  ])
  const total = weights.reduce((acc, item) => acc + item.weight, 0)
  assert.ok(Math.abs(total - 1) < 0.001, `权重和应为 1，实际 ${total}`)
  assert.equal(weights[0].weight, 0.7)
  assert.equal(weights[1].weight, 0.3)
})

test('weights that do not add up to one are rescaled proportionally', () => {
  const weights = normalizeAngleWeights([
    { angle: 'feature', weight: 2 },
    { angle: 'business', weight: 2 }
  ])
  assert.equal(weights[0].weight, 0.5)
  assert.equal(weights[1].weight, 0.5)
})

test('the backend weights that already sum to one are left untouched', () => {
  const plan = normalizeResearchPlan({
    angles: [
      { angle: 'feature', weight: 0.35 },
      { angle: 'business', weight: 0.3 },
      { angle: 'engineering', weight: 0.2 },
      { angle: 'product', weight: 0.15 }
    ]
  })
  assert.deepEqual(plan.angles.map(item => item.weightText), ['0.35', '0.30', '0.20', '0.15'])
})

test('missing weights are split evenly instead of rendering empty bars', () => {
  const weights = normalizeAngleWeights([
    { angle: 'feature', weight: undefined },
    { angle: 'business', weight: 0 },
    { angle: 'engineering', weight: null },
    { angle: 'product', weight: undefined }
  ])
  assert.equal(weights.length, 4)
  weights.forEach(item => assert.equal(item.weight, 0.25))
  weights.forEach(item => assert.equal(item.barPercent, 100))
})

test('the weight bar is drawn relative to the heaviest angle', () => {
  const weights = normalizeAngleWeights([
    { angle: 'feature', weight: 0.35 },
    { angle: 'business', weight: 0.30 },
    { angle: 'engineering', weight: 0.20 },
    { angle: 'product', weight: 0.15 }
  ])
  assert.equal(weights[0].barPercent, 100)
  assert.equal(weights[1].barPercent, 86)
  assert.equal(weights[2].barPercent, 57)
  assert.equal(weights[3].barPercent, 43)
})

test('plan angles carry a Chinese label and a readable priority', () => {
  const plan = normalizeResearchPlan({
    angles: [
      { angle: 'feature', priority: 'primary', weight: 0.54, reason: '用户最关心能不能用' },
      { angle: 'business', priority: 'unknown_priority', weight: 0.46 }
    ]
  })
  assert.equal(plan.angles.length, 2)
  assert.equal(plan.angles[0].label, '功能')
  assert.equal(plan.angles[0].priorityLabel, '重点')
  assert.equal(plan.angles[0].weightText, '0.54')
  assert.equal(plan.angles[0].reason, '用户最关心能不能用')
  // 认不出来的优先级不能漏到界面上
  assert.equal(plan.angles[1].priorityLabel, '次要')
})

// ---------------------------------------------------------------------------
// 默认口径 / 降级口径的标记
// ---------------------------------------------------------------------------

test('a default or fallback plan is marked as the default criteria', () => {
  assert.equal(normalizeResearchPlan({ source: 'default' }).isDefault, true)
  assert.equal(normalizeResearchPlan({ source: 'fallback' }).isDefault, true)
  assert.equal(normalizeResearchPlan({ source: 'model' }).isDefault, false)
  assert.equal(normalizeResearchPlan({ source: 'model' }).sourceLabel, '模型生成')
  assert.equal(normalizeResearchPlan({ source: 'default' }).sourceLabel, '默认口径')
  assert.ok(normalizeResearchPlan({ source: 'fallback' }).sourceLabel.includes('默认口径'))
})

test('a plan without a source is not silently presented as model-generated', () => {
  const plan = normalizeResearchPlan({})
  assert.equal(plan.isDefault, false)
  assert.equal(plan.sourceLabel, '来源未知')
})

// ---------------------------------------------------------------------------
// 入选轨迹（discovery）归一化
// ---------------------------------------------------------------------------

test('site types are mapped to Chinese names', () => {
  assert.equal(siteTypeLabel('product_site'), '产品官网')
  assert.equal(siteTypeLabel('media'), '媒体/内容站')
  assert.equal(siteTypeLabel('ugc_platform'), '开发者社区/UGC 博客')
  assert.equal(siteTypeLabel('app_store'), '应用商店/下载站')
  assert.equal(siteTypeLabel('directory'), '产品收录/比价目录')
  // 认不出来的类型与缺失都显示"未知"，不把 slug 漏到界面上
  assert.equal(siteTypeLabel('brand_new_type'), '未知')
  assert.equal(siteTypeLabel(undefined), '未知')
  assert.equal(siteTypeLabel(''), '未知')
  assert.equal(SITE_TYPE_LABELS.unknown, '未知')
})

test('the label tables cover every backend enum', () => {
  assert.equal(ENTITY_TYPE_LABELS.app, '应用')
  assert.equal(ANGLE_LABELS.engineering, '研发与实现')
  assert.equal(COMPETITOR_LAYER_LABELS.direct, '直接竞品')
  assert.equal(PRIORITY_LABELS.primary, '重点')
})

test('a missing discovery degrades to an empty state instead of throwing', () => {
  const discovery = normalizeDiscovery(undefined)
  assert.equal(discovery.available, false)
  assert.deepEqual(discovery.rows, [])
  assert.deepEqual(discovery.notes, [])
  assert.equal(discovery.poolSize, 0)
  assert.equal(discovery.selectedCount, 0)
  assert.equal(discovery.rejectedCount, 0)
  assert.ok(discovery.emptyText.includes('未走候选发现流程'))
})

test('the candidate pool shows who was selected and why the rest was dropped', () => {
  const discovery = normalizeDiscovery({
    candidates: [
      { name: 'LumiChat', url: 'https://lumichat.example', site_type: 'product_site' },
      { name: '卿卿我我', url: 'https://qq.example', site_type: 'app_store' },
      { name: '2026年AI陪伴盘点', url: 'https://media.example/rank', site_type: 'media' }
    ],
    selected: [
      {
        name: 'LumiChat',
        url: 'https://lumichat.example',
        score: 0.5,
        dimensions: 0,
        site_type: 'product_site',
        reasons: ['像产品自己的官网']
      }
    ],
    scored: [
      {
        name: 'LumiChat',
        url: 'https://lumichat.example',
        score: 0.5,
        dimensions: 0,
        site_type: 'product_site',
        reasons: ['像产品自己的官网']
      },
      {
        name: '卿卿我我',
        url: 'https://qq.example',
        score: 0,
        dimensions: 0,
        site_type: 'app_store',
        reasons: ['站点类型是应用商店/下载站，不是产品官网']
      },
      {
        name: '2026年AI陪伴盘点',
        url: 'https://media.example/rank',
        score: 0,
        dimensions: 0,
        site_type: 'media',
        reasons: ['站点类型是媒体/内容站，不是产品官网']
      }
    ],
    notes: ['候选池剔除 2 个非官网页（媒体/内容站 1 个、应用商店 1 个）']
  })

  assert.equal(discovery.available, true)
  assert.equal(discovery.poolSize, 3)
  assert.equal(discovery.selectedCount, 1)
  assert.equal(discovery.rejectedCount, 2)
  assert.equal(discovery.rows.length, 3)

  // 入选的排前面
  assert.equal(discovery.rows[0].name, 'LumiChat')
  assert.equal(discovery.rows[0].selected, true)
  assert.equal(discovery.rows[0].siteTypeLabel, '产品官网')
  assert.deepEqual(discovery.rows[0].reasons, ['像产品自己的官网'])

  const rejected = discovery.rows.filter(row => !row.selected)
  assert.equal(rejected.length, 2)
  assert.ok(rejected.every(row => row.reasons.length > 0), '每个被剔除的候选都要有理由')
  assert.deepEqual(discovery.notes, ['候选池剔除 2 个非官网页（媒体/内容站 1 个、应用商店 1 个）'])
})

test('a candidate pool without scoring details still renders with unknown types', () => {
  const discovery = normalizeDiscovery({
    candidates: [{ name: '某个竞品', url: 'https://x.example' }]
  })
  assert.equal(discovery.available, true)
  assert.equal(discovery.rows.length, 1)
  assert.equal(discovery.rows[0].siteTypeLabel, '未知')
  assert.equal(discovery.rows[0].scoreText, '—')
})

// ---------------------------------------------------------------------------
// 旧报告空态：报告对象里取不到这两个字段时不能报错
// ---------------------------------------------------------------------------

test('an old report without research_plan or discovery yields null, not an error', () => {
  const oldReport = { id: 'r1', markdown: '# 报告', raw: { crawl_results: { competitors: [] } } }
  assert.equal(getReportResearchPlan(oldReport), null)
  assert.equal(getReportDiscovery(oldReport), null)
  assert.equal(normalizeResearchPlan(getReportResearchPlan(oldReport)).available, false)
  assert.equal(normalizeDiscovery(getReportDiscovery(oldReport)).available, false)
})

test('the plan and discovery are read from raw when the top level has none', () => {
  const report = {
    raw: {
      research_plan: { entity: { name: '百度搭子' } },
      discovery: { candidates: [{ name: 'a', url: 'https://a.example' }] }
    }
  }
  assert.equal(getReportResearchPlan(report).entity.name, '百度搭子')
  assert.equal(getReportDiscovery(report).candidates.length, 1)
})

test('the plan text used by "复制当前" survives a report with neither field', () => {
  const text = buildResearchPlanText(null, null)
  assert.ok(text.includes('调研口径'))
  assert.ok(text.includes('未生成研究计划'))
  assert.ok(text.includes('未走候选发现流程'))
})

// ---------------------------------------------------------------------------
// 可执行准入条件：计划里那句自然语言被拆成了"真的判了"与"判不了"
// ---------------------------------------------------------------------------

test('the executable rule is separated from the parts that cannot be judged', () => {
  const plan = normalizeResearchPlan({
    entity: { name: '百度搭子' },
    competitor_criteria: { admission_rule: '必须有官网 + 至少 2 个维度有数据' },
    admission_rule: {
      source: 'model',
      executable: ['必须有官网（必须是产品官网）', '至少 2 个维度有数据（抓后判定）'],
      not_executable: ['直接层须满足个人用户×移动端App（层归属要语义判断，代码不据此裁决）']
    }
  })
  assert.equal(plan.rule.hasRule, true)
  assert.equal(plan.rule.executable.length, 2)
  assert.equal(plan.rule.notExecutable.length, 1)
  assert.ok(plan.rule.executable[0].includes('产品官网'))
  assert.equal(plan.rule.sourceLabel, '按计划口径执行')
})

test('a plan without a parsed rule reports no executable conditions at all', () => {
  const plan = normalizeResearchPlan({ entity: { name: 'x' } })
  assert.equal(plan.rule.hasRule, false)
  assert.deepEqual(plan.rule.executable, [])
  assert.deepEqual(plan.rule.notExecutable, [])
  assert.equal(plan.rule.sourceLabel, '')
})

test('an unparseable rule is labelled as such instead of silently ignored', () => {
  const plan = normalizeResearchPlan({
    entity: { name: 'x' },
    admission_rule: { source: 'unparsed', executable: [], not_executable: [] }
  })
  assert.equal(plan.rule.sourceLabel, '口径无法解析，已退回默认口径')
})

// ---------------------------------------------------------------------------
// 未采信清单：抓后数据不足被闸门剔除的竞品
// ---------------------------------------------------------------------------

test('the unverified list carries the reason each competitor was dropped', () => {
  const discovery = normalizeDiscovery(
    { candidates: [{ name: '猫箱', url: 'https://maoxiangai.com/' }] },
    [{ name: '数据不足的', url: 'https://thin.test/', dimension_count: 1, reasons: ['只抽到 1 个维度'] }]
  )
  assert.equal(discovery.unverified.length, 1)
  assert.equal(discovery.unverified[0].name, '数据不足的')
  assert.equal(discovery.unverified[0].dimensionCount, 1)
  assert.deepEqual(discovery.unverified[0].reasons, ['只抽到 1 个维度'])
})

test('the unverified list can also be read from the admission gate record', () => {
  const discovery = normalizeDiscovery(
    { candidates: [] },
    null,
    { rejected: [{ name: '补位的', url: 'https://b.test/', dimension_count: 0 }] }
  )
  assert.equal(discovery.unverified.length, 1)
  assert.equal(discovery.unverified[0].name, '补位的')
  assert.equal(discovery.unverified[0].dimensionCount, 0)
})

test('a report without a gate shows no unverified list at all', () => {
  const discovery = normalizeDiscovery({ candidates: [{ name: 'a', url: 'https://a.test' }] })
  assert.deepEqual(discovery.unverified, [])
  assert.deepEqual(discovery.backfilled, [])
})
