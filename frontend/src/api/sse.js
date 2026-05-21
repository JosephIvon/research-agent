function storedAuthToken() {
  try {
    return globalThis.sessionStorage?.getItem('auth_token') || ''
  } catch (e) {
    return ''
  }
}

function normalizeBearerToken(token) {
  if (!token) return ''
  return token.startsWith('Bearer ') ? token : `Bearer ${token}`
}

/**
 * Creates an EventSource-like interface that works across all browsers.
 * Uses native EventSource if available and supports fetch+ReadableStream,
 * otherwise falls back to XMLHttpRequest-based polling.
 */
export function createEventSource(url, options = {}) {
  const { headers = {}, withCredentials = false, onopen, onmessage, onerror } = options;

  // Check if native EventSource with SSE support is available
  const hasNativeSSE = typeof EventSource !== 'undefined' &&
    typeof ReadableStream !== 'undefined' &&
    typeof fetch === 'function';

  if (hasNativeSSE) {
    // Use native EventSource for modern browsers
    return createNativeEventSource(url, headers, onopen, onmessage, onerror);
  } else {
    // Fallback to polling for older browsers (Safari < 15, older mobile browsers)
    return createPolyfillEventSource(url, headers, withCredentials, onmessage, onerror);
  }
}

function createNativeEventSource(url, headers, onopen, onmessage, onerror) {
  // Current implementation - wrap in EventSource-like interface
  let eventSource;
  let reconnectAttempts = 0;
  const maxReconnectAttempts = 5;
  const reconnectDelay = 3000;

  function connect() {
    eventSource = new EventSource(url, { withCredentials: false });

    eventSource.onopen = () => {
      reconnectAttempts = 0;
      if (onopen) onopen();
    };

    eventSource.onmessage = (event) => {
      if (onmessage) onmessage(event.data, event);
    };

    eventSource.onerror = (error) => {
      eventSource.close();
      if (onerror) onerror(error);

      if (reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts++;
        globalThis.setTimeout(connect, reconnectDelay * reconnectAttempts);
      }
    };
  }

  connect();

  return {
    close: () => eventSource?.close(),
    url,
  };
}

function createPolyfillEventSource(url, headers, withCredentials, onmessage, onerror) {
  // Simple polling-based polyfill
  // Polls the same SSE endpoint every 1 second
  let lastEventId = null;
  let intervalId = null;
  let running = true;
  const pollInterval = 1000; // 1 second

  async function poll() {
    if (!running) return;

    try {
      const xhr = new XMLHttpRequest();
      xhr.open('GET', url, true);
      xhr.setRequestHeader('Accept', 'text/event-stream');
      xhr.setRequestHeader('Cache-Control', 'no-cache');
      if (lastEventId) {
        xhr.setRequestHeader('Last-Event-ID', lastEventId);
      }
      if (withCredentials) {
        xhr.withCredentials = true;
      }

      Object.entries(headers).forEach(([key, value]) => {
        xhr.setRequestHeader(key, value);
      });

      xhr.onprogress = () => {
        if (!running) return;
        const lines = xhr.responseText.split('\n');
        let eventType = null;
        let data = [];

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7);
          } else if (line.startsWith('data: ')) {
            data.push(line.slice(6));
          } else if (line === '') {
            // End of event
            if (data.length > 0) {
              const dataStr = data.join('\n');
              if (eventType === 'TASK_COMPLETED' || eventType === 'TASK_FAILED') {
                lastEventId = eventType;
              }
              if (onmessage) onmessage(dataStr, { type: eventType || 'message' });
            }
            data = [];
            eventType = null;
          }
        }
      };

      xhr.onerror = () => {
        if (onerror) onerror(new Error('XHR polling error'));
      };

      xhr.send();
    } catch (error) {
      if (onerror) onerror(error);
    }
  }

  poll();
  intervalId = globalThis.setInterval(poll, pollInterval);

  return {
    close: () => {
      running = false;
      if (intervalId) globalThis.clearInterval(intervalId);
    },
    url,
  };
}

export function createAuthorizedEventSource(url, options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch?.bind(globalThis)
  const getToken = options.getToken || storedAuthToken
  const reconnect = options.reconnect !== false
  const reconnectDelay = options.reconnectDelay || 2000
  const listeners = new Map()
  const decoder = new TextDecoder()

  let activeController = null
  let buffer = ''
  let closed = false
  let lastEventId = ''
  let reconnectTimer = null
  let readyState = 0

  function dispatch(type, event) {
    const callbacks = listeners.get(type)
    if (!callbacks) return
    for (const callback of callbacks) {
      callback(event)
    }
  }

  function dispatchError(error, status) {
    dispatch('error', {
      type: 'error',
      error,
      status
    })
  }

  function dispatchSseBlock(block) {
    if (!block.trim()) return

    let eventType = 'message'
    const data = []

    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue

      const separator = line.indexOf(':')
      const field = separator >= 0 ? line.slice(0, separator) : line
      let value = separator >= 0 ? line.slice(separator + 1) : ''
      if (value.startsWith(' ')) value = value.slice(1)

      if (field === 'event') eventType = value || 'message'
      if (field === 'data') data.push(value)
      if (field === 'id') lastEventId = value
    }

    dispatch(eventType, {
      type: eventType,
      data: data.join('\n'),
      lastEventId
    })
  }

  function drainBuffer() {
    buffer = buffer.replace(/\r\n/g, '\n')
    let separatorIndex = buffer.indexOf('\n\n')
    while (separatorIndex >= 0) {
      const block = buffer.slice(0, separatorIndex)
      buffer = buffer.slice(separatorIndex + 2)
      dispatchSseBlock(block)
      separatorIndex = buffer.indexOf('\n\n')
    }
  }

  function scheduleReconnect() {
    if (closed || !reconnect) return
    reconnectTimer = globalThis.setTimeout(connect, reconnectDelay)
  }

  async function connect() {
    if (closed) return
    if (!fetchImpl) {
      dispatchError(new Error('Fetch API is not available'))
      return
    }

    activeController = new AbortController()
    const token = normalizeBearerToken(getToken())
    const headers = {
      Accept: 'text/event-stream'
    }
    if (token) headers.Authorization = token
    if (lastEventId) headers['Last-Event-ID'] = lastEventId

    try {
      const response = await fetchImpl(url, {
        method: 'GET',
        headers,
        cache: 'no-cache',
        signal: activeController.signal
      })

      if (!response.ok) {
        const error = new Error(`SSE request failed with status ${response.status}`)
        error.status = response.status
        throw error
      }
      if (!response.body?.getReader) {
        throw new Error('SSE response body is not readable')
      }

      readyState = 1
      const reader = response.body.getReader()
      try {
        while (!closed) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          drainBuffer()
        }
      } finally {
        reader.releaseLock?.()
      }

      if (!closed) scheduleReconnect()
    } catch (error) {
      if (closed || error?.name === 'AbortError') return
      readyState = 0
      dispatchError(error, error.status)
      if (error.status !== 401) scheduleReconnect()
    }
  }

  const source = {
    CONNECTING: 0,
    OPEN: 1,
    CLOSED: 2,
    get readyState() {
      return readyState
    },
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(callback)
    },
    removeEventListener(type, callback) {
      listeners.get(type)?.delete(callback)
    },
    close() {
      closed = true
      readyState = 2
      if (reconnectTimer) globalThis.clearTimeout(reconnectTimer)
      activeController?.abort()
    }
  }

  if (globalThis.queueMicrotask) {
    globalThis.queueMicrotask(connect)
  } else {
    Promise.resolve().then(connect)
  }
  return source
}
