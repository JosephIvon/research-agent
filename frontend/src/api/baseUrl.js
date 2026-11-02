const DEFAULT_API_BASE_URL = ''

// 开发态的默认 API 前缀：走 vite 代理（vite.config.js 里 /api → 8080，转发时去掉 /api）。
// 空串在 dev 下等于"同源绝对路径"，请求打到 vite 自己，被 SPA 回退成 index.html
// （实测 /research/history 返回 200 text/html），接口永远拿不到 JSON。
// SSE 那条路早就是 `(API_BASE_URL || '/api')`——axios 这边跟上即可。
export const DEV_DEFAULT_API_BASE_URL = '/api'

export function resolveApiBaseUrl(rawBaseUrl = '', isDev = false) {
  const raw = String(rawBaseUrl || '').trim()
  if (!raw) return DEFAULT_API_BASE_URL

  if (/^https?:\/\//i.test(raw)) {
    return isDev ? raw.replace(/\/+$/, '') : DEFAULT_API_BASE_URL
  }

  const relativePath = raw.startsWith('/') ? raw : `/${raw}`
  return relativePath.replace(/\/+$/, '') || DEFAULT_API_BASE_URL
}

/** 开发态的 API base：显式配置优先，没配就走 vite 代理的 /api 前缀。 */
export function resolveDevApiBaseUrl(rawBaseUrl = '') {
  const raw = String(rawBaseUrl || '').trim()
  if (!raw) return DEV_DEFAULT_API_BASE_URL
  return resolveApiBaseUrl(raw, true)
}

