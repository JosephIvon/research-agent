import axios from 'axios'
import { ElMessage } from 'element-plus'
import { resolveApiBaseUrl } from './baseUrl'
import { createAuthorizedEventSource } from './sse'

const DEV_API_BASE_URL = import.meta.env.DEV ? import.meta.env.VITE_API_BASE_URL : ''
const API_BASE_URL = resolveApiBaseUrl(DEV_API_BASE_URL, import.meta.env.DEV)

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json'
  }
})

api.interceptors.request.use(config => {
  const token = sessionStorage.getItem('auth_token')
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  response => response.data,
  error => {
    const detail = error.response?.data?.detail || error.response?.data?.error
    const message = Array.isArray(detail)
      ? detail.map(item => item.msg).join('；')
      : (detail || error.message || '请求失败')
    ElMessage.error(message)
    if (error.response?.status === 401 && !['/login', '/settings'].includes(window.location.pathname)) {
      const redirect = encodeURIComponent(`${window.location.pathname}${window.location.search}`)
      window.location.assign(`/login?redirect=${redirect}`)
    }
    return Promise.reject(error)
  }
)

export const researchApi = {
  followup: (params) => api.post('/research/followup', params),

  suggestFollowup: (params) => api.post('/research/suggest-followup', params),

  multiRoleReview: (params) => api.post('/research/multi-role-review', params),

  prd: (params) => api.post('/research/prd', params),

  prdFromQuery: (params) => api.post('/research/prd-from-query', params),

  history: () => api.get('/research/history'),

  getReport: (id) => api.get(`/research/history/${encodeURIComponent(id)}`),

  deleteReport: (id) => api.delete(`/research/history/${encodeURIComponent(id)}`),

  // Async task queue API
  createTask: (params) => api.post('/research/tasks', params),

  getTask: (taskId) => api.get(`/research/tasks/${taskId}`),

  getTaskEvents: (taskId) => {
    const base = (API_BASE_URL || '/api').replace(/\/$/, '')
    return createAuthorizedEventSource(`${base}/research/tasks/${encodeURIComponent(taskId)}/events`)
  }
}

export const syncApi = {
  feishu: (params) => api.post('/sync/feishu', params),

  tencent: (params) => api.post('/sync/tencent', params),

  status: () => api.get('/sync/status')
}

export const settingsApi = {
  get: () => api.get('/settings')
}

export const authApi = {
  register: (params) => api.post('/auth/register', params),
  login: (params) => api.post('/auth/login', params),
  me: () => api.get('/auth/me')
}

export default api
