# Research Agent

AI-powered competitive research tool. Input natural language queries, automatically complete search, crawl, analysis, and reporting.

## One-liner

Helps product managers and market researchers quickly complete competitive analysis—no coding required, professional reports in 5 minutes.

## Features

| Feature | Description |
|---------|-------------|
| 🔀 Dual-mode orchestration | Deterministic pipeline (default) + bounded ReAct tool loop (`AGENT_MODE=react`): step/token/USD/wall-clock budgets, six explicit stop reasons |
| 📦 Budget-aware partial delivery | When any budget trips, the runner does not simply abort — it issues one tool-free convergence call with a hard token ceiling, folds the evidence it already has into an answer, and terminates as `partial` rather than `completed`. The report still ships, and both the UI and the persisted state can tell "finished" apart from "truncated" |
| ✅ Citation verification loop | Every numeric claim carries an `[e:id]` evidence marker; deterministic checks (coverage / validity / numeric grounding / hallucinated URLs); failed reports are regenerated once with a violation list, then explicitly marked "low-trust" — never silently delivered. The prompt contract is frozen at tag `citations-contract-frozen` |
| 🛡️ Indirect prompt-injection defense | Boundary markers, scan-and-neutralize (zero-width stripped first), read-only tool whitelist with second-pass SSRF validation at execution points, output echo checking, audit counters, 62 deterministic regression fixtures |
| 🔌 MCP Server | Research capabilities exposed over standard MCP (`/mcp`, official SDK, stateless streamable-http); tool schemas share a single source with the agent registry (consistency-tested); callable from Claude Desktop / Cursor |
| 🚦 Rate limiting | Global backstop middleware plus an explicit `/mcp` guard (the middleware cannot reach mounted sub-apps); bucketed by token and by IP; switchable off globally |
| 📊 Offline evals + CI gate | 13 deterministic cases (regular 4 / conflicting-sources 2 / missing-data 2 / injection 2 / 10-source synthesis 1 / ReAct comparison 1 / budget truncation 1), sha256-pinned fixtures, injection pass rate = 1.00 as a hard gate; 👍/👎 report feedback feeds new eval cases |
| 📈 Observability & cost | Per-task token/USD accounting with budget guard (real usage first, estimates always flagged; per-page extraction cost is accounted too); Langfuse tracing via contextvars, zero-overhead when disabled |
| 🔎 Smart Search | SearXNG/Bing/SerpAPI, auto-retrieve competitor info |
| 🕷️ Web Crawling | Login scenarios, batch-shared browser instance, SSRF protection |
| 📋 Data completeness scoring | Honest "completeness" scoring, separated from trustworthiness (which belongs to citation checks) |
| 💬 Follow-up Questions / 📄 PRD / 🔄 Multi-role Review / 📤 Doc Sync | Deep-dive Q&A, one-click PRD, dev/test/ops review, Feishu/Tencent Docs sync |

## Architecture

```text
Vue 3 + Element Plus (thinking panel / injection notice / feedback / trace / cost card)
        |
        | REST / SSE (agent_step / partial / injection_detected events)   MCP clients
        v                                                                        |
FastAPI server ──── /mcp mount (stateless streamable-http, dedicated rate-limit guard)
        |
        +-- AGENT_MODE=legacy: deterministic pipeline (decompose→crawl→sanitize→map-reduce extract→gates→report)
        |       +-- injection defense (boundary / neutralize / canaries / output check)
        |       +-- EvidenceStore + 4 citation metrics → regenerate-once → low-trust marking
        +-- AGENT_MODE=react: bounded ReAct (web_search/fetch_site/extract_facts/compare)
        |       +-- read-only tool registry (whitelist / arg validation / URL re-validation / timeouts)
        |       +-- TaskBudget accounting (token+USD) → convergence into a PARTIAL terminal state
        +-- Redis TaskStore (WATCH-based CAS + pub/sub fanout)   +-- SQLite (reports/versions/feedback/task status)
        +-- SearXNG / Bing / SerpAPI search   +-- Playwright crawler (batch-shared instance)
        +-- LLM gateway (5 providers + deterministic fake playback; breaker/backoff/failover)
        +-- Langfuse tracing (contextvars, no-op fallback)

evals/ (offline evals + CI gate) ── 13 cases drive the real workflow end-to-end, sha256-pinned fixtures
```

## Engineering & Eval Metrics

All numbers are reproducible (commands below); sources: `evals/baselines/v1.2.json` and test runs.

| Metric | Value |
|--------|-------|
| Backend tests | 580 passed / 7 skipped (every skip is environmental: needs a local backend, real credentials, or a real Redis) |
| Frontend tests | 40 passing, `vite build` clean |
| Offline eval gate | 13/13 cases completed (one delivered as `partial`), PASS |
| Citation coverage / validity / grounding (all categories) | 1.0 / 1.0 / 1.0 |
| Injection fixture pass rate (hard CI gate) | 1.00 |
| Delivery rate / truncation rate | 1.00 / 0.077 (`partial` counts as delivered; truncation is tracked separately so it is never smoothed away) |
| ReAct comparison | 66.7% fewer pages fetched (6→2), citation metrics not degraded |
| 10-source synthesis | 10/10 sources cited with valid evidence |

**Read these metrics together with their limits**

- The four citation metrics prove "no format-level fabrication", **not** factual correctness. "Data completeness" is presented separately and explicitly declared as not measuring correctness.
- The 1.00 injection rate is green on **this repository's own fixture set** — it is not a guarantee against all attacks. Output validation is the last hard boundary in the six-layer defense.
- The 0.077 truncation rate is 1 of 13 cases — a **deliberately constructed budget case** (the token allowance is pushed into the measured convergence band to force a `partial`). It does **not** mean 7.7% of real tasks get truncated. That case self-certifies that the budget is what terminates it via a flip experiment: the same fixture with the budget released finishes as `completed`, and with the budget so small it trips before the first round it is `failed`. Asserting "it is partial" alone proves nothing. The metric used to sit at a constant 0.00 (no case could trigger it); the gate only warns about it, never fails on it.
- Grounding accepts **multiplicative derivations between evidence numbers** (annualisation ×12, a 20% discount as ×8/10, …); those do not block delivery. Known trade-off and residual boundary:
  - Addition and subtraction were removed from the operation table after measured ablation — they let "two unrelated prices whose difference happens to equal the target" pass as a derivation (`99 - 49 = 50`). The cost is that financial phrasings like "a 15-percentage-point increase" are no longer accepted as derived.
  - Residual boundary: when a single evidence item holds several unrelated numbers, multiplying two of them can still coincide with a fabricated value. On real extraction shapes (0–3 numbers per evidence item) the measured leakage rate is 0.2%–1.3%, most of which is legitimate annualisation. A regression test pins that ceiling so it cannot silently grow.
- See `evals/` for the harness and thresholds; [`docs/experiments/m7-gate-decision.md`](docs/experiments/m7-gate-decision.md) records the worked evidence for the M7 (RAG gating) "do not build" decision.

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
| `AGENT_MODE` | Orchestration mode: `legacy` (default) or `react` (bounded ReAct) |
| `AGENT_MAX_STEPS` / `AGENT_TOKEN_BUDGET` / `AGENT_USD_BUDGET` / `AGENT_WALL_CLOCK_BUDGET` | ReAct budgets (defaults: 8 steps / 120k tokens / $0.5 / 180s) |
| `AGENT_BUDGET_CONVERGE_MAX_TOKENS` | Token ceiling for the convergence call issued when a budget trips (default 2048; set 0 to disable convergence and fail outright) |
| `LLM_PROVIDER` | LLM provider: minimax/deepseek/doubao/glm/openai, plus `fake` for deterministic playback (tests & evals) |
| `LANGFUSE_ENABLED` | Langfuse tracing toggle, off by default (zero-overhead no-op) |
| `RATE_LIMIT_ENABLED` | Master rate-limit switch, on by default; turning it off disables both the global middleware and the `/mcp` guard |
| `RATE_LIMIT_PER_IP` / `RATE_LIMIT_PER_TOKEN` | Rate-limit thresholds (defaults: `60/minute` / `120/minute`) |
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

# Offline evals + gate (deterministic, zero network & credentials)
uv run python -m evals.runner --gate

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
│   ├── agent/            # Bounded ReAct runner, tool registry, citations
│   ├── auth/             # JWT authentication & user management
│   ├── config/           # Configuration management
│   ├── crawler/          # Playwright web crawler
│   ├── llm/              # Multi-LLM provider abstraction + token budget
│   ├── logging/          # Audit logging
│   ├── mcpserver/        # Standard MCP endpoint (stateless streamable-http)
│   ├── observability/    # Langfuse tracing (contextvars, no-op fallback)
│   ├── security/         # URL validation & injection defense
│   ├── storage/          # SQLite report/feedback storage
│   ├── sync/             # Feishu/Tencent Doc sync
│   ├── tools/            # Search, scoring, follow-up, PRD
│   └── workflow/         # Research workflow orchestration (legacy & react)
├── evals/                # Offline deterministic eval suite + CI gate
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
| `/research/reports/{id}/feedback` | POST/GET | 👍/👎 report feedback & counts (feeds the eval suite) |
| `/research/export` | POST | Export PDF, Word or HTML |

### Settings & Sync

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/settings` | GET | Get runtime settings summary |
| `/sync/feishu` | POST | Sync to Feishu Docs |
| `/sync/tencent` | POST | Sync to Tencent Docs |
| `/mcp` | MCP | Standard MCP endpoint (stateless streamable-http, auth required, independently rate-limited) |

### Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` / `/health/ready` | GET | Liveness / readiness (LLM credential & MCP dependency checks) |
| `/llm/status` | GET | LLM provider circuit breaker status |
| `/llm/circuit/reset` | POST | Reset LLM circuit breaker |

## Production Checklist

- [ ] Replace every placeholder secret in `.env`, especially `JWT_SECRET_KEY` and `SEARXNG_SECRET`
- [ ] Set production `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS`
- [ ] Tune `RATE_LIMIT_PER_IP` / `RATE_LIMIT_PER_TOKEN` to your actual traffic
- [ ] Persist and back up Redis, SQLite data and generated report files
- [ ] Put the API behind HTTPS and a reverse proxy; avoid exposing raw backend ports
- [ ] Run tests, frontend build and Compose config validation before release

## Documentation

- [README.md](./README.md) - Chinese documentation (中文文档)
- This file - English documentation

> **Note**: Both documentation files are maintained in sync. Last updated: 2026-10-14.

## License

MIT - Commercial use, modification, and private use allowed.
