# Research Agent

AI-powered competitive research tool. Input natural language queries, automatically complete search, crawl, analysis, and reporting.

## One-liner

Helps product managers and market researchers quickly complete competitive analysis—no coding required, professional reports in 5 minutes.

## Features

| Feature | Description |
|---------|-------------|
| 🔍 Smart Search | SearXNG/Bing/SerpAPI, auto-retrieve competitor info |
| 🕷️ Web Crawling | Login scenarios, content extraction, SSRF protection |
| 📊 Competitive Comparison | Multi-product comparison of features/pricing/users |
| ✅ Quality Scoring | Data quality rating (A-F), missing dimension alerts |
| 💬 Follow-up Questions | Deep-dive into specific dimensions |
| 📄 PRD Generation | One-click from competitive report to product requirements |
| 🔄 Multi-role Review | Development/testing/operations perspective review |
| 📤 Doc Sync | One-click sync to Feishu/Tencent Docs |

## Architecture

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

## Workflow

1. User signs up or logs in
2. User enters research goal, target sites, and optional credentials
3. Backend creates async task; frontend subscribes to SSE events
4. System executes: search → crawl → extract → analyze → generate report
5. Result page shows report, PRD, source status, follow-up, review, export, and sync

The UI shows auditable workflow stages and source status, not private model reasoning.

## Quick Start

### Option 1: Docker (Recommended)

```bash
git clone https://github.com/JosephIvon/research-agent.git
cd research-agent
cp .env.example .env
# Edit .env: set at least MINIMAX_API_KEY, JWT_SECRET_KEY, and SEARXNG_SECRET
docker compose up -d --build
```

Open:

- Frontend: http://localhost:3000
- API health check: http://localhost:8080/health
- SearXNG: http://localhost:8888

### Option 2: Local Development

```bash
# Backend
uv sync
uv run python server.py

# Frontend (new terminal)
cd frontend
yarn install --frozen-lockfile
yarn dev
```

For local Redis and SearXNG:

```bash
docker compose up -d redis searxng
```

## Supported LLM Providers

| Provider | Model | Note |
|----------|-------|------|
| Minimax | MiniMax-M2.5 | Default |
| DeepSeek | deepseek-chat | Optional |
| Doubao | Doubao-3.5 | Optional |
| GLM | glm-4 | Optional |
| OpenAI | gpt-4o | Optional |

Switch: `LLM_PROVIDER=deepseek`

## Supported Search Providers

| Provider | Note |
|----------|------|
| SearXNG | Default, free self-hosted |
| Bing | Fallback, no config needed |
| SerpAPI | Paid SaaS |

## Key Configuration

| Variable | Description |
|----------|-------------|
| `MINIMAX_API_KEY` | Default LLM API key |
| `JWT_SECRET_KEY` | JWT signing secret; **required in production** |
| `SEARXNG_SECRET` | SearXNG secret; **required in production** |
| `SEARCH_PROVIDER` | Search provider, defaults to `searxng` |
| `REDIS_URL` | Redis connection URL |
| `TASK_STORE_BACKEND` | Task storage backend, `redis` in Docker |
| `ALLOWED_HOSTS` | Trusted backend hosts |
| `CORS_ALLOWED_ORIGINS` | Allowed frontend origins |
| `API_AUTH_TOKEN` | Optional API Bearer token, recommended for public deployments |
| `LLM_CONNECT_TIMEOUT` | LLM connection timeout (default: 10s) |
| `LLM_READ_TIMEOUT` | LLM read timeout (default: 120s) |

## Common Commands

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

Full external search and LLM path requires real credentials. Enable with `E2E_RUN_FULL_FLOW=1`.

## Project Structure

```
research-agent/
├── frontend/              # Vue 3 Frontend UI
├── src/
│   ├── auth/             # JWT authentication & user management
│   ├── config/           # Configuration management
│   ├── crawler/          # Playwright web crawler
│   ├── llm/              # Multi-LLM provider abstraction
│   ├── logging/          # Audit logging
│   ├── storage/          # SQLite report storage
│   ├── sync/             # Feishu/Tencent Doc sync
│   ├── tools/            # Search, scoring, follow-up, PRD
│   └── workflow/         # Research workflow orchestration
├── tests/                # Test suite
├── docker-compose.yml
└── README.md
```

## API Endpoints

### Authentication

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/auth/register` | POST | Register and return access token |
| `/auth/login` | POST | Login and return access token |
| `/auth/me` | GET | Get current user info |

### Research

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/research/tasks` | POST | Create async research task |
| `/research/tasks/{task_id}` | GET | Get task status |
| `/research/tasks/{task_id}/events` | GET | SSE task event stream |
| `/research/competitive` | POST | Synchronous competitive analysis |
| `/research/followup` | POST | Follow-up questions |
| `/research/prd` | POST | PRD generation |
| `/research/prd-from-query` | POST | All-in-one (research→PRD) |
| `/research/history` | GET | List saved reports |
| `/research/history/{id}` | GET | Get report by ID |
| `/research/history/{id}` | DELETE | Delete report |
| `/research/compare/{id1}/{id2}` | GET | Compare report versions |
| `/research/versions/{id}` | GET | Get report version chain |
| `/research/export` | POST | Export PDF, Word or HTML |

### Settings & Sync

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/settings` | GET | Get runtime settings summary |
| `/sync/feishu` | POST | Sync to Feishu Docs |
| `/sync/tencent` | POST | Sync to Tencent Docs |

### Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/llm/status` | GET | LLM provider circuit breaker status |
| `/llm/circuit/reset` | POST | Reset LLM circuit breaker |

## Production Checklist

- [ ] Replace every placeholder secret in `.env`, especially `JWT_SECRET_KEY` and `SEARXNG_SECRET`
- [ ] Set production `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS`
- [ ] Persist and back up Redis, SQLite data and generated report files
- [ ] Put the API behind HTTPS and a reverse proxy; avoid exposing raw backend ports
- [ ] Run tests, frontend build and Compose config validation before release
- [ ] Configure rate limiting appropriate for your traffic

## Documentation

- [README.md](./README.md) - Chinese documentation (中文文档)
- This file - English documentation

> **Note**: Both documentation files are maintained in sync. Last updated: 2026-05-21.

## License

MIT - Commercial use, modification, and private use allowed.
