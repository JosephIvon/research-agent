import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveApiBaseUrl } from '../src/api/baseUrl.js'

test('uses same-origin /api by default', () => {
  assert.equal(resolveApiBaseUrl('', false), '/api')
  assert.equal(resolveApiBaseUrl(undefined, false), '/api')
})

test('ignores absolute API hosts in production builds', () => {
  assert.equal(resolveApiBaseUrl('http://198.51.100.10:8080', false), '/api')
  assert.equal(resolveApiBaseUrl('https://api.example.com', false), '/api')
})

test('allows absolute API hosts only for local development', () => {
  assert.equal(resolveApiBaseUrl('http://127.0.0.1:8080/', true), 'http://127.0.0.1:8080')
})

test('keeps relative API paths normalized', () => {
  assert.equal(resolveApiBaseUrl('/api/', false), '/api')
  assert.equal(resolveApiBaseUrl('api', false), '/api')
})
