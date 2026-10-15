# Research Agent

智能竞品调研助手 / AI competitive research assistant.

---

## 中文

### 项目定位

Research Agent 面向产品、市场和创业团队。你输入调研问题、可选竞品网站和登录凭据，系统自动完成搜索、抓取、分析、报告生成和 PRD 生成，并在前端展示可审计的任务进度。

它解决的是：非技术人员想快速理解竞品、提炼产品机会、形成可交付文档，但不想手工搜索、截图、整理表格和写长报告。

### 核心能力

- **双模式编排**：确定性流水线（默认）与有界 ReAct 工具循环（`AGENT_MODE=react`）灰度切换——runner 具步数/token/USD/墙钟四重预算与六种显式终止原因。
- **预算耗尽仍交付（PARTIAL）**：任一预算触发时不再直接中断，而是发一次不带工具、token 上限写死的收敛调用，把已获得的证据收成结论，终态标 `partial` 而非 `completed`——前端与落库都能区分"跑完"和"被截断"，报告照常交付。
- **引用校验闭环**：报告事实句带 `[e:id]` 证据标记，确定性校验引用覆盖/有效/数值扎根/幻觉 URL，不过阈值带违规清单重生成一次，仍不过显式标记"低可信"交付，绝不静默（契约冻结点见 tag `citations-contract-frozen`）。
- **间接提示注入纵深防护**：边界标记、扫描中和（先剥零宽再匹配）、只读工具白名单（URL 执行点二次 SSRF 校验）、出口回声校验、审计计数、62 条确定性回归夹具；单条注入中和而非整单失败。
- **MCP Server**：调研能力以标准 MCP 协议暴露（`/mcp`，官方 SDK、无状态 streamable-http），工具 schema 与 Agent 注册表同源并有防漂移一致性测试，可被 Claude Desktop / Cursor 等外部客户端调用。
- **速率限制**：全局兜底中间件 + `/mcp` 显式守卫（中间件够不到 mount 子应用），按 token / IP 双口径分桶，可整体关停。
- **离线评测与 CI 门禁**：13 条确定性 case（常规 4 / 冲突口径 2 / 资料缺失 2 / 注入 2 / 跨 10 来源综合 1 / ReAct 对照 1 / 预算截断 1），夹具 sha256 防篡改，注入通过率=1.00 为硬门槛；报告页 👍/👎 反馈回流为评测用例种子。
- **可观测与成本**：任务级 token/USD 计量与预算守卫（真实 usage 优先，估算必打标；逐页抽取开销同样入账）；Langfuse 全链路 trace（contextvars 关联，未启用零开销）。
- 竞品调研：自然语言需求、多竞品网站、每站独立登录凭据；任务队列 + SSE 实时进度（含 Agent 思考过程面板）。
- 报告交付：竞品报告、PRD、追问、多角色审查、版本对比；PDF/Word/HTML 导出，飞书与腾讯文档同步。
- 安全基线：JWT 登录、SQLite 租户隔离（无权一律 404）、特权端点查库鉴权（降权即时生效）、SSRF 全链路拦截、占位密钥生产三道硬失败。
- 持久化：Redis 任务状态（WATCH 乐观锁防并发丢写，终态回写 SQLite），SQLite 报告/版本/反馈数据。

### 技术架构

```text
Vue 3 + Element Plus（思考面板 / 注入提示 / 反馈 / Trace / 成本卡）
        |
        | REST / SSE（agent_step / partial / injection_detected 等事件流）   MCP 客户端
        v                                                                        |
FastAPI server ──── /mcp 挂载（无状态 streamable-http，独立限流守卫）
        |
        +-- AGENT_MODE=legacy：确定性流水线（分解→抓取→清洗→map-reduce 抽取→门禁→报告）
        |       +-- 注入防护（边界/中和/canaries/出口校验）
        |       +-- EvidenceStore + 引用校验四指标 → 不过重生成 → 低可信标记
        +-- AGENT_MODE=react：有界 ReAct（web_search/fetch_site/extract_facts/compare）
        |       +-- 只读工具注册表（白名单/入参校验/URL 二次 SSRF/超时/统一返回）
        |       +-- TaskBudget 记账（token+USD）→ 超额收敛为 PARTIAL 终态
        +-- Redis TaskStore（WATCH CAS + pub/sub 分发）  +-- SQLite（报告/版本/反馈/任务终态）
        +-- SearXNG / Bing / SerpAPI 搜索  +-- Playwright 爬虫（批次共享实例）
        +-- LLM 网关（5 provider + fake 确定性回放；熔断/退避/降级路由）
        +-- Langfuse trace（contextvars，no-op 降级）

evals/（离线评测 + CI 门禁）── 13 case 驱动真实工作流全链路，夹具 sha256 防篡改
```

### 工程与评测指标

全部数字可复现（命令见下），出自 `evals/baselines/v1.2.json` 与测试运行：

| 指标 | 数值 |
| --- | --- |
| 后端测试 | 580 passed / 7 skipped（跳过项全部为环境性：需本地后端、真实凭据或真实 Redis） |
| 前端测试 | 40 全绿，`vite build` 通过 |
| 离线评测门禁 | 13/13 case 全部完成（其中 1 条以 `partial` 交付），PASS |
| 引用覆盖 / 有效 / 扎根（全类目） | 1.0 / 1.0 / 1.0 |
| 注入夹具通过率（CI 硬门槛） | 1.00 |
| 交付率 / 截断率 | 1.00 / 0.077（`partial` 计入交付，另单列截断率，不抹平） |
| ReAct 对照 | 抓取削减 66.7%（6 页→2 页），引用三率不降 |
| 跨 10 来源综合 | 10/10 来源被有效引用 |

**指标边界（请连同上表一起读）**

- 引用四指标只证明"未发现格式性造假"，**不证明事实正确**；"数据完整度"独立呈现并声明不衡量结论正确性。
- 注入 1.00 是**本仓库自建夹具集上的全绿**，不是对所有攻击的保证；六层纵深里输出校验是最后一道硬边界。
- 截断率 0.077 = 13 条里的 1 条，是**专门造出来的预算 case**（把 token 额度压到实测收敛区间内逼出 `partial`），**不代表真实业务有 7.7% 的任务被截断**。该 case 用翻转实验自证是预算导致终止：同一份夹具放开额度即 `completed`，额度压到开跑即超额则 `failed`——只断言"是 partial"证明不了任何事。该指标此前恒为 0.00（没有 case 能触发它），门禁对它只提示不判红。
- 扎根性判定允许**证据数值之间的乘除推导**（年化 ×12、折扣 ×8/10 等），这部分不阻塞。已知取舍与边界：
  - 加减已被实测消融移除——它让"两个不相关的价格相减恰好等于目标"这类巧合蒙混过关（`99 - 49 = 50`），代价是"提升 15 个百分点"这类财务口径不再被认作推导。
  - 剩余边界：单条证据里含多个互不相关的数时，两个证据数相乘仍可能构成巧合。真实抽取形态（每条证据 0~3 个数）下漏放行率 0.2%~1.3%，其中大部分是合法年化换算；该上限由回归测试钉住，不会悄悄变大。
  - 评测口径与阈值见 `evals/`；M7（RAG 门控）为何判定"不做"的完整证据见 [`docs/experiments/m7-gate-decision.md`](docs/experiments/m7-gate-decision.md)。

### 业务流程

1. 用户注册或登录。
2. 在首页输入调研目标，可补充多个竞品网站和账号密码。
3. 后端创建异步任务，前端通过 SSE 订阅进度。
4. 系统执行搜索、登录抓取、信息提取、竞品分析和 PRD 生成。
5. 任务完成后进入结果页，查看报告、PRD、来源状态、追问、审查、导出和同步。

系统展示的是可审计的工作阶段和来源状态，不展示模型私密思考链。

### 快速开始

#### Docker 启动

```bash
cp .env.example .env
# 编辑 .env：至少设置 MINIMAX_API_KEY、JWT_SECRET_KEY、SEARXNG_SECRET
docker compose up -d --build
```

访问：

- 前端：http://localhost:3000
- 后端健康检查：http://localhost:8080/health
- SearXNG：http://localhost:8888

#### 本地开发

```bash
# 后端
uv sync
uv run python server.py

# 前端
cd frontend
yarn install --frozen-lockfile
yarn dev
```

本地开发如需 Redis 和 SearXNG：

```bash
docker compose up -d redis searxng
```

### 关键配置

| 变量 | 说明 |
| --- | --- |
| `MINIMAX_API_KEY` | 默认 LLM 调用密钥 |
| `JWT_SECRET_KEY` | JWT 签名密钥，生产环境必须替换 |
| `SEARXNG_SECRET` | SearXNG 服务密钥，生产环境必须替换 |
| `SEARCH_PROVIDER` | 搜索提供方，默认 `searxng` |
| `AGENT_MODE` | 编排模式：`legacy`（默认）或 `react`（有界 ReAct） |
| `AGENT_MAX_STEPS` / `AGENT_TOKEN_BUDGET` / `AGENT_USD_BUDGET` / `AGENT_WALL_CLOCK_BUDGET` | ReAct 四重预算（默认 8 步 / 120k tokens / $0.5 / 180s） |
| `AGENT_BUDGET_CONVERGE_MAX_TOKENS` | 预算耗尽时那次收敛调用的 token 上限（默认 2048，置 0 关闭收束直接失败） |
| `LLM_PROVIDER` | LLM 提供商，支持 minimax/deepseek/doubao/glm/openai/fake（fake 为确定性回放，用于测试与评测） |
| `LANGFUSE_ENABLED` | Langfuse 追踪开关，默认关闭（零开销 no-op） |
| `RATE_LIMIT_ENABLED` | 速率限制总开关，默认开启；关闭后全局中间件与 `/mcp` 守卫同时停用 |
| `RATE_LIMIT_PER_IP` / `RATE_LIMIT_PER_TOKEN` | 限流阈值，默认 `60/minute` / `120/minute` |
| `REDIS_URL` | Redis 连接地址 |
| `TASK_STORE_BACKEND` | 任务存储后端，Docker 默认 `redis` |
| `ALLOWED_HOSTS` | 后端可信 Host |
| `CORS_ALLOWED_ORIGINS` | 允许访问 API 的前端来源 |

### 常用命令

```bash
# 后端测试
uv run pytest tests/ -q --ignore=tests/e2e/

# 离线评测 + 门禁（零网络零密钥，确定性）
uv run python -m evals.runner --gate

# 前端测试与构建
cd frontend
yarn test
yarn build

# 检查 Docker Compose 配置
docker compose --env-file .env.example config
```

端到端测试需要正在运行的后端：

```bash
E2E_API_URL=http://localhost:8080 uv run pytest tests/e2e/ -q
```

完整外部搜索和 LLM 链路依赖真实密钥，可设置 `E2E_RUN_FULL_FLOW=1` 手动开启。

### 主要 API

| Endpoint | Method | 说明 |
| --- | --- | --- |
| `/auth/register` | POST | 注册并返回访问令牌 |
| `/auth/login` | POST | 登录并返回访问令牌 |
| `/research/tasks` | POST | 创建异步调研任务 |
| `/research/tasks/{task_id}` | GET | 查询任务状态 |
| `/research/tasks/{task_id}/events` | GET | SSE 任务事件流 |
| `/research/competitive` | POST | 同步竞品分析接口 |
| `/research/prd-from-query` | POST | 调研并生成 PRD |
| `/research/history` | GET | 历史报告列表 |
| `/research/export` | POST | 导出 PDF、Word 或 HTML |
| `/research/compare/{id1}/{id2}` | GET | 报告版本对比 |
| `/research/reports/{id}/feedback` | POST/GET | 报告 👍/👎 反馈与计数（评测用例回流入口） |
| `/mcp` | MCP | 标准 MCP 端点（无状态 streamable-http，需鉴权，独立限流） |
| `/settings` | GET | 运行配置摘要 |
| `/sync/feishu` | POST | 同步到飞书文档 |
| `/sync/tencent` | POST | 同步到腾讯文档 |
| `/health` / `/health/ready` | GET | 存活 / 就绪（LLM 密钥与 MCP 依赖体检） |

### 上线检查

- 替换 `.env` 中所有占位密钥，尤其是 `JWT_SECRET_KEY` 和 `SEARXNG_SECRET`。
- 配置真实域名下的 `ALLOWED_HOSTS` 和 `CORS_ALLOWED_ORIGINS`。
- 确认 `RATE_LIMIT_ENABLED` 的阈值与你的实际流量匹配（默认按 token / IP 双口径）。
- 确认 Redis、SQLite 输出目录和报告文件目录有持久化备份。
- 使用 HTTPS 和反向代理，不让后端 API 直接暴露到公网裸端口。
- 运行测试、前端构建和 Compose 配置检查。

---

## English Version

For the complete English documentation, see [README_EN.md](./README_EN.md).

## License

MIT
