# 上线前全面审查记录

审查日期：2026-05-17

## 审查范围

- 完成度：CLI、REST API、前端 UI、SearXNG 搜索、报告历史、追问、审查、PRD、文档同步、Docker 部署。
- 架构：2-Agent 工作流、LLM 多提供商入口、搜索工具、Playwright 抓取、FastAPI 服务、Vue/Vite 前端、Nginx 生产代理。
- 风险：API 契约错位、同步/异步状态不一致、密钥暴露、SSRF、前端 XSS 面、容器部署缺口、生产配置漂移。

## 已修复的上线阻塞项

| 领域 | 问题 | 处理 |
|---|---|---|
| 前端 API | 默认直连 `127.0.0.1:8080`，绕过代理和容器网络 | 默认使用 `/api`，Vite 和 Nginx 统一反代 |
| 前端依赖 | `package.json` 升级但锁文件被忽略 | 允许提交 `frontend/yarn.lock`，固定 Vite 6 和 esbuild 版本 |
| 业务流 | 后端同步返回，前端却进入轮询进度页 | 提交完成后直接进入报告页，进度页仅兼容旧链接 |
| 报告页 | 按不存在的 `task_id` 查询接口加载报告 | 增加历史报告读取接口和本地报告缓存 |
| 追问/审查/PRD | 页面传 `task_id`，后端要求报告正文 | 页面加载真实报告正文后再调用接口 |
| 历史报告 | 前端有页面，后端无接口 | 增加列表、读取、删除接口，限制报告 ID 格式 |
| 设置页 | 前端尝试保存 API 密钥到后端 | 改为安全状态页，只保存浏览器会话 Token |
| 同步页 | `/sync/status`、同步端点缺失，且前端无法指定报告 | 增加飞书/腾讯状态和同步端点，支持选择报告并传 `report_id`/`report_content` |
| Markdown 渲染 | 报告/PRD 页面使用 `v-html` | MarkdownIt 关闭原始 HTML，并用 DOMPurify 二次净化 |
| 图表资源 | ReportView 初始化 ECharts 后未释放 | 页面卸载时调用 `dispose()` |
| 工具包导入 | `src.tools` 仍导入已删除的 `search_tool` 实例 | 改为导出 `get_search_tool()` 懒加载工厂，并增加导入回归测试 |
| SearXNG | Compose/配置文件硬编码密钥 | 使用 `.env` 的 `SEARXNG_SECRET`，配置文件只保留占位值 |
| SearXNG 暴露面 | 8888 端口绑定所有网卡 | 改为 `127.0.0.1:8888:8080` |
| Docker | 没有生产前端 UI 服务 | 增加 `frontend/Dockerfile`、`nginx.conf` 和 Compose 服务 |
| 入口脚本 | `research-server=server:main` 指向不存在函数 | 增加 `server.main()` |
| 搜索错误 | SerpAPI 请求异常可能携带敏感查询参数 | 搜索异常改为通用错误，详情保留在异常链 |

## 剩余上线注意项

- `API_AUTH_TOKEN` 仍是轻量 Bearer 保护；公网建议前置网关、正式登录态、限流和审计。
- Playwright 抓取成本较高；高并发上线前建议接任务队列和全局速率限制。
- SearXNG 镜像仍使用 `latest`，生产环境建议进一步固定镜像版本或 digest。
- 前端 Token 存在 `sessionStorage`，只适合轻量内部使用；正式多用户场景应改为服务端会话或 OIDC。
- 调研任务仍是同步请求；v1 可上线内测，公网大流量建议接后台任务队列和实时进度推送。

## 验收入口

- 前端：`http://localhost:3000`
- 后端健康检查：`http://localhost:8080/health`
- SearXNG：`http://localhost:8888`

## 验收命令

```bash
uv run pytest
cd frontend && yarn build
docker compose config
```

## 2026-05-17 最终验收结果

| 检查项 | 结果 |
|---|---|
| 后端测试 | `uv run pytest`：9 passed |
| 前端依赖锁 | `yarn install --frozen-lockfile`：Already up-to-date |
| 前端构建 | `yarn build`：Vite 6.4.2 构建通过 |
| 前端依赖审计 | `yarn audit --level moderate`：0 vulnerabilities |
| Python 依赖审计 | `pip-audit` 基于 `uv.lock` 导出的 pinned requirements：No known vulnerabilities found |
| Docker 配置 | `docker compose --env-file .env.example config`：配置可渲染，`SEARXNG_SECRET` 模板已覆盖 |
| 浏览器烟测 | Playwright 检查桌面 `/`、`/settings`、`/reports`、`/sync`、`/report/:id` 和移动端 `/`、`/settings`、`/sync`：无 4xx/5xx、无控制台错误、无横向溢出 |
