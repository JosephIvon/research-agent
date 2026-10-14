// `DEFAULT_API_BASE_URL` 是空串而不是 '/api'：axios 的 `baseURL: ''` 会把
// `/research/history` 解析为**同源绝对路径**，这正是 466aadd "force same-origin
// API base in production" 要的效果——生产环境绝不接受外部传入的 host。
// SSE 路径另有 `(API_BASE_URL || '/api')` 兜底，所以空串在两处都是安全的。
// 曾有这些断言写的是 '/api'，实现改了之后没同步，导致 CI 的 frontend job 长期红着。
import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveApiBaseUrl } from '../src/api/baseUrl.js'

test('defaults to same-origin (empty base), not a hardcoded /api prefix', () => {
  assert.equal(resolveApiBaseUrl('', false), '')
  assert.equal(resolveApiBaseUrl(undefined, false), '')
})

test('ignores absolute API hosts in production builds', () => {
  assert.equal(resolveApiBaseUrl('http://198.51.100.10:8080', false), '')
  assert.equal(resolveApiBaseUrl('https://api.example.com', false), '')
})

test('allows absolute API hosts only for local development', () => {
  assert.equal(resolveApiBaseUrl('http://127.0.0.1:8080/', true), 'http://127.0.0.1:8080')
})

test('keeps relative API paths normalized', () => {
  assert.equal(resolveApiBaseUrl('/api/', false), '/api')
  assert.equal(resolveApiBaseUrl('api', false), '/api')
})
