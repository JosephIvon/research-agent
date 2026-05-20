import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthorizedEventSource } from '../src/api/sse.js'

test('authorized SSE client sends bearer token and dispatches named events', async () => {
  const encoder = new TextEncoder()
  const chunks = [
    encoder.encode('id: 7\nevent: decompose\ndata: {"message":"ok"}\n\n')
  ]
  let captured = null
  let fetchCalls = 0

  const fetchImpl = async (url, options) => {
    fetchCalls += 1
    captured = { url, options }
    return {
      ok: true,
      status: 200,
      body: {
        getReader() {
          return {
            async read() {
              if (!chunks.length) return { done: true }
              return { done: false, value: chunks.shift() }
            },
            releaseLock() {}
          }
        }
      }
    }
  }

  const source = createAuthorizedEventSource('/api/research/tasks/1/events', {
    fetchImpl,
    getToken: () => 'test-token',
    reconnect: false
  })

  const event = await new Promise(resolve => {
    source.addEventListener('decompose', resolve)
  })

  assert.equal(captured.url, '/api/research/tasks/1/events')
  assert.equal(captured.options.headers.Authorization, 'Bearer test-token')
  assert.equal(captured.options.headers.Accept, 'text/event-stream')
  assert.equal(event.lastEventId, '7')
  assert.equal(event.data, '{"message":"ok"}')
  source.close()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(fetchCalls, 1)
})
