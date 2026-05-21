# Research Agent

智能竞品调研助手 / AI competitive research assistant.

---

## 中文

### 项目定位

Research Agent 面向产品、市场和创业团队。你输入调研问题、可选竞品网站和登录凭据，系统自动完成搜索、抓取、分析、报告生成和 PRD 生成，并在前端展示可审计的任务进度。

它解决的是：非技术人员想快速理解竞品、提炼产品机会、形成可交付文档，但不想手工搜索、截图、整理表格和写长报告。

### 核心能力

- 竞品调研：支持自然语言需求、多个竞品网站、每站独立登录凭据。
- 实时进度：任务队列 + SSE，展示需求理解、搜索、抓取、提取、报告和 PRD 生成阶段。
- 报告交付：竞品报告、PRD、追问、多角色审查、报告版本对比。
- 数据来源：默认 SearXNG，支持 Bing/SerpAPI 降级；Playwright 抓取网页内容。
- 安全基线：JWT 登录、Legacy Token 兼容、CORS/Host 收紧、速率限制、SSRF 防护、审计日志。
- 持久化：Redis 任务状态，SQLite 报告与版本数据，本地输出目录。
- 导出同步：PDF、Word、HTML 导出；飞书与腾讯文档同步入口。

### 技术架构

```text
Vue 3 + Element Plus
        |
        | REST / SSE
        v
FastAPI server
        |
        +-- Redis TaskStore
        +-- SQLite report storage
        +-- SearXNG / Bing / SerpAPI search
        +-- Playwright crawler
        +-- LLM client with retry, timeout and circuit breaker
        +-- Export and document sync tools
```

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
| `REDIS_URL` | Redis 连接地址 |
| `TASK_STORE_BACKEND` | 任务存储后端，Docker 默认 `redis` |
| `ALLOWED_HOSTS` | 后端可信 Host |
| `CORS_ALLOWED_ORIGINS` | 允许访问 API 的前端来源 |

### 常用命令

```bash
# 后端测试
uv run pytest tests/ -q --ignore=tests/e2e/

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
| `/settings` | GET | 运行配置摘要 |
| `/sync/feishu` | POST | 同步到飞书文档 |
| `/sync/tencent` | POST | 同步到腾讯文档 |
| `/health` | GET | 健康检查 |

### 上线检查

- 替换 `.env` 中所有占位密钥，尤其是 `JWT_SECRET_KEY` 和 `SEARXNG_SECRET`。
- 配置真实域名下的 `ALLOWED_HOSTS` 和 `CORS_ALLOWED_ORIGINS`。
- 确认 Redis、SQLite 输出目录和报告文件目录有持久化备份。
- 使用 HTTPS 和反向代理，不让后端 API 直接暴露到公网裸端口。
- 运行测试、前端构建和 Compose 配置检查。

---

## English

### What It Does

Research Agent is built for product, marketing and startup teams. Give it a research question, optional competitor URLs and login credentials, and it will search, crawl, analyze, generate a competitive report and produce a PRD with visible task progress.

It helps non-technical users turn scattered competitor information into useful product decisions and deliverable documents.

### Core Features

- Competitive research with natural-language input, multiple target sites and per-site credentials.
- Real-time task progress through a queue and SSE events.
- Report, PRD, follow-up questions, multi-role review and version comparison.
- Search through SearXNG by default, with Bing/SerpAPI fallback support.
- Web crawling through Playwright.
- Security baseline with JWT auth, legacy token compatibility, CORS/Host restrictions, rate limiting, SSRF protection and audit logs.
- Redis-backed task state and SQLite-backed report/version storage.
- PDF, Word and HTML export, plus Feishu and Tencent Docs sync entry points.

### Architecture

```text
Vue 3 + Element Plus
        |
        | REST / SSE
        v
FastAPI server
        |
        +-- Redis TaskStore
        +-- SQLite report storage
        +-- SearXNG / Bing / SerpAPI search
        +-- Playwright crawler
        +-- LLM client with retry, timeout and circuit breaker
        +-- Export and document sync tools
```

### Workflow

1. The user signs up or logs in.
2. The user enters a research goal, target sites and optional credentials.
3. The backend creates an async task; the frontend subscribes to SSE events.
4. The system searches, crawls, extracts, analyzes and generates report artifacts.
5. The result page shows the report, PRD, source status, follow-up, review, export and sync actions.

The UI shows auditable workflow stages and source status, not private model reasoning.

### Quick Start

#### Docker

```bash
cp .env.example .env
# Edit .env: set at least MINIMAX_API_KEY, JWT_SECRET_KEY and SEARXNG_SECRET
docker compose up -d --build
```

Open:

- Frontend: http://localhost:3000
- API health check: http://localhost:8080/health
- SearXNG: http://localhost:8888

#### Local Development

```bash
# Backend
uv sync
uv run python server.py

# Frontend
cd frontend
yarn install --frozen-lockfile
yarn dev
```

For local Redis and SearXNG:

```bash
docker compose up -d redis searxng
```

### Key Configuration

| Variable | Description |
| --- | --- |
| `MINIMAX_API_KEY` | Default LLM API key |
| `JWT_SECRET_KEY` | JWT signing secret; required in production |
| `SEARXNG_SECRET` | SearXNG secret; required in production |
| `SEARCH_PROVIDER` | Search provider, defaults to `searxng` |
| `REDIS_URL` | Redis connection URL |
| `TASK_STORE_BACKEND` | Task storage backend, `redis` in Docker |
| `ALLOWED_HOSTS` | Trusted backend hosts |
| `CORS_ALLOWED_ORIGINS` | Allowed frontend origins |

### Common Commands

```bash
# Backend tests
uv run pytest tests/ -q --ignore=tests/e2e/

# Frontend tests and build
cd frontend
yarn test
yarn build

# Validate Docker Compose config
docker compose --env-file .env.example config
```

E2E tests require a running backend:

```bash
E2E_API_URL=http://localhost:8080 uv run pytest tests/e2e/ -q
```

The full external search and LLM path requires real credentials. Enable it manually with `E2E_RUN_FULL_FLOW=1`.

### Main API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/auth/register` | POST | Register and return an access token |
| `/auth/login` | POST | Log in and return an access token |
| `/research/tasks` | POST | Create an async research task |
| `/research/tasks/{task_id}` | GET | Get task status |
| `/research/tasks/{task_id}/events` | GET | Stream task events through SSE |
| `/research/competitive` | POST | Run synchronous competitive research |
| `/research/prd-from-query` | POST | Research and generate a PRD |
| `/research/history` | GET | List saved reports |
| `/research/export` | POST | Export PDF, Word or HTML |
| `/research/compare/{id1}/{id2}` | GET | Compare report versions |
| `/settings` | GET | Get runtime settings summary |
| `/sync/feishu` | POST | Sync to Feishu Docs |
| `/sync/tencent` | POST | Sync to Tencent Docs |
| `/health` | GET | Health check |

### Production Checklist

- Replace every placeholder secret in `.env`, especially `JWT_SECRET_KEY` and `SEARXNG_SECRET`.
- Set production `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS`.
- Persist and back up Redis, SQLite data and generated report files.
- Put the API behind HTTPS and a reverse proxy; avoid exposing raw backend ports.
- Run tests, frontend build and Compose config validation before release.

## License

MIT
