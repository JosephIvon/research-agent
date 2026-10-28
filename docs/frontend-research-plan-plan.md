# 前端方案：把"研究计划"与"入选轨迹"显示出来（已执行）

> 状态：**已执行（2026-10-29）**。第 1–8 节是执行前的方案稿，保留原文；
> 第 9 节是执行记录：实际做了什么、方案与实现有出入的地方、还没做的。

## 1. 为什么要做（对应本轮暴露的三个问题）

现在后端已经知道"这轮为什么选了这些竞品"，但前端**看不到**：

| 用户此刻会问 | 现在能回答吗 | 缺什么 |
|---|---|---|
| 这轮的主体到底是哪个"百度搭子"？ | 只在报告 markdown 里有一段 | 结构化的主体/置信度/备选 |
| 竞品是怎么定义的？选几家？ | 只在报告 markdown 里有一段 | 结构化的口径、数量区间、入选标准 |
| 角度权重怎么来的？ | 只在报告 markdown 里有一段 | 权重 + 优先级 + 理由，且**标明是代码算的** |
| **为什么选了猫箱、没选某个"友商"？** | ❌ 完全没有 | 候选池、站点类型、打分与理由、剔除清单 |
| **入选指标是不是还在用代理？** | ❌ 完全没有 | 准入规则 vs 实际打分的对照 |

最后两行是本轮真实搜索实测暴露的问题（媒体站/应用商店/收录目录混进"竞品"）。
没有这条轨迹，用户只能看结论、看不出口径，也就无法判断这轮结果可不可信。

## 2. 现状（已核实的事实，带出处）

| 事实 | 出处 |
|---|---|
| 任务终态已落库 `research_plan`（结构化计划） | `server.py:1019` |
| 任务终态已落库 `competitor_matrix`（结构化矩阵） | `server.py:1017` |
| `discovery`（候选池/入选/剔除）**只在 `/research/competitive` 的响应里** | `server.py:708-709` |
| 报告 markdown 已渲染主体/口径/角度权重 | `src/workflow/competitor_matrix.py` 的 `_render_subject` |
| 前端报告对象 = `{...task}` 全量放在 `report.raw` | `frontend/src/stores/research.js:311-327` |
| 前端有独立单测（`node --test`），工具函数集中在 `researchTaskHelpers.js` | `frontend/tests/*.test.js` |
| 视图层**不使用 i18n**（`$t(` 出现 0 次），文案硬编码中文 | `frontend/src/views/*.vue` |
| 前端可构建（vite，node_modules 已就位） | `frontend/package.json` |

**结论**：前端**不需要后端改接口就能拿到 `research_plan`**（已经在任务结果里）。
只差 `discovery` 一项没随任务落库 → 后端 1 行。

## 3. 界面设计

### 3.1 位置：第 4 个 tab「调研口径」

`ReportView.vue` 现有 tab：竞品报告 / PRD / 来源状态（`ReportView.vue:204-215`）。
新增第 4 个 tab「**调研口径**」，放在最右。

为什么不做成侧栏：内容有 4 块（主体 / 口径 / 权重 / 候选池），
侧栏宽度放不下候选池表格；且侧栏现在是"分数 + 成本 + 缺失维度 + 反馈"的元信息区。

### 3.2 四个区块

```
┌─ 调研口径 ────────────────────────────────────────────────┐
│ [主体]  百度搭子 · app · 置信度 0.72                        │
│         释义：百度面向个人用户的 AI 原生社交陪伴应用…        │
│         备选：百度搭子独立App（…）／百度App内"搭子"功能（…） │
│         ⓘ 来源：模型生成（降级时显示"默认口径"）             │
├─ [口径]  层级 direct/alternative/adjacent · 数量 3–5 家     │
│         入选标准：必须有官网 + 至少 2 个维度有数据           │
│         ⚠ 开放问题：要不要含替代方案？（默认：否）           │
├─ [重点角度]  功能 ███████ 0.35 (primary)                    │
│               业务价值 ██████ 0.30 (primary)                │
│               研发与实现 ████ 0.20 (secondary)              │
│               产品与体验 ███ 0.15 (secondary)               │
│         ⓘ 权重由代码按优先级算（primary×1.5 / secondary×1.0）│
├─ [候选池与入选]  候选 12 · 入选 4 · 剔除 8                   │
│   名称            站点类型      入选  得分  理由            │
│   LumiChat        产品官网       ✓    0.50  像产品自己的官网  │
│   卿卿我我        应用商店       ✗    0.00  应用商店，不是官网 │
│   2026年AI陪伴…   媒体/内容站    ✗    0.00  文章页           │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 组件划分

| 组件 | 文件 | 职责 | 数据来源 |
|---|---|---|---|
| `ResearchPlanPanel.vue` | 新建 | 主体 / 口径 / 开放问题 | `research_plan` |
| `AngleWeightBars.vue` | 新建 | 四维权重条 + 优先级标签 | `research_plan.angles` |
| `DiscoveryTrail.vue` | 新建 | 候选池表格 + 入选标记 + 剔除原因 | `discovery` |
| `ResearchPlanTab.vue` | 新建 | 组装上面三个 + 空态/降级态 | 两者 |

**逻辑外置**：所有取数与格式化（字段缺失兜底、权重归一化、站点类型中文名）写进
`frontend/src/stores/researchTaskHelpers.js`（新增 `normalizeResearchPlan()` /
`normalizeDiscovery()`），`.vue` 只做渲染。理由：现有约定就是这样
（`buildSourceRows` / `getReportCompetitorCount` 都在 helpers 里，且有单测），
而且**能写 node --test 单测**，不必跑浏览器。

## 4. 数据契约（前端逐字段依赖）

### 4.1 `research_plan`（后端 `ResearchPlan.to_dict()`）

```jsonc
{
  "entity": {
    "name": "百度搭子",
    "entity_type": "app",                    // 枚举，12 类
    "what_it_is": "百度面向个人用户的 AI 原生社交陪伴应用…",
    "disambiguation_candidates": ["百度搭子独立App（…）（置信度 0.62）", "…"],
    "resolved_confidence": 0.72
  },
  "criteria": {
    "layers": ["direct", "alternative", "adjacent"],
    "must_include": [], "must_exclude": [],
    "min_count": 3, "max_count": 5, "candidate_pool": 10,
    "admission_rule": "必须有官网 + 至少 2 个维度有数据"
  },
  "angles": [
    {"angle": "feature", "label": "功能", "priority": "primary",
     "weight": 0.35, "reason": "…", "page_hints": ["功能", "定价"]}
  ],
  "search_plan": [{"stage": "entity_anchor", "queries": ["百度搭子 官网"]}],
  "open_questions": [{"question": "要不要含替代方案？", "default": "否"}],
  "source": "model",                          // model / default / fallback
  "error": ""
}
```

### 4.2 `discovery`（后端 `DiscoveryResult.to_dict()`）

```jsonc
{
  "entity_pages": [{"url": "...", "title": "...", "content": "..."}],
  "candidates": [{"name": "...", "url": "...", "description": "...",
                   "dimension_count": 0, "site_type": "product_site",
                   "is_aggregator": false}],
  "selected":   [{"name": "...", "url": "...", "score": 0.5,
                   "dimensions": 0, "site_type": "product_site",
                   "reasons": ["像产品自己的官网"]}],
  "scored":     [ /* 同 selected 的全部候选，按分排序 */ ],
  "queries_run": [{"query": "...", "stage": "..."}],
  "notes": ["候选池剔除 8 个非官网页（媒体/内容站 6 个、应用商店 1 个…）"]
}
```

前端只用：`candidates` / `selected` / `notes` / `scored`；
`entity_pages` 与 `queries_run` 先不展示（体积大，价值低），但**字段留着不丢**。

## 5. 后端改动（只有 1 行 + 1 个测试）

`server.py` 的 `research_metadata` 增加一项：

```python
# 候选池与入选轨迹随终态落库：复盘"这轮为什么选了这些竞品"要靠它
"discovery": result.get("discovery"),
```

验收：`tests/` 加 1 个用例——任务终态落库后 `task["discovery"]["selected"]` 非空。
（`research_plan` 已有落库，回归即可。）

**体积**：真实一轮 `discovery` 约 10–30 KB（29 个候选 × 名称/URL/理由）。
可接受；若要更小，裁剪 `entity_pages` 的 `content` 与 `candidates.description`
（已在 `Candidate.to_dict()` 里截到 200 字符）。

## 6. 实施步骤（逐条可审、可回滚）

| # | 步骤 | 涉及文件 | 验收方式 |
|---|---|---|---|
| 1 | `discovery` 随任务落库 | `server.py` | `uv run pytest tests/ -q`（新增用例） |
| 2 | helpers：`normalizeResearchPlan()` / `normalizeDiscovery()`（含字段缺失兜底、站点类型中文映射、权重归一） | `frontend/src/stores/researchTaskHelpers.js` | `cd frontend && npm test`（新增 node --test 用例） |
| 3 | store：`report.raw` 里取 `research_plan` / `discovery`，缺失时不报错 | `frontend/src/stores/research.js` | `npm test` + 旧报告（无这两个字段）能正常打开 |
| 4 | 三个新组件 + tab 接线 | `frontend/src/components/ResearchPlanPanel.vue`、`AngleWeightBars.vue`、`DiscoveryTrail.vue`、`frontend/src/views/ReportView.vue` | `cd frontend && npm run build` 通过；人工看四区块 |
| 5 | 空态/降级态：无计划 → "本次未生成研究计划（按默认口径执行）"；`source=default/fallback` → 标"默认口径" | 同上 | 人工看降级报告 |
| 6 | 样式对齐现有 `side-panel` / `report-body`（暗色主题变量） | 同上 | 人工看主题切换 |

**不做**（明确排除，避免范围膨胀）：
- 不做"用户改计划/改口径后重跑"——那要后端支持计划参数化与重跑接口，是另一个量级的活。
- 不做计划生成过程的流式展示——等任务终态再显示。
- 不把 markdown 里已有的主体段删掉——报告要能独立于前端阅读；UI 侧只做结构化展示，
  两处重复是可接受的（若审时觉得重复，可在 UI 隐藏主体块，只留候选池轨迹）。

## 7. 风险与边界

1. **旧报告没有这两个字段** → 必须有空态，不能白屏（step 5）。
2. **`discovery` 可能为空**（计划走了默认路径或搜索失败）→ 显示"本轮未走候选发现流程"。
3. **前端与 markdown 重复**：报告正文已有主体/口径段。若审时认为重复，
   可只保留候选池轨迹区块（step 4 可拆成两次提交）。
4. **i18n**：现有视图全部硬编码中文、没用 `$t()`。本方案跟随现状（硬编码中文），
   不单独为这个 tab 引入 i18n（会与其它视图不一致）；若要全局 i18n 化，是另一个独立任务。
5. **不做像素级还原**：样式跟随现有组件库的间距与配色，不引入新 UI 依赖。

## 8. 做完之后能回答的问题（验收口径）

做完这版，用户在"调研口径"tab 里能自己回答：

- 主体认的是哪个"百度搭子"、置信度多少、还有哪些备选？（消歧有没有跑偏）
- 竞品口径是什么、几家算够、准入标准是哪条？
- 四个角度的权重各是多少、谁定的（代码算的，不是模型给的数）？
- **候选池里有多少、入了哪几家、每家凭什么入、其余为什么被剔（媒体站/应用商店/文章页/主体自身）？**

第 4 条就是本轮实测暴露的问题——它现在是**不可见的**，这是要补的洞。

---

# 9. 执行记录（2026-10-29）

第 6 节的 6 步全部做完，另加 4 处方案里没写的修正（见 9.3）。

## 9.1 落地的东西

| 位置 | 文件 | 内容 |
|---|---|---|
| helpers | `frontend/src/stores/researchTaskHelpers.js`（**追加**） | `normalizeResearchPlan()` / `normalizeDiscovery()` / `normalizeAngleWeights()` / `siteTypeLabel()` / `getReportResearchPlan()` / `getReportDiscovery()` / `buildResearchPlanText()` + 五张中文标签表（对齐后端枚举） |
| 组件 | `frontend/src/components/ResearchPlanPanel.vue` | 主体 / 口径 / 提名候选 / 开放问题 |
| 组件 | `frontend/src/components/AngleWeightBars.vue` | 四维权重条 + 优先级 + 理由 + "权重由代码算" |
| 组件 | `frontend/src/components/DiscoveryTrail.vue` | 候选池表格（名称/站点类型/入选✓✗/得分/理由），入选的排前面 |
| 视图 | `frontend/src/views/ReportPlanTab.vue` | 组装三组件 + 空态 + 降级态（"默认口径"提示） |
| 视图 | `frontend/src/views/ReportView.vue` | 第 4 个 tab「调研口径」放最右；「复制当前」在该 tab 复制口径文本 |
| store | `frontend/src/stores/research.js` | `research_plan` / `discovery` / `rejected_competitors` / `admission_gate` 显式取（缺失写 `null`） |
| 后端 | `server.py` | 终态落库 `discovery`；历史冷加载补回 4 个字段；导出新增 `content_type=plan` |
| 后端 | `src/workflow/research_plan.py` | `render_plan_markdown()`：口径导出的渲染 |

**数据流**：`server.py` 终态 → `store.update()` → task 记录 → `stores/research.js`
→ helpers 归一化 → `ReportPlanTab.vue`（只渲染）→ 三个子组件。
零 i18n（跟随现有视图的硬编码中文）、无新 UI 依赖、样式走主题变量。

## 9.2 验收（真实输出）

- `cd frontend && npm test`：**80 passed**（改动前 51，新增 29）。
- `cd frontend && npm run build`：✓ built，无报错。
- **SSR 真渲染校验**（vite `ssrLoadModule` + `@vue/server-renderer` + 真实 Element Plus）：
  三种场景（有计划+候选池 / 旧报告两个字段都没有 / `source=default` 降级）全部渲染成功。
  build 只证明模板能编译，SSR 才证明运行时不炸。
- 后端：`tests/test_plan_export.py` 4 例（渲染 + 直接调 handler + **真走 HTTP** 的导出与历史冷加载）。

## 9.3 方案与实现有出入的地方（都改了，理由在括号里）

1. **字段名**：方案写 `research_plan.criteria`，后端 `ResearchPlan.to_dict()` 实际是
   `competitor_criteria`（`src/workflow/research_plan.py:335`）。
   → 前端两个都认；**方案文档第 4.1 节已改正**（口径不一致的源头在这）。
2. **历史冷加载没有口径**（方案 §5 只写了 1 行，隐含假设"前端总能拿到"）：
   `research_plan`/`discovery` 只随**任务终态**落库，而 `/research/history/{report_id}`
   只 enrich 了成本/注入数/trace/不完整四个字段。
   → **从历史列表重新打开或换浏览器时只剩空态**。后端补了 4 个字段（`research_plan` /
   `discovery` / `rejected_competitors` / `admission_gate`），并补了真走 HTTP 的回归测试。
3. **导出的口径 tab 导出的是报告正文**（点的是 A 导出的是 B）：
   → 后端 `/research/export` 新增 `content_type=plan`，导出的是口径本身
   （文件名 `research_plan_<id>.*`，内容由后端 `render_plan_markdown()` 渲染）。
4. **方案里的四区块没包含准入闸门的新数据**（当时还没有）：
   → 口径区加「实际执行的入选条件 / 判不了的条件」，候选区加「未采信（N）」清单。

## 9.4 没做的（按方案排除，仍然不做）

- 用户改计划/改口径后重跑（要后端支持计划参数化与重跑接口）。
- 计划生成过程的流式展示。
- 删掉 markdown 里已有的主体段（报告要能独立阅读；UI 侧重复是可接受的，已按此保留）。
- 全局 i18n 化（跟随现有视图的硬编码中文）。
