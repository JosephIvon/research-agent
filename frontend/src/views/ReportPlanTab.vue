<template>
  <section class="plan-body">
    <div class="plan-header">
      <div>
        <h2>调研口径</h2>
        <p>这一轮的主体是谁、竞品怎么定义、权重怎么算、候选怎么选——全部来自任务终态落库的结构化记录。</p>
      </div>
      <el-tag :type="headerTagType" effect="light">{{ headerTagText }}</el-tag>
    </div>

    <el-alert
      v-if="plan.isDefault"
      class="plan-alert"
      type="warning"
      show-icon
      :closable="false"
      title="默认口径"
      :description="plan.sourceNote"
    />

    <el-alert
      v-if="plan.error"
      class="plan-alert"
      type="info"
      show-icon
      :closable="false"
      title="计划生成过程有降级"
      :description="plan.error"
    />

    <ResearchPlanPanel v-if="plan.available" :plan="plan" />

    <div v-else class="plan-empty">
      <h3>本次未生成研究计划</h3>
      <p>{{ plan.emptyText }}</p>
    </div>

    <AngleWeightBars v-if="plan.angles.length" :angles="plan.angles" :note="plan.weightNote" />

    <DiscoveryTrail v-if="discovery.available" :discovery="discovery" />

    <div v-else class="plan-empty">
      <h3>本轮未走候选发现流程</h3>
      <p>{{ discovery.emptyText }}</p>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import AngleWeightBars from '../components/AngleWeightBars.vue'
import DiscoveryTrail from '../components/DiscoveryTrail.vue'
import ResearchPlanPanel from '../components/ResearchPlanPanel.vue'
import { normalizeDiscovery, normalizeResearchPlan } from '../stores/researchTaskHelpers'

// 组件只接收**原始**结构化数据，取数与格式化统一走 helpers——
// 这样字段缺失的兜底、旧报告的空态都能被 node --test 覆盖。
const props = defineProps({
  researchPlan: {
    type: Object,
    default: null
  },
  discovery: {
    type: Object,
    default: null
  }
})

const plan = computed(() => normalizeResearchPlan(props.researchPlan))
const discovery = computed(() => normalizeDiscovery(props.discovery))

const headerTagText = computed(() => {
  if (!plan.value.available) return '无研究计划'
  return plan.value.sourceLabel
})

const headerTagType = computed(() => {
  if (!plan.value.available) return 'info'
  return plan.value.isDefault ? 'warning' : 'success'
})
</script>

<style lang="scss" scoped>
.plan-body {
  min-height: 360px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 22px;
  border-radius: 10px;
  background: var(--bg-card);
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
}

.plan-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;

  h2 {
    margin: 0 0 6px;
    color: var(--text-primary);
    font-size: 20px;
  }

  p {
    margin: 0;
    max-width: 720px;
    color: var(--text-secondary);
  }
}

.plan-alert {
  border-radius: 8px;
}

.plan-empty {
  padding: 22px;
  border-radius: 8px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);

  h3 {
    margin: 0 0 6px;
    color: var(--text-primary);
    font-size: 16px;
  }

  p {
    margin: 0;
    color: var(--text-secondary);
  }
}

@media (max-width: 960px) {
  .plan-header {
    flex-direction: column;
  }
}
</style>
