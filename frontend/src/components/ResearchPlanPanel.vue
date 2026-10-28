<template>
  <section class="plan-panel">
    <h3>主体与口径</h3>

    <div class="plan-block">
      <div class="plan-block-head">
        <span class="plan-tag">主体</span>
        <strong class="plan-strong">{{ plan.entity.name || '未识别主体' }}</strong>
        <el-tag size="small" type="info" effect="plain">{{ plan.entity.typeLabel }}</el-tag>
        <span class="plan-muted">置信度 {{ plan.entity.confidenceText }}</span>
      </div>
      <p v-if="plan.entity.whatItIs" class="plan-text">{{ plan.entity.whatItIs }}</p>
      <div v-if="plan.entity.candidates.length" class="plan-sub">
        <span class="plan-muted">备选</span>
        <ul class="plan-list">
          <li v-for="(item, index) in plan.entity.candidates" :key="index">{{ item }}</li>
        </ul>
      </div>
      <div v-if="plan.entity.officialDomains.length" class="plan-sub">
        <span class="plan-muted">主体官网</span>
        <ul class="plan-list">
          <li v-for="(item, index) in plan.entity.officialDomains" :key="index">{{ item }}</li>
        </ul>
      </div>
    </div>

    <div class="plan-block">
      <div class="plan-block-head">
        <span class="plan-tag">口径</span>
        <span>层级 {{ plan.criteria.layerText }}</span>
        <span>数量 {{ plan.criteria.countText }}</span>
        <span v-if="plan.criteria.candidatePool">候选池 {{ plan.criteria.candidatePool }}</span>
      </div>
      <p v-if="plan.criteria.admissionRule" class="plan-text">
        入选标准：{{ plan.criteria.admissionRule }}
      </p>
      <div v-if="plan.criteria.mustInclude.length" class="plan-sub">
        <span class="plan-muted">必须包含</span>
        <ul class="plan-list">
          <li v-for="(item, index) in plan.criteria.mustInclude" :key="index">{{ item }}</li>
        </ul>
      </div>
      <div v-if="plan.criteria.mustExclude.length" class="plan-sub">
        <span class="plan-muted">必须排除</span>
        <ul class="plan-list">
          <li v-for="(item, index) in plan.criteria.mustExclude" :key="index">{{ item }}</li>
        </ul>
      </div>
    </div>

    <div v-if="plan.competitorCandidates.length" class="plan-block">
      <div class="plan-block-head">
        <span class="plan-tag">提名候选</span>
        <span class="plan-muted">模型提名，官网与类型仍由代码核验</span>
      </div>
      <ul class="plan-list plan-chips">
        <li v-for="(item, index) in plan.competitorCandidates" :key="index">{{ item }}</li>
      </ul>
    </div>

    <div v-if="plan.openQuestions.length" class="plan-block">
      <div class="plan-block-head">
        <span class="plan-tag">开放问题</span>
      </div>
      <ul class="plan-list">
        <li v-for="(item, index) in plan.openQuestions" :key="index">
          {{ item.question }}
          <span v-if="item.default" class="plan-muted">（默认：{{ item.default }}）</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup>
// 纯展示组件：取数与格式化都在 stores/researchTaskHelpers.js 里
// （normalizeResearchPlan），这里只负责把归一化后的结构画出来。
defineProps({
  plan: {
    type: Object,
    required: true
  }
})
</script>

<style lang="scss" scoped>
.plan-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.plan-panel h3 {
  margin: 0;
  color: var(--text-primary);
  font-size: 15px;
}

.plan-block {
  padding: 14px 16px;
  border-radius: 8px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);
}

.plan-block-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  color: var(--text-primary);
  font-size: 13px;
}

.plan-tag {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--primary);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
}

.plan-strong {
  font-size: 15px;
  font-weight: 700;
}

.plan-muted {
  color: var(--text-secondary);
  font-size: 12px;
}

.plan-text {
  margin: 10px 0 0;
  color: var(--text-secondary);
  line-height: 1.7;
}

.plan-sub {
  display: flex;
  gap: 10px;
  align-items: baseline;
  margin-top: 10px;
}

.plan-list {
  margin: 0;
  padding-left: 18px;
  color: var(--text-secondary);
  line-height: 1.7;
}

.plan-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-left: 0;
  list-style: none;
}

.plan-chips li {
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--bg-card);
  box-shadow: inset 0 0 0 1px var(--border);
  font-size: 12px;
}
</style>
