# 金标准域活性行核·审计结论（2026-11-02，人确认后落档）

证据源：`host_liveness_20261102.md`（代理视角，333 域）+ `host_liveness_20261102_direct.md`
（直连/国内视角，17 域复测）+ App Store iTunes Lookup 元数据 + 逐站抓取。
拍板方式：三项决定经人确认（2026-11-02）✓。

## 拍板 1：迁移域加入 hosts（旧域保留）✓ 已执行

| 条目 | 旧域（保留） | 新增域 | 证据 |
|---|---|---|---|
| Hangjam: AI Stories | hangjam.ai | **studio.hangjam.ai** | 200 重定向，标题同产品 ✓ |
| ParadiseAI | paradiseai.app | **paradise.app** | 200 重定向 ✓ |
| 怦然心动-… | foxdata.com | **foxdata.cn** | 200 重定向，标题"App大数据平台…FoxData" ✓ |
| Replika | —（**无需改**） | — | replika.com 早已在 hosts ✓（见"修正说明"） |

**修正说明（诚实记录）**：拍板时预估"加入 replika.com 可使 host R1 0.403→0.444"有误 ✗——
核查发现 companion_apps 与 track_universe 的 Replika hosts **原本就含 replika.com**，
第十三轮那 9/12 轮的命中本来就在账上。本项变更的实际影响 = **维护正确性**
（域搬迁后未来轮次仍可命中），不是当轮 R1 变化。教训见 lessons #98。

## 拍板 2：Talkie 维持不改 + 审计记证 ✓

证据链：
1. `talkie-ai.com` 双视角均 502（代理视角不可达、直连视角 502）——**服务器故障，域未死**；
2. **权威归属**：iTunes Lookup（id6450140383，"Talkie Lab - AI Playground"，
   SUBSUP PTE. LTD.）`sellerUrl = https://talkie-ai.com/` ✓——App Store 元数据证实
   它仍是官方域；美区该 id 已查无（resultCount=0，疑似下架或区域调整，另记）；
3. `talkieai.cc`（池中现居者）200，标题"Talkie AI - Chat, Roleplay & Collect Memories"，
   但**无任何开发者印记**（无 minimax/skyword/备案/开发者链接），"Contact Sales"
   指向 aiaff.ink（联盟导流）——**疑似蹭名站** ✗ 不入 hosts。

**推论（给测量口径的警告）**：品牌口径对 Talkie 的命中（12/12）很可能是**假阳性**
（发现的是蹭名站不是产品）⚠️——品牌口径只能做并列观测口径，判定口径留在 host，
这个案例是铁证。

## 拍板 3：死域只写审计，金标准 schema 不动 ✓

双视角均不可达（6 个）：`ai陪伴.cn`、`duxiangai.com`（400）、`myaifriendchat.com`、
`nexusbond.ai`、`secrets--ai.com`、`www.theramind.org`。
这些条目在 host 口径下**结构性不可达**（如实反映，不剔出分母）。

疑似域主变更（1 个）：`www.wasedoll.com`（梦艾）现在是一硅胶娃娃爱好者论坛
（Discuz! 站）——产品疑已死或域名易主，待下轮行核复核。

## 其它记录

- `xingyeai.com`（星野）：双视角 502——官方域没记错（池内仍有其 URL 且命中 ✓），
  属"官方站当前故障"，不动。
- **探测视角偏差**：代理视角（海外出口）会把 CN-only 域误判为不可达
  （16 个"不可达"直连复测后 9 个存活 ✓）——**行核必须双视角** ✗。
