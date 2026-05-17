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

## Quick Start

### Option 1: Docker (Recommended)

```bash
git clone https://github.com/yourusername/research-agent.git
cd research-agent
cp .env.example .env
# Edit .env, fill in MINIMAX_API_KEY, and replace SEARXNG_SECRET
docker compose up -d
# Visit http://localhost:3000
```

### Option 2: Local Development

```bash
# Backend
pip install uv && uv sync
cp .env.example .env
uv run python server.py

# Frontend (new terminal)
cd frontend && yarn install && yarn dev
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
| `SEARXNG_SECRET` | Production SearXNG secret, required by Docker startup |
| `API_AUTH_TOKEN` | Optional API Bearer token, recommended for public deployments |
| `SEARCH_PROVIDER` | Defaults to `searxng`; can switch to `bing` or `serpapi` |

## Project Structure

```
research-agent/
├── frontend/            # Vue 3 Frontend UI
├── src/
│   ├── llm/            # Multi-LLM Provider Abstraction
│   ├── crawler/        # Playwright Web Crawler
│   ├── tools/          # Search, Scoring, Follow-up, PRD
│   ├── sync/           # Feishu/Tencent Doc Sync
│   └── workflow/       # 2-Agent Workflow
├── docker-compose.yml
└── LICENSE
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| POST /research/competitive | Competitive analysis |
| POST /research/followup | Follow-up questions |
| POST /research/prd | PRD generation |
| POST /research/prd-from-query | All-in-one (research→PRD) |
| GET /research/history | Report history list |
| GET/DELETE /research/history/{id} | Read or delete a history report |
| GET /settings | Runtime configuration status |
| GET /sync/status | Doc sync configuration status |
| POST /sync/feishu | Sync to Feishu Docs |
| POST /sync/tencent | Sync to Tencent Docs |
| GET /health | Health check |

## License

MIT - Commercial use, modification, and private use allowed.
