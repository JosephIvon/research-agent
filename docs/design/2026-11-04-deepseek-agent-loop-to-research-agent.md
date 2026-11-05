# DeepSeek 两层 Agent Loop → research-agent 改造方案

> 类型：设计提案（只新增本文档，不改任何既有代码；所有代码改动均为待确认提案）
> 日期：2026-11-04
> 上游证据：本地 `git clone --depth 1` 直读源码，不使用二手解读作为事实依据
> 结论先行：确认上游为 **`deepseek-ai/dsh-superagent-prototype`（外层 Router）+ `deepseek-ai/deepseek-harness`（内层 AgentLoop）**；两层循环 = **main Session 的“裁决/派活/回收”循环 + 每个 Ticket 独立 Executor Session 的 turn/step 工具循环**。

---

## 0. 摘要

### 0.1 确认的上游项目

| 项 | 值 | 证据 |
| --- | --- | --- |
| 外层项目 | `deepseek-ai/dsh-superagent-prototype` | GitHub 仓库页、README、docs/007 |
| 版本 | `package.json` version `0.1.0` | 本地源码 |
| commit | `8c32a9dc5d72c033816c6a8110b37f40cf94fba4` | `git rev-parse HEAD` |
| License | MIT | `LICENSE` 第 1 行 `MIT License` |
| 内层运行时 | `deepseek-ai/deepseek-harness`，`@deepseek-ai/dsh-agent-loop` `0.2.2-alpha.3` | 包 `package.json` |
| commit | `8bd073392eef6f54e921a093b95076248eb08cbb` | `git rev-parse HEAD` |
| License | MIT | `LICENSE` 第 1 行 `MIT License` |

**上游性质必须如实标注**：Super-agent 仓库 README 第 3 行原文是
“**Research prototype for reference only, not an official DeepSeek product.** ... Treat it as research code, not production-ready software.”
因此它是**官方组织开源的研究原型**，不是官方宣称的生产框架；官方生产运行时是 DeepSeek Harness（`deepseek.com/harness` 与 README 均指向 `deepseek-ai/deepseek-harness`）。

### 0.2 两层循环一句话

**外层循环**（常驻 main Session，即 Router）只做四件事：判断消息该不该接（Eval → accept/defer/ignore/stop）→ 开 Ticket 定边界 → 派生一个隔离的 Executor Session 干活 → 收 Report 决定继续/结束；
**内层循环**（每个 Ticket 一个 Executor Session，即 Worker）只做一件事：在自己的上下文中按 Harness 的 **turn → step** 循环调模型、跑工具、写结果，直到这一轮没有“欠账”为止。
**外层不直接调工具，内层不直接做全局路由**——这是整套设计真正的职责切分线。

---

## 1. 上游定位：候选、判据与排除

### 1.1 候选列表

| 候选 | 仓库 | 与“两层 agent loop”的关系 | 判定 |
| --- | --- | --- | --- |
| A | `deepseek-ai/deepseek-harness` | 每个 agent 内部是 **turn / step** 双层循环（`docs/architecture.md` L81-106） | 命中“两层”，但两层都在**单个 agent 内部**，没有“派生/回收” |
| B | `deepseek-ai/dsh-superagent-prototype` | **main Session（Router）+ Executor Session（Worker）** 两层 agent 会话；外层派活、回收，内层干活 | 命中“两层 + 规划/派生/回收/终止”，是本次主参照 |
| C | `docs/001-domain.md` L33 的 “in two layers: gate and assessment” | 字面含 “two layers”，但指 **Eval 的两层判定**，不是 loop | 排除（同词异义） |

### 1.2 选定依据（均为源码/文档原文）

1. **B 的两层有明确的会话边界**
   `docs/003-execution.md` L23-29：
   “Each Ticket gets an Executor Session named `worker-<ticket id>` ...”
   “The main Session uses `dsh-superagent-router` ...”
   `docs/007-architecture.md` L117：
   “A Ticket keeps each fact once ... its Executor Session is named after it (`workerSessionId` in `src/executor/dsh.ts`).”
2. **B 的“派生与回收”有代码**
   `src/executor/dsh.ts` L24 `workerSessionId = ticketId => 'worker-' + ticketId`；L424-466 `launch()` 创建/唤醒 worker session 并 `prompt`；L531 取消 worker；`src/executor/dsh.ts` L171 `WorkerSlots` 控制并发槽。
3. **B 的内层就是 A**
   Super-agent 是 “packaged as a bundle for DeepSeek Harness”（README L7），worker 的 turn/step 直接由 `@deepseek-ai/dsh-agent-loop` 驱动（`docs/003-execution.md` L27-29）。
4. **A 的 turn/step 定义是原文**
   `docs/architecture.md` L83：
   “A **step** is one model request plus the tools it calls. A **turn** is zero or more steps: it opens before its first input is claimed and closes once nothing is owed.”
   `packages/core/agent-loop/src/agent.ts` L327-433 是 `turn()` 实现，L435 `step()`。

### 1.3 一个必须纠正的偏差

用户记忆中的“外层循环做规划/分解”，在**上游并不成立**：

- 上游外层（main Session）做的是**准入裁决与定界**：Eval 决定是否开始，Decision 产出 Ticket 的 mandate（目标与边界），并不下发细粒度步骤。证据：`src/core/evals.ts` L22-25（“batches a Thread's queued Triggers into one Eval ... records the Decision”）、`src/core/tickets.ts` L37 `create(id, decisionId, mandate, ...)`。
- 细粒度“怎么做”发生在**内层 worker**：worker 自己调工具、跑子 agent（`docs/003-execution.md` L27-35）。

对 research-agent 的含义：**不要在外层复刻一个“详细任务分解器”**；外层应负责“单元化 + 边界 + 预算 + 回收”，分解只作为单元化的输入。这直接决定下面改造的取舍。

---

## 2. 两层循环的源码级职责切分

### 2.1 外层循环（Router / main Session）

外层的形态不是一个大 `while`，而是**事件驱动的状态机 + 每 2 秒一次的 pass**：

`docs/007-architecture.md` L40：
“Passes ... One pass at a time, every two seconds and whenever a command asks: the due effects, then `Engine.evaluateDue`; a Source's Trigger reaches the Engine through it.”

职责链（按调用顺序）：

| 职责 | 代码 | 关键事实 |
| --- | --- | --- |
| 批量评估 | `src/core/evals.ts` L22-25、L87-111 | 同一 Thread 的多个 Trigger 合成一个 Eval，避免逐条烧模型 |
| 截止时间与暂停顺延 | `src/core/evals.ts` L31-43、L53 | `deadline` 从“候选送达”开始计时；暂停期顺延 |
| 超时重排 | `src/core/evals.ts` L57、L167-173 | 超时后 Trigger 重新排队，最多 `REQUEUES` 次 |
| 决策 | `src/core/engine.ts` L100 `decide()`；`evals.ts` L128-152 | `accept` / `stop` / `ignore` / `unavailable`；`stop` 只能停 ticket，不能替 worker 判断跟进 |
| 原子提交 | `src/core/engine.ts` L64、L100 | 所有状态迁移在 `repo.atomic()` 内，单写者 |
| 建 Ticket | `src/core/tickets.ts` L37 `create` | Ticket 记录 mandate、decision、trigger 集合 |
| 结束 Ticket | `src/core/tickets.ts` L345 `end(ticketId, by, outcome, ...)` | 记录“何时/由谁/为什么/什么结果” |
| 跟进线程判定 | `src/core/tickets.ts` L95 `followsDone` | `ready` 也要等被 follow 的线程结束 |
| 派生 Worker | `src/executor/dsh.ts` L424-466 | `sessions.ensure(sessionId, workspace, title, 'worker')` → `prompt` 下发 assignment |
| 并发槽 | `src/executor/dsh.ts` L171、L229-230；`docs/007` L203 | `maxWorkers`；人类消息可额外占 1 个槽；唤醒优先于启动 |
| 回收与卡住 | `src/core/assignments.ts` L66-71、L78-92 | `turnEnded` 记录空闲；`interrupted` 在宿主重启后决定“重开”还是“保留会话” |
| 阶段判定 | `src/core/stage.ts` L9-21 | 纯函数，按 docs/003 表从上到下第一条命中 |

**终止条件（外层）**不是“模型说完了”，而是 **Ticket 进入 `ended`**；`stage.ts` L10 `if (ticket.ended) return { stage: 'ended' }`。中途的 `blocked/failed` 会变成 `person` 阶段等人（L17-19），`ready` 会进入 `person/review`（L15）——即“机器循环暂停，人类接管”，而不是硬失败。

### 2.2 内层循环（Worker / AgentLoop 的 turn-step）

`packages/core/agent-loop/src/agent.ts`：

- `turn()` L327-433：
  - L336 `session.append('turn/start')`；
  - L344 `while (true)`：每轮先 `preStep`，被拒则 `turnEnds = {kind:'blocked'}` 并结束（L348-351）；
  - L361 `step/start`；L370 `await this.step(decision)`；
  - L395-399：当 `turnEnds` 已确定且 `inbox.nextStep` 为空 → `agent/turn-stopping` → break；
  - L404-407：中断产生 `{kind:'aborted', reason}`；L412-417：所有异常折叠为结构化 `{kind:'error', code}`；
  - L422 `turn/end` 一定落盘（finally）。
- `step()` L435 起：一次模型请求 + 该请求发起的工具批；L363-385 `ToolCallRecovery` 在 step 抛错时把已产生的工具结果补写进会话，避免“工具跑了但结果丢了”。
- 工具批并发：`packages/core/tools/src/batch.ts` L187 `while (!aborted && inFlight.size < maxParallelToolCalls)`，L221 `await Promise.allSettled(inFlight.values())`。
- 工具执行管线与审批缝：`packages/core/tools/src/pipeline.ts` L185-215（`tools/pre-execute` → allow/deny → `checkGuards` → `tool.execute`），L422-428（`ask` 决策通过 approval seam 解析，默认拒绝、卸载即降级）。

**内层终止条件**：
1. 模型不再发工具调用且有正文 → 该 step 结束；turn 是否继续看 `inbox.nextStep`（L395-399）；
2. `turnEnds` 取 `completed / aborted / blocked / error / max-tokens`；
3. Worker 通过 `orchestrator_report` 工具显式回传状态（见 2.3）。

### 2.3 配套机制对照表

| 机制 | 上游做法（证据） | 对 research-agent 的可迁移性 |
| --- | --- | --- |
| 上下文管理 | 内外分离：main Session 持全局；worker 每次只拿 assignment 上下文与角色提示（`src/app/context.ts` L6-24；`docs/003` L25-29）；Harness 有压缩包 `packages/compaction` | 高：按“单元”裁剪上下文，成本最低 |
| 状态持久化 | Ticket/Decision/Effect 落 SQLite（`src/app/effects.ts` L43-44）；会话是 append-only 事件日志（Harness `docs/architecture.md` L59） | 高：research-agent 已有 SQLite，只缺阶段检查点 |
| 错误与重试 | 效果队列按 kind 配置退避与顺序（`src/app/effects.ts` L26-36，`docs/007` L174-178）；供应商/网络失败 3 次（`docs/007` L205）；工具结果补写（`agent.ts` L363-385） | 中：只要“阶段级重试 + 明确 reason”，不要通用队列 |
| 工具注册与权限 | 单一管线：pre-execute 决策 allow/deny/ask，再 guard，再执行（`pipeline.ts` L185-215）；worker 用 `dsh-superagent-worker` 预设，主会话用 `dsh-superagent-router`，工具集不同（`docs/003` L29） | 中：research-agent 已有只读白名单，补“每单元工具集”即可 |
| 并发与中断 | `WorkerSlots` 并发上限、人类消息额外槽（`dsh.ts` L171/L229；`docs/007` L203）；`AbortController` 驱动取消（`agent.ts` L196/L428）；宿主重启恢复 assignment（`assignments.ts` L78-92） | 高：“中断可恢复”是 research-agent 最大缺口 |
| 结果回传 | `orchestrator_report` 工具：`progress/ready/blocked/failed/dropped` + `human_action`（`src/app/tools/executor.ts` L74-101）；报告与 relay 在同一事务（L89-101）；`turnEnded` 只表示“这一轮结束”，不表示“工作结束”（`docs/003` L33） | **最高**：直接解决“静默失败/不可归因” |
| 派生与回收 | assignment 结束时杀 shell、释放子 agent（`docs/003` L43）；handoff 立刻释放槽（L37） | 中：research-agent 无子进程，只需回收任务与浏览器 |

---

## 3. research-agent 现状（改造落点的事实基础）

### 3.1 主循环：两套并存

| 路径 | 事实 | 行号 |
| --- | --- | --- |
| legacy 流水线 | `MVPWorkflow.run()` 顺序执行：decompose → crawl → extract → verify → report → citation → sync | `src/workflow/mvp_workflow.py:922` |
| 分解 | `coordinator.decompose_task()` 让模型输出 JSON 子任务 | `mvp_workflow.py:204`、`:984` |
| ReAct 模式 | `_run_react_mode()`，由 `AGENT_MODE=react` 开启 | `mvp_workflow.py:1502`；`src/agent/config.py:13` |
| ReAct 内层 | `Runner.run()` 单层 `for index in range(1, max_steps+1)`，预算/重复/空观测均有显式 `stop_reason` | `src/agent/runner.py:74-220` |
| 预算 | 全局 token/墙钟/USD，不是按单元 | `src/agent/config.py:15-19`；`runner.py:77-88` |
| 收敛 | 预算耗尽后允许一次强制收敛调用，产出 PARTIAL | `config.py:27`；`runner.py:225` |
| 并发抓取 | 动态并发控制器 + 信号量 + `gather(return_exceptions=True)` | `mvp_workflow.py:1789`、`:1829-1851` |

### 3.2 工具层

- 只读白名单在注册处强制（非只读工具直接抛错）：`src/agent/tools.py:54-59`。
- 执行层把超时/异常统一转成 `ok=False` 的可观察结果，不终止循环：`tools.py:70-98`。
- URL 二次 SSRF 校验通过注入口完成：`tools.py:48-52`。

判断：**工具层的“失败即观察”已经与内层循环的正确语义一致**，不需要照搬上游 tool pipeline。

### 3.3 任务/阶段与状态

- 状态枚举：`src/schema/state.py:64-65`（`pending/decomposing/researching/verifying/writing/completed/partial/failed`）。
- 子任务字段存在但**没有被消费**：`state.py:76-77`；写入 `mvp_workflow.py:984`；`current_subtask_index` 只在初始化出现（`mvp_workflow.py:1753`）。
- 事件类型与白名单：`src/workflow/task_store.py:35-65`。
- SSE 支持 `Last-Event-ID` 续传：`server.py:1243-1264`。
- 事件 id 是**进程内计数**：`server.py:1021-1027`，进程重启后从 1 重新开始。
- Redis 后端有 CAS 防丢写：`task_store.py:477-507`；事件另存 list 并 publish：`:541-555`。

### 3.4 持久化与续跑能力

- SQLite 只有 `reports` / `tasks` / `report_feedback` 三张表：`src/storage/database.py:70-109`；`tasks` 表没有 checkpoint/事件字段（`:88-98`）。
- 全仓库检索 `resume|checkpoint|断点|续跑|recover` **无任何实现**（仅测试里出现 SSE 续传）。
- 服务启动没有“扫描 running 任务并恢复”的逻辑（`server.py` 无相关入口）。

结论：**可观测性有（事件流 + SSE 回放），断点续跑没有；失败后只能整单重跑。**

### 3.5 评测与基线（用于验证改造）

- 离线评测是“流水线保真度”，不是模型能力：`evals/__init__.py:1-23`；共 25 个 case 目录，基线 `evals/baselines/v1.8.json` 记录 23 个 case 的 `citation_coverage / citation_validity / grounding / dimension_coverage`。
- 运行方式：`uv run python -m evals.runner`；门禁：`--gate`（`evals/__init__.py:9-10`）。
- e2e 已验证 SSE 断流重连：`tests/e2e/test_research_flow.py:155-236`。

---

## 4. 逐条映射：上游机制 → research-agent 落点

> 每条都按“上游机制 / 落点 / 改造要点 / 收益 / 成本 / 验证方法”给出。收益与成本是对本仓库结构的工程判断（**推断**），不是上游事实。

### 4.1 值得抄（6 条）

#### C1. Ticket 化工作单元 + 结构化 Report（最高优先级）

| 项 | 内容 |
| --- | --- |
| 上游机制 | 一个 Ticket 一个 worker 会话；worker 用 `orchestrator_report` 回传 `progress/ready/blocked/failed/dropped`（`src/app/tools/executor.ts:74-101`）；报告与 relay 同事务（`:89-101`）；`assignments.report()` 校验状态组合（`src/core/assignments.ts:99-125`） |
| research-agent 落点 | `state["subtasks"]`（`mvp_workflow.py:984`）目前只用来判断 `degraded`（`:986`）；每个竞品抓取在 `_fetch_all_competitors()`（`:1789`）里只是 `gather` 后 append，失败被折叠成结果字典（`:1864-1869`） |
| 改造要点 | 把 `decompose_task` 的输出变成 `ResearchUnit`：`{unit_id, dimension, target_url, status, attempts, deadline, report:{state,reason,evidence_count}}`；`_fetch_all_competitors` 返回 `unit.report` 而不是裸 dict；`run()` 汇总 `units` 生成 `delivery_readiness`（已有字段，见 `mvp_workflow.py:1197-1202`） |
| 收益 | 任务完成率：单个单元 `failed/blocked` 不再拖垮整单，可跳过/补位并如实标注；可观测性：失败能定位到“哪个竞品/哪个维度”；也顺带修掉“子任务只分解不使用”的死字段 |
| 成本 | 中：新增 1 个 dataclass 模块 + 改 `_fetch_all_competitors` 与汇总出口；无新依赖 |
| 验证 | 同一 25 case 集跑 `evals.runner`：`dimension_coverage` 不低于 v1.8（当前 coverage 类 n=3、`dimension_coverage=0.722`，是已知短板；取数见 `evals/baselines/v1.8.json` + `uv run python -m evals.runner`）；新增 `unit_completion_rate`、`blocked_units`；e2e 断言每个失败单元都有 `report.reason` |

#### C2. 显式阶段表 + 单写者 + 阶段级 attempt

| 项 | 内容 |
| --- | --- |
| 上游机制 | `Engine` 所有迁移都包在 `repo.atomic()`（`src/core/engine.ts:64,100`）；一个 pass 内按固定顺序推进（`docs/007` L40）；Ticket 阶段用纯函数判定（`src/core/stage.ts:9-21`） |
| research-agent 落点 | `run()` 是一条 `if` 顺序链（`mvp_workflow.py:922-1464`）；阶段只靠 `_emit`（`:898`）与 `state["status"]`；无 `attempt` 字段，只有全局 `retry_count`（`:1783`） |
| 改造要点 | 新增 `_run_stage(name, fn, *, retryable=False)` 包装：进入写 `stage_start`、结束写 `stage_end{ok,reason,attempt,elapsed}`；把 `run()` 内 6 个阶段改为调用它；阶段表放常量（顺序即执行顺序） |
| 收益 | 可观测性：每个阶段的 start/end 都能配对（目标 25/25 case），能直接算每阶段耗时/失败率；为 C3 的检查点提供天然边界 |
| 成本 | 低：只改控制流外壳，业务函数不变 |
| 验证 | 每个阶段都有 start 且都以 end 收尾（25/25 case，n=25）；对同一批 case 比较前后 `failed` 任务的错误阶段分布 |

#### C3. 追加式事件 + 检查点 + 断点续跑

| 项 | 内容 |
| --- | --- |
| 上游机制 | 会话是 append-only 事件日志（Harness `docs/architecture.md` L59）；启动时“resumes interrupted Assignments”（`docs/007` L33）；`assignments.interrupted()` 按“是否已报告且无新输入”决定重开或保留（`assignments.ts:78-92`） |
| research-agent 落点 | 事件存 Redis list 且 `id` 是进程内计数（`task_store.py:541-555`；`server.py:1021-1027`）；SQLite 无事件/检查点表（`database.py:70-109`）；无 resume 入口 |
| 改造要点 | ① SQLite 增 `task_events(task_id, seq, type, stage, status, payload_json, created_at)`（`seq` 由 DB 生成，跨进程单调）；② 增 `task_checkpoints(task_id, phase, seq, state_json, updated_at)`，只在阶段边界写；③ `state_json` 只序列化原语（见 5.4 的可序列化边界）；④ 启动扫描 `status=running` 的任务，按检查点决定 resume/fail；⑤ 新增 `POST /tasks/{id}/resume`（同一 `user_id` 鉴权） |
| 收益 | 断点续跑：抓取/生成中途崩溃不再整单重跑；可回放：`seq` 单调，SSE 可跨重启续传 |
| 成本 | 中：1 个迁移 + 1 个新模块（约 150 行）+ 启动钩子；Redis 与 SQLite 双写需明确“SQLite 为真源、Redis 仅做 fanout” |
| 验证 | 混沌测试：抓取中 kill -9，重启后 resume；指标 `resume_success_rate`、重复 LLM 调用次数、恢复后总耗时；事件 `seq` 严格递增且无重复 |

#### C4. 每单元预算/截止时间 + 超时回收

| 项 | 内容 |
| --- | --- |
| 上游机制 | Eval 有 `evalTimeoutMs` 与暂停顺延（`src/core/evals.ts:31-53`）；worker 槽上限与人类消息额外槽（`dsh.ts:171,229`；`docs/007` L203）；一轮后台工作 30 分钟上限、到点杀作业并如实报告（`docs/003` L33-35） |
| research-agent 落点 | 只有全局预算（`config.py:15-19`）与全局 180s 墙钟（`:17`）；ReAct `max_steps` 全局（`:15`）；单个竞品可无限占用爬虫并发 |
| 改造要点 | 给 `ResearchUnit` 加 `wall_clock_budget` 与 `max_pages`；在 `_limited_fetch`（`mvp_workflow.py:1829`）外层加 `asyncio.wait_for`；超时产出 `report.state='failed'/'blocked'` 并标注 `timeout`；不回收已完成的单元 |
| 收益 | 尾部延迟（p95）与完成率：一个慢站不再拖住整批；与 C1 报告契约天然配套 |
| 成本 | 低：复用现有信号量与 `wait_for`，不需要新调度器 |
| 验证 | 同一 case 集比较 p50/p95 耗时；构造“1 个站点挂起”的夹具，断言整单仍在预算内完成且该单元为 `blocked(timeout)` |

#### C5. 统一终止契约（reason 不是自由文本）

| 项 | 内容 |
| --- | --- |
| 上游机制 | `turnEnds` 是联合类型 `completed/aborted/blocked/error/max-tokens`（`agent.ts:341-417`）；worker report 是枚举 + 必填 summary（`executor.ts:74-84`）；阶段判定纯函数，按表匹配（`stage.ts:9-21`） |
| research-agent 落点 | `stop_reason` 已很规范（`runner.py:74-220`），但 workflow 层用自由文本 `termination_reason`（`mvp_workflow.py:1462`、`:1469`）；两条环路词表不统一 |
| 改造要点 | 定义 `RunOutcome = {state, reason_code, unit_id?, detail?, retryable}`；`reason_code` 收敛为 `budget_exceeded / duplicate_loop / empty_observations / max_steps / halt / timeout / quality_gate / citation_unresolved / llm_error`；所有 `return`/`raise` 出口都经 `_finish(outcome)` |
| 收益 | 失败可诊断、可重试策略可编程；看板能直接统计 reason 分布，而不是解析中文 |
| 成本 | 低：不改业务逻辑，只改出口与事件 payload |
| 验证 | 25 case 跑完后 `reason_code` 覆盖率 100%；对 `failed/partial` 抽样断言无自由文本；与 v1.8 比 `partial` 率不上升 |

#### C6. 单元级上下文隔离

| 项 | 内容 |
| --- | --- |
| 上游机制 | main Session 与 worker 用不同 preset/工具集（`docs/003` L27-29）；每轮只装配角色提示与 assignment 上下文（`src/app/context.ts:6-24`）；输入按当前 Ticket 状态渲染（`docs/007` L205） |
| research-agent 落点 | ReAct 的 system prompt 只构建一次（`runner.py:57`），`messages` 全程累积；legacy copywriter 一次性拿到全部 `extracted_data`（`mvp_workflow.py:1215-1223`） |
| 改造要点 | 对每个 `ResearchUnit` 组装“本单元证据 + 全局维度摘要（≤N 条）”；跨单元只传结构化摘要，不传原始 HTML/全文 |
| 收益 | token 成本与长任务漂移；对“多竞品”场景尤其明显 |
| 成本 | 中：需要一次证据归属（evidence → unit）的整理；会改动提示词组装 |
| 验证 | `token/task` 下降 ≥15% 且 `citation_coverage/grounding` 不下降；同一 case 重复 3 次的方差缩小 |

### 4.2 不该抄（5 条）

| 编号 | 上游机制 | 为什么不该抄（License + 耦合度） | 可以借的那一点点 |
| --- | --- | --- | --- |
| N1 | Cordis 插件内核 / “Everything is a plugin”（`deepseek.com/harness` 原文） | License 是 MIT，**法律上可抄代码**；但它是 TypeScript 运行时 + 服务容器 + 依赖图（`packages` 规模上百），research-agent 是 Python 显式构造函数。移植等于重写运行时，耦合成本远大于收益 | 只借“能力边界显式化”的思想：工具/检索/报告各自可替换，但用现有构造函数注入即可 |
| N2 | Router/Worker 多会话 + 人类路由 + GitHub/Feishu 订阅 + PR claim | MIT；但强绑定“长期在线的多轮协作产品”（`docs/003` L41 人可直接在 worker 会话里发言、`src/sources/github-pull.ts:5` 的 `claimPull` 认领 PR）。research-agent 是**一次性任务**，没有持续会话、没有 PR 归属问题 | 只借“一单元一隔离会话/上下文”与“外层只裁决不干活” |
| N3 | 细粒度全量 Session log（每个 stream chunk / attempt / surface op 都落盘） | MIT；但事件模型是 Harness 的 `SessionEventMap`，字段与 TS 类型强耦合；research-agent 已有 `TaskEvent` + SSE，粒度刚好。全量复制会带来存储与迁移负担 | 借“append-only + 单调 seq + 可回放”，不借事件 schema |
| N4 | 人工审批缝（`tools/pre-execute` 的 ask/allow/deny，`pipeline.ts:422-428`） | MIT；research-agent 工具全部只读且注册处已强制白名单（`tools.py:54-59`），审批只增加延迟与 UI 面积，不增加安全性 | 保留现有白名单 + URL 校验即可；若将来接写入工具再引入 |
| N5 | 多车道 durable effect queue（SQLite 行级队列、按 kind 的 limit/order/backoff/health，`effects.ts:26-44`） | MIT；但它是为“多源事件 + 多进程 + 外部副作用（发消息/开会话）”设计的。research-agent 单进程、副作用只有落盘与 SQLite，通用队列是纯过度设计 | 借“检查点 + 有限重试 + 退避”三点，用 30 行实现，不建队列表 |
| N6（补充） | 30 分钟后台作业与 subagent 生命周期管理（`docs/003` L31-47） | MIT；research-agent 没有长驻 shell/子 agent，只有 HTTP 抓取与 LLM 调用 | 借“超时要显式报告并回收”，不借作业树 |

---

## 5. 最小改造方案（不引入新框架）

原则：**先观测、再单元化、最后续跑**；每步可独立回滚；全部用现有依赖（FastAPI/SQLite/Redis/现有 Runner）。

### 5.1 目标与优先级

| 优先级 | 目标 | 对应机制 | 预期可量化收益 |
| --- | --- | --- | --- |
| P0 | 失败可归因 | C1 + C5 | 所有 `failed` 任务都带 `reason_code` 与 `unit_id`（目标 25/25 case）；便于后续补位 |
| P1 | 任务完成率 | C1 + C4 | `coverage` 类 `dimension_coverage` 从 0.722（n=3）提升；`blocked_units` 可统计 |
| P2 | 可观测性 | C2 + C3(事件部分) | 阶段 start/end 全覆盖（25/25 case）；跨重启 SSE `seq` 连续（重启注入 n=3） |
| P3 | 断点续跑 | C3(检查点部分) | 崩溃后 `resume_success_rate` ≥90%，重复 LLM 调用显著下降 |

### 5.2 变更清单（提案，未实施）

#### M0. 统一出口与事件（P0，低风险）

- 文件：`src/workflow/mvp_workflow.py`、`src/agent/runner.py`（仅新增字段，不改判定）
- 新增 `RunOutcome`（放 `src/schema/state.py` 或新 `src/workflow/outcome.py`）：

```python
# 提案：所有终止出口共用
@dataclass(frozen=True)
class RunOutcome:
    state: Literal["completed", "partial", "blocked", "failed"]
    reason_code: str          # 枚举，不是自由文本
    detail: str = ""
    unit_id: str | None = None
    retryable: bool = False
```

- `Runner` 的 `stop_reason` 原样保留（已经很好），在 workflow 边界映射成 `reason_code`。
- `_emit("error", ...)` / `completed` 的 payload 增加 `reason_code`、`unit_id`。

#### M1. ResearchUnit + Report 契约（P1，核心）

- 文件：新增 `src/workflow/units.py`；改 `mvp_workflow.py:984`、`:1789-1880`
- 结构：

```python
@dataclass
class UnitReport:
    state: Literal["progress", "ready", "blocked", "failed", "dropped"]
    reason: str = ""
    evidence_count: int = 0

@dataclass
class ResearchUnit:
    unit_id: str
    dimension: str
    target_url: str | None
    status: Literal["pending", "running", "ready", "blocked", "failed", "dropped"] = "pending"
    attempts: int = 0
    deadline_s: float = 60.0
    report: UnitReport | None = None
```

- `decompose_task` 输出映射为 unit（`description/priority` → `dimension`）；单元执行体仍是 `_fetch_single_competitor`，但返回 `UnitReport`。
- 汇总出口：`delivery_readiness`（`mvp_workflow.py:1197`）的 `hard_gates` 改用 `units` 统计，不再只看全局覆盖率。

#### M2. 阶段包装器（P2，低风险）

- 文件：`mvp_workflow.py` 内新增 `async def _run_stage(self, name, fn, *, retryable=False, max_attempts=1)`。
- 只包裹现有阶段函数调用；失败时 `retryable` 才重试（默认不重试，保持现有语义）。

#### M3. SQLite 检查点 + resume（P3，中风险）

- 文件：`src/storage/database.py`（`init_db` 增两表）、新增 `src/workflow/checkpoint.py`、`server.py`（启动钩子 + `POST /tasks/{id}/resume`）

```sql
-- 提案：SQLite 为真源
CREATE TABLE IF NOT EXISTS task_events (
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS task_checkpoints (
  task_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  seq INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, phase)
);
```

- 写入时机：仅阶段边界（decompose 后、每批抓取后、verify 后、report 后），避免高频写。
- 恢复语义照抄上游 `assignments.interrupted()` 的判断（`assignments.ts:78-92`）：**已到终态的阶段不重跑；未到终态的阶段从头重跑，但复用已落盘的单元报告**。
- `state_json` 只允许原语：`task_id/user_query/target_urls/units/status/citation_attempts/errors/...`；`EvidenceStore`、`datetime`、爬虫实例等派生对象不入库，恢复时重建（见 5.4）。

#### M4. 单元预算（P1，低风险）

- 在 `_limited_fetch`（`mvp_workflow.py:1829`）加 `asyncio.wait_for(self._fetch_single_competitor(...), timeout=unit.deadline_s)`；`TimeoutError` → `UnitReport(state="blocked", reason="timeout")`。
- 不做全局调度器、不做优先队列。

### 5.3 实施顺序与回滚

1. M0（无行为变化）→ 观察 1 轮评测；
2. M1 + M4（行为变化，核心收益）→ 对照评测；
3. M2（只加观测）；
4. M3（续跑）→ 混沌测试；
5. 每步用独立开关（如 `RESEARCH_UNITS=1`、`RESEARCH_CHECKPOINT=1`），默认关；关闭即回到当前路径。

### 5.4 可序列化边界（最容易踩的坑）

`state` 里混有 `datetime`、`EvidenceStore`、`AsyncExitStack`、浏览器实例、`LLMClient` 等（`mvp_workflow.py:1740-1787`）。检查点**不能**直接 `json.dumps(state)`。提案：

- 白名单序列化：只写 `task_id/user_query/target_urls/units/status/report_id/citation_attempts/errors/quality_*`；
- 恢复时重建派生对象：`EvidenceStore.from_extracted_data(...)`（`mvp_workflow.py:1213` 已有构造入口）、爬虫按需惰性创建（`mvp_workflow.py:1819-1827` 已说明惰性）；
- 断言：检查点 JSON 中不得出现 `datetime`/非原语（启动自检）。

### 5.5 过度设计清单（明确不做）

| 不做 | 理由 |
| --- | --- |
| 通用 effect queue / 多车道调度器 | 单进程任务，副作用只有落盘与 SQLite；30 行检查点足够 |
| Cordis 式插件内核 / 动态能力注册 | 引入运行时，收益与成本倒挂（N1） |
| 多 agent 对话路由、线程订阅、PR 认领 | 产品形态不同（N2） |
| 全量 token/stream 事件溯源 | 存储膨胀；现有 `TaskEvent` 粒度已够（N3） |
| 人工审批流 | 工具只读，审批无安全收益（N4） |
| 子任务递归分解（subtask 再拆 subtask） | 上游外层层级只到 Ticket；再拆会把预算与归因搞乱 |
| 向量库/长期记忆 | 本项目无跨任务记忆需求 |
| 为 resume 引入消息队列（Kafka/RabbitMQ） | SQLite 单机足够；多机再议 |

---

## 6. 验证方法（怎么证明改造有效）

### 6.1 基线口径

- 固定：同一 25 个 case（`evals/cases`）、同一 fake LLM 回放、同一依赖版本；基线用 `evals/baselines/v1.8.json`（23 case 记录）。
- 命令：`uv run python -m evals.runner`（全量）、`uv run python -m evals.runner --gate`（门禁）。
- 现有指标：`citation_coverage`、`citation_validity`、`grounding`、`dimension_coverage`，以及按类别的结果（`evals/baselines/v1.8.json`：`coverage` 类 n=3、`dimension_coverage=0.722`）。

### 6.2 新增指标（由 M0/M1/M3 的事件直接计算）

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| `unit_completion_rate` | `ready` 单元 / 总单元 | ≥ 基线 +5pp |
| `blocked_units` | `blocked/failed` 单元数及原因分布 | 可解释，`timeout` 占比可观测 |
| `reason_code_coverage` | 终态事件带 `reason_code` 的比例 | 全部终态事件（25/25 case） |
| `stage_pair_coverage` | `stage_start` 有对应 `stage_end` 的比例 | 每个阶段均配对（25/25 case） |
| `seq_monotonic` | 跨重启事件 seq 严格递增 | 全部事件（重启注入 n=3，3/3 序列单调） |
| `resume_success_rate` | 注入崩溃后成功续跑的比例 | ≥9/10 次崩溃注入 |
| `tokens_per_unit` | 单元级 token 均值 | ≤1.10× 基线（同一 25 case） |

### 6.3 对照组设计

- A/B 同机同模型同夹具，各跑 3 次，取中位数与 p95；报告差异时同时给置信区间（bootstrap）。
- 崩溃注入：在 `crawl` 与 `report_generate` 两个阶段各 kill -9 10 次，对比“重跑整单”与“从检查点续跑”的总耗时与重复 LLM 调用数。
- 回归底线：`grounding`、`citation_validity` 不得下降；`--gate` 必须仍通过（注入类 `1.00` 硬门槛，见 `evals/__init__.py:10`）。

### 6.4 判定阈值（建议写进 PR 描述）

- P0：`reason_code_coverage=100%`，失败任务可定位到 unit；
- P1：`dimension_coverage` ≥ 0.78（基线 0.722，coverage 类 n=3），`grounding` 不降；
- P2：`stage_pair_coverage=100%`，事件回放跨重启可用；
- P3：`resume_success_rate ≥ 90%`，恢复后重复 LLM 调用 < 5%。

---

## 7. 风险与未决问题

1. **Redis 与 SQLite 双写一致性**（事实）：现有事件在 Redis（`task_store.py:541-555`），检查点计划放 SQLite。提案是“SQLite 为真源，Redis 仅 fanout”，需要明确写入顺序与失败补偿；否则会出现“事件有、检查点无”。
2. **多实例部署**（推断）：若未来多 worker 共享 Redis，SQLite 本地检查点会失效，需要改为共享存储；当前单机假设必须写进文档。
3. **resume 的鉴权**（事实）：`server.py` 的任务接口以 `user_id` 隔离；新增 resume 端点必须复用同一鉴权，不能只凭 `task_id`。
4. **重跑边界**（推断）：抓取阶段重跑会重复付费；建议“单元级幂等 + 已 ready 单元不重跑”，但这需要 M1 先落地。
5. **上游可用性**（事实）：Super-agent 是研究原型（README L3-5），接口与语义可能变化；本文只借机制，不引入其代码或依赖。
6. **用户记忆的二义性**（事实）：若用户实际指的是 Harness 的 turn/step 两层，则第 2.2 节已覆盖，落地重点是 M2（step 级事件）而非 M1；建议开工前用一句话确认“是指 Router/Worker，还是 turn/step”。

---

## 8. 证据索引（便于复核）

### 8.1 上游（本地 clone，路径为仓库内相对路径）

**Super-agent（commit `8c32a9dc...`，MIT）**

- `README.md:3-5` 研究原型声明；`:7` 打包为 Harness bundle；`:39` MIT
- `docs/001-domain.md:33` Eval 的 “two layers”（用于排除同词异义）
- `docs/003-execution.md:23-29` Ticket/Executor Session/preset；`:31-47` 后台作业、槽位、回收与重启
- `docs/007-architecture.md:33` 启动恢复 interrupted Assignments；`:40` passes；`:72-86` 回调表；`:92` assignmentInterrupted；`:117` Ticket 唯一事实；`:174-178` 效果队列表；`:203` WorkerSlots；`:205` 重试
- `src/core/engine.ts:64` 原子提交；`:75` evaluateDue；`:100` decide
- `src/core/evals.ts:22-25` 批量 Eval；`:31-53` 截止/暂停；`:57` 超时重排；`:87-111` 评估记录；`:128-152` 决策
- `src/core/tickets.ts:37` create；`:95` followsDone；`:345` end
- `src/core/assignments.ts:66-71` turnEnded；`:78-92` interrupted；`:99-125` report 校验
- `src/core/stage.ts:9-21` 阶段判定
- `src/executor/dsh.ts:24` workerSessionId；`:171` WorkerSlots；`:229-230` maxWorkers；`:424-466` launch/prompt；`:531` cancel
- `src/app/tools/executor.ts:74-101` orchestrator_report 与原子 relay
- `src/app/context.ts:6-24` 角色提示；`src/app/effects.ts:5-7,26-44,57-66` 持久效果队列
- `src/core/memory.ts:1-9` 内存仓库与原子回滚（测试替身，不作为生产事实）

**Harness（commit `8bd07339...`，`dsh-agent-loop 0.2.2-alpha.3`，MIT）**

- `docs/architecture.md:59` append-only SessionEvent；`:81-106` turn flow；`:83` step/turn 定义；`:164` 扩展持久会话
- `packages/core/agent-loop/README.md:12` 创建/恢复会话与“call model, run tools, repeat”
- `packages/core/agent-loop/src/agent.ts:327-433` turn；`:435` step；`:363-385` 工具结果补写；`:404-417` aborted/error
- `packages/core/tools/src/batch.ts:187,221` 并发与收敛
- `packages/core/tools/src/pipeline.ts:185-215` 执行管线；`:422-428` approval seam

### 8.2 research-agent（当前工作树）

- `src/workflow/mvp_workflow.py:204` decompose；`:922` run；`:984` 写 subtasks；`:1064/1128/1132/1212` 阶段；`:1240-1304` 引用闭环；`:1461-1474` 终态出口；`:1502` ReAct 入口；`:1739-1787` state 初始化；`:1789-1869` 并发抓取；`:2756+` SQLite 落盘
- `src/schema/state.py:64-65` 状态；`:76-77` subtasks 字段
- `src/agent/runner.py:57` system prompt；`:74-220` 内层循环与 stop_reason
- `src/agent/tools.py:48-59` 只读白名单；`:70-98` 执行语义
- `src/agent/config.py:13-31` 模式与预算
- `src/workflow/task_store.py:35-65` 事件类型；`:477-507` CAS；`:541-555` add_event
- `src/storage/database.py:70-109` 表结构
- `server.py:1009-1040` 任务执行与进程内 event id；`:1243-1264` SSE 续传
- `evals/__init__.py:1-23` 评测定位与命令；`evals/baselines/v1.8.json` 基线
- `tests/e2e/test_research_flow.py:155-236` SSE 重连测试

### 8.3 二手资料的使用边界

检索阶段用到的解读文章（如 51CTO/智东西/腾讯云）只用于**发现关键词**（“Super-agent”“Router/Worker”“Harness”），文中任何事实都以第 8.1 节的一手源码为准，不引用其结论。

---

## 9. 开工前建议确认的三件事

1. 你说的“两层 agent loop”是指 **Router/Worker 两层会话**，还是 **Harness 的 turn/step**？（决定先做 M1 还是 M2）
2. 断点续跑的最小要求是“进程崩溃后恢复”还是“机器重启/多实例”？后者会推翻 SQLite 检查点方案。
3. 是否接受“单元级失败不拖垮整单、报告如实标注 blocked”的产品语义？（这是 C1 的收益来源，也是与现有“整单 failed”语义的冲突点）
