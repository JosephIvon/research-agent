# 多副本启动恢复：租约设计结论（不实现）

- 状态：设计结论（2026-11-04）
- 适用范围：将来同一数据库跑多个应用副本/worker 时的启动恢复
- 当前行为：`_recover_running_tasks` 无租约，**只允许单实例**（见 [ADR-001](2026-11-04-adr-startup-recovery.md)）

## 结论（一句话）

**选 DB 抢占，不选 leader 选举**：在现有 SQLite 上加一张 `task_leases` 表，用条件
`UPDATE` 按任务粒度和过期时间抢占；不需要新框架，也不需要"一个 leader 跑全部任务"。

## 为什么不是 leader 选举

- leader 选举要求副本间有心跳/选主通道（Redis、etcd、K8s Lease 等），当前栈里没有；
  引入任何一个都是新依赖，且选主本身只解决"谁来跑"，仍需解决"同一任务不能被两个副本跑"。
- DB 抢占的粒度正好是任务：副本 A 抢到 task-1、副本 B 抢到 task-2，天然并行；
  leader 模式反而把恢复串行化到一个副本上。
- SQLite 已经是跨进程唯一共享点（`tasks`/`task_checkpoints`/`task_events`），
  租约放同一事务域内，崩溃语义与检查点一致。

## 最小形态（若将来实现）

```sql
CREATE TABLE task_leases (
  task_id     TEXT PRIMARY KEY,
  owner_id    TEXT NOT NULL,          -- 实例 ID（启动时生成 uuid）
  lease_until TEXT NOT NULL,          -- ISO 时间；到期即可被抢占
  heartbeat_at TEXT NOT NULL
);
```

抢占（单事务）：

```sql
INSERT INTO task_leases(task_id, owner_id, lease_until, heartbeat_at)
VALUES(:task, :owner, :until, :now)
ON CONFLICT(task_id) DO UPDATE
  SET owner_id=:owner, lease_until=:until, heartbeat_at=:now
  WHERE task_leases.lease_until <= :now;
```

- 只有 `changes() == 1` 的副本获得该任务，才允许读检查点并 resume。
- 运行中每 `TTL/3` 续租；续租失败立即停止任务（避免"租约已丢还在写产物"）。
- 任务终态时删除租约行；重启后租约表按 `lease_until` 自然过期。

## 前置条件

1. 数据库对**所有副本真实共享**（同一文件/同一网络盘，或换成 Postgres）；每个副本
   各自本地 SQLite 文件时，租约表不共享，等于没做。
2. 各副本时钟可信或统一走 DB 时间（`CURRENT_TIMESTAMP`），否则过期判断会漂移。
3. `TTL` > 最长 GC/暂停时间；否则活着的副本会被误判过期而遭重复抢占。
4. 写事务必须能容忍 SQLite 锁竞争（建议 WAL + 短事务）；否则抢占本身会成为瓶颈。
5. 实例 ID 在重启后必须变化（`uuid4`），不能复用机器名，否则旧租约会被"自己"继承。

## 失败模式

| 失败模式 | 后果 | 缓解 |
|---|---|---|
| 副本崩溃但未删租约 | 该任务要等 `TTL` 到期才被接手 | TTL 取小（如 30s）+ 启动时清理过期行；接受最长一个 TTL 的恢复延迟 |
| 暂停超过 TTL（GC/VM 挂起） | 两个副本同时认为自己持有租约 → 重复抓取/重复写 | 续租失败立刻自停；TTL 留足余量；任务级幂等仍以 SQLite 终态为准 |
| 数据库不共享（本地文件） | 各副本都能抢到同一任务 | 部署清单强制校验共享路径；否则禁止开恢复 |
| 时钟漂移 | 过早/过晚过期 | 用 DB 时间或 NTP；`lease_until` 冗余存 `heartbeat_at` 便于排查 |
| 抢占后读检查点与写产物不在同一事务 | 极端下仍可能重复写报告文件 | 报告落盘保持"后写覆盖/按 task 前缀唯一"策略；不把租约当幂等替代品 |
| 数据库整体回滚/恢复备份 | 租约与终态一起倒退 | 恢复期间必须关闭自动恢复（见 ADR 的必须关闭清单） |

## 明确不做

- 不实现租约、不做心跳、不引入 Redis/etcd/K8s Lease。
- 不把恢复改成通用任务队列；租约只包住现有的启动恢复循环。
- 鉴权站点续跑失败仍是既定取舍，租约不改变安全边界。
