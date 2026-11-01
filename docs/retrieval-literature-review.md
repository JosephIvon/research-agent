# 检索文献综述与升级方案（2026-11-01）

> 背景：第八轮实跑之后，全集口径下 recall_L0 = 0.0496（289 条分母），
> "仅独立来源"口径 0.0044——管线只覆盖赛道头部。
> 这一轮去传统搜推领域找**成熟做法**，看哪些能直接补强我们的问题。
>
> 结论先行：**我们踩的四个坑（闸门偏宽、截断归因、LLM 空响应、计划不可复现），
> 在 IR/RecSys 里全都有成熟解法，而且有一半是"评测协议"层面的问题，不是代码 bug。**
> 最有用的一条：我们的排序键 `(-score, path_depth, name)` 让**字母序决定生死**——
> 这在文献里叫"tie 处理属于评测协议的一部分"，有现成的解析解法。

---

## 1. 检索方法（及一次故障处置）

- `web_search` 端点返回 **HTTP 402（余额不足）** → 改用**本地 SearXNG**（`localhost:8888`）
  做多引擎检索，再用 `web_fetch` 直取原文（arXiv/ACL/官方文档）。
- 检索词覆盖五个方向：① 不完备标注下的评测 ② 长尾/覆盖率指标
  ③ 页面类型与列表页抽取 ④ 实体归并 ⑤ 排序/稳定性/可复现。
- **取原文优先于取摘要**：下面 6 条建议里有 4 条的细节（解析期望式、报告清单、
  JSON-LD 类型名）只在正文里有。

---

## 2. 来源清单（10 个）

| # | 来源 | 类型 | 核心主张（对我们有用的部分） |
|---|---|---|---|
| S1 | [Tie Handling Is Part of the Evaluation Protocol](https://arxiv.org/abs/2609.26977)（FRAME'26, RecSys 2026 workshop） | 论文 | **同分块的 tie-break 规则就是排序的一部分**；给出随机 tie 下的 **E[Hit@k]/E[NDCG@k] 解析式**；给出 6 条"同分密集评测"的报告清单；实测 NDCG@10 在输入序 tie-break 下 0.85、换成确定性 hash 后 0.17 |
| S2 | [Found in the Middle: Permutation Self-Consistency](https://aclanthology.org/2024.naacl-long.129/)（NAACL 2024，代码 [castorini/perm-sc](https://github.com/castorini/perm-sc)） | 论文+开源 | 反复打乱候选顺序让 LLM 排序，再取"离所有排序都最近的中心排序"；消除位置偏置；相对常规推理提升 **34–52%（Mistral）/ 7–18%（GPT-3.5）** |
| S3 | [LLMs Can Patch Up Missing Relevance Judgments](https://arxiv.org/abs/2405.04727) | 论文 | 未标注文档被当成不相关，**评测因此有偏**；用 LLM 自动补判"洞"：TREC DL 上只保留 10% 标注时，与真值的 Kendall τ 仍有 **0.87–0.92** |
| S4 | [A Survey of Blocking and Filtering Techniques for Entity Resolution](https://arxiv.org/abs/1905.06167) | 综述 | 实体归并的标准流程 = **blocking（廉价键生成候选）+ filtering（相似度筛）+ 聚类**；blocking 要 schema-agnostic，避免"超级节点" |
| S5 | [Unified LTR for Multi-Channel Retrieval](https://arxiv.org/abs/2602.23530) + [RRF 文档](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/) | 论文+工程 | 多路召回要**融合**；固定全局权重的 RRF/Weighted Interleaving 不够，要"query-dependent 的通道感知排序"；**RRF 只用名次不用分数**，天然解决不同通道分数不可比 |
| S6 | [Beyond Accuracy](https://dl.acm.org/doi/10.1145/1864708.1864761) / [Long-Tail Item Recommendation 综述](https://onlinelibrary.wiley.com/doi/10.1155/2021/7536316) | 论文 | 评测不能只看精度，要看 **catalog coverage / long-tail share / Tail-Recall@k**；长尾方法的评估指标惯例是"覆盖率+多样性+长尾效果"，不只看 hit rate |
| S7 | [Judging the Judges: Position Bias in LLM-as-a-Judge](https://arxiv.org/html/2406.07791v7) | 论文 | LLM 裁判有**系统性位置偏置**，必须显式测量与控制（换序、对照提示），不能默认它 unbiased |
| S8 | [List Crawling 指南](https://www.zenrows.com/blog/guide-to-list-crawling) | 工程博客 | 把列表页当列表来爬：**先看列表是不是已经在 HTML 里**（在→HTTP+解析器；不在→渲染/接口）；分页类型（页码/加载更多/无限滚动/表格/搜索结果）与停止条件 |
| S9 | [Competitor identification: A review of use cases, data sources, and algorithms](https://dl.acm.org/doi/10.1016/j.ijinfomgt.2022.102507)（J. Business Research 2022） | 综述 | 综述 2002–2019 的 40 篇：**现有竞品识别系统普遍"忽视间接与潜在竞品"**——正是我们"只找到头部"的学理说法 |
| S10 | [schema.org ItemList / Product](https://schema.org/Product)、[分类页结构化数据实践](https://www.schema-validator.com/blog/json-ld-for-e-commerce-category-pages/) | 规范/博客 | 理论上：产品页标 `Product`、列表页标 `ItemList`+`CollectionPage`、文章标 `Article`——**但我们实测不成立（见 §4.1）** |

---

## 3. 六条成熟做法 → 我们的现状 → 补强

### 3.1 【最重要】同分块（tie）就是排序的一半 —— 治 B 与 S1

**文献（S1）**：候选同分时，tie-break 规则**就是**排序规则；用输入序 tie-break 会让
"行构造方式"变成一个没声明的排序特征。论文给出两种更诚实的口径：

- 确定性 hash tie-break（可复现的单一排序）；
- **随机 tie 下的期望指标**（解析式，单相关文档时）：

  ```
  E[Hit@k]  = max{0, min(t, k-a)} / t
  E[NDCG@k] = (1/t) · Σ_{r=a+1..min(a+t,k)} 1/log2(r+1)
  ```
  其中 a = 分数严格更高的候选数，t = 同分块大小。论文实测：解析期望与
  "100 个独立 hash 种子的均值"几乎一致（0.1693 vs 0.1694±0.0012）。

**我们的现状**：`admit_competitors` 的排序键是 `(-score, _path_depth(url), name)`
——同分块内**按名字字母序**。而我们的分数几乎全是并列 0.5（官网 0.5 + 无维度 + 无词面重合），
所以**字母序在决定谁能进 8 个名额**。这既解释了"丢在截断"里混着真竞品，
也解释了 S1（入选名单稳定性）为什么只有 0.4811 且逐轮抖。

**补强**：

1. **报告层（先做，成本最低）**：按 S1 的 6 条清单改报告——
   ①候选集口径 ②排序前候选顺序 ③**同分流行度**（多少候选同分、同分块是否跨过 top-k 边界）
   ④tie-break 规则 ⑤**置换测试**（打乱输入顺序，结果是否不变）⑥敏感性分析（换一种 tie-break 再报一次）。
2. **指标层**：R3 / keep_L3 / S1 不再报"某一次字母序结果"，改报
   **tie 期望值**（用上面的解析式，或多组 hash 种子取均值±方差）。
   这样 S1 的抖动有一部分会被解释掉——**S1 掉线未必是管线不稳，可能只是同分块成员随机**。
3. **排序层（可选）**：把同分块的第二判据从"字母序"换成**外部真值**（模型提名 / 赛道名册 / 抓到的页面证据强度）。

**验收**：报告里出现"同分流行度"与"tie 期望值"两栏；同一批实跑下，
两种 tie-break 口径的差距能被显式报出来（而不是隐藏在单次数字里）。

### 3.2 【架构级】多通道召回 + 名次融合（RRF）—— 新增 E 项

**文献（S5）**：大规模电商/垂直搜索用**多个专门召回通道**（每个通道有各自的目标），
再融合；融合不能直接比分数（不同通道分数不可比），**RRF 只用名次**：

```
RRF(d) = Σ_channels 1 / (k0 + rank_channel(d))
```

论文进一步指出固定全局权重的 RRF 不足，要"query-dependent 的通道感知排序"。

**我们的现状**：单一通道（搜索 API + 几种查询词串）→ 候选 → 打分 → 闸门 → 截断。
所有召回压力都压在"搜索引擎能搜到什么"上——这正是全集口径 0.0496 的根因。

**补强**：把发现阶段改成**多通道**：

| 通道 | 内容 | 对应我们文档里的"下一步" |
|---|---|---|
| C1 主体+竞品词 | 现有 `猫箱 竞品/类似应用` | 已实现 |
| C2 赛道词 | 现有赛道词查询 | 已实现（第五轮 vs 第四轮，同一引擎组合、同一 18 条抽样金标准：R1 0.2315 → 0.3287，绝对 +0.0972） |
| C3 **目录分类页/集合页** | 抓导航站的品类页、收藏夹页（实测 aigc.cn 有 `#term-6373-6381 AI角色扮演` 这类赛道分类） | §8.11 里的第 1 优先级 |
| C4 **应用商店** | iTunes/Play/应用宝 | §8.11 里的第 2 优先级（358 个无官网产品的唯一入口） |
| C5 **模型提名直查** | 逐个搜提名品牌的官网（`_brand_in_host` 已有雏形） | 补长尾头部 |

然后 **RRF 融合**（名次而非分数）→ 统一候选池 → 闸门 → 槽位分配。
**为什么用 RRF 而不是学一个融合模型**：我们没有点击日志，S5 的学习式排序要行为信号；
RRF 是零训练、跨通道可比、且是工业界默认基线 ✓。

**验收**：报告里给出"每个通道各贡献多少个金标准命中"（**通道归因**），
这是我们现在最缺的诊断信息——现在只知道"丢在发现 8–13"，不知道是哪个通道漏的。

### 3.3 同赛道闸门：用 listwise 重排 + permutation self-consistency —— 升级 A

**文献（S2）**：把候选**整批**给 LLM 排序（而不是逐个问"是不是同赛道"），
多次打乱输入顺序，取**中心排序**（离所有排序最近的排名）——既消除位置偏置，
又天然产出一个**全局一致的有序表**。同族还有 SIGIR'26 的 "One pass, any order"
（位置不变的一趟重排）。

**我们的现状**：A 项（同赛道闸门）原方案是"逐个候选调用 LLM 判是/否"——成本高、位置偏置没控、
而且判完还是**无序**（截断时仍要靠字母序）。

**补强（一次改造同时解决 A 与 B-2）**：

1. 把通过站点类型闸门的候选（≤20–30 个）**整批**交给 LLM：
   输入 = 主体 + 赛道定义 + 每个候选的 {名称, 域名, 页面标题, 描述}，
   输出 = **一个有序名单 + 每个候选一句"为什么算/不算同赛道"**。
2. **打乱 3 次输入顺序，各跑一次，取中心排序**（S2）→ 消除位置偏置，
   顺带得到**稳定的同分排序**（不再是字母序）。
3. 位置偏置要**显式测量**（S7）：同一批候选换序再判一次，把两次判不同的候选标出来，
   单独看——这批就是"闸门口径不稳"的地方。

**验收**：① "同赛道"从 `not_executable` 变成可执行条款；② 已知负例（talkie.ai 医疗、
hoshinoresorts 酒店、mewatch.sg 钱包）被判否；③ 置换前后的判定差异率被报告出来。

### 3.4 截断与名额分配：归因细分 + 多样性选择 —— 升级 B

**文献**：
- S1（同分）→ 3.1 的 tie 期望值；
- MMR（Carbonell & Goldstein 1998）/ 子模选择 → 名额有限时在"相关性"与"多样性"之间取平衡，
  贪心实现，λ 可调（[Elastic 的实现说明](https://www.elastic.co/search-labs/blog/maximum-marginal-relevance-diversify-results)）；
- 我们的"同一站点只算一家"已经是半个 MMR（去冗余），但**没有跨来源层的多样性约束**。

**补强**：

1. 归因细分（沿用上一版草案 B-1）：把"丢在截断"拆成 ①名额不够 ②同主机去重
   ③分数地板 ④must_include 优先占位，并记录**名次/分数/被谁挤掉**。
2. 在名额分配时加 **MMR 式去冗余**：同一品牌（规范化名 + 域名主体词）只留一个，
   且对"已入选来自同一来源层"的产品降权——避免 8 个名额里 5 个来自模型提名、3 个来自赛道词。
3. **候选池太大时**（现在 20–30 个候选抢 5–8 个名额），名次分配按 §3.2 的 RRF 名次 + 3.1 的期望口径算。

### 3.5 实体归并：blocking + filtering + 聚类 —— 新增 F 项（修合并缺陷）

**文献（S4）**：实体归并的效率与正确性都靠两段式：
**blocking**（用廉价键生成候选对，schema-agnostic）→ **filtering**（相似度筛）→ 聚类（连通分量/相关聚类）。
要避免"超级节点"（一个常见键把所有人连成一团）。

**我们的现状**：合并键是"规范化品牌名"（去修饰词 + 去标点 + casefold），
**非 ASCII 品牌名永远匹配不上域名**（`brand.isascii()` 为 False 时 `_brand_in_host` 不生效）
——星野/xingyeai 就因此没被认作同一个品牌，最后靠"域名更短"让 cxgn.cn（导航站）赢了，
把真官网挤掉。

**补强**：

1. **blocking 键补三类**：①规范化品牌名 ②**拼音/罗马化**（星野→xingye；用轻量拼音库即可）
   ③域名主体词（取 host 的主标签，cxgn.cn→cxgn）。
2. **filtering**：标题相似度 + 页面证据（同一 title 模板/同一 footer/同一 ICP 备案），
   再加一条**反向证据**：一方是目录站（一条挂 ≥5 个产品名）→ 直接判不同产品。
3. **聚类**：用**连通分量**而不是"首个候选胜出"，让合并结果不依赖候选顺序。
4. **hub 防护**：某个 blocking 键下候选数异常多（如"ai"）→ 降权或跳过该键。

### 3.6 评测：不完备标注、覆盖率指标、领域综述 —— 升级测量口径

**文献**：

- **S3（不完备标注）**：金标准没标到的文档会被当成"不相关"，评测因此有偏，
  偏差大小取决于池化过程。我们的赛道全集**就是这样一个"池"**：
  池外的真竞品（358 个无官网产品 + 池外未知）会被算成"漏检"。
  → **表述纪律**：全集口径的 recall 是**保守下界**（已在文档里写了），
  但要补一句文献口径的话：**"pooled recall"是有偏估计，且偏差方向已知**。
  S3 给了一个可操作工具：**用 LLM 补判池内的"洞"**——对我们就是
  **用 LLM 对候选池里没进全集的产品判"是否同赛道"，把判定为同赛道的外部产品补进金标准**
  （作为 `pipeline` 层，已有的 35 条就是这么来的；现在可以自动化补全）。
- **S6（覆盖率/长尾指标）**：不要只看总体 hit rate，要看 **catalog coverage / long-tail share / Tail-Recall@k**。
  → 我们已经有现成的对应物：**"仅独立来源"口径（134 条）就是长尾切片**，
  它的 recall 现在是 0.0044 → **把它正式命名为"长尾召回（Tail-Recall@L0）"**，
  并与总体 recall 并列作为**优化目标**（总体 recall 涨而长尾不涨，不算过关）。
- **S9（领域综述）**：40 篇竞品识别研究普遍"忽视间接与潜在竞品"。
  → 这是"扩金标准到全集"这个决策的**外部佐证**：竞品识别系统的通病就是只认直接竞品。

---

## 4. 本轮实测验证（文献主张要先在自家数据上验）

### 4.1 结构化数据（JSON-LD）路线 **实测不成立** ❌

| 站点 | 结果 |
|---|---|
| aigc.cn（导航站） | 无 JSON-LD（HTML 510 KB） |
| ai-bot.cn（导航站） | 无 JSON-LD（HTML 698 KB） |
| xingyeai.com（星野官网） | 无 JSON-LD（HTML 15 KB） |
| character.ai（英文站） | 直连超时（容器代理问题，未取到） |

**结论**：S10 说的"产品页标 Product、列表页标 ItemList"在**中文 AI 陪伴赛道不成立**
（广告型/SPA 站点普遍不写结构化数据）。→ **判站点类型与"抽列表"都不能靠 JSON-LD**，
只能靠 ①标题/URL 规则（现有）②**页面结构特征**（重复卡片块、外链密度、
同主机挂多少个不同产品名）。**这一条避免了按文献照抄后白做一轮。**

### 4.2 目录站当召回源：**可行，但要走对入口** ✅

- `aigc.cn` 首页：751 条链接，**指向 291 个不同站外域名、353 条外链**——是典型链接农场 ✓；
  且有**赛道专属分类**：`#term-6373-6381`（AI角色扮演）、`#term-6373-13906`（AI女友）、
  `#term-6373-242`（AI聊天对话）、`#term-1909-14875`（AI互动影游）。
  → 这些 tab 的内容是**锚点/JS 加载**的（首页 HTML 里没有对应产品链接）。
- `ai-bot.cn` 首页：只有 17 条站外链接（多半是页脚），品类导航同样是 `#term-*` 锚点。
- 两站都有**服务端渲染的集合页**（如 `aigc.cn/favorites/*`、`/ai-short-drama`、`/guo-jia-fan-zha-ai-app`），
  这类页面才是**可稳定抓、可分页、可复现**的入口 ✓。

**结论**：S8 的判断成立且有具体入口——**优先抓服务端渲染的集合页/分类页**，
不要去啃 JS 渲染的首页 tab；若能拿到 tab 的列表接口，成本会更低。
另外 `/favorites/new-ai-tools`（新收录）与 `/submit`（收录入口）提示：
**目录站自己就是持续更新的长尾源**。

### 4.3 多通道的必要性已被自己的数据证明 ✅

全集口径命中里，"仅独立来源"层几乎全灭（0.0044），而命中主要来自
**模型提名头部**——说明当前单通道（搜索 + 赛道词）覆盖的是"模型知道的那部分"。
S5 的多通道架构正是冲着这个来的。

---

## 5. 升级后的方案（重排上一版草案的 A/B/C/D，并新增 E/F）

| 优先级 | 项 | 来源 | 必要性 | 功能性（交付什么） | 成本 | 依赖 |
|---|---|---|---|---|---|---|
| **P0** | **B-0 tie-aware 评测**（同分流行度 + 期望指标 + 6 条报告清单 + 置换测试） | S1 | 必要·高 | 指标不再由字母序决定；S1 掉线能被解释 | 低 | 无 |
| **P0** | **C 空响应契约**（显式异常 + 降级标注） | S7（规范）+ 工程 | 必要·高 | 失败要么显式、要么显式标注降级 | 低 | 无 |
| **P0** | **D-1 计划落盘** | 工程 | 必要·中高 | 事后可复算当时口径 | 低 | 无 |
| **P1** | **E 多通道 + RRF 融合 + 通道归因** | S5, S8 | 必要·高 | 覆盖长尾的唯一结构性办法；并给出"哪个通道漏了什么" | 中 | §4.2 入口 |
| **P1** | **A 同赛道闸门**（listwise + permutation self-consistency） | S2, S7 | 必要·高 | 同赛道条款可执行；顺带给出稳定同分排序 | 中 | 候选 ≤30 |
| **P1** | **B-1 截断归因细分 + 名额账** | S1 | 必要·高 | 报告能回答"为什么没进、名额给了谁" | 低 | 无 |
| **P1** | **F 实体归并 blocking/filtering/聚类** | S4 | 必要·高 | 修"中文品牌匹配不上域名 + 首个候选胜出"的合并缺陷 | 中 | 拼音库 |
| **P2** | **B-2 MMR 多样性名额分配** | MMR | 中 | 名额不再被同源产品占满 | 中 | B-1 |
| **P2** | **D-2 计划冻结 / 协变量** | RecSys 种子协议 | 中 | 轮间可比 | 低-中 | D-1 |
| **P2** | **S3 自动补判扩全集**（LLM 补"洞"） | S3 | 中 | 全集可以增量维护，不再只靠人工 | 中 | 金标准流程 |
| **P2** | **长尾召回指标化**（Tail-Recall@L0 / catalog coverage） | S6 | 中 | 总体涨、长尾不涨不算过关 | 低 | 现有口径 |
| **P3** | 站点类型的页面结构特征（替代 JSON-LD） | §4.1 实测 | 中 | 通用化判目录站 | 中 | 抓取成本 |
| **P3** | 名额自适应、C-3 thinking 预算 | — | 低 | — | 低 | — |

---

## 6. 执行顺序（每批都有可验收产物）

**第一批（1 天，纯观测，不改发现行为）**
B-0 tie-aware 评测报告字段 · C 空响应契约与降级标注 · D-1 计划落盘 · B-1 截断归因细分
→ 用现有第八轮实跑产物离线复算即可（**不需要重跑网络**），验收 = 报告多出三段（新字段）。

**第二批（2–3 天，改裁决逻辑）**
A 同赛道闸门（listwise + 置换自洽）· F 实体归并三段式 · E 多通道 + RRF（先上 C3 目录页通道）
→ 验收 = **前后各跑一轮同一批实跑对照**，阈值不动；指标下降如实报（这是修缺陷，不是调阈值）。
→ 关键防回归：**金标准 289 条不得被新闸门误挡**（用全集做保留率检查）。

**第三批（按需）**
B-2 MMR 名额分配 · D-2 计划冻结 · S3 自动补判扩全集 · 长尾指标固化到门禁。

---

## 7. 需要拍板的三件事

1. **多通道的通道集合**：C3（目录分类页）与 C4（商店）先上哪个？
   建议 **C3 先上**（实测入口已找到，且能直接喂"仅独立来源"那一层的长尾）。
2. **同赛道判据源**：只用"品类词表 + 模型提名 + LLM 裁决"，还是允许用赛道名册？
   **若用名册，必须留出集**（否则测量自证）——本轮仍建议**只用词表+提名+LLM 裁决**。
3. **tie 口径**：R3/keep_L3/S1 是否统一改报 **tie 期望值**（而非单次字母序结果）？
   建议**改**，并在报告里同时留"单次结果"以便与历史轮次对齐。

---

## 8. 引用清单

1. Guo, Chen, Zhu, Li. *Tie Handling Is Part of the Evaluation Protocol: An Order-Invariance Audit for Tie-Heavy Recommender Scores.* FRAME'26 @ RecSys 2026. <https://arxiv.org/abs/2609.26977>（工件：<https://github.com/Guoo123/tie-evaluation-audit>）
2. Tang, Zhang, Ma, Lin, Ture. *Found in the Middle: Permutation Self-Consistency Improves Listwise Ranking in Large Language Models.* NAACL 2024. <https://aclanthology.org/2024.naacl-long.129/>（代码：<https://github.com/castorini/perm-sc>）
3. Upadhyay, Kamalloo, Lin. *LLMs Can Patch Up Missing Relevance Judgments in Evaluation.* <https://arxiv.org/abs/2405.04727>
4. Papadakis, Skoutas, Thanos, Palpanas. *A Survey of Blocking and Filtering Techniques for Entity Resolution.* <https://arxiv.org/abs/1905.06167>
5. Gaydhani, Xu, Kamath, Singh, Li. *Unified Learning-to-Rank for Multi-Channel Retrieval in Large-Scale E-Commerce Search.* <https://arxiv.org/abs/2602.23530>；RRF：<https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/>
6. Castells & Vargas. *Beyond Accuracy.* RecSys 2011. <https://dl.acm.org/doi/10.1145/1864708.1864761>；长尾综述：<https://onlinelibrary.wiley.com/doi/10.1155/2021/7536316>
7. *Judging the Judges: A Systematic Study of Position Bias in LLM-as-a-Judge.* <https://arxiv.org/html/2406.07791v7>
8. *A Comprehensive Guide to List Crawling.* Zenrows. <https://www.zenrows.com/blog/guide-to-list-crawling>
9. Werle et al. *Competitor identification: A review of use cases, data sources, and algorithms.* Int. J. Information Management, 2022. <https://dl.acm.org/doi/10.1016/j.ijinfomgt.2022.102507>
10. Carbonell & Goldstein. *The Use of MMR, Diversity-Based Reranking.* CMU 1998；工程实现：<https://www.elastic.co/search-labs/blog/maximum-marginal-relevance-diversify-results>
11. schema.org 规范（**注意：实测本赛道站点普遍未使用**）：<https://schema.org/Product>
