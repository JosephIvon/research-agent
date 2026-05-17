<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <span class="logo">🧠</span>
        <span class="title">智能调研助手</span>
      </div>
      <div class="header-right">
        <el-button text @click="$router.push('/reports')">历史报告</el-button>
        <el-button text @click="$router.push('/sync')">同步</el-button>
        <el-button text @click="$router.push('/settings')">设置</el-button>
      </div>
    </header>

    <main class="layout-main">
      <div class="home-container">
        <div class="search-section">
          <h1 class="search-title">开启智能竞品调研</h1>
          <p class="search-subtitle">输入您想了解的问题，AI将自动为您收集和分析竞品信息</p>

          <div class="search-box">
            <el-input
              v-model="query"
              type="textarea"
              :rows="3"
              placeholder="我想了解..."
              resize="none"
              class="search-input"
            />
          </div>

          <div class="quick-templates">
            <div class="template-label">快捷示例</div>
            <div class="template-cards">
              <div class="template-card" @click="fillTemplate('对比在线文档产品：Notion、Obsidian、飞书文档的功能和定价')">
                <span class="template-icon">📱</span>
                <span class="template-text">SaaS产品对比</span>
              </div>
              <div class="template-card" @click="fillTemplate('分析AI助手的定价策略：ChatGPT、Claude、 Gemini的收费模式')">
                <span class="template-icon">💰</span>
                <span class="template-text">定价策略分析</span>
              </div>
              <div class="template-card" @click="fillTemplate('对比低代码平台的技术架构：OutSystems Mendix 的优劣势')">
                <span class="template-icon">🔧</span>
                <span class="template-text">技术架构对比</span>
              </div>
            </div>
          </div>

          <div class="url-section">
            <div class="section-header" @click="showUrls = !showUrls">
              <span>📎 指定竞品网站（可选）</span>
              <span class="toggle-icon">{{ showUrls ? '▲' : '▼' }}</span>
            </div>
            <div v-show="showUrls" class="url-inputs">
              <el-tag
                v-for="(url, index) in urls"
                :key="index"
                closable
                @close="removeUrl(index)"
                class="url-tag"
              >
                {{ url }}
              </el-tag>
              <el-input
                v-if="urls.length < 10"
                v-model="newUrl"
                placeholder="输入URL后按回车添加"
                @keyup.enter="addUrl"
                class="url-input"
              />
            </div>
          </div>

          <div class="advanced-section">
            <div class="section-header" @click="showAdvanced = !showAdvanced">
              <span>▼ 高级选项</span>
            </div>
            <div v-show="showAdvanced" class="advanced-content">
              <div class="option-row">
                <el-switch v-model="disableAutoSearch" />禁用自动搜索
              </div>
              <div class="option-group">
                <div class="option-label">登录凭据（用于需要登录的网站）</div>
                <el-input v-model="loginUrl" placeholder="登录页 URL" />
                <el-input v-model="loginUsername" placeholder="用户名" />
                <el-input v-model="loginPassword" type="password" placeholder="密码" show-password />
              </div>
            </div>
          </div>

          <el-button type="primary" size="large" :loading="isSubmitting" @click="startResearch" class="start-btn">
            🚀 开始调研
          </el-button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useResearchStore } from '../stores/research'

const router = useRouter()
const route = useRoute()
const store = useResearchStore()

const query = ref('')
const urls = ref([])
const newUrl = ref('')
const showUrls = ref(false)
const showAdvanced = ref(false)
const disableAutoSearch = ref(false)
const loginUrl = ref('')
const loginUsername = ref('')
const loginPassword = ref('')
const isSubmitting = ref(false)

function fillTemplate(template) {
  query.value = template
}

function addUrl() {
  const url = newUrl.value.trim()
  if (url && !urls.value.includes(url)) {
    urls.value.push(url)
    newUrl.value = ''
  }
}

function removeUrl(index) {
  urls.value.splice(index, 1)
}

async function startResearch() {
  if (!query.value.trim()) {
    ElMessage.warning('请输入调研主题')
    return
  }

  isSubmitting.value = true
  try {
    const params = {
      query: query.value,
      urls: urls.value.length > 0 ? urls.value : undefined,
      enable_search: !disableAutoSearch.value,
      login_url: loginUrl.value.trim() || undefined,
      auth_credentials: (loginUsername.value && loginPassword.value) ? {
        username: loginUsername.value,
        password: loginPassword.value
      } : undefined
    }

    const result = await store.createTask(params)
    router.push(`/report/${result.id}`)
  } catch (e) {
    ElMessage.error('提交调研失败')
  } finally {
    isSubmitting.value = false
  }
}

onMounted(() => {
  if (route.query.query) {
    query.value = String(route.query.query)
  }
})
</script>

<style lang="scss" scoped>
.layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.layout-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: #fff;
  border-bottom: 1px solid var(--border);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  .logo { font-size: 24px; }
  .title { font-size: 18px; font-weight: 600; }
}

.header-right {
  display: flex;
  gap: 8px;
}

.layout-main {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 48px 24px;
}

.home-container {
  width: 100%;
  max-width: 800px;
}

.search-section {
  text-align: center;
}

.search-title {
  font-size: 32px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.search-subtitle {
  font-size: 16px;
  color: var(--text-secondary);
  margin-bottom: 32px;
}

.search-box {
  margin-bottom: 24px;
}

.search-input {
  :deep(.el-textarea__inner) {
    font-size: 16px;
    padding: 16px 20px;
    border: 2px solid var(--border);
    border-radius: 12px;
    &:focus { border-color: var(--primary); }
  }
}

.quick-templates {
  margin-bottom: 32px;
}

.template-label {
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}

.template-cards {
  display: flex;
  gap: 12px;
  justify-content: center;
}

.template-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
  &:hover {
    border-color: var(--primary);
    box-shadow: 0 2px 8px rgba(79, 70, 229, 0.1);
  }
  .template-icon { font-size: 18px; }
  .template-text { font-size: 14px; color: var(--text-primary); }
}

.url-section, .advanced-section {
  text-align: left;
  margin-bottom: 16px;
  background: #fff;
  border-radius: 12px;
  overflow: hidden;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  cursor: pointer;
  font-size: 14px;
  color: var(--text-secondary);
  &:hover { background: var(--bg-hover); }
}

.url-inputs {
  padding: 0 20px 16px;
}

.url-tag {
  margin-right: 8px;
  margin-bottom: 8px;
}

.url-input {
  width: 300px;
}

.advanced-content {
  padding: 0 20px 16px;
}

.option-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 14px;
}

.option-group {
  .option-label {
    font-size: 12px;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }
  .el-input { margin-bottom: 8px; }
}

.start-btn {
  width: 200px;
  height: 48px;
  font-size: 16px;
  border-radius: 24px;
  margin-top: 16px;
}
</style>
