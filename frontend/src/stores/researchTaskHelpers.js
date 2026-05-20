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
