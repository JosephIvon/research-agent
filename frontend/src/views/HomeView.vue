<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <div class="logo-mark">
          <el-icon><DataAnalysis /></el-icon>
        </div>
        <div>
          <span class="title">{{ t('message.app_name') }}</span>
          <span class="subtitle">{{ t('message.app_subtitle') }}</span>
        </div>
      </div>
      <div class="header-right">
        <ThemeToggle />
        <el-button text @click="$router.push('/reports')">{{ t('message.history') }}</el-button>
        <el-button text @click="$router.push('/sync')">{{ t('message.sync') }}</el-button>
        <el-button text @click="$router.push('/settings')">{{ t('message.settings') }}</el-button>
        <LanguageSwitcher />
      </div>
    </header>

    <main class="layout-main">
      <section class="workspace">
        <div class="workspace-main">
          <section class="brief-panel">
            <div class="section-kicker">{{ t('message.home_title') }}</div>
            <h1>{{ t('message.home_brief_heading') }}</h1>
            <p>{{ t('message.home_brief_desc') }}</p>

            <el-input
              v-model="query"
              type="textarea"
              :rows="5"
              :placeholder="t('message.query_placeholder')"
              resize="none"
              class="query-input"
            />

            <div class="quick-templates" aria-label="快速示例">
              <button type="button" @click="fillTemplate('对比多模态 AI API 聚合平台，重点分析模型覆盖、接口稳定性、价格和开发者体验')">
                <el-icon><Connection /></el-icon>
                多模态 API 聚合
              </button>
              <button type="button" @click="fillTemplate('分析 AI 助手产品的定价策略，比较套餐、用量限制和企业版能力')">
                <el-icon><Money /></el-icon>
                定价策略
              </button>
              <button type="button" @click="fillTemplate('对比开发者平台的技术架构和接入流程，输出产品机会点')">
                <el-icon><Tools /></el-icon>
                技术架构
              </button>
            </div>
          </section>

          <section class="targets-panel">
            <div class="panel-heading">
              <div>
                <div class="section-kicker">{{ t('message.competitor_urls') }}</div>
                <h2>{{ t('message.specify_competitors') }}</h2>
              </div>
              <el-button
                :disabled="competitorSites.length >= MAX_SITES"
                @click="addSite"
              >
                <el-icon><Plus /></el-icon>
                {{ t('message.add_competitor') }}
              </el-button>
            </div>

            <div class="target-list">
              <article
                v-for="(site, index) in competitorSites"
                :key="site.id"
                class="target-site"
              >
                <div class="target-topline">
                  <div class="target-index">{{ index + 1 }}</div>
                  <el-input
                    v-model="site.url"
                    placeholder="https://example.com/product"
                    clearable
                    class="site-url-input"
                    @keyup.enter="addSite"
                  >
                    <template #prefix>
                      <el-icon><Link /></el-icon>
                    </template>
                  </el-input>
                  <el-switch
                    v-model="site.authEnabled"
                    inline-prompt
                    active-text="登录"
                    inactive-text="公开"
                  />
                  <el-button
                    v-if="competitorSites.length > 1"
                    text
                    class="icon-btn"
                    aria-label="删除网站"
                    @click="removeSite(index)"
                  >
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>

                <div v-if="site.authEnabled" class="auth-grid">
                  <el-input v-model="site.loginUrl" placeholder="登录页 URL">
                    <template #prefix>
                      <el-icon><Lock /></el-icon>
                    </template>
                  </el-input>
                  <el-input v-model="site.username" placeholder="用户名 / 邮箱">
                    <template #prefix>
                      <el-icon><User /></el-icon>
                    </template>
                  </el-input>
                  <el-input v-model="site.password" type="password" placeholder="密码" show-password>
                    <template #prefix>
                      <el-icon><Key /></el-icon>
                    </template>
                  </el-input>
                </div>
              </article>
            </div>
          </section>

          <section class="advanced-panel">
            <button type="button" class="advanced-toggle" @click="showAdvanced = !showAdvanced">
              <span>
                <el-icon><Setting /></el-icon>
                {{ t('message.advanced_options') }}
              </span>
              <el-icon><component :is="showAdvanced ? ArrowUp : ArrowDown" /></el-icon>
            </button>
            <div v-show="showAdvanced" class="advanced-content">
              <div class="advanced-row">
                <div>
                  <strong>{{ t('message.enable_search') }}</strong>
                  <p>{{ t('message.auto_search_hint') }}</p>
                </div>
                <el-switch v-model="enableAutoSearch" />
              </div>
            </div>
          </section>
        </div>

        <aside class="run-panel">
          <div class="run-card">
            <div class="section-kicker">{{ t('message.task_this_run') }}</div>
            <dl>
              <div>
                <dt>{{ t('message.specified_sites') }}</dt>
                <dd>{{ filledSiteCount }} / {{ MAX_SITES }}</dd>
              </div>
              <div>
                <dt>{{ t('message.needs_login') }}</dt>
                <dd>{{ loginSiteCount }}</dd>
              </div>
              <div>
                <dt>{{ t('message.auto_search') }}</dt>
                <dd>{{ enableAutoSearch ? t('message.on') : t('message.off') }}</dd>
              </div>
            </dl>

            <div class="deliverable-panel">
              <div class="deliverable-title">{{ t('message.this_deliverable') }}</div>
              <label class="deliverable-option locked">
                <span class="option-icon">
                  <el-icon><Document /></el-icon>
                </span>
                <span>
                  <strong>{{ t('message.competitor_report') }}</strong>
                  <em>{{ t('message.report_default_desc') }}</em>
                </span>
                <el-checkbox :model-value="true" disabled />
              </label>
              <label class="deliverable-option">
                <span class="option-icon">
                  <el-icon><DocumentChecked /></el-icon>
                </span>
                <span>
                  <strong>{{ t('message.prd_product_doc') }}</strong>
                  <em>{{ t('message.prd_auto_generate') }}</em>
                </span>
                <el-checkbox v-model="deliverables.prd" />
              </label>
            </div>

            <el-button
              type="primary"
              size="large"
              :loading="isSubmitting"
              class="start-btn"
              @click="startResearch"
            >
              <el-icon><Search /></el-icon>
              {{ t('message.start_research') }}
            </el-button>
          </div>
        </aside>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from '../i18n'
import { ElMessage } from 'element-plus'
import {
  ArrowDown,
  ArrowUp,
  Connection,
  DataAnalysis,
  Delete,
  Document,
  DocumentChecked,
  Key,
  Link,
  Lock,
  Money,
  Plus,
  Search,
  Setting,
  Tools,
  User
} from '@element-plus/icons-vue'
import LanguageSwitcher from '../i18n/LanguageSwitcher.vue'
import ThemeToggle from '../components/ThemeToggle.vue'
import { useResearchStore } from '../stores/research'

const { t } = useI18n()

const MAX_SITES = 10

const router = useRouter()
const route = useRoute()
const store = useResearchStore()

const query = ref('')
const competitorSites = ref([createSite()])
const showAdvanced = ref(false)
const enableAutoSearch = ref(true)
const deliverables = ref({
  report: true,
  prd: true
})
const isSubmitting = ref(false)

const filledSiteCount = computed(() => competitorSites.value.filter(site => site.url.trim()).length)
const loginSiteCount = computed(() => competitorSites.value.filter(site => site.url.trim() && site.authEnabled).length)

function createSite() {
  return {
    id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    url: '',
    authEnabled: false,
    loginUrl: '',
    username: '',
    password: ''
  }
}

function fillTemplate(template) {
  query.value = template
}

function addSite() {
  if (competitorSites.value.length >= MAX_SITES) return
  competitorSites.value.push(createSite())
}

function removeSite(index) {
  competitorSites.value.splice(index, 1)
}

function buildTargetSites() {
  return competitorSites.value
    .map(site => ({
      url: site.url.trim(),
      login_url: site.authEnabled ? site.loginUrl.trim() : '',
      username: site.authEnabled ? site.username.trim() : '',
      password: site.authEnabled ? site.password : '',
      authEnabled: site.authEnabled
    }))
    .filter(site => site.url)
}

function validateTargetSites(targetSites) {
  for (const site of targetSites) {
    const hasPartialAuth = site.authEnabled || site.login_url || site.username || site.password
    const hasCompleteAuth = site.login_url && site.username && site.password
    if (hasPartialAuth && !hasCompleteAuth) {
      ElMessage.warning('开启登录的网站需要填写登录页 URL、用户名和密码')
      return false
    }
  }
  if (!enableAutoSearch.value && targetSites.length === 0) {
    ElMessage.warning('关闭自动搜索时，至少需要填写一个竞品网站')
    return false
  }
  return true
}

async function startResearch() {
  if (!query.value.trim()) {
    ElMessage.warning('请输入调研主题')
    return
  }

  const targetSites = buildTargetSites()
  if (!validateTargetSites(targetSites)) return

  isSubmitting.value = true
  try {
    const normalizedSites = targetSites.map(site => ({
      url: site.url,
      login_url: site.authEnabled ? site.login_url : undefined,
      auth_credentials: site.authEnabled ? {
        username: site.username,
        password: site.password
      } : undefined
    }))

    const params = {
      query: query.value.trim(),
      urls: normalizedSites.map(site => site.url),
      target_sites: normalizedSites,
      enable_search: enableAutoSearch.value
    }

    const run = await store.createAsyncTask(params, {
      deliverables: deliverables.value
    })
    router.push(`/progress/${run.id}`)
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
  background: #f4f7fb;
}

.layout-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 28px;
  background: rgba(255, 255, 255, 0.94);
  border-bottom: 1px solid #dbe3ee;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo-mark {
  width: 36px;
  height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: #fff;
  background: #1f6feb;
}

.title {
  display: block;
  font-size: 16px;
  font-weight: 700;
  color: #172033;
}

.subtitle {
  display: block;
  font-size: 12px;
  color: #64748b;
}

.header-right {
  display: flex;
  gap: 8px;
}

.layout-main {
  flex: 1;
  padding: 32px 28px 48px;
}

.workspace {
  width: min(1180px, 100%);
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 24px;
  align-items: start;
}

.workspace-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.brief-panel,
.targets-panel,
.advanced-panel,
.run-card {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
}

.brief-panel,
.targets-panel,
.run-card {
  padding: 24px;
}

.section-kicker {
  font-size: 12px;
  font-weight: 700;
  color: #2563eb;
  margin-bottom: 8px;
}

h1,
h2 {
  letter-spacing: 0;
  color: #172033;
}

h1 {
  font-size: 28px;
  line-height: 1.25;
  margin: 0 0 8px;
}

h2 {
  font-size: 18px;
  margin: 0;
}

.brief-panel p {
  color: #64748b;
  margin: 0 0 18px;
}

.query-input {
  :deep(.el-textarea__inner) {
    min-height: 148px;
    padding: 16px 18px;
    font-size: 15px;
    border-radius: 8px;
    border-color: #cfd8e6;
    box-shadow: none;
  }
}

.quick-templates {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 14px;

  button {
    min-height: 40px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 0 14px;
    border-radius: 8px;
    border: 1px solid #dbe3ee;
    background: #fff;
    color: #334155;
    cursor: pointer;
    transition: border-color 0.16s ease, background-color 0.16s ease, transform 0.16s ease;

    &:hover {
      border-color: #93b4ef;
      background: #f7faff;
    }

    &:active {
      transform: scale(0.98);
    }
  }
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.target-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.target-site {
  padding: 14px;
  border-radius: 8px;
  background: #f8fbff;
  box-shadow: inset 0 0 0 1px #dbe6f4;
}

.target-topline {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 82px 40px;
  gap: 10px;
  align-items: center;
}

.target-index {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #e8f1ff;
  color: #1d4ed8;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.site-url-input {
  :deep(.el-input__wrapper) {
    min-height: 40px;
    border-radius: 8px;
    box-shadow: 0 0 0 1px #cfd8e6 inset;
  }
}

.icon-btn {
  width: 40px;
  height: 40px;
  padding: 0;
}

.auth-grid {
  display: grid;
  grid-template-columns: 1.3fr 1fr 1fr;
  gap: 10px;
  margin-top: 12px;

  :deep(.el-input__wrapper) {
    min-height: 40px;
    border-radius: 8px;
  }
}

.advanced-panel {
  overflow: hidden;
}

.advanced-toggle {
  width: 100%;
  min-height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  border: 0;
  background: #eef3f9;
  color: #334155;
  cursor: pointer;

  span {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
  }
}

.advanced-content {
  padding: 18px 20px;
}

.advanced-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;

  strong {
    color: #172033;
  }

  p {
    color: #64748b;
    margin: 4px 0 0;
  }
}

.run-panel {
  position: sticky;
  top: 24px;
}

.run-card dl {
  margin: 8px 0 20px;
}

.run-card dl > div {
  display: flex;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid #edf2f7;
}

.run-card dt {
  color: #64748b;
}

.run-card dd {
  margin: 0;
  color: #172033;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.deliverable-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  margin-bottom: 18px;
  border-radius: 8px;
  background: #f8fbff;
  box-shadow: inset 0 0 0 1px #dbe6f4;
}

.deliverable-title {
  color: #172033;
  font-size: 13px;
  font-weight: 700;
}

.deliverable-option {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 28px;
  gap: 10px;
  align-items: center;
  cursor: pointer;
  color: #172033;

  &.locked {
    cursor: default;
  }

  strong,
  em {
    display: block;
    font-style: normal;
    letter-spacing: 0;
  }

  strong {
    font-size: 14px;
  }

  em {
    margin-top: 2px;
    color: #64748b;
    font-size: 12px;
  }
}

.option-icon {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: #1d4ed8;
  background: #e8f1ff;
}

.start-btn {
  width: 100%;
  height: 44px;
  border-radius: 8px;
  font-size: 15px;
}

@media (max-width: 900px) {
  .layout-header {
    align-items: flex-start;
    gap: 12px;
    flex-direction: column;
  }

  .workspace {
    grid-template-columns: 1fr;
  }

  .run-panel {
    position: static;
  }

  .target-topline,
  .auth-grid {
    grid-template-columns: 1fr;
  }

  .target-index {
    display: none;
  }
}
</style>
