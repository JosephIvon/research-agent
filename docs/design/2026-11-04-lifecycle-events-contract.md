# 生命周期事件对外契约（v1）

适用范围：`GET /research/tasks/{task_id}/event-log?after_seq=0&limit=200`。
数据源是 SQLite `task_events`（跨进程持久化、append-only），不是内存事件流；
`GET /research/tasks/{task_id}/events`（SSE）只用于在线实时推送，不负责补历史。
前端断线后应以 `after_seq` 游标从 `event-log` 补齐。

## 1. 统一信封

每一行事件的字段（后端 `TaskEvent.to_dict()` 原样序列化）：

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `seq` | int | 是 | SQLite 自增序号，**同一数据库内全局单调**。排序、去重、断线补齐都以它为准；不要用 `created_at`（秒级精度，同秒内会重复）。 |
| `event` | string | 是 | 事件名，见第 2 节。未列出的非生命周期事件（`crawl_progress`、`citation_check` 等）按既有行为透传。 |
| `task_id` | string | 是 | 任务 ID。 |
| `stage` | string | 是 | 事件所属阶段：`search` / `decompose` / `crawl` / `extract` / `verify` / `report` / `citation` / `lifecycle`。 |
| `status` | string | 是 | 阶段/任务在该事件后的状态：`running` / `completed` / `failed` / `cancelled`。 |
| `message` | string | 是 | 人类可读文案，前端可直接展示。 |
| `payload` | object | 是 | 事件专属字段，可能为空 `{}`。不要假设字段固定存在。 |
| `created_at` | string | 是 | 服务端本地时间，`YYYY-MM-DDTHH:MM:SS`，**秒级、无时区**。仅用于展示。 |

分页：`after_seq` 为不含边界的下一条起点；`limit` 上限 500（默认 200）。
前端保存本批最大 `seq`，下次用该值作为 `after_seq`。

## 2. 四个生命周期事件的字段

### `stage_start`

| payload 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `attempt` | int | 是 | 当前阶段第几次尝试，从 1 开始。阶段内重试会递增（v1 实现中固定为 1，字段先占位，前端不要假设恒为 1）。 |

`status` 恒为 `running`。

### `stage_end`

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `attempt` | int | 是 | 与对应 `stage_start` 相同。 |
| `elapsed_s` | float | 是 | 该阶段耗时（秒），恢复跳过阶段约为 0。 |
| `error_code` | string | 否 | 仅 `status=failed` 时存在，值为异常类型名（如 `TimeoutError`）。 |

`status` 为 `completed` 或 `failed`。注意：**`stage_end` 的 `status=completed` 只代表阶段完成，
不代表任务终态**；前端判定任务结束要看 `event` 为 `completed` / `partial` / `task_cancelled` / `error`。

### `task_resumed`

同一个任务在一次恢复中会出现**两条** `task_resumed`，语义不同：

1. `automatic` 恢复调度事件（服务端产生）：`payload.automatic=true`，`message` 为
   `进程重启后自动恢复`，`stage` 为检查点阶段；
2. 工作流接管事件（`MvpWorkflow._apply_resume` 产生）：`payload.automatic` 缺省/`false`，
   `message` 为 `从检查点恢复：已完成到 <stage> 阶段`。

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `checkpoint_seq` | int | 是 | 从哪个检查点恢复（对应 `task_checkpoints.seq`）。 |
| `attempt` | int | 是 | 该任务的尝试序号；v1 为检查点里的值，通常为 1。 |
| `automatic` | bool | 否 | 仅服务端自动恢复事件存在且为 `true`；工作流事件缺省。前端用 `automatic===true` 统计"崩溃恢复次数"。 |
| `stage` | string | 否 | 仅工作流事件存在，与信封 `stage` 相同，冗余方便消费。 |

### `task_cancelled`

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `reason_code` | string | 是 | 取消原因。v1 用户取消固定为 `cancelled`。 |
| `attempt` | int | 是 | 固定为 1（取消不区分尝试）。 |

`stage` 恒为 `lifecycle`，`status` 恒为 `cancelled`。
取消是**单调终态**：同一任务随后不会再出现 `completed`；前端收到 `task_cancelled` 即应停止渲染"运行中"。

失败路径补充：恢复失败写 `event=error`、`stage=lifecycle`、`status=failed`，
`payload.reason_code` 取值见 `LifecycleReasonCode`：`no_checkpoint` / `request_rebuild_failed` /
`schedule_failed` / `execution_failed`。前端可将 `error` 与 `task_cancelled` 一并视作终态。

### `report_draft_reused` / `crawl_cache_hit`（断点复用提示，非终态）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stage` | string | 分别为 `report` / `crawl` |
| `status` | string | 恒为 `running` |
| `payload.url` / `payload.page_count` | — | 仅 `crawl_cache_hit`：命中的站点与页数 |

两者只表示“本次重跑复用了持久化产物”，**不改变任务状态**；消费端可忽略，也可用于展示
“已恢复，未重复付费”。同一任务可能多次出现，按需合并。

## 3. 真实序列样例

以下为 2026-11-04 本地压测（任务 `95cef172-…`，进程在 crawl 阶段被强杀后重启）从 SQLite
`task_events` 导出的原始记录，仅删去与本节无关的 `crawl_progress` / `search` 等行，字段未改写：

```json
[
  {"seq": 27, "event": "stage_start", "stage": "decompose", "status": "running",
   "message": "正在理解需求、拆解调研维度", "payload": {"attempt": 1},
   "created_at": "2026-11-04T23:26:25"},
  {"seq": 29, "event": "stage_end", "stage": "decompose", "status": "completed",
   "message": "需求拆解完成", "payload": {"attempt": 1, "elapsed_s": 0.0},
   "created_at": "2026-11-04T23:26:25"},
  {"seq": 30, "event": "stage_start", "stage": "crawl", "status": "running",
   "message": "开始检索、抓取与准入筛选", "payload": {"attempt": 1},
   "created_at": "2026-11-04T23:26:25"},
  {"seq": 98, "event": "task_resumed", "stage": "decompose", "status": "running",
   "message": "进程重启后自动恢复",
   "payload": {"checkpoint_seq": 8, "attempt": 1, "automatic": true},
   "created_at": "2026-11-04T23:26:57"},
  {"seq": 108, "event": "task_resumed", "stage": "decompose", "status": "running",
   "message": "从检查点恢复：已完成到 decompose 阶段",
   "payload": {"stage": "decompose", "checkpoint_seq": 8, "attempt": 1},
   "created_at": "2026-11-04T23:26:57"},
  {"seq": 112, "event": "stage_end", "stage": "decompose", "status": "completed",
   "message": "需求拆解完成", "payload": {"attempt": 1, "elapsed_s": 0.0},
   "created_at": "2026-11-04T23:26:57"},
  {"seq": 126, "event": "stage_end", "stage": "crawl", "status": "completed",
   "message": "抓取与准入筛选完成", "payload": {"attempt": 1, "elapsed_s": 49.143},
   "created_at": "2026-11-04T23:27:46"},
  {"seq": 140, "event": "stage_end", "stage": "citation", "status": "completed",
   "message": "引用校验闭环完成", "payload": {"attempt": 1, "elapsed_s": 0.078},
   "created_at": "2026-11-04T23:27:46"},
  {"seq": 142, "event": "completed", "stage": "completed", "status": "completed",
   "message": "全部完成", "payload": {},
   "created_at": "2026-11-04T23:27:46"}
]
```

取消序列（同一环境另一个任务 `8f1e57ef-…`，抓取中被用户取消）的尾部：

```json
[
  {"seq": 369, "event": "stage_start", "stage": "decompose", "status": "running",
   "message": "正在理解需求、拆解调研维度", "payload": {"attempt": 1},
   "created_at": "2026-11-04T23:31:05"},
  {"seq": 371, "event": "stage_end", "stage": "decompose", "status": "completed",
   "message": "需求拆解完成", "payload": {"attempt": 1, "elapsed_s": 0.029},
   "created_at": "2026-11-04T23:31:05"},
  {"seq": 372, "event": "stage_start", "stage": "crawl", "status": "running",
   "message": "开始检索、抓取与准入筛选", "payload": {"attempt": 1},
   "created_at": "2026-11-04T23:31:05"},
  {"seq": 375, "event": "task_cancelled", "stage": "lifecycle", "status": "cancelled",
   "message": "任务已取消", "payload": {"reason_code": "cancelled", "attempt": 1},
   "created_at": "2026-11-04T23:31:07"}
]
```

## 4. 前端消费要点

- 终态判定：`event ∈ {completed, partial, task_cancelled, error}`，不要用 `stage_end.status`。
- 重启后历史任务仍可查：`GET /tasks/{id}` 与 `/event-log` 在内存未命中时从 SQLite 回填；
  内存命中以内存为准，不合并两份来源。
- 恢复次数：统计 `event=task_resumed && payload.automatic===true` 的行数；正常为 1。
- 断线补齐：记录 `max(seq)`，重连后 `after_seq=max(seq)` 拉增量；SSE 只做实时提示。
- 同一任务可能出现两条 `task_resumed`（见第 2 节），展示时合并为一条"已恢复"即可，
  但不要把两条都算作一次重启。
- `payload` 未知字段应忽略，不要做严格模式校验；后续版本只会加字段，不改已有字段语义。
