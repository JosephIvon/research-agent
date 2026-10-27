<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">← 返回</el-button>
        <span class="page-title">设置</span>
      </div>
    </header>

    <main class="layout-main">
      <div class="settings-container">
        <div class="settings-section card">
          <div class="card-header">运行状态</div>
          <div class="status-grid">
            <div class="status-row">
              <span>环境</span>
              <el-tag>{{ settings.app_env }}</el-tag>
            </div>
            <div class="status-row">
              <span>LLM</span>
              <el-tag :type="settings.llm_configured ? 'success' : 'danger'">
                {{ settings.llm_provider }} / {{ settings.llm_configured ? '已配置' : '未配置' }}
              </el-tag>
            </div>
            <div class="status-row">
              <span>API 鉴权</span>
              <el-tag :type="settings.api_auth_enabled ? 'success' : 'info'">
                {{ settings.api_auth_enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
          </div>
        </div>

        <div class="settings-section card">
          <div class="card-header">搜索配置</div>
          <div class="status-grid">
            <div class="status-row">
              <span>搜索提供商</span>
              <el-tag type="success">{{ settings.search_provider }}</el-tag>
            </div>
            <div class="status-row">
              <span>SearXNG</span>
              <span class="mono">{{ settings.searxng_url }}</span>
            </div>
            <div class="status-row">
              <span>结果上限</span>
              <span>{{ settings.search_max_results }}</span>
            </div>
            <div class="status-row">
              <span>搜索超时</span>
              <span>{{ settings.search_timeout }} 秒</span>
            </div>
          </div>
        </div>

        <div class="settings-section card">
          <div class="card-header">文档平台</div>
          <div class="status-grid">
            <div class="status-row">
              <span>飞书文档</span>
              <el-tag :type="settings.feishu_configured ? 'success' : 'info'">
                {{ settings.feishu_configured ? '已配置' : '未配置' }}
              </el-tag>
            </div>
            <div class="status-row">
              <span>腾讯文档</span>
              <el-tag :type="settings.tencent_configured ? 'success' : 'info'">
                {{ settings.tencent_configured ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </div>
          <div class="form-hint">
            未配置时同步功能不可用。此前这里只有状态标签、没有输入框，
            「连接」按钮把人指向一个**无法完成配置**的页面。
          </div>
          <div class="form-group">
            <label>飞书 App ID / App Secret</label>
            <div class="credential-row">
              <el-input v-model="feishuAppId" placeholder="App ID" />
              <el-input v-model="feishuAppSecret" type="password" show-password placeholder="App Secret" />
              <el-button type="primary" @click="saveCredential('feishu')">保存</el-button>
            </div>
            <div class="form-hint">
              {{ credentialHint('feishu') }}
            </div>
          </div>
          <div class="form-group">
            <label>腾讯文档 App ID / App Secret</label>
            <div class="credential-row">
              <el-input v-model="tencentAppId" placeholder="App ID" />
              <el-input v-model="tencentAppSecret" type="password" show-password placeholder="App Secret" />
              <el-button type="primary" @click="saveCredential('tencent')">保存</el-button>
            </div>
            <div class="form-hint">
              {{ credentialHint('tencent') }}
            </div>
          </div>
        </div>

        <div class="settings-section card">
          <div class="card-header">浏览器会话 Token</div>
          <div class="form-group">
            <label>Bearer Token</label>
            <el-input v-model="authToken" type="password" show-password placeholder="输入后用于后续 API 请求" />
            <div class="form-hint">Token 只保存在当前浏览器会话，关闭标签页后失效。</div>
          </div>
        </div>

        <div class="settings-section card">
          <div class="card-header">运行参数</div>
          <div class="status-grid">
            <div class="status-row">
              <span>最大重试次数</span>
              <span>{{ settings.max_retries }}</span>
            </div>
            <div class="status-row">
              <span>抓取超时</span>
              <span>{{ settings.timeout_seconds }} 秒</span>
            </div>
            <div class="status-row">
              <span>最大 URL 数</span>
              <span>{{ settings.max_fetch_urls }}</span>
            </div>
            <div class="status-row">
              <span>私网 URL</span>
              <el-tag :type="settings.allow_private_network_urls ? 'warning' : 'success'">
                {{ settings.allow_private_network_urls ? '允许' : '阻断' }}
              </el-tag>
            </div>
          </div>
        </div>

        <div class="settings-actions">
          <el-button type="primary" @click="saveToken">保存 Token</el-button>
          <el-button @click="clearToken">清除 Token</el-button>
          <el-button @click="loadSettings">刷新状态</el-button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { settingsApi, syncApi } from '../api'

const settings = ref({
  app_env: 'development',
  llm_provider: 'minimax',
  llm_configured: false,
  minimax_configured: false,
  api_auth_enabled: false,
  search_provider: 'searxng',
  searxng_url: '',
  search_max_results: 10,
  search_timeout: 15,
  feishu_configured: false,
  tencent_configured: false,
  max_retries: 3,
  timeout_seconds: 60,
  max_fetch_urls: 10,
  allow_private_network_urls: false
})

const authToken = ref(sessionStorage.getItem('auth_token') || '')

// 文档平台凭据：只有管理员能写。secret 不回显——服务端只给掩码后的 app_id。
// 状态走公开的 GET /settings（同样是掩码），这样非管理员打开设置页不会撞 403。
const feishuAppId = ref('')
const feishuAppSecret = ref('')
const tencentAppId = ref('')
const tencentAppSecret = ref('')

function credentialHint(platform) {
  const configured = platform === 'feishu'
    ? settings.value.feishu_configured
    : settings.value.tencent_configured
  const masked = platform === 'feishu'
    ? settings.value.feishu_app_id_masked
    : settings.value.tencent_app_id_masked
  if (!configured) return '未配置。未配置时同步功能不可用。'
  return `已配置（${masked || '凭据已就绪'}）。填写新的值即可覆盖。`
}

async function saveCredential(platform) {
  const appId = platform === 'feishu' ? feishuAppId.value.trim() : tencentAppId.value.trim()
  const appSecret = platform === 'feishu' ? feishuAppSecret.value.trim() : tencentAppSecret.value.trim()
  if (!appId || !appSecret) {
    ElMessage.warning('App ID 与 App Secret 都要填')
    return
  }
  try {
    await syncApi.saveCredentials({ platform, app_id: appId, app_secret: appSecret })
    if (platform === 'feishu') {
      feishuAppSecret.value = ''
    } else {
      tencentAppSecret.value = ''
    }
    ElMessage.success('凭据已保存，同步立即生效')
    await loadSettings()
  } catch (e) {
    // 错误提示由 axios 拦截器统一给出（403 会显示「需要管理员权限」）
  }
}

async function loadSettings() {
  try {
    const data = await settingsApi.get()
    settings.value = { ...settings.value, ...data }
  } catch (e) {
    ElMessage.error('加载设置失败')
  }
}

function saveToken() {
  if (authToken.value.trim()) {
    sessionStorage.setItem('auth_token', authToken.value.trim())
  } else {
    sessionStorage.removeItem('auth_token')
  }
  ElMessage.success('Token 已保存')
}

function clearToken() {
  authToken.value = ''
  sessionStorage.removeItem('auth_token')
  ElMessage.success('Token 已清除')
}

onMounted(loadSettings)
</script>

<style lang="scss" scoped>
.layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.layout-header {
  display: flex;
  align-items: center;
  padding: 16px 24px;
  background: #fff;
  border-bottom: 1px solid var(--border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.layout-main {
  flex: 1;
  padding: 24px;
}

.settings-container {
  max-width: 680px;
  margin: 0 auto;
}

.settings-section {
  margin-bottom: 16px;

  .form-group {
    margin-bottom: 16px;

    label {
      display: block;
      font-size: 14px;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }

    .form-hint {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 4px;
    }
  }
}

.status-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 凭据一行：App ID + Secret + 保存，窄屏折行 */
.credential-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;

  .el-input {
    flex: 1 1 180px;
  }
}

.status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 14px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--text-secondary);
  word-break: break-all;
}

.settings-actions {
  display: flex;
  justify-content: center;
  gap: 12px;
  margin-top: 24px;
}
</style>
