# 缓存生命周期（page_cache / task_drafts）清理策略

- 范围：只清 `page_cache`（抓取页快照）与 `task_drafts`（报告草稿、固定文件名映射）。
  **不碰** `task_events` / `task_checkpoints` / `tasks` / `reports`，续跑证据与台账完整保留。
- 实现：`ReportDB.prune_caches()`（[database.py](file:///d:/workspace/research-agent/src/storage/database.py)）+ CLI
  [prune.py](file:///d:/workspace/research-agent/src/storage/prune.py)；启动钩子可选。
- 约束：无新依赖；running/pending 与无任务行的缓存**永不清理**（没有终态证据就不动）。

## 默认值与理由

| 参数 | 默认 | 环境变量 | 理由 |
|---|---|---|---|
| completed/cancelled/partial 保留 | **7 天** | `CACHE_COMPLETED_KEEP_DAYS` | 覆盖“完成后再看/追问”的典型窗口；再久的数据已可从 `reports` 正文重建，缓存页无保留价值 |
| failed 保留 | **30 天** | `CACHE_FAILED_KEEP_DAYS` | 失败要复盘：需要原始抓取页与草稿定位原因；30 天覆盖一个月度排障周期 |
| 全局行数上界 | **300,000 行** | `CACHE_MAX_ROWS` | 按约 33 行/任务（30 页 + 3 草稿），约 9000 个任务的 7 天热区；防止小页场景把行数撑爆 |
| 全局字节上界 | **2 GiB** | `CACHE_MAX_BYTES` | 见下方推算：1000 任务/天、7 天窗口约 1.6 GiB，离上界约 0.4 GiB；超过则最旧优先淘汰 |
| 启动自动清理 | **关闭**（`CACHE_PRUNE_ON_STARTUP=0`） | 同上 | 避免每次重启都写库；由运维显式打开或用 CLI 定时执行 |
| 清理总开关 | **开**（`CACHE_PRUNE_ENABLED=1`） | 同上 | 可整体禁用（如排障时冻结数据） |

淘汰顺序：先按任务终态年龄过期；若仍超双限，再在**终态任务**内按 `tasks.updated_at` 最旧优先删除。
若 running 数据本身就超过上界，结果里 `caps.exceeded_after=true`，**不会误删 running**。

## 用法

```powershell
# 干跑（默认）：只报告将删除的行数/字节数
uv run python -m src.storage.prune --dry-run

# 实际删除
uv run python -m src.storage.prune --apply

# 覆盖参数 / 指定库
uv run python -m src.storage.prune --apply --db research_agent.db --completed-days 3 --failed-days 14
```

启动钩子：`CACHE_PRUNE_ENABLED=1` 且 `CACHE_PRUNE_ON_STARTUP=1` 时，在恢复流程前执行一次真实清理
（失败只告警，不阻断启动）。

## 干跑真实输出（2026-11-05，演示库 4 任务 × 33 行）

```json
{
  "dry_run": true,
  "deleted": {
    "page_cache": {"rows": 60, "bytes": 372280},
    "task_drafts": {"rows": 6, "bytes": 49152},
    "total": {"rows": 66, "bytes": 421432}
  },
  "remaining": {"rows": 66, "bytes": 421432},
  "caps": {"rows": 300000, "bytes": 2147483648, "exceeded_after": false},
  "retention": {"completed_keep_days": 7, "failed_keep_days": 30}
}
```

演示库构成：`completed` 10 天前、`failed` 40 天前 → 删除（各 30 页 + 3 草稿）；
`failed` 2 天前、`running` 1 天前 → 保留。干跑后库内行数不变（测试覆盖）。

## 容量推算（**假设推算，不是实测**）

假设：

- 每任务 30 页；每页缓存 payload ≈ 6 KiB（正文 + URL/JSON 开销），30 页 ≈ 180 KiB。
- 每任务 3 份草稿（report / report_filename / matrix），每份 ≈ 8 KiB，合计 24 KiB。
- 单任务缓存 ≈ **210,716 B ≈ 0.201 MiB、33 行**（与演示库一致，属于按假设构造的尺寸，不是生产实测）。
- 字节只计数据列 BLOB 长度；SQLite 实际磁盘约为其 1.2–2 倍。

| 场景 | 行数 | 数据字节 | 量级 |
|---|---|---|---|
| 1000 个任务、不清理 | 33,000 | ≈ 201 MiB | 0.2 GiB |
| 稳态：1000 任务/天、失败率 5%、7/30 天窗口 | ≈ 269,000 | ≈ 1.63 GiB | 默认上界内（2 GiB / 300k 行） |
| 稳态：100 任务/天、同上 | ≈ 26,900 | ≈ 163 MiB | 远低于上界 |

结论：默认值按“单实例、1000 任务/天”设定，年龄策略先于容量上界生效；
若任务量或页数显著更大，容量上界会提前淘汰（可能短于 7 天），需按部署上调或调低保留天数。
