# 智能调研助手 (Research Agent)

AI 驱动的竞品调研工具。输入自然语言，自动完成搜索、抓取、分析、报告全流程。

## 一句话定位

帮助产品经理、市场调研人员快速完成竞品分析，无需编写代码，5分钟生成专业报告。

## 核心功能

| 功能 | 说明 |
|------|------|
| 🔍 智能搜索 | 支持 SearXNG/Bing/SerpAPI，自动检索竞品信息 |
| 🕷️ 网页抓取 | 登录场景、内容提取、SSRF 防护 |
| 📊 竞品对比 | 多产品横向对比、功能/定价/用户画像 |
| ✅ 质量评估 | 数据质量评分 (A-F)，缺失维度标注 |
| 💬 追问扩展 | 基于报告追问，深入特定维度 |
| 📄 PRD 生成 | 一键从竞品报告生成产品需求文档 |
| 🔄 多角色审查 | 开发/测试/运营视角评审 |
| 📤 文档同步 | 飞书、腾讯文档一键同步 |

## 快速开始

### 方式一：Docker 一键启动（推荐）

```bash
git clone https://github.com/yourusername/research-agent.git
cd research-agent
cp .env.example .env
# 编辑 .env，填入 MINIMAX_API_KEY，并替换 SEARXNG_SECRET
docker compose up -d
# 访问 http://localhost:3000
```

### 方式二：本地开发

```bash
# 后端
pip install uv && uv sync
cp .env.example .env
uv run python server.py

# 前端（新窗口）
cd frontend && yarn install && yarn dev
```

## 支持的 LLM 提供商

| 提供商 | 模型 | 说明 |
|--------|------|------|
| Minimax | MiniMax-M2.5 | 默认 |
| DeepSeek | deepseek-chat | 可选 |
| 豆包 | Doubao-3.5 | 可选 |
| 智谱 GLM | glm-4 | 可选 |
| OpenAI | gpt-4o | 可选 |

切换方式：`LLM_PROVIDER=deepseek`

## 支持的搜索提供商

| 提供商 | 说明 |
|--------|------|
| SearXNG | 默认，免费自托管 |
| Bing | 降级方案，无需配置 |
| SerpAPI | 付费 SaaS |

## 关键配置

| 变量 | 说明 |
|------|------|
| `MINIMAX_API_KEY` | 默认 LLM 调用密钥 |
| `SEARXNG_SECRET` | SearXNG 生产密钥，Docker 启动必填 |
| `API_AUTH_TOKEN` | 可选 API Bearer Token，公网部署建议开启 |
| `SEARCH_PROVIDER` | 默认 `searxng`，可切换 `bing` 或 `serpapi` |

## 项目结构

```
research-agent/
├── frontend/            # Vue 3 前端 UI
├── src/
│   ├── llm/            # 多 LLM 提供商抽象层
│   ├── crawler/        # Playwright 网页抓取
│   ├── tools/          # 搜索、评分、追问、PRD 生成
│   ├── sync/           # 飞书/腾讯文档同步
│   └── workflow/       # 2-Agent 工作流
├── docker-compose.yml
└── LICENSE
```

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/research/competitive` | POST | 竞品分析 |
| `/research/followup` | POST | 追问 |
| `/research/prd` | POST | PRD 生成 |
| `/research/prd-from-query` | POST | 一体化（调研→PRD） |
| `/research/history` | GET | 历史报告列表 |
| `/research/history/{id}` | GET/DELETE | 读取或删除历史报告 |
| `/settings` | GET | 运行配置状态 |
| `/sync/status` | GET | 文档同步配置状态 |
| `/sync/feishu` | POST | 同步到飞书文档 |
| `/sync/tencent` | POST | 同步到腾讯文档 |
| `/health` | GET | 健康检查 |

## 许可证

MIT - 允许商用、修改、私有闭源

---

**有问题？** 欢迎提交 Issue 或 Pull Request
