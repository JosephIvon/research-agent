<template>
  <section class="angle-panel">
    <h3>重点角度</h3>

    <ul class="angle-list">
      <li v-for="angle in angles" :key="angle.angle" class="angle-item">
        <div class="angle-head">
          <strong class="angle-name">{{ angle.label }}</strong>
          <span class="angle-weight">{{ angle.weightText }}</span>
          <el-tag size="small" :type="priorityTagType(angle.priority)" effect="plain">
            {{ angle.priorityLabel }}
          </el-tag>
          <span class="angle-percent">{{ angle.percentText }}</span>
        </div>
        <div class="angle-track" role="presentation">
          <div class="angle-fill" :style="{ width: `${angle.barPercent}%` }"></div>
        </div>
        <p v-if="angle.reason" class="angle-reason">{{ angle.reason }}</p>
        <p v-if="angle.pageHints.length" class="angle-hints">
          站内页面关键词：{{ angle.pageHints.join('、') }}
        </p>
      </li>
    </ul>

    <p class="angle-note">ⓘ {{ note }}</p>
  </section>
</template>

<script setup>
// 纯展示组件：权重归一化与相对刻度都在 stores/researchTaskHelpers.js 里
// （normalizeAngleWeights），这里只负责把归一化后的角度画成条形。
defineProps({
  angles: {
    type: Array,
    required: true
  },
  note: {
    type: String,
    default: ''
  }
})

function priorityTagType(priority) {
  if (priority === 'primary') return 'danger'
  if (priority === 'ignore') return 'info'
  return 'warning'
}
</script>

<style lang="scss" scoped>
.angle-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.angle-panel h3 {
  margin: 0;
  color: var(--text-primary);
  font-size: 15px;
}

.angle-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.angle-item {
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);
}

.angle-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.angle-name {
  color: var(--text-primary);
  font-size: 14px;
}

.angle-weight {
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.angle-percent {
  color: var(--text-secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.angle-track {
  height: 8px;
  margin-top: 10px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bg-hover);
}

.angle-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--primary);
}

.angle-reason {
  margin: 10px 0 0;
  color: var(--text-secondary);
  line-height: 1.7;
}

.angle-hints {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.angle-note {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}
</style>
