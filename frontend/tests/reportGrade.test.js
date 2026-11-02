import assert from 'node:assert/strict'
import test from 'node:test'

import {
  UNKNOWN_GRADE,
  gradeClassName,
  gradeFromScore,
  resolveGrade
} from '../src/utils/reportGrade.js'

test('gradeFromScore maps score bands to letters', () => {
  // 边界必须逐个钉死：阈值就是后端 QualityScorer.SCORE_THRESHOLDS 的 8 / 6 / 4 / 2，
  // 差 0.5 就会让同一份报告在库里和界面上显示成两个等级。
  assert.equal(gradeFromScore(9.5), 'A')
  assert.equal(gradeFromScore(8), 'A')
  assert.equal(gradeFromScore(7.9), 'B')
  assert.equal(gradeFromScore(7), 'B')
  assert.equal(gradeFromScore(6), 'B')
  assert.equal(gradeFromScore(5.9), 'C')
  assert.equal(gradeFromScore(5.5), 'C')
  assert.equal(gradeFromScore(4), 'C')
  assert.equal(gradeFromScore(3.9), 'D')
  assert.equal(gradeFromScore(3), 'D')
  assert.equal(gradeFromScore(2), 'D')
  assert.equal(gradeFromScore(1.9), 'F')
  assert.equal(gradeFromScore(0), 'F')
})

test('a numeric score wins over a backend letter grade', () => {
  // 后端等级兜底已被移除，这里确认分数确实优先，避免 8.4 被一个陈旧的 B 掩盖
  assert.equal(resolveGrade(8.4, 'C'), 'A')
  assert.equal(resolveGrade(9.9, 'F'), 'A')
})

test('a backend letter grade is used when no score exists', () => {
  assert.equal(resolveGrade(null, 'B'), 'B')
  assert.equal(resolveGrade(undefined, 'B'), 'B')
  assert.equal(resolveGrade(null, 'c'), 'C')
  assert.equal(resolveGrade(null, ' B '), 'B')
})

test('missing grade renders as unknown, never as a fabricated B', () => {
  // 这是本次修复的核心断言：后端返回 null 时，界面必须说"未知"
  assert.equal(resolveGrade(null, null), UNKNOWN_GRADE)
  assert.equal(resolveGrade(null, undefined), UNKNOWN_GRADE)
  assert.equal(resolveGrade(undefined, undefined), UNKNOWN_GRADE)
  assert.equal(resolveGrade(null, ''), UNKNOWN_GRADE)
  assert.equal(resolveGrade(null, '   '), UNKNOWN_GRADE)
})

test('non-numeric or non-finite scores fall through instead of grading nonsense', () => {
  assert.equal(resolveGrade('8.5', null), UNKNOWN_GRADE)
  assert.equal(resolveGrade(NaN, null), UNKNOWN_GRADE)
  assert.equal(resolveGrade(Infinity, null), UNKNOWN_GRADE)
  assert.equal(resolveGrade(null, NaN), UNKNOWN_GRADE)
})

test('gradeClassName never styles an unknown grade as a real letter', () => {
  assert.equal(gradeClassName('A'), 'grade-a')
  assert.equal(gradeClassName('b'), 'grade-b')
  assert.equal(gradeClassName(UNKNOWN_GRADE), 'grade-unknown')
  assert.equal(gradeClassName(null), 'grade-unknown')
  assert.equal(gradeClassName(''), 'grade-unknown')
})