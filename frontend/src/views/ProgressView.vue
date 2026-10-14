<template>
  <div class="layout">
    <header class="layout-header">
      <div class="header-left">
        <el-button text @click="$router.push('/')">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <span class="task-title">AI 调研驾驶舱</span>
      </div>
    </header>

    <main class="layout-main">
      <section v-if="run" class="cockpit">
        <aside class="status-panel">
          <div class="status-kicker">{{ statusKicker }}</div>
          <h1>{{ run.params.query }}</h1>
          <p>{{ headline }}</p>

          <dl class="task-meta">
            <div>
              <dt>指定网站</dt>
              <dd>{{ summary.siteCount }} 个</dd>
            </div>
            <div>
              <dt>需要登录</dt>
              <dd>{{ summary.loginCount }} 个</dd>
            </div>
            <div>
              <dt>自动搜索</dt>
              <dd>{{ summary.autoSearchText }}</dd>
            </div>
            <div>
              <dt>交付物</dt>
              <dd>{{ deliverableText }}</dd>
            </div>
          </dl>

          <div class="status-actions">
            <el-button v-if="canOpenReport" type="primary" @click="openReport">
              打开结果中心
            </el-button>
            <el-button v-else-if="run.status === 'failed'" type="primary" @click="retryRun">
              重新执行
            </el-button>
            <el-button @click="$router.push('/')">回到首页</el-button>
          </div>
        </aside>

        <section class="work-panel">
          <div class="timeline">
            <article
              v-for="(step, index) in timeline"
              :key="step.id"
              class="timeline-step"
              :class="stepClass(step, index)"
            >
              <div class="step-marker">
                <el-icon v-if="stepState(step, index) === 'done'"><CircleCheck /></el-icon>
                <el-icon v-else-if="stepState(step, index) === 'error'"><CircleClose /></el-icon>
                <el-icon v-else-if="stepState(step, index) === 'active'" class="spin"><Loading /></el-icon>
                <el-icon v-else><Clock /></el-icon>
              </div>
              <div class="step-body">
                <h2>{{ step.title }}</h2>
                <p>{{ step.detail }}</p>
              </div>
            </article>
          </div>

          <el-collapse v-if="agentSteps.length" v-model="activeAgentPanel" class="agent-collapse">
            <el-collapse-item name="steps">
              <template #title>
                <span class="agent-collapse-title">Agent 思考过程</span>
                <span class="agent-collapse-count">{{ agentSteps.length }} 轮</span>
              </template>
              <div class="agent-step-list">
                <div
                  v-for="(item, index) in agentSteps"
                  :key="index"
                  class="agent-step-line"
                >
                  {{ formatAgentStepSummary(item) }}
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div class="activity-panel">
            <div class="panel-heading">
              <div>
                <div class="status-kicker">过程记录</div>
                <h2>系统正在做什么</h2>
              </div>
              <el-tag :type="statusTagType">{{ statusText }}</el-tag>
            </div>

            <div class="event-list">
              <div v-for="event in events" :key="event.id" class="event-item" :class="event.type">
                <span class="event-time">{{ event.time }}</span>
                <span class="event-dot"></span>
                <span class="event-message">{{ event.message }}</span>
              </div>
              <div v-if="!events.length" class="event-empty">
                正在准备任务，请稍候。
              </div>
            </div>
          </div>
        </section>
      </section>

      <section v-else class="missing-state">
        <h1>没有找到这个调研任务</h1>
        <p>任务可能已被清理，或者当前浏览器没有保存这次调研记录。</p>
        <el-button type="primary" @click="$router.push('/')">重新开始调研</el-button>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleCheck,
  CircleClose,
  Clock,
  Loading
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useResearchStore } from '../stores/research'
import {
  formatAgentStepSummary,
  getRunHeadline,
  getRunStatusKicker,
  getRunStatusLabel,
  getRunStatusTagType,
  getTaskInputSummary
} from '../stores/researchTaskHelpers'

const route = useRoute()
const router = useRouter()
const store = useResearchStore()
const taskId = String(route.params.id)
const run = ref(store.getResearchRun(taskId))
const events = ref(run.value?.events || [])
const isExecuting = ref(false)
const activeAgentPanel = ref([])

const timeline = computed(() => run.value?.timeline || [])
const agentSteps = computed(() => run.value?.agent_steps || [])
const summary = computed(() => getTaskInputSummary(run.value?.params || {}))
const currentStageIndex = computed(() => {
  const stage = run.value?.current_stage
  const index = timeline.value.findIndex(step => step.id === stage)
  return index >= 0 ? index : 0
})
const canOpenReport = computed(() => Boolean(run.value?.report_id))
const deliverableText = computed(() => run.value?.deliverables?.prd ? '报告 + PRD' : '报告')
const statusText = computed(() => getRunStatusLabel(run.value?.status))
const statusTagType = computed(() => getRunStatusTagType(run.value?.status))
const statusKicker = computed(() => getRunStatusKicker(run.value?.status))
const headline = computed(() => getRunHeadline(run.value || {}))

function syncRun() {
  run.value = store.getResearchRun(taskId)
  events.value = run.value?.events || []
}

function stepState(step, index) {
  if (run.value?.status === 'failed' && step.id === run.value?.current_stage) return 'error'
  if (run.value?.status === 'completed' || run.value?.status === 'needs_input') return 'done'
  if (index < currentStageIndex.value) return 'done'
  if (index === currentStageIndex.value && run.value?.status === 'running') return 'active'
  return 'pending'
}

function stepClass(step, index) {
  return `is-${stepState(step, index)}`
}

function openReport() {
  router.push({
    path: `/report/${run.value.report_id}`,
    query: { run: taskId }
  })
}

async function executeRun() {
  if (!run.value || isExecuting.value) return
  if (run.value.status === 'completed' || run.value.status === 'needs_input') {
    setTimeout(openReport, 900)
    return
  }
  if (run.value.status === 'failed') return

  isExecuting.value = true
  try {
    const result = await store.runResearchRun(taskId, {
      onEvent: () => syncRun(),
      onComplete: () => {
        syncRun()
        setTimeout(openReport, 900)
      },
      onError: () => syncRun()
    })
    syncRun()
  } catch (e) {
    syncRun()
    ElMessage.error(e?.message || '任务执行失败')
  } finally {
    isExecuting.value = false
  }
}

async function retryRun() {
  store.updateResearchRun(taskId, {
    status: 'pending',
    error: null,
    current_stage: null,
    events: [],
    agent_steps: []
  })
  syncRun()
  await executeRun()
}

onMounted(() => {
  syncRun()
  executeRun()
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
  align-items: center;
  padding: 14px 28px;
  background: rgba(255, 255, 255, 0.94);
  border-bottom: 1px solid #dbe3ee;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 14px;
}

.task-title {
  color: #172033;
  font-size: 16px;
  font-weight: 700;
}

.layout-main {
  flex: 1;
  padding: 32px 28px 48px;
}

.cockpit {
  width: min(1180px, 100%);
  margin: 0 auto;
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}

.status-panel,
.work-panel,
.activity-panel,
.missing-state {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
}

.status-panel {
  position: sticky;
  top: 24px;
  padding: 24px;

  h1 {
    margin: 0 0 10px;
    color: #172033;
    font-size: 24px;
    line-height: 1.3;
    letter-spacing: 0;
  }

  p {
    margin: 0;
    color: #64748b;
  }
}

.status-kicker {
  margin-bottom: 8px;
  color: #2563eb;
  font-size: 12px;
  font-weight: 700;
}

.task-meta {
  margin: 22px 0;

  > div {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 0;
    border-bottom: 1px solid #edf2f7;
  }

  dt {
    color: #64748b;
  }

  dd {
    margin: 0;
    color: #172033;
    font-weight: 700;
  }
}

.status-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;

  .el-button {
    width: 100%;
  }
}

.work-panel {
  padding: 22px;
}

.timeline {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.timeline-step {
  min-height: 118px;
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 12px;
  padding: 16px;
  border-radius: 8px;
  background: #f8fbff;
  box-shadow: inset 0 0 0 1px #dbe6f4;

  h2 {
    margin: 0 0 6px;
    color: #172033;
    font-size: 15px;
    letter-spacing: 0;
  }

  p {
    margin: 0;
    color: #64748b;
  }

  &.is-done {
    background: #f1fbf5;
    box-shadow: inset 0 0 0 1px #bde8ca;
  }

  &.is-active {
    background: #eef6ff;
    box-shadow: inset 0 0 0 1px #93b4ef;
  }

  &.is-error {
    background: #fff5f5;
    box-shadow: inset 0 0 0 1px #fecaca;
  }
}

.step-marker {
  width: 38px;
  height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: #2563eb;
  background: #e8f1ff;
}

.is-done .step-marker {
  color: #15803d;
  background: #dcfce7;
}

.is-error .step-marker {
  color: #dc2626;
  background: #fee2e2;
}

.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.activity-panel {
  padding: 20px;
}

.agent-collapse {
  margin-bottom: 18px;
  border-top: 1px solid #e4e7ed;
  border-bottom: 1px solid #e4e7ed;
}

.agent-collapse-title {
  color: #172033;
  font-weight: 700;
}

.agent-collapse-count {
  margin-left: 10px;
  color: #2563eb;
  font-size: 12px;
}

.agent-step-list {
  max-height: 240px;
  overflow: auto;
}

.agent-step-line {
  padding: 8px 0;
  border-bottom: 1px dashed #edf2f7;
  color: #334155;
  line-height: 1.6;

  &:last-child {
    border-bottom: 0;
  }
}

.panel-heading {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;

  h2 {
    margin: 0;
    color: #172033;
    font-size: 18px;
    letter-spacing: 0;
  }
}

.event-list {
  max-height: 320px;
  overflow: auto;
}

.event-item {
  display: grid;
  grid-template-columns: 78px 12px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  padding: 10px 0;
  border-bottom: 1px solid #edf2f7;
  color: #334155;
}

.event-time {
  color: #64748b;
  font-variant-numeric: tabular-nums;
}

.event-dot {
  width: 8px;
  height: 8px;
  margin-top: 7px;
  border-radius: 999px;
  background: #93b4ef;
}

.event-item.success .event-dot {
  background: #16a34a;
}

.event-item.warning .event-dot {
  background: #f97316;
}

.event-item.error .event-dot {
  background: #ef4444;
}

.event-empty {
  padding: 18px 0;
  color: #64748b;
}

.missing-state {
  width: min(680px, 100%);
  margin: 0 auto;
  padding: 32px;
  text-align: center;

  h1 {
    margin: 0 0 10px;
    color: #172033;
    font-size: 24px;
    letter-spacing: 0;
  }

  p {
    margin: 0 0 18px;
    color: #64748b;
  }
}

@media (max-width: 900px) {
  .layout-main {
    padding: 20px 16px 32px;
  }

  .cockpit,
  .timeline {
    grid-template-columns: 1fr;
  }

  .status-panel {
    position: static;
  }
}
</style>
