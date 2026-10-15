# L2 生产基准落地计划

> 承接 `docs/implementation-draft-gap-refetch-2026-10.md` 第三问（「没有真实数据时基准怎么立」）。
> 目标不是先定一个基准值，而是把**取数规程**和**判定线**固化成可复现的资产。
> 状态：**计划，未实施**。实施前先确认每项的验收判据。

## 为什么是现在

L1（合成夹具上的行为基准）已经有完整骨架：`evals/cases/manifest.json`（含 sha256
防篡改）、`evals/baselines/v1.X.json`（版本化、不滚动覆盖）、`evals/runner.py`
（`load_manifest(verify_digests=True)`、结果落 `results/run_<ts>.json`）。

L2（真实站点上的可达率 / 失败模式）**什么都没有**。2026-10-15 那轮评审产出的
714 次可达性观测、保真度 2×2、P2 双臂对照，全部是一次性脚本，存在
`%TEMP%\ra-scratch\`，写完就没了。后果已经付出过一次：这些数字**跨批次不可比**，
因为没有记录负载画像，于是「上周 78%、这周 75%」这句话在物理上不成立。

## 设计原则

**照抄 `evals/` 的骨架，不发明新范式。** 同一套 `manifest + baseline + results`
三件套，两套并存、各管一层，读者只学一次。

**三样东西各自独立可交付**，互不阻塞。第 ③ 项依赖 P1，而 P1 已在 `3a5b4e2` 落地。

---

## ① 取数 manifest —— 让两批观测可比

### 新增目录

```
probes/
  README.md                 # 规程：每个字段为什么必填（承载「探针画像≠生产」这条教训）
  manifest.json             # URL 池 + 负载画像
  collector.py              # 唯一入口，只调生产路径 fetch_site_profile
  results/run_<ts>.json     # 每次运行落盘（对齐 evals/results/）
  baselines/v1.json         # 见第 ② 项
```

### manifest schema

```json
{
  "version": "v1",
  "load_profile": {
    "entry": "fetch_site_profile",
    "max_pages": 3,
    "discovered_page_timeout": 15,
    "max_attempts": 1,
    "timeout_seconds": 60,
    "request_interval": 1.0,
    "concurrency": 1
  },
  "sites": [
    { "host": "www.linode.com/", "pool_size": 30, "pool_hash": "sha256:..." },
    { "host": "wise.com/",         "pool_size": 30, "pool_hash": "sha256:..." }
  ]
}
```

### 两个关键决定

1. **`load_profile` 是 manifest 的必填字段，不是运行时参数。**
   这是整份计划里最重要的一条。并发度、超时、重试、请求间隔——这些不是性能旋钮，
   它们**定义了这次测量是什么**。把它们放进 manifest，两批观测是否可比就是一个
   可以机械判定的问题（`load_profile` 相同 且 `pool_hash` 相同），而不是靠人回忆。

2. **`pool_hash` 用 sha256，照抄 `fixture_digests` 的做法。**
   URL 池变了而哈希没变，说明有人手改了池子——这是最隐蔽的一类污染。

### 验收判据

- 同一份 manifest 连跑两次，`results/` 两份结果的 `load_profile` 与 `pool_hash` 逐字节相等。
- 故意改一个 `load_profile` 字段再跑，diff 工具能直接报出「这两批不可比」。
- `collector.py` 里**不允许**出现任何硬编码的 timeout / concurrency；
  全部从 manifest 读（用 `tests/` 断言这一点）。

---

## ② 判定线（预注册）—— 先写死阈值，再看数据

### baselines/v1.json

```json
{
  "version": "v1",
  "registered_at": "2026-10-15",
  "note": "阈值在看到第一批 L2 数据之前写死；改阈值必须升版本并写明理由",
  "rules": [
    {
      "metric": "content_page_success_rate",
      "op": "<",
      "threshold": 0.90,
      "on_breach": "阻塞发布，复查抓取链路"
    },
    {
      "metric": "err_connection_closed_rate",
      "op": ">",
      "threshold": 0.10,
      "on_breach": "触发 §5.7 P4 复评"
    }
  ]
}
```

### 规则

- **不滚动覆盖**，照抄 `evals/runner.py` 里 `BASELINE_VERSION` 的既有约定。
- `registered_at` 是这一项的全部意义：它证明阈值不是看着数据挑的。
- 阈值本身**必须带样本量下界**。`content_page_success_rate = 0.85` 在 n=10 和 n=600
  下是两个结论，所以 rules 里要带 `min_n`，不足样本量时结论只能是「数据不足，维持现状」。

### 验收判据

- 拿一份**历史 results**（即 2026-10-15 那轮的数据手工整理进来）跑判定器，
  得到的结论与当时手写的结论一致。
- 有一个测试：改 `threshold` 会让 `BASELINE_VERSION` 检查失败（防止偷偷改阈值不升版本）。

---

## ③ L2 采集出口 —— 让生产运行自己喂基准

P1 已经在 `fetch_site_profile` 的返回值里记了 `failed_pages`
（`url` / `error` / `elapsed_seconds` / `detail`），**采集端已经通了，缺的是聚合端**。

### 最小实现

- `collector.py` 直接把 `failed_pages` 汇总进 `results/run_<ts>.json`，
  并算出 rules 里要的两个 metric。探针和生产的口径从此同源。
- 生产侧：`gap_refetch_result` 那类观测字段已经在写事件/Redis 载荷，
  加 `failed_pages` 的同类字段即可——**形态照抄现成的那几个**，不新建机制。

### 验收判据

- 连续跑 N 次真实任务，`failed_pages` 聚合出来的两个 metric 与 `collector.py`
  在同一批 URL 上算出来的一致（允许采集时间窗差异带来的波动，阈值写进测试）。
- `failed_pages` 为空时聚合函数返回空结构而不是 `None`（`web_crawler.py`
  已经保证该键恒存在，这条是回归钉子）。

---

## 阶段与不做的事

| 阶段 | 内容 | 产出 |
|---|---|---|
| 一 | `probes/manifest.json` + `collector.py` + `results/` | 两批观测可比 |
| 二 | `baselines/v1.json` + 判定器 + 版本检查 | 阈值不可事后偷改 |
| 三 | `failed_pages` 生产聚合出口 | L2 基准随真实运行自动积累 |

**本轮明确不做**：不接 CI 门禁（先在本地跑通，门禁是阶段二之后的事）；
不做 URL 抓取缓存（会改变「同 URL 多次观测」的独立性，统计口径要重定义，
是另一份设计）；不做显著性检验（先能跑通，再谈统计）。

## 风险

- 阶段一落地后，**以前的 714 次观测仍然不可比**——它们没有 manifest，补不回来。
  本计划的第一个交付物不是新数据，是「从今往后不再产生不可比的数据」。
- 真实站点的行为会随时间变（反爬策略、CDN、页面改版）。`collected_at` 必须进
  manifest 字段，否则半年后没人说得清某批数字是哪天的。
