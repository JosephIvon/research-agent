# 第三章 · 预算与 PARTIAL

## 本章能回答什么

- 预算耗尽时，为什么这个系统**不直接中断**，而是再发一次收敛调用。
- `partial` 到底是一种什么状态：它既不是成功也不是失败，那它是什么。
- 为什么"完成率 1.00"这种指标单独看会骗人，必须再配一个截断率。

## 一次任务的四种耗尽

预算是四重的，任一条触到就停（`research-agent@ef7204f src/agent/runner.py:74-88`）：

| 预算 | 判据 | 位置 |
|---|---|---|
| 步数 | 循环上界 | `research-agent@ef7204f src/agent/runner.py:74` |
| token | `prompt + completion > token_budget` | `research-agent@ef7204f src/agent/runner.py:77` |
| 墙钟 | `elapsed > wall_clock_budget` | `research-agent@ef7204f src/agent/runner.py:80` |
| 美元 | `estimate_usd() > max_usd` | `research-agent@ef7204f src/llm/token_budget.py:152` |

四重的意义在于**它们卡的是不同的失败形态**：步数卡"绕圈"、token 卡"话多"、
墙钟卡"外部服务慢"、美元卡"模型贵"。只设一种，另外三种就会以"看起来跑得挺久"的形式漏过去。

## 触发之后：不是中断，是收束

朴素做法是 `raise BudgetExceeded`，让任务以失败收场。这里不这么做，而是再发**一次**
不带工具、token 上限写死的收敛调用，把已经拿到的证据压成结论：

```python
answer = await self._converge_on_budget(messages, usage, steps, rounds)
```

`research-agent@ef7204f src/agent/runner.py:226`（调用）、`research-agent@ef7204f src/agent/runner.py:244`（实现）

收敛调用的上限是常量 `AGENT_BUDGET_CONVERGE_MAX_TOKENS`，
且允许整体关闭（置 0 则跳过收敛）：

`research-agent@ef7204f src/agent/config.py:27`、`research-agent@ef7204f src/agent/runner.py:265`

**为什么不直接中断**：预算耗尽时用户已经付过了——抓到的页面、抽出的证据、烧掉的 token
都在那儿。直接中断等于把已付费的成果丢掉，只留一条"任务失败"。
再花一次小额收敛调用把它写成报告，用户至少拿到半份东西。

## `partial` 是什么

`partial` 的定义只有一句话：**报告已经产出，但被预算截断**。

它在数据模型里是独立终态，不与 `completed` 合并：

```python
DELIVERED_STATUSES = frozenset({"completed", "partial"})
```

`research-agent@ef7204f evals/metrics.py:105`

注意这个集合叫 **DELIVERED**（已交付）而不是 COMPLETED：它承认 `partial` 也是交付，
但**不把它改名叫 completed**。服务端映射同样单列：

`research-agent@ef7204f server.py:623`、`research-agent@ef7204f server.py:836`、`research-agent@ef7204f server.py:862`

为什么值得单列一个状态，而不是打两个布尔标记？因为三态能表达"都不是"：
`completed` / `failed` / `partial`。用两个布尔（成功、失败）拼不出第三种，
最后一定有人把 `partial` 塞进其中一边——塞进成功，看板好看但骗人；塞进失败，
用户明明拿到了报告却被记为失败。

## 为什么必须再配一个截断率

这是本章最想留下的一条：

```python
summary["truncation_rate"] = round(len(truncated) / total, 3) if total else 0.0
```

`research-agent@ef7204f evals/metrics.py:140`

`completion_rate` 如果把 `partial` 算作已完成，它可以是 1.00——**而这正是危险之处**：
一个只看完成率的看板，会悄悄掩盖"一半报告都是半截的"。
所以两个指标必须并列存在，各答各的问题（`research-agent@ef7204f evals/metrics.py:103`）。

评测里有一条 case 专门守这件事：

```python
assert summary["truncation_rate"] == 0.5, "但截断率必须单列，不能被完成率抹平"
```

`research-agent@ef7204f tests/test_eval_metrics.py:99`

## 这条链路的诚实边界

- 收敛调用本身也可能失败。代码里对 ReAct runner 的 LLM 调用失败有单独处理
  （`research-agent@ef7204f src/agent/runner.py:98`），此时任务不会得到 `partial` 报告。
- `partial` 能告诉用户"被截断了"，但**不能**告诉用户"缺的是哪一节"。
  想知道缺什么，要看第 4 章的维度覆盖与缺口补抓。

## 可验证主张

| # | 主张 | 怎么判死 |
|---|---|---|
| 1 | 预算是四重的，且判据写在循环内 | `src/agent/runner.py:74-88`、`src/llm/token_budget.py:149-154` |
| 2 | 预算耗尽走收敛调用，不是抛异常中断 | `src/agent/runner.py:226`（调用点）与 `src/agent/runner.py:244`（实现） |
| 3 | 收敛调用有独立上限，且可整体关停 | `src/agent/config.py:27`、`src/agent/runner.py:265-267` |
| 4 | `partial` 是独立终态，未与 `completed` 合并 | `evals/metrics.py:105`、`server.py:836` |
| 5 | 截断率与完成率并列计算，互不抹平 | `evals/metrics.py:140`，并有测试断言 `tests/test_eval_metrics.py:99` |

## 证据与坐标

```bash
git checkout ef7204f
git show ef7204f:src/agent/runner.py | sed -n '74,88p;226p;244p;265,270p'
git show ef7204f:src/llm/token_budget.py | sed -n '149,154p'
git show ef7204f:evals/metrics.py | sed -n '103p;105p;140p'
git show ef7204f:server.py | sed -n '623p;836p;862p'
git show ef7204f:tests/test_eval_metrics.py | sed -n '99p'
```
