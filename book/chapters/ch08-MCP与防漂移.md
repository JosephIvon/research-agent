# 第八章 · MCP 与防漂移

## 本章能回答什么

- 一套已有能力，怎么以标准 MCP 协议对外暴露，而不另起一套鉴权与限流。
- "schema 只允许有一个来源"这条规矩，在代码里是怎么被强制的。
- 挂载子应用时那两个具体的坑（限流够不到、lifespan 不执行），各是什么成因。

## 为什么是"挂载进现有服务"，不是"再起一个进程"

`src/mcpserver/__init__.py` 开头的设计决策写得比任何转述都清楚：

> **单一 server，挂载进现有 FastAPI**（server.py 一行 `app.mount("/mcp", ...)`）：
> 复用 JWT/限流/TrustedHost 中间件；`/mcp` 不进 PUBLIC_PATHS——鉴权由外层保证，
> 不另起进程重写一套鉴权。

`research-agent@ef7204f src/mcpserver/__init__.py:7-9`

这个选择的代价与收益都很具体：**代价**是要处理两处"中间件够不到"的缝隙（下面讲）；
**收益**是鉴权、限流、就绪判定只有一份实现——另起一个服务意味着两套鉴权，
两套鉴权意味着一定有一套先腐坏。

## 坑一：中间件够不到 mount 出来的子应用

限流中间件在路由匹配失败时会跳过，而挂载出去的子应用恰好会走进这条缝：

`research-agent@ef7204f server.py:147-155`

所以 `/mcp` 有一个**显式**守卫（路径前缀判断）：

`research-agent@ef7204f server.py:176`

注释里还记着一个更阴的反面案例：想把限流整体关掉时，`/mcp` 仍被硬限——
守卫反而成了唯一关不掉的（`server.py:164-167`）。这类"开关关不掉"的问题，只有实测才会暴露。

## 坑二：裸挂载的子应用 lifespan 不执行

`research-agent@ef7204f server.py:1630-1634`

原文注释：Starlette 的 `Mount` 不会执行子应用 lifespan，裸挂载的 `/mcp`
每个请求都会炸 "Task group 未初始化"。所以挂载要经过 `mount_mcp`
（它把子应用 lifespan 聚合进主 app），而不是直接 `app.mount(...)`。

这两条坑合起来给了一个通用的判断：**"把它挂上去"比看起来多两件事**——
中间件的覆盖范围、生命周期的归属，都必须在挂载时逐一确认。

## 防漂移：schema 只允许一个来源

> **schema 单一来源**：`tools/list` 直接由 ToolRegistry 的 JSON Schema 生成，
> MCP 层禁止手抄第二份 schema；`test_mcp_server.py` 锁死两者一致，漂移即 CI 红。

`research-agent@ef7204f src/mcpserver/__init__.py:10-11`

手抄第二份 schema 是这类集成的头号腐坏源：工具加了参数、MCP 那侧没跟上，
调用方按旧 schema 传参，报错现场却在业务逻辑里。这里用一条一致性测试把它钉住：
`tests/test_mcp_schema_consistency.py`。

## 执行零旁路

> **执行零旁路**：所有调用一律走 `registry.execute`——只读白名单、入参校验、
> URL 二次 SSRF 校验、超时、统一返回 `{ok, data, error, elapsed}` 全部继承。

`research-agent@ef7204f src/mcpserver/__init__.py:12-14`

这一条保证了 **MCP 不是一条绕过安全网的侧门**：第五章那几道闸门（只读白名单、
执行点 SSRF）在 MCP 路径上同样生效，因为它走的是同一执行入口。

## 降级：SDK 缺席只让 /mcp 回 503

`research-agent@ef7204f server.py:1565-1566`

MCP 是**可选能力**：`mcp` 包没装时，`/mcp` 返回 503 并给出补救说明，
主链路（调研、报告）不受影响。这条设计避免了"引入一个新可选依赖，结果整个服务起不来"。

## 一个被命名为教训的包名

> 包名刻意叫 `mcpserver` 而不是 `mcp`：后者会遮蔽官方 SDK 的 `mcp` 包
> （同根目录下 `import mcp` 会解析到本仓库），实测踩过。

`research-agent@ef7204f src/mcpserver/__init__.py:3-4`

这和本仓另一处老坑同源：`src/logging/` 会遮住标准库的 `logging`
（第 9 章会讲它怎么影响"怎么跑测试"）。**同名包遮蔽**是 Python 项目里最常见的
"代码没写错、环境悄悄变了"的一类事故。

## 可验证主张

| # | 主张 | 怎么判死 |
|---|---|---|
| 1 | MCP 挂载进现有 FastAPI，复用同一套鉴权 | `src/mcpserver/__init__.py:7-9`、`server.py:1632-1634` |
| 2 | `/mcp` 有限流中间件够不到，因此有显式守卫 | `server.py:147-155`、`:176` |
| 3 | 挂载必须经 `mount_mcp`（聚合 lifespan） | `server.py:1630-1634` |
| 4 | schema 只有 ToolRegistry 一个来源，且有一致性测试 | `src/mcpserver/__init__.py:10-11`、`tests/test_mcp_schema_consistency.py` |
| 5 | MCP 调用走同一 `registry.execute`，安全网不被绕过 | `src/mcpserver/__init__.py:12-14` |
| 6 | SDK 缺席时只降级 `/mcp`，主链路不受影响 | `server.py:1565-1566` |

## 证据与坐标

```bash
git checkout ef7204f
git show ef7204f:src/mcpserver/__init__.py | sed -n '1,16p'
git show ef7204f:server.py | sed -n '147,176p;1565,1566p;1628,1634p'
git show ef7204f:tests/test_mcp_schema_consistency.py | head -30
```
