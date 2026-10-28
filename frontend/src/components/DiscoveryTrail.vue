<template>
  <section class="discovery-panel">
    <div class="discovery-head">
      <h3>候选池与入选</h3>
      <div class="discovery-stats">
        <span class="discovery-stat">候选 {{ discovery.poolSize }}</span>
        <span class="discovery-stat selected">入选 {{ discovery.selectedCount }}</span>
        <span class="discovery-stat rejected">剔除 {{ discovery.rejectedCount }}</span>
      </div>
    </div>

    <div v-if="discovery.rows.length" class="discovery-table-wrap">
      <table class="discovery-table">
        <thead>
          <tr>
            <th>名称</th>
            <th>站点类型</th>
            <th>入选</th>
            <th>得分</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in discovery.rows" :key="row.url || row.name + index" :class="{ rejected: !row.selected }">
            <td>
              <strong class="row-name">{{ row.name }}</strong>
              <a v-if="row.url" class="row-url" :href="row.url" target="_blank" rel="noreferrer">{{ row.url }}</a>
            </td>
            <td>
              <span class="site-type" :class="row.siteType">{{ row.siteTypeLabel }}</span>
            </td>
            <td>
              <span class="mark" :class="row.selected ? 'yes' : 'no'">{{ row.selected ? '✓' : '✗' }}</span>
            </td>
            <td class="row-score">{{ row.scoreText }}</td>
            <td>
              <ul v-if="row.reasons.length" class="row-reasons">
                <li v-for="(reason, reasonIndex) in row.reasons" :key="reasonIndex">{{ reason }}</li>
              </ul>
              <span v-else class="row-muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="discovery-empty">
      <p>{{ discovery.emptyText }}</p>
    </div>

    <div v-if="discovery.notes.length" class="discovery-notes">
      <span class="notes-label">本轮记录</span>
      <ul>
        <li v-for="(note, index) in discovery.notes" :key="index">{{ note }}</li>
      </ul>
    </div>
  </section>
</template>

<script setup>
// 纯展示组件：候选池的打分、站点类型中文名与入选判定都在
// stores/researchTaskHelpers.js 里（normalizeDiscovery）。
defineProps({
  discovery: {
    type: Object,
    required: true
  }
})
</script>

<style lang="scss" scoped>
.discovery-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.discovery-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.discovery-head h3 {
  margin: 0;
  color: var(--text-primary);
  font-size: 15px;
}

.discovery-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.discovery-stat {
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
}

.discovery-stat.selected {
  color: var(--success);
}

.discovery-stat.rejected {
  color: var(--text-muted);
}

.discovery-table-wrap {
  overflow-x: auto;
}

.discovery-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.discovery-table th {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-secondary);
  font-weight: 700;
  text-align: left;
  white-space: nowrap;
}

.discovery-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary);
  vertical-align: top;
}

.discovery-table tr.rejected td {
  color: var(--text-secondary);
}

.row-name {
  display: block;
  color: var(--text-primary);
}

.discovery-table tr.rejected .row-name {
  color: var(--text-secondary);
}

.row-url {
  display: block;
  margin-top: 2px;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.site-type {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.site-type.product_site {
  color: var(--success);
}

.mark {
  font-weight: 800;
}

.mark.yes {
  color: var(--success);
}

.mark.no {
  color: var(--text-muted);
}

.row-score {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.row-reasons {
  margin: 0;
  padding-left: 16px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.row-muted {
  color: var(--text-muted);
}

.discovery-empty {
  padding: 18px;
  border-radius: 8px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);
  color: var(--text-secondary);
}

.discovery-empty p {
  margin: 0;
}

.discovery-notes {
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--bg-page);
  box-shadow: inset 0 0 0 1px var(--border);
}

.notes-label {
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
}

.discovery-notes ul {
  margin: 6px 0 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.7;
}
</style>
