const DEFAULT_API_BASE_URL = '/api'

export function resolveApiBaseUrl(rawBaseUrl = '', isDev = false) {
  const raw = String(rawBaseUrl || '').trim()
  if (!raw) return DEFAULT_API_BASE_URL

  if (/^https?:\/\//i.test(raw)) {
    return isDev ? raw.replace(/\/+$/, '') : DEFAULT_API_BASE_URL
  }

  const relativePath = raw.startsWith('/') ? raw : `/${raw}`
  return relativePath.replace(/\/+$/, '') || DEFAULT_API_BASE_URL
}

