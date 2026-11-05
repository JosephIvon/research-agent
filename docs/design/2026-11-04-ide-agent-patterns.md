# IDE 智能体工具范式拆解与 research-agent 设计标准

- 日期：2026-11-04
- 范围：TraeCode/Trae 当前会话可见的智能体工具范式 vs `D:\workspace\research-agent`
- 纪律：不改代码、不做 git 写操作；所有"缺失"断言均先直读文件并给行号
- 证据分级：**[事实]** 可直读验证；**[推断]** 由可见证据外推；**[工作假设]** 待验证
- 路径口径：文中 `C:\Users\admin\...` 与 `D:\workspace\...` 均为**仓库外的本机路径**，随机器而变，只作当时取证记录

---

## 0. 结论先行

1. **research-agent 不是"缺工具"，而是缺"工具契约元数据 + 长任务生命周期 + 失败分类"**。它已经有统一返回、只读白名单、SSRF 二次校验、预算收敛、Langfuse trace、29 条离线评测和整套测试（通过数口径见 [README.md](file:///d:/workspace/research-agent/README.md#L63)，`uv run pytest -q` 含参数化），工具数量不是瓶颈。
2. **相对 IDE 范式，最大的结构性差距有 5 个**：工具副作用/幂等/错误码未声明（D1）、输出无游标分页（D2）、ReAct 历史无压缩且无规则/技能/记忆注入（D6）、任务不可取消/不可断点续跑（D7）、只有单循环没有子会话与延迟工具发现（D8）。其中 D8 属于"场景不需要"，D6 的知识注入属于"不该抄"。
3. **最该补的 3 项**：
   - **A. 任务生命周期闭环**：取消 + 终态单调 + 阶段级断点续跑（D7）。
   - **B. 工具契约 v2**：副作用/幂等/错误码 + 输出游标（D1+D2）。
   - **C. 失败分类与恢复决策表**：机器可读错误码 → 重试/降级/补抓（D4）。
4. **补了也白补的 3 项**：
   - **多智能体/子会话编排**（单机批处理、无并行独立研究单元，收益 < 成本）。
   - **逐工具人工审批 + Plan/Spec 模式**（全部工具只读，审批只增加延迟）。
   - **skills/记忆/插件市场 + 延迟工具发现**（只有 6 个静态工具，配置面大于收益）。
5. **总评分**：IDE 13/20，research-agent 13/20。**总分持平但分布互补**：IDE 强在工具契约、上下文治理、编排；research-agent 强在可观测与评测回归。因此不应照总分做"全面对齐"，只补关键 3 项。

---

## 1. 取证方法与边界

### 1.1 三类证据

| 类型 | 来源 | 用途 |
|---|---|---|
| 一手会话事实 | 本会话系统提示中的工具定义、权限模式说明、memory/skills/rules 注入块 | IDE 工具清单与契约特征 |
| 本机可读配置 | `C:\Users\admin\.trae-cn` 下 permission、plugin-config、installed-plugins、skill-config、plugins、builtin skills | 权限档位、插件/技能/连接器证据 |
| 仓库源码 | `D:\workspace\research-agent` 的 `src/`、`tests/`、`evals/`、`server.py` | research-agent 现状与行号证据 |

### 1.2 IDE 内部不可验证清单

以下内容**没有**可直读证据，本报告只标注、不编造：

- 主循环如何拼装 system prompt、如何决定工具可见性；
- 上下文压缩算法与触发阈值（系统提示只说明"接近上限会自动压缩"）；
- 权限模式内部判定、审批弹窗的规则引擎；
- 会话标题/分支的内部实现（**本会话未暴露 title/branch 工具**，仅见 `new_context` 与会话来源标签）；
- IDE 自身的评测集、评分与回归门禁；
- 工具调用结果的内部截断阈值（只能看到各工具声明的上限参数）。

> **[事实]** 本会话可见工具里没有"重命名会话/创建分支"这一类元操作工具；因此"标题/分支"只能标为"能力不可验证"，不能写成"IDE 有"。

---

## 2. IDE 范式拆解

### 2.1 工具清单（本会话一手）

按职责分组，均为本会话真实可见：

| 组 | 工具 | 关键参数/特征 |
|---|---|---|
| 文件读取 | `Read`、`Glob`、`Grep` | `Read` 支持 `offset/limit`、图片与 `target`；`Grep` 支持 `output_mode`、`-A/-B/-C`、`head_limit/offset`、`glob/type`、`multiline` |
| 文件写入 | `Edit`、`Write`、`DeleteFile` | 精确替换、全量写、批量删；写操作走审批 |
| 执行 | `Shell`、`StopCommand` | `cwd`、`timeout`(最大 600s)、`run_in_background`、沙箱与 `dangerouslyDisableSandbox` |
| 编排 | `Agent`、`TaskOutput`、`TaskStop`、`SendMessage` | 子智能体类型、`resume`、`run_in_background`；子智能体**不能再派子智能体**（深度 1） |
| 上下文 | `TodoWrite`、`new_context`、`NotifyUser` | 任务清单状态机；`new_context` 带 handoff 摘要 + 最多 9 个文件重开；计划/规格审批通知 |
| 发现与延迟加载 | `ToolSearch`、`DeferExecuteTool` | 延迟工具的 schema 不加载即不可调用，先搜后执行 |
| 技能/插件 | `Skill` | `plugin:skill` 命名空间、参数透传 |
| 人机交接 | `browser_waiting_for_user_interaction` | 登录/验证码等交给用户 |
| 延迟工具 | `WebSearch`、`WebFetch`、`GenerateImage`、`PureShowWidget`、`Exec` | 需先 `ToolSearch` 再 `DeferExecuteTool` |
| MCP | `chrome-devtools`、`cloudflare-api`、`codegraph` 等命名空间 | 外部服务以 MCP 工具暴露 |

### 2.2 工具契约特征逐条

| 特征 | IDE 的做法 | 证据 |
|---|---|---|
| 参数校验 | JSON Schema 风格的字段/必填/枚举；工具描述里写死范围（如 `timeout` 最大 600000ms） | 系统提示工具定义 **[事实]** |
| 结构化返回 | 文件读取返回带行号内容；Grep 有 content/count/files 三种模式；MCP 返回 JSON | 系统提示 + MCP 说明 **[事实]** |
| 输出截断 | `Read` 默认 2000 行、图片 10MB；`Grep` 默认 `head_limit=100`；Shell 默认 120s、最大 600s | 系统提示工具定义 **[事实]** |
| 分页/游标 | `Read` 的 `offset/limit`、`Grep` 的 `offset/head_limit`、记忆目录按日期分桶 | 系统提示 + memory 布局 **[事实]** |
| 只读/写区分 | 读工具与写工具分离；写工具、Shell、浏览器动作都可能触发审批 | 系统提示权限说明 **[事实]** |
| 审批点 | 非自动允许的工具调用会弹出用户批准；Plan/Spec 模式只读；`NotifyUser` 作为计划审批 | 系统提示 **[事实]** |
| 权限档位 | 全局 `permission/global.json` 当前为 `full_access` | [global.json](file:///C:/Users/admin/.trae-cn/permission/global.json#L1-L3) **[事实]** |
| 沙箱 | Shell 有文件系统/网络边界，越界需显式关闭沙箱并说明理由 | 系统提示 Shell 说明 **[事实]** |
| 会话发现 | 系统提示注入 primary working dir、OS、时区、附件目录、memory 目录、规则块 | 系统提示 **[事实]** |
| 子会话派发 | `Agent` 可指定 `subagent_type`、后台运行、`resume`；子代理不能再派子代理 | 系统提示 **[事实]** |
| 记忆注入 | `c:\Users\admin\.trae-cn\memory` 下项目记忆、`user_profile.md`、按日期 `topics.md`/`session_memory_*.jsonl` | 系统提示 memory 布局 **[事实]** |
| 规则注入 | workspace rules（`.trae/rules/project_rules.md`、`AGENTS.md`）+ user rules | 本会话上下文块 **[事实]** |
| 技能注入 | 系统提示列出内置/插件技能及触发条件，技能按需加载 | 本会话 `available_skills` **[事实]** |
| 插件模型 | 插件清单含 `skills/`、`connector.json`、`mcpServers`、`binaries`、`env`；连接器有 `auth_policy`（`ON_INVOKE`/`ON_INSTALL`） | [installed-plugins.json](file:///C:/Users/admin/.trae-cn/installed-plugins.json#L93-L196) **[事实]** |
| 插件市场分页 | 返回 `page.total/page/page_size` | [installed-plugins.json](file:///C:/Users/admin/.trae-cn/installed-plugins.json#L84-L92) **[事实]** |
| 技能配置 | `managedSkills` 与 `marketplaceSkills` 记录来源 | [skill-config.json](file:///C:/Users/admin/.trae-cn/skill-config.json#L1-L16) **[事实]** |
| 插件启用 | `.claude/settings.json` 的 `enabledPlugins` | [settings.json](file:///d:/workspace/research-agent/.claude/settings.json#L1-L5) **[事实]** |
| 元操作 | `TodoWrite` 有合并语义、单 `in_progress`；`new_context` 是显式上下文切换 | 系统提示 **[事实]** |
| 标题/分支 | 未见工具；不可验证 | **[不可验证]** |

**本机配置补充证据**

- 用户级规则文件：`C:\Users\admin\.trae-cn\user_rules\rule-1792071981220.md`（本会话已读取，内容作为 user_rules 注入）。
- 内置技能定义：`C:\Users\admin\.trae-cn\builtin\baseline\code\default\skills\dynamic-ui\SKILL.md` 等（目录存在，按需加载）。
- 插件配置与启用：`C:\Users\admin\.trae-cn\plugin-config.json`、`.claude/settings.json`。
- 项目级规则：`.trae/rules/project_rules.md`、`AGENTS.md`。

### 2.3 从范式反推的设计意图

**[推断]** IDE 的工具层不是"函数集合"，而是四层契约：

1. **能力契约**：schema + 必填 + 范围 + 返回形状，保证模型可预测调用。
2. **安全契约**：只读/写分类、权限档位、审批、沙箱，保证"模型不能悄悄造成副作用"。
3. **上下文契约**：截断、分页、延迟加载、压缩、handoff，保证"上下文是预算而不是垃圾桶"。
4. **编排契约**：子会话、后台任务、resume、stop、send message，保证"长任务可管理"。

**[工作假设]** 这四层是 IDE 好用的主因，而不是某个具体工具。research-agent 应对齐契约层，而不是复制工具清单。

---

## 3. research-agent 现状（带行号）

### 3.1 架构与两种模式

- 双模式：`AGENT_MODE=legacy|react`，默认 `legacy`；ReAct 上限 `AGENT_MAX_STEPS=8`、token 120k、墙钟 180s、USD 0.5。见 [config.py](file:///d:/workspace/research-agent/src/agent/config.py#L13-L32)。
- 确定性流水线 `MVPWorkflow.run`：[mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L922-L930)；含拆解、搜索、抓取、抽取、质量门禁、引用校验、readiness、落库。
- 有界 ReAct：[runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L53-L74)；每轮工具调用、重复/空结果检测、预算收敛。
- 六类显式 stop reason：`budget_exceeded`、`error`、`halt`、`duplicate_loop`、`empty_observations`、`max_steps`。见 [runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L78-L220)。
- 预算耗尽不直接失败：`_converge_on_budget` 生成 `partial` 交付。见 [runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L222-L242) 与 [mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L1462-L1469)。

### 3.2 工具注册与执行

- `Tool` 契约字段：`name/description/parameters/handler/timeout/read_only/url_params`。见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L42)。
- **只读白名单强制**：`read_only=False` 直接拒绝注册。见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L54-L59)。
- **统一返回** `{ok,data,error,elapsed}`；工具异常/超时归一为 `ok=False`，不终止 Runner。见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L98)。
- **参数校验**：必填、JSON 对象、URL 参数二次 SSRF。见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L100-L117)。
- 注册的 6 个工具：`web_search`（[tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L78-L95)）、`fetch_site`（[L115-L132](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L115-L132)）、`extract_facts`（[L201-L217](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L201-L217)）、`compare_competitors`（[L239-L249](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L239-L249)）、`verify_entity`（[L301-L312](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L301-L312)）、`discover_competitors`（[L340-L351](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L340-L351)）。
- MCP 只额外暴露 `generate_research_report`，且刻意不放进 ReAct 工具集，避免递归。见 [tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L356-L370) 与 [mcpserver/server.py](file:///d:/workspace/research-agent/src/mcpserver/server.py#L31-L76)。

### 3.3 上下文管理

- 粗粒度 token 估算（约 4 字符/token）。见 [context.py](file:///d:/workspace/research-agent/src/agent/context.py#L10-L19)。
- 单次观察结果字符预算 6000；`fetch_site` 只给 LLM 1500 字符预览，全文进 workspace。见 [context.py](file:///d:/workspace/research-agent/src/agent/context.py#L21-L47)、[tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L27-L28)。
- 逐页 map-reduce 抽取，最多 5 页。见 [context.py](file:///d:/workspace/research-agent/src/agent/context.py#L49-L86)、[tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L142)。
- **缺口 [事实]**：ReAct 的 `messages` 只追加不压缩；仅靠 `max_steps` 与 token 预算兜底，未发现跨轮摘要或历史折叠逻辑（静态阅读 [runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L53-L220)）。
- **缺口 [事实]**：`src/` 内未检索到 `skills`/`memory`/`approval`/`subagent`/`resume`/`checkpoint` 命中（本报告 1.2 节除外）。

### 3.4 状态持久化

- `TaskStatus`：`pending/running/completed/partial/failed`，**没有 `cancelled`**。见 [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L26-L32)。
- `TaskEventType` 是事件名唯一登记处，`_emit` 对表，未登记即抛错。见 [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L35-L69)。
- `PersistenceBackend` 抽象：`create/get/update/add_event/set_artifact/list_tasks/subscribe`。见 [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L257-L295)。
- Redis 实现带 `WATCH` 乐观锁 CAS，失败重读重放。见 [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L477-L509)。
- SSE 支持 `Last-Event-ID` 回放，先订阅再取快照以避免窗口丢事件。见 [server.py](file:///d:/workspace/research-agent/server.py#L1232-L1260)。
- SQLite 报告/版本/反馈落库；任务终态回写。见 [mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L2756-L2857)。

### 3.5 错误、并发、中断

- 工具层：失败归一为可观察结果，不抛异常。见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L98)。
- LLM 层：可重试错误分类、指数退避、熔断器。见 [llm/__init__.py](file:///d:/workspace/research-agent/src/llm/__init__.py#L145-L145)、[L295-L330](file:///d:/workspace/research-agent/src/llm/__init__.py#L295-L330)、[L431-L440](file:///d:/workspace/research-agent/src/llm/__init__.py#L431-L440)。
- 并发：每个请求 `asyncio.create_task` 起后台任务；SSE 订阅队列；Redis CAS 防丢写。见 [server.py](file:///d:/workspace/research-agent/server.py#L1180-L1195)、[task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L183-L255)。
- **缺口 [事实]**：没有取消端点；`server.py` 中 `cancel` 只出现在 `fut.cancelled()` 的收尾判断（[L1185-L1193](file:///d:/workspace/research-agent/server.py#L1185-L1193)），不是任务取消能力。
- **缺口 [事实]**：没有断点续跑/阶段 checkpoint；`src/` 未检索到 `resume`/`checkpoint`。
- 抓取侧有并发闸门：`asyncio.Semaphore`（[concurrency.py](file:///d:/workspace/research-agent/src/crawler/concurrency.py#L71-L72)）。
- **缺口 [事实]**：任务级（请求级）并发上限未见显式闸门；并发压力依赖部署与 Redis。

### 3.6 可观测性

- Langfuse trace/span 全走 `contextvars`，未启用零开销，观测失败不拖垮主流程。见 [langfuse_tracer.py](file:///d:/workspace/research-agent/src/observability/langfuse_tracer.py#L1-L110)、[L113-L145](file:///d:/workspace/research-agent/src/observability/langfuse_tracer.py#L113-L145)。
- 任务详情返回 `trace_url`、注入命中数、成本报告。见 [server.py](file:///d:/workspace/research-agent/server.py#L1215-L1229)。
- 审计日志：研究开始事件。见 [server.py](file:///d:/workspace/research-agent/server.py#L1172-L1178)。
- 健康检查 `/health`、`/health/ready`。见 [server.py](file:///d:/workspace/research-agent/server.py#L1795-L1835)。

### 3.7 评测与测试

- 测试数统一取 `uv run pytest -q` 的 passed 用例数（含参数化）；权威口径见 [README.md](file:///d:/workspace/research-agent/README.md#L63-L65)。**不要**用 `def test_` 静态计数或历史快照数当口径。
- 离线评测 25 个 case（`"id"` 计数 25）；`manifest.json` 描述仍写"共 23 条"，属文档内漂移。见 [manifest.json](file:///d:/workspace/research-agent/evals/cases/manifest.json#L1-L15)。
- 夹具 sha256 防篡改、注入通过率硬门槛、CI 门禁（README）。
- MCP schema 与 Agent 注册表同源，有防漂移测试。见 [test_mcp_schema_consistency.py](file:///d:/workspace/research-agent/tests/test_mcp_schema_consistency.py#L63-L127)。
- 交付 readiness 评分：`assess_delivery_readiness`。见 [readiness.py](file:///d:/workspace/research-agent/src/agent/readiness.py#L129-L206)。

### 3.8 安全

- JWT/legacy token 中间件。见 [server.py](file:///d:/workspace/research-agent/server.py#L187-L216)。
- 租户隔离：无权返回 404 而非 403。见 [server.py](file:///d:/workspace/research-agent/server.py#L1204-L1212)。
- 特权端点查库鉴权。见 [server.py](file:///d:/workspace/research-agent/server.py#L639-L641)。
- 限流：全局中间件 + 逐路由 + MCP 独立限流。见 [server.py](file:///d:/workspace/research-agent/server.py#L135-L183)。
- SSRF 全链路 + 只读白名单 + 注入防护（README 第 21 行；工具层见 [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L100-L117)）。

---

## 4. 对照表：IDE 有 / research-agent 有 / 差距 / 本质还是形态

| # | 能力 | IDE 有 | research-agent 有 | 差距 | 本质/形态 | 证据 |
|---|---|---|---|---|---|---|
| 1 | 工具 schema 与必填校验 | 有 | 有 | 无 | 已对齐 | [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L42) |
| 2 | 读写副作用/幂等声明 | 有分类，未见显式幂等元数据 | 仅 `read_only` 布尔 | 缺幂等/副作用枚举 | 形态 | [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L31) |
| 3 | 机器可读错误码 | 工具报错文本 + 可操作建议 | 文本 `error` + 六类 stop reason | 缺稳定错误码/可重试标记 | 形态 | [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L98) |
| 4 | 工具输出截断 | 有 | 有（切片/观察预算） | 无 | 已对齐 | [context.py](file:///d:/workspace/research-agent/src/agent/context.py#L21-L47) |
| 5 | 输出分页/游标 | `offset/limit`、`head_limit` | 无游标；slice 截断 | 缺 `next_cursor` | 形态 | [tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L197-L199) |
| 6 | 参数边界/SSRF | 沙箱+权限 | SSRF+必填+白名单 | 无 | 已对齐（方向不同） | [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L100-L117) |
| 7 | 失败重试/熔断 | 有建议，无可见熔断 | 有分类退避+熔断 | 缺"错误码→恢复决策"表 | 形态 | [llm/__init__.py](file:///d:/workspace/research-agent/src/llm/__init__.py#L295-L330) |
| 8 | 权限审批 | 权限档位+审批+沙箱 | JWT/租户/限流/只读，无审批 | 无审批点 | **本质**（全只读） | [server.py](file:///d:/workspace/research-agent/server.py#L187-L216) |
| 9 | 审计 | 未见 | 有 | 无（RA 更强） | — | [server.py](file:///d:/workspace/research-agent/server.py#L1172-L1178) |
| 10 | 上下文压缩 | 有自动压缩 | 有预算/观察压缩，无历史折叠 | 缺跨轮压缩 | 形态 | [runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L53-L220) |
| 11 | 规则/技能/记忆注入 | 有 | 运行时无 | 缺注入层 | **本质**（单用途批处理） | [project_rules.md](file:///d:/workspace/research-agent/.trae/rules/project_rules.md) |
| 12 | 会话/子会话 | 有 | 只有 task，无子会话 | 缺子会话 | **本质**（无独立并行单元） | [server.py](file:///d:/workspace/research-agent/server.py#L1152-L1180) |
| 13 | 延迟工具发现 | 有 | 无（6 个静态工具） | 无 | **本质**（工具少） | [tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L39-L353) |
| 14 | 会话 resume / task cancel | 有 | resume 无、cancel 无 | 两项都缺 | 形态 | [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L26-L32) |
| 15 | 事件流回放 | 不适用/不可见 | 有 SSE + Last-Event-ID | 无（RA 更强） | — | [server.py](file:///d:/workspace/research-agent/server.py#L1232-L1260) |
| 16 | 成本/trace | 不可验证 | Langfuse + cost_report | 无（RA 更强） | — | [langfuse_tracer.py](file:///d:/workspace/research-agent/src/observability/langfuse_tracer.py#L1-L110) |
| 17 | 评测/回归 | 不可验证 | 25 case + 全量测试 + CI | 无（RA 更强） | — | [manifest.json](file:///d:/workspace/research-agent/evals/cases/manifest.json#L1-L15) |
| 18 | 计划-执行分离 | Plan/Spec 模式 | 确定性阶段 + readiness 门禁 | 形态不同，基本等价 | 形态 | [readiness.py](file:///d:/workspace/research-agent/src/agent/readiness.py#L129-L206) |

**判读**：

- **本质差距**只有 3 处：审批（无写操作）、规则/技能/记忆（单用途）、子会话/延迟工具发现（工具少且无并行单元）。
- **形态差距**集中在工具契约元数据、分页、错误码、历史压缩、取消/续跑。
- research-agent 在**可观测、事件回放、评测回归**上反超 IDE。

---

## 5. 智能体设计成熟度 rubric（10 维，0/1/2）

### 5.1 评分规则

- 每维按"可直读证据"打分；**2 = 核心判据全部满足；1 = 部分满足；0 = 缺失/不可验证**。
- "不可验证"按 0 计，并在备注写"不可验证"，避免把猜测算成能力。
- 复算方式：按每维列出的判据逐条打 ✔/✘，再按 5.1 映射。

### 5.2 rubric 定义

| 维度 | 0 分 | 1 分 | 2 分 | 复算判据 |
|---|---|---|---|---|
| D1 工具契约自描述 | 自由文本工具 | 有 schema/必填/超时 | 再加副作用、幂等、错误码 | ①JSON Schema ②必填/类型 ③读写副作用 ④幂等 ⑤超时 ⑥错误码 |
| D2 输出可控 | 裸文本 | 统一结构+截断 | 再加游标分页+空/失败区分 | ①统一 `{ok,...}` ②截断 ③`next_cursor` ④空结果≠失败 |
| D3 边界安全 | 无校验 | 必填/类型校验 | 再加 URL/SSRF/注入/越权拦截 | ①必填 ②类型 ③URL 边界 ④注入/越权拦截 |
| D4 失败分类与恢复 | 抛异常/静默 | 归一失败+重试 | 再加错误码+可重试标记+熔断/降级+终止原因 | ①错误码 ②可重试标记 ③退避 ④熔断/降级 ⑤终止原因 |
| D5 权限与审批 | 无 | 认证+租户+只读白名单 | 再加审批点（审计为加分项） | ①认证 ②租户隔离 ③读写分类 ④审批 ⑤审计（加分） |
| D6 上下文治理 | 无界增长 | 预算或压缩单项 | 预算+压缩+分页检索+规则/技能/记忆注入 | ①token 预算 ②观察压缩 ③历史摘要 ④分页检索 ⑤知识注入 |
| D7 状态与恢复 | 内存态 | 持久化+事件回放 | 再加断点续跑+取消+幂等重放 | ①持久化 ②事件回放 ③续跑 ④取消 ⑤幂等 |
| D8 编排与扩展 | 硬编码 | 单循环+静态工具 | 再加子会话+延迟发现+计划/执行分离 | ①子会话 ②延迟工具发现 ③计划/执行 ④多角色 |
| D9 可观测 | 无 | 日志 | trace/成本/审计/健康检查/事件流 | ①trace ②成本 ③审计 ④健康 ⑤事件流 |
| D10 评测与回归 | 无 | 单测 | 确定性评测集+防篡改+CI+schema 防漂移 | ①评测集 ②确定性 ③防篡改 ④CI 门禁 ⑤防漂移 |

### 5.3 打分：IDE（Trae 会话）

| 维度 | 分 | 依据（✔/✘） |
|---|---|---|
| D1 | 1 | ①✔ ②✔ ③✔（读写分离） ④✘ ⑤✔ ⑥✘ |
| D2 | 2 | ①✔ ②✔ ③✔（offset/limit） ④✔ |
| D3 | 2 | ①✔ ②✔ ③✔（沙箱/URL） ④✔（权限拦截） |
| D4 | 1 | ①✘ ②✔（错误文本可操作） ③✔ ④✘（熔断不可见） ⑤✔（StopCommand） |
| D5 | 1 | ①✘ 本地应用无认证/租户 ②✘ ③✔ 读写分离 ④✔ 审批 ⑤✘ 审计不可验证 |
| D6 | 2 | ①✔ ②✔ ③✔ ④✔（memory/源码检索） ⑤✔（rules/skills/memory） |
| D7 | 1 | ①✔ ②✔ ③✘ ④✔（Stop/TaskStop） ⑤✘ |
| D8 | 2 | ①✔ ②✔ ③✔ ④✔ |
| D9 | 1 | ①✘ ②✘ ③✘ ④✘ ⑤✔（会话/工具日志可见） |
| D10 | 0 | 全部不可验证 |
| **合计** | **13/20** | |

> 评分说明：IDE 的 D5 扣分是"设计面完整性"扣分（本地应用无认证/租户/审计），不代表其会话内权限能力弱；其审批与沙箱为本会话一手验证。D10 为"不可验证"，按 0 计。

### 5.4 打分：research-agent

| 维度 | 分 | 依据（✔/✘，含证据） |
|---|---|---|
| D1 | 1 | ①✔ schema ②✔ 必填 ③✔ `read_only` ④✘ 幂等 ⑤✔ timeout ⑥✘ 错误码（[tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L42)） |
| D2 | 1 | ①✔ 统一结构 ②✔ 截断 ③✘ 无游标 ④✔ 空≠失败（[tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L98)） |
| D3 | 2 | ①✔ ②✔ ③✔ SSRF ④✔ 注入/越权；额外：未知字段丢弃（[tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L100-L124)） |
| D4 | 1 | ①✘ 字符串 ②✘ 无 `retryable` ③✔ 退避 ④✔ 熔断 ⑤✔ 六类 stop reason（[llm/__init__.py](file:///d:/workspace/research-agent/src/llm/__init__.py#L295-L330)、[runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L78-L220)） |
| D5 | 1 | ①✔ JWT ②✔ 404 ③✔ 只读 ④✘ 审批 ⑤✔ 审计（[server.py](file:///d:/workspace/research-agent/server.py#L187-L216)、[L1204-L1212](file:///d:/workspace/research-agent/server.py#L1204-L1212)、[L1172-L1178](file:///d:/workspace/research-agent/server.py#L1172-L1178)） |
| D6 | 1 | ①✔ token 预算 ②✔ 观察压缩 ③✘ 历史摘要 ④✘ 分页检索 ⑤✘ 知识注入（[context.py](file:///d:/workspace/research-agent/src/agent/context.py#L15-L47)） |
| D7 | 1 | ①✔ Redis/SQLite ②✔ SSE 回放 ③✘ 续跑 ④✘ 取消 ⑤✘ 幂等（[task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L26-L32)、[server.py](file:///d:/workspace/research-agent/server.py#L1232-L1260)） |
| D8 | 1 | ①✘ 子会话 ②✘ 延迟发现 ③✔ 阶段/readiness ④✘ 多角色（单 ReAct） |
| D9 | 2 | ①✔ Langfuse ②✔ cost_report ③✔ 审计 ④✔ health ⑤✔ SSE（[langfuse_tracer.py](file:///d:/workspace/research-agent/src/observability/langfuse_tracer.py#L1-L145)） |
| D10 | 2 | ①✔ 25 case ②✔ fake provider ③✔ sha256 ④✔ CI ⑤✔ MCP 防漂移（[manifest.json](file:///d:/workspace/research-agent/evals/cases/manifest.json#L1-L15)、[test_mcp_schema_consistency.py](file:///d:/workspace/research-agent/tests/test_mcp_schema_consistency.py#L63-L127)） |
| **合计** | **13/20** | |

### 5.5 最该补的 3 项（按价值/成本比）

| 优先级 | 维度 | 为什么最该补 | 不补的后果 |
|---|---|---|---|
| P0 | **D7 任务生命周期** | 长任务 180s+，现在只能等；进程重启任务卡死；用户无法止损 | 运维只能杀进程；失败任务无法续跑，重复烧 token |
| P1 | **D1+D2 工具契约 v2** | 6 个工具已够用，但错误码/副作用/幂等/游标缺失使工具无法被可靠编排 | 任意新增工具靠约定，契约漂移只能靠人审 |
| P2 | **D4 失败分类与恢复** | 已有重试/熔断，但错误是字符串，无法做"限流→换源/超时→补抓/参数错→停止"的决策 | 失败处理靠 if-else 文本匹配，回归不可测 |

### 5.6 补了也白补的 3 项

| 不建议补 | 维度 | 理由 |
|---|---|---|
| 多智能体/子会话编排 | D8 | 单机批处理没有独立并行的研究单元；已有 `_fetch_all_competitors` 并行抓取，子会话只增加合并与成本 |
| 逐工具人工审批 + Plan/Spec | D5/D8 | 全部工具只读；风险边界已由 SSRF/白名单/租户隔离覆盖，审批只增加延迟 |
| skills/记忆/插件市场 + 延迟工具发现 | D6/D8 | 6 个静态工具、单用途流水线；配置面和加载开销大于收益 |

**次优先但值得做**：D6 的"跨轮历史摘要"。它只在 ReAct 长任务（接近 120k）才有收益，应等 D7/D4 完成后再做。

---

## 6. 分阶段实施路线（改动提案 → 实施记录）

> **实施状态（2026-11-05 更新）**：P1/P2/P3 已全部落地并通过验收——
> 后端全量测试 **1199 passed / 7 skipped（环境性）/ 0 failed**，
> 离线评测门禁 **29 case（含 4 条契约 case）PASS**。
> 三阶段实施细节与实际落点见下文各小节的「实施落点」补充框。

### 6.1 阶段总览

| 阶段 | 目标 | 主要改动 | 验收判据 | 风险与回退 |
|---|---|---|---|---|
| P1 契约 v2 | 工具可声明副作用/幂等/错误码，返回可游标分页 | `src/agent/tools.py`、`src/agent/tool_specs.py`、新增 `src/agent/error_codes.py`、新增 `tests/test_tool_contract_v2.py` | 6 个工具 100% 声明新字段；错误码覆盖率 100%；旧行为测试全绿 | 改 dataclass 默认值保持兼容；新增字段有默认值；回退仅回退 3 个文件 |
| P2 生命周期 | 任务可取消、终态单调、阶段 checkpoint、可续跑 | `src/workflow/task_store.py`、`server.py`、`src/workflow/mvp_workflow.py`、`src/agent/runner.py`、新增 `tests/test_task_lifecycle.py` | 取消 ≤2s 生效且不再调用 LLM；SSE 收到 `task_cancelled`；重启后续跑不重复抓取/不重复落库 | 终态 CAS 单调；取消与完成竞态用"终态先写者赢"；回退删除端点与状态字段 |
| P3 上下文与输出 | 历史摘要、证据分页、评测扩展 | `src/agent/context.py`、`src/agent/runner.py`、`src/agent/tool_specs.py`、`evals/cases/manifest.json`、新增 case | 12 轮合成任务 token 不超 120k；证据分页无重无漏；评测 25→29 条全绿 | 摘要只影响 prompt，不动 workspace；回退摘要开关与游标字段 |

### 6.2 P1：工具契约 v2

**新增契约（提案）**

```python
# src/agent/error_codes.py（新增）
class ToolErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    UNSAFE_URL = "unsafe_url"
    NOT_FOUND_IN_WORKSPACE = "not_found_in_workspace"
    TIMEOUT = "timeout"
    UPSTREAM_RATE_LIMIT = "upstream_rate_limit"
    UPSTREAM_AUTH = "upstream_auth"
    UPSTREAM_5XX = "upstream_5xx"
    PARSE_ERROR = "parse_error"
    INTERNAL = "internal"
```

```python
# src/agent/tools.py（扩展 Tool，不改既有默认行为）
@dataclass
class Tool:
    ...
    side_effects: Literal["none", "read", "write"] = "read"
    idempotent: bool = True
    error_codes: tuple[ToolErrorCode, ...] = ()
    output_cursor: bool = False
```

返回结构只做**加法**：保留 `error` 字符串，新增 `error_code`、`retryable`、`next_cursor`（默认 `None`），避免破坏现有消费方（[tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L98)）。

**具体改动点**

- [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L42)：扩 `Tool` 字段；`to_schema` 增加 `x-side-effects`/`x-idempotent` 扩展键（不污染 OpenAI 标准字段）。
- [tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L70-L117)：`execute`/`_validate_arguments` 返回结构化错误码；异常映射表放 `error_codes.py`。
- [tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L78-L351)：6 个 `Tool(...)` 声明补齐 `side_effects="read"`、`idempotent=True`、`error_codes`。
- 新增 `tests/test_tool_contract_v2.py`：断言每个注册工具的 4 个新字段非空、错误码属于枚举、未知工具返回 `unknown_tool`。

**验收判据**

1. `ToolRegistry` 注册的每个工具都有 `side_effects/idempotent/error_codes`，缺失即测试失败。
2. 人为触发 6 类错误（缺参、URL 非法、workspace 未抓取、超时、上游 429、解析失败），返回的 `error_code` 分别命中枚举。
3. 既有测试全绿（口径见 [README.md](file:///d:/workspace/research-agent/README.md#L63)）；MCP 防漂移测试仍通过（[test_mcp_schema_consistency.py](file:///d:/workspace/research-agent/tests/test_mcp_schema_consistency.py#L63-L127)）。

**风险与回退**

- 风险：扩展键改变 MCP schema 指纹，可能影响外部客户端。对策：扩展键放 `x-` 命名空间，标准字段不动；MCP 测试覆盖。
- 回退：仅回退 `tools.py`/`tool_specs.py`/`error_codes.py` 与新增测试，无数据迁移。

**实施落点（2026-11-05，与提案的偏差标注）**

- ✅ `src/agent/error_codes.py`：`ToolErrorCode`（7 码）+ `ReasonCode`（含 `CANCELLED`）+ `RunOutcome` + `RETRYABLE_TOOL_ERROR_CODES` + `ToolContractError`（handler 显式报错码——提案未列，实施中补上，否则"未先抓取就抽取"会被兜底成可重试的 `execution_error`）。
- ✅ `src/agent/tools.py`：`Tool` 扩 `side_effects/idempotent/error_codes/paginated/page_field`；注册校验（read_only+write 拒绝、未登记错误码拒绝）；`execute` 返回 `{ok,data,error,elapsed,error_code,retryable[,nextOffset]}`；`offset/limit` 注入 schema 且不透传 handler。偏差：提案的 `next_cursor`/`x-` 扩展键改为 OpenAI 原生 `offset/limit` 参数 + 顶层 `nextOffset`，语义等价、无需自定义命名空间。
- ✅ `src/agent/tool_specs.py`：7 个工具全部显式声明元数据；`extract_facts`/`compare_competitors`/`discover_competitors` 前置不满足 → `precondition_failed`；`read_evidence` 未知 key → `not_found`；`generate_research_report` 声明 `idempotent=False`（付费流水线不宜盲目重试）。
- ✅ `src/mcpserver/server.py`：`input_schema` 改用注入分页参数后的 schema，MCP 客户端可见 `offset/limit`。
- ✅ 测试：`tests/test_tool_contract_v2.py`（26 项）；`test_mcp_server.py` 断言更新；验收第 2 条的"6 类错误触发"由契约 case 与专项测试共同覆盖。

### 6.3 P2：任务生命周期（取消 + 续跑）

**新增契约**

- `POST /research/tasks/{task_id}/cancel` → `202 {task_id, status:"cancelling"}`；终态重复取消返回当前状态（幂等）。
- `TaskStatus` 增加 `CANCELLED="cancelled"`，终态集合 `{completed, partial, failed, cancelled}` 单调不可逆。
- SSE 新增 `task_cancelled` 事件，进入 `TaskEventType` 登记表（[task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L35-L69)）。
- checkpoint 记录：`{stage, input_hash, artifact_key, completed_at}`，以 `set_artifact` 落库（复用 [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L277-L279)）。

**具体改动点**

- [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L26-L32)：加 `CANCELLED`；`update` 增加终态守卫（已是终态则拒绝覆盖）。
- [task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L477-L509)：CAS 写入里加"终态不可逆"判定。
- [server.py](file:///d:/workspace/research-agent/server.py#L1151-L1201) 后新增 cancel 端点；`task.cancel_requested` 走 CAS 更新。
- [mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L922-L930)：阶段边界检查 `cancel_requested`，命中则落 `cancelled` 并 `return`；在 `_fetch_all_competitors` 的批次间隙也检查。
- [mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L2756-L2857)：阶段完成后写 checkpoint artifact。
- 新增 `MvpWorkflow.resume(task_id)`：按 checkpoint 顺序跳过已完成阶段；`input_hash` 不匹配则从该阶段重跑。
- 新增 `tests/test_task_lifecycle.py`：取消、重复取消、完成后再取消、进程重启续跑、重复 artifact 五项。

**验收判据**

1. 用 fake provider 起一个长任务，1s 后取消：任务状态 ≤2s 变 `cancelled`，之后 LLM 调用计数为 0。
2. SSE 连接收到 `task_cancelled`，且 `Last-Event-ID` 重连能回放。
3. 在抓取阶段后杀进程再启动，`resume` 不重复搜索/抓取已完成阶段；报告哈希与不中断运行一致（fake provider）。
4. 重复 `cancel` 不报错、不产生第二个终态事件；已完成任务取消返回 `completed` 不变。
5. 终态后任何 `update` 不得回退状态。

**风险与回退**

- 风险 1：取消与完成竞态导致终态双写。对策：CAS + 终态单调；测试注入并发。
- 风险 2：续跑重复落库。对策：artifact key 由 `stage + input_hash` 生成，`set_artifact` 幂等覆盖。
- 回退：删除取消端点、`CANCELLED` 枚举与 checkpoint 写入；已产生的 artifact 不影响旧读取逻辑。

**实施落点（2026-11-05）**

- ✅ 取消链路：[server.py](file:///d:/workspace/research-agent/server.py#L1360) `POST /research/tasks/{task_id}/cancel`（幂等，终态直接返回）；`_cancel_events` 广播信号 → `MVPWorkflow.set_cancel_event` 阶段边界检查 → `TaskStatus.CANCELLED` + `task_cancelled` 事件；已完成任务取消返回原状态不回退。
- ✅ 终态单调：[task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L38-L48) `TERMINAL_STATUSES`（含 `cancelled`），update 时已终态且不同状态直接拒绝（L238 附近）。
- ✅ checkpoint/续跑：[mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L860) `_save_checkpoint` 每阶段落 SQLite；`_apply_resume` 按 `_resume_past_{stage}` 跳过已完成段；[server.py](file:///d:/workspace/research-agent/server.py#L1528) `POST /research/tasks/{task_id}/resume` 对外触发。偏差：checkpoint 实际走 SQLite `load_latest_checkpoint`（复用报告库），未用提案里的 artifact 通道——语义相同且崩溃后仍可读。
- ✅ 测试：`tests/test_lifecycle_v2.py` 12 项（终态不可回退、取消无副作用、事件序号跨连接存活、崩溃后幂等写、各阶段续跑到 completed）；评测 `contract_resume`/`contract_cancel` 两条确定性 case 进门禁。

### 6.4 P3：上下文与输出

**新增契约**

- 工具返回 `next_cursor`（不透明字符串，客户端只回传）；`fetch_site`/`extract_facts` 增加 `cursor` 入参。
- 新增只读工具 `list_facts(url, cursor=None, limit=20)`：从 workspace 分页读事实，单页 ≤4k 字符，返回 `next_cursor`。
- ReAct 历史治理：当估算 token 超过预算 70% 时，把"最近 2 轮之外"的工具观察折叠为 `{url, fact_ids, one_line}` 摘要；原文保留在 workspace，引用校验仍读原文。

**具体改动点**

- [context.py](file:///d:/workspace/research-agent/src/agent/context.py#L15-L47)：新增 `summarize_history(messages, keep_last_rounds=2)`；新增游标编解码（base64 of index）。
- [runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L53-L226)：每轮结束检查 token 占比，触发 `summarize_history`；不改变工具调用结果与 step 记录。
- [tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L197-L199)：`extract_facts` 返回 `next_cursor`；`fetch_site` 页面清单分页。
- [evals/cases/manifest.json](file:///d:/workspace/research-agent/evals/cases/manifest.json#L1-L15)：新增 4 条 case（取消、续跑、分页、错误码映射），版本升到 v1.9。
- 新增 `tests/test_tool_output_cursor.py`、`tests/test_context_summarize.py`。

**验收判据**

1. 100 条事实按 `limit=20` 分 5 页读取，无重复、无遗漏，末页 `next_cursor=None`。
2. 12 轮合成 ReAct 在 120k 内完成，引用 ID 不丢；关闭摘要时行为与现在一致（A/B 对照）。
3. 新增 4 条评测 case 全绿，原 25 条不回归；CI 门禁保持。
4. `summarize_history` 只改 prompt，不改 `workspace`；引用校验仍能定位原文。

**风险与回退**

- 风险：摘要丢关键数字。对策：摘要只保留 `url + fact_ids + 一行结论`，数字仍由引用校验从原文取；加回归 case。
- 风险：游标与 workspace 变更不一致。对策：游标带 `input_hash`，不匹配返回 `cursor_stale` 错误码。
- 回退：摘要函数与游标字段均为加法；删除后旧路径不变。

**实施落点（2026-11-05，与提案的偏差标注）**

- ✅ 证据回读：[context.py](file:///d:/workspace/research-agent/src/agent/context.py#L66-L116) `EvidenceIndex`（内容寻址，key = sha256 前 16 位）+ `read_page` 分块；新工具 `read_evidence`（[tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L409-L431)）复用注册表游标切片。偏差：提案的 `list_facts`/不透明 `cursor` 未做——`extract_facts` 本身已 `paginated`（page_field="facts"），事实分页已有；`read_evidence` 用统一 `offset/nextOffset` 而非自定义 cursor，与 P1 契约一致。
- ✅ 历史折叠：[context.py](file:///d:/workspace/research-agent/src/agent/context.py#L123-L183) `fold_history` 把较早的长 tool 观察折成 `evidence_key + digest` 指针；[runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L283) 在预算压力下触发。原文可经 `read_evidence` 回读且哈希前后一致（可断言），引用校验不受影响。
- ✅ 评测：manifest v1.9（29 case）新增 `contract_evidence_page`（分页拼回原文 sha256 一致）与 `contract_tool_decl`（读写声明拦截）；baseline v1.9 已重建，门禁 PASS。偏差：提案的"12 轮合成 ReAct A/B 对照"由 `test_context_fold.py` 的确定性断言替代（零网络零密钥）。

### 6.5 实施顺序与依赖

```
P1 契约 v2  →  P2 生命周期  →  P3 上下文/输出
      │              │
      └── 错误码为 P2 的"取消原因/失败分类"提供枚举
                     └── P3 的游标复用 P1 的 output_cursor 标记
```

**不要并行做**：P2 依赖 P1 的错误码；P3 依赖 P1 的 `output_cursor` 字段。并行会造成契约二次漂移。

---

## 7. 哪些 IDE 做法不该抄（过度设计清单）

| 做法 | 为什么不该抄 | 替代 |
|---|---|---|
| 子会话/多智能体编排 | 单机批处理、无独立并行研究单元；合并成本 > 收益 | 保留现有 `_fetch_all_competitors` 的受控并发 |
| 逐工具人工审批 | 6 个工具全只读；风险已由 SSRF/白名单/租户隔离覆盖 | 只在"新增写工具"时引入审批，当前不做 |
| Plan/Spec 双模式 | 已有确定性阶段 + readiness 门禁，等价能力已存在 | 继续用 `assess_delivery_readiness` |
| skills/记忆/插件市场 | 单用途流水线，没有跨项目复用需求 | 项目规则留在 `.trae/rules` 与 `AGENTS.md` |
| 延迟工具发现（ToolSearch/Defer） | 工具只有 6 个，搜索开销大于收益 | 静态注册表 + MCP schema 防漂移测试 |
| 会话标题/分支 | 无交互式多轮会话需求 | 任务 ID + 报告版本已够 |
| 浏览器交接/内联可视化/图像生成 | 与研究报告流水线无关 | 不引入 |
| MCP 客户端联邦 | 现在只需 MCP Server 对外暴露 | 保持 `/mcp` 单向暴露 |

---

## 8. 复算命令与证据索引

```powershell
# 测试通过数（权威口径：passed 用例数，含参数化；结果以 README.md 为准）
uv run pytest -q

# 评测 case 计数（本报告口径 = 25）
(Select-String -Path d:\workspace\research-agent\evals\cases\manifest.json -Pattern '"id"' -AllMatches).Matches.Count

# 确认没有续跑/恢复实现（应为 0 命中）
Get-ChildItem d:\workspace\research-agent\src -Recurse -Filter *.py |
  Select-String -Pattern 'resume|checkpoint'

# 确认没有取消端点（server.py 仅 1 处 fut.cancelled()）
Select-String -Path d:\workspace\research-agent\server.py -Pattern 'cancel'
```

**关键证据文件**

- 工具契约：[tools.py](file:///d:/workspace/research-agent/src/agent/tools.py#L23-L124)
- 工具 schema：[tool_specs.py](file:///d:/workspace/research-agent/src/agent/tool_specs.py#L39-L370)
- ReAct 循环：[runner.py](file:///d:/workspace/research-agent/src/agent/runner.py#L53-L242)
- 上下文：[context.py](file:///d:/workspace/research-agent/src/agent/context.py#L10-L86)
- 任务状态：[task_store.py](file:///d:/workspace/research-agent/src/workflow/task_store.py#L26-L69)
- 任务 API/SSE：[server.py](file:///d:/workspace/research-agent/server.py#L1151-L1260)
- 工作流：[mvp_workflow.py](file:///d:/workspace/research-agent/src/workflow/mvp_workflow.py#L922-L930)
- 可观测：[langfuse_tracer.py](file:///d:/workspace/research-agent/src/observability/langfuse_tracer.py#L1-L145)
- 评测：[manifest.json](file:///d:/workspace/research-agent/evals/cases/manifest.json#L1-L15)
- MCP 防漂移：[test_mcp_schema_consistency.py](file:///d:/workspace/research-agent/tests/test_mcp_schema_consistency.py#L63-L127)
- IDE 权限档位：[global.json](file:///C:/Users/admin/.trae-cn/permission/global.json#L1-L3)
- IDE 插件/连接器：[installed-plugins.json](file:///C:/Users/admin/.trae-cn/installed-plugins.json#L84-L196)

---

## 9. 事实 / 推断 / 工作假设汇总

**事实**

- IDE 工具具备 schema、截断、分页、权限审批、子会话、延迟工具发现、记忆/规则/技能注入。
- research-agent 具备统一工具返回、只读白名单、SSRF 二次校验、预算收敛、六类终止原因、Redis CAS、SSE 回放、Langfuse、29 条评测、全量测试（通过数口径见 [README.md](file:///d:/workspace/research-agent/README.md#L63)）。
- research-agent **没有**取消端点、`cancelled` 终态、断点续跑、子会话、审批点、运行时 skills/memory。

**推断**

- IDE 的四层契约（能力/安全/上下文/编排）是其好用主因；工具清单只是表象。
- research-agent 的可观测与评测已超过 IDE；差距集中在契约元数据与生命周期。

**工作假设**

- 取消与续跑是 research-agent 用户最可感知的缺口（需用真实使用数据验证）。
- 跨轮历史摘要在 8 步上限下收益有限，应排在 P3。

---

## 10. 一句话路线

**P1 先让工具"可声明、可分类"（副作用/幂等/错误码/游标），P2 再让任务"可取消、可续跑"（终态单调 + checkpoint），P3 最后让上下文"可折叠、可回读"（历史摘要 + 证据分页 + 评测扩到 29 条）。**
