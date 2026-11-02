// 报告等级解析。
//
// 背景：后端已修掉 `_report_payload` / `list_history` 里硬编码的 `"grade": "B"` 兜底，
// 等级缺失时返回 None。但前端 6 处各自写着 `grade || 'B'`，后端改成 null 之后界面照样
// 显示 B —— 那次修复对用户完全不可见。这里把规则收敛到单一函数，让"缺失即未知"
// 成为可测试的约定，而不是散落在 6 个模板里的字面量。
//
// 优先级：数值分数 > 后端等级 > 未知。
// 数值分数优先是因为它比字母等级更具体（8.4 与 8.6 都是 B，但前者不该被 B 掩盖）。

export const UNKNOWN_GRADE = '未知'

/**
 * 分数 → 等级。
 *
 * 阈值必须与后端 `QualityScorer.SCORE_THRESHOLDS`（8 / 6 / 4 / 2）一字不差。
 * 两边各写一套时，前端会用数值分重算并覆盖后端给的字母——分叉就显形了：
 * 同一个 8.2 分，库里是 A、界面显示 B。
 */
export function gradeFromScore(score) {
  if (score >= 8) return 'A'
  if (score >= 6) return 'B'
  if (score >= 4) return 'C'
  if (score >= 2) return 'D'
  return 'F'
}

/**
 * 解析要展示的等级。
 *
 * @param {number|null|undefined} score 质量分数（0-10）
 * @param {string|null|undefined} grade 后端返回的等级
 * @returns {string} 等级字母，或 UNKNOWN_GRADE
 */
export function resolveGrade(score, grade) {
  if (typeof score === 'number' && Number.isFinite(score)) {
    return gradeFromScore(score)
  }
  if (typeof grade === 'string' && grade.trim()) {
    return grade.trim().toUpperCase()
  }
  return UNKNOWN_GRADE
}

/** 等级对应的 CSS class。未知等级走中性样式，不得冒充某个等级。 */
export function gradeClassName(grade) {
  if (!grade || grade === UNKNOWN_GRADE) return 'grade-unknown'
  return `grade-${String(grade).toLowerCase()}`
}