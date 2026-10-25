# 第七章 · 脚本化 LLM 的陷阱（一次真实事故）

## 本章能回答什么

- 一个"确定性评测"为什么会在同一份代码上给出两种结果。
- 并发 + 按调用顺序取响应，是怎么把"测试夹具的错"伪装成"业务逻辑的错"的。
- 遇到"本地绿、CI 红"时，应该先排除什么、后怀疑什么。

## 事故：3 站里 1 站对

CI 在 `tests/test_eval_runner.py::test_smoke_regular_case_completes_with_citations`
上偶发失败，**同一提交本地全绿**。失败时的指标是：

```
citation_coverage = 1.0
citation_validity = 0.333     ← 三个站点只有 1 个对得上
grounding         = 0.0
```

注意 `coverage` 是 1.0：报告每句都挂了引用。挂得整整齐齐，**但引的是别的站点的证据**。

## 先排除两个假假设

按"最便宜的实验先做"的顺序：

| 假设 | 实验 | 结果 |
|---|---|---|
| 行尾（CRLF vs LF） | 强制 LF 工作区重新 clone，跑同一条测试 | 仍绿 → 排除 |
| Python 版本 | 查本地 venv 与 CI 的版本 | 都是 3.13 → 排除 |

还有一个更省事的排查：**看"弄红 CI 的那个提交"改了什么**。
它只改了一个 `.gitignore` 和一份 docs——不可能让引用校验掉到 0.333。
结论：这不是回归，是**潜伏的抖动**。

## 根因：按调用顺序取响应 × 并发抓取

两条各自都合理的代码，合起来是错的：

**其一**，抓取是并发的：

```python
tasks = [_limited_fetch(site) for site in site_configs]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

`research-agent@ef7204f src/workflow/mvp_workflow.py:1449`

**其二**，抽取就发生在每个站点的并发分支里：

```python
extracted = await self.coordinator.extract_key_info(cleaned_content, url)
```

`research-agent@ef7204f src/workflow/mvp_workflow.py:1565`

**其三**（这才是错的那一条），脚本化 LLM 是按"第几次调用"取响应的：

```python
step = sum(1 for call in self.calls if call["intent"] == intent)
entry = self._script[intent][min(step, len(self._script[intent]) - 1)]
```

`research-agent@ef7204f src/llm/__init__.py:841`（`_next_entry`）

于是：三个站点并发完成，**谁先回来谁拿走第一条脚本**。
顺序一变，A 站的抽取条目就配给了 B 站的 URL，`make_evidence_id(url, key, value)` 里的
`url` 与 `value` 不再是同一站点的组合 → id 对不上 → 引用"不实"。

## 复现：数字必须对得上

把分派退回"按调用顺序"，再把站点反序喂进去：

```
旧实现（按调用顺序）反序喂 → 错配的站点: ['shimo', 'tencent']
新实现（按主机）同一反序输入全对: True
```

3 站错 2 站 = 1/3 正确，正好等于 CI 的 `validity 0.333`。
**数字对上，才算根因确认**；否则只是"我改了个东西，它好像绿了"。

## 修法：按站点主机分派，认不出就判错

```python
class _SiteKeyedScriptedLLM(ScriptedLLMClient):
    """`extract` 意图按站点主机取脚本条目；其余意图沿用父类的按序语义。"""
```

`research-agent@ef7204f evals/runner.py:130`

配套的识别函数在 `research-agent@ef7204f evals/runner.py:76`。两条设计要点：

1. **按内容分派，不按顺序**：从提示词里的站点 URL 认主机，同一站点第 n 次调用取第 n 版
   （多版本 case 不变语义）。
2. **认不出就报错**：

```
[fake] extract 意图无法唯一确定站点（命中 []；已知 [...]）。
按调用顺序取响应会在并发抓取下把 A 站的条目配给 B 站，证据 id 对不上——
所以这里必须判错，不能退回按序取。
```

`research-agent@ef7204f evals/runner.py:76-91`

第 2 点比第 1 点更重要：如果认不出时"退回按序"，这类错配下次还会继续伪装成业务失败。
**判错会把问题暴露在它该暴露的地方。**

## 顺带把注释里的约定变成数据

`inj_heavy` 这条 case 有两个站点，但只有一条抽取脚本——因为其中一站的页面会被整页丢弃，
按约定"被丢弃的站点不占条目"。这个约定原先**只写在注释里**，没有任何东西守它。

现在：

- 在 `manifest.json` 里给那一站加 `"blocked": true`，让约定变成可读数据；
- 加一条结构不变量测试：**抽取条目数必须等于非 blocked 站点数**。

注释里的约定不算约定。

## 顺带：写这本书的门禁自己，也有同一个盲区

写到这里，本卷的坐标审计工具被查出一个同构的毛病：它只认 `repo@sha 路径:行号`
的**完整写法**。而正文里大量存在 `路径:行号`（省略 sha）和 `:行号`（省略路径，继承同行前一个坐标）
的简写——这些简写**根本没被统计过**。修好之后，同一批稿子的坐标数从 88 条涨到 173 条：
**八十五条以前从不检查范围里**。

这和第七章的事故是同一个形状：不是"检查失败了"，而是"这些行压根没进检查"。
所以现在的规则是（`book/tools/audit_coords.py`）：

| 写法 | 怎么校验 |
|---|---|
| `research-agent@ef7204f 路径:行号` | 按写明的提交读 |
| `路径:行号`（省略提交） | 按当前锚点读 |
| `:行号`（省略路径） | 继承**同一行**前一个坐标的路径；同行没有则判错 |
| 只写文件名（`token_budget.py:149`） | 在锚点树里按**后缀唯一**解析；重名即判错，不猜 |

最后两条是刻意的：**歧义就报错**。猜一个，等于给"指错了目标"开门。

## 这一章的教训

1. **"本地绿、CI 红"先怀疑顺序与并发**，别先怀疑行尾和版本（那两个假设我用实验排除了，
   但它们是更常见的"直觉答案"，而直觉答案往往先被写进结论）。
2. **假对象的语义错误会伪装成业务失败**。引用校验没写错，它忠实地报告了"这些 id 对不上"；
   错的是喂给它的夹具。
3. **确定性评测的前提是"控制变量"**。并发是一个没被控制的变量，于是同一份夹具
   在不同调度下得出不同结论——而它看起来完全像"产品不稳定"。

## 可验证主张

| # | 主张 | 怎么判死 |
|---|---|---|
| 1 | 抓取是并发的，抽取发生在并发分支内 | `mvp_workflow.py:1449`、`:1565` |
| 2 | 脚本化 LLM 原按调用次数取响应 | `src/llm/__init__.py:841` |
| 3 | 现在按站点主机分派，且认不出时抛错 | `evals/runner.py:130`、`:76-91` |
| 4 | 该错配能复现出与 CI 相同的 1/3 | 反序喂旧实现，错配 2/3 站点 |
| 5 | `blocked` 站点是显式数据，并有结构不变量测试 | `evals/cases/manifest.json` 的 `inj_heavy` |

## 证据与坐标

```bash
git checkout ef7204f
git show ef7204f:src/workflow/mvp_workflow.py | sed -n '1449p;1565p'
git show ef7204f:src/llm/__init__.py | sed -n '832p;841,845p'
git show ef7204f:evals/runner.py | sed -n '76,91p;93,145p'
git show ef7204f:tests/test_eval_runner.py | grep -n "按站点主机分派" 
```
