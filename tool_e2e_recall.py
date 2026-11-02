"""发现召回的实跑工装：跑 N 次发现阶段，量漏斗、说清召回丢在哪一步。

判定线与口径在 `docs/discovery-recall-measurement.md`（**实跑前定稿**，本脚本不改线）。
金标准在 `evals/discovery_golden/companion_apps.json`（人核对过的真实产品官网）。

用法：
    uv run python tool_e2e_recall.py                       # 2 主体 × 2 句式 × 3 次
    uv run python tool_e2e_recall.py --repeats 2           # 少跑几轮
    uv run python tool_e2e_recall.py --subject 猫箱
    uv run python tool_e2e_recall.py --analyze output/recall_<ts>.json

产出：output/recall_<ts>.json（原始）+ output/recall_<ts>.md（人读）

只跑**发现阶段**（实体锚定 + 竞品发现），不跑逐家验证/反证、不抓取——
召回测量不需要它们，而它们占了整轮耗时的一大半。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import Counter

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.llm import get_llm_client  # noqa: E402
from src.tools import recall_metrics as rm  # noqa: E402
from src.tools.search_tool import get_search_tool  # noqa: E402
from src.workflow import research_plan as rp  # noqa: E402
from src.workflow import search_stages as ss  # noqa: E402

GOLDEN_FILE = REPO / "evals" / "discovery_golden" / "companion_apps.json"

# 主体 → (任务句式, 搜索词句式)。与 tool_e2e_search_real.py 保持一致，可与前几轮对照。
SUBJECTS = {
    "猫箱": (
        "帮我看看猫箱这个产品的全部竞品信息",
        "猫箱 竞品",
    ),
    "百度搭子": (
        "找一下百度搭子的全部竞品信息",
        "百度搭子 竞品",
    ),
}

# 判定线（实跑前定稿，见 docs/discovery-recall-measurement.md §4）
# 名字是给人看的标签，取值要落到 funnel_metrics 实际产出的字典里：
#   recall → {"L0","L1","L2","L3"}；keep → {"L1","L2","L3"}（L1/L2/L3 是相对上一层的保留率）
LINES = {
    "R1": ("recall", "L0", 0.70, "发现召回：金标准里被搜索捞到的比例"),
    "R2": ("keep", "L1", 0.90, "过滤保留：站点类型判定的漏杀率 ≤ 10%"),
    "R3": ("keep", "L3", 0.60, "入选保留：截断与闸门一共留下多少真竞品"),
    "S1": ("stability", "selected", 0.50, "重复稳定性：同句式重复跑，入选名单的重合度"),
}
LINE_LABELS = {
    "R1": "recall_L0", "R2": "keep_L1", "R3": "keep_L3", "S1": "stability_selected",
}


async def run_once(llm: Any, search_tool: Any, subject: str, style: str,
                   query: str, attempt: int,
                   plan: Optional[rp.ResearchPlan] = None) -> Dict[str, Any]:
    """跑一次发现阶段，返回一条 run 记录。

    `plan` 传入时**直接复用**（D-2 计划冻结 ✓：同一 (主体×句式) 跨轮复用
    同一份计划，消掉"查询词每轮不同"的 A/B 污染——第九轮实测两轮计划
    Jaccard 只有 0.097 ✗，管线改动和计划波动分不开）；不传则现场生成。
    """
    started = time.time()
    record: Dict[str, Any] = {
        "subject": subject,
        "style": style,
        "query": query,
        "attempt": attempt,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_frozen": plan is not None,
    }
    if plan is None:
        plan = await rp.build_research_plan(llm, query)
    record["entity"] = {
        "name": plan.entity_name,
        "confidence": plan.resolved_confidence,
        "source": plan.source,
        "error": plan.error,
        "model_candidates": list(plan.competitor_candidates or []),
        "queries": {stage: plan.queries_for(stage)
                    for stage in ("entity_anchor", "competitor_discovery")},
    }
    runner = ss.SearchStageRunner(search_tool)
    anchor_pages = runner.run_entity_anchor(plan)
    discovery = runner.run_competitor_discovery(plan)

    record["candidates"] = [
        {"url": c.url, "name": c.name, "site_type": c.site_type,
         "aggregator": bool(c.is_aggregator),
         "officiality": str(getattr(c, "officiality", "") or ""),
         "description": str(getattr(c, "description", "") or "")}
        for c in discovery.candidates
    ]
    record["scored"] = [
        {"url": item.get("url", ""), "name": item.get("name", ""),
         "score": item.get("score"), "gate_passed": bool(item.get("gate_passed")),
         "gate_reasons": list(item.get("gate_reasons") or []),
         "description": str(item.get("description") or ""),
         "nominated": bool(item.get("nominated")),
         "audit_flag": item.get("audit_flag"),
         "officiality": item.get("officiality") or "",
         "track_verdict": item.get("track_verdict")}
        for item in discovery.scored
    ]
    record["selected"] = [
        {"url": item.get("url", ""), "name": item.get("name", ""),
         "score": item.get("score"), "reasons": list(item.get("reasons") or []),
         "description": str(item.get("description") or ""),
         "source": item.get("source"),
         "officiality": item.get("officiality") or ""}
        for item in discovery.selected
    ]
    record["notes"] = list(discovery.notes)
    record["search_failed"] = bool(discovery.search_failed)
    # C3 通道读过的目录页与它贡献的候选：
    # 测量时要把"来源正好是这些页"的金标准条目从分母里剔掉（读答案≠发现 ✓）。
    record["channel_pages"] = list(getattr(discovery, "channel_pages", []) or [])
    record["channel_candidates"] = list(getattr(discovery, "channel_candidates", []) or [])
    # C4 应用商店通道：读过的分类/榜单页 + 贡献的候选（同 C3 的观测口径 ✓；
    # 测量剔"通道自证条目"时与 channel_pages 取并集 ✓）
    record["store_pages"] = list(getattr(discovery, "store_pages", []) or [])
    record["store_candidates"] = list(getattr(discovery, "store_candidates", []) or [])
    # 实体下钻：补查过官网的品牌 + 新进池的候选（观测口径 ✓；
    # 测量时用它与金标准命中的交集回答"下钻带来了多少" ✓）
    record["deep_dive_brands"] = list(getattr(discovery, "deep_dive_brands", []) or [])
    record["deep_dive_candidates"] = list(getattr(discovery, "deep_dive_candidates", []) or [])
    # 通道自己的 site: 查询失败（**不**作废整轮 ✓，但要看得见）
    record["channel_failures"] = list(getattr(discovery, "channel_failures", []) or [])
    # 名额账：每个名额给了谁、依据是什么、谁被挤掉了（纯观测 ✓）
    record["slot_ledger"] = dict(getattr(discovery, "slot_ledger", {}) or {})
    # **当时按什么口径选的**：max_count / must_include / 分数地板 / MMR 配置。
    # 这四样决定"名额怎么分"，不存下来就无法在事后复算
    # "同分块换一种次序会怎样"（tie 期望值，见 src/tools/recall_metrics.py）。
    record["selection"] = {
        "max_count": int(plan.max_count),
        "min_admission_score": float(rp.MIN_ADMISSION_SCORE),
        "must_include": list((plan.criteria or {}).get("must_include") or []),
        "diversity": rp.effective_diversity(plan.criteria or {}),
    }
    # **计划快照**：事后要能复算"这轮当时的口径是什么"（含命名、赛道词、
    # 准入规则原文），否则跨轮的差异分不清是"计划变了"还是"管线变了"。
    record["plan"] = {
        "entity_name": plan.entity_name,
        "source": plan.source,
        "track_keywords": list(plan.track_keywords or []),
        "competitor_candidates": list(plan.competitor_candidates or []),
        "criteria": dict(plan.criteria or {}),
        "queries": {stage: plan.queries_for(stage)
                    for stage in ("entity_anchor", "competitor_discovery")},
    }
    record["search_failures"] = list(discovery.search_failures)
    record["entity_pages"] = len(anchor_pages)
    record["elapsed_s"] = round(time.time() - started, 1)
    return record


def _invalid(rec: Dict[str, Any]) -> bool:
    """这一轮的数据是否无效（不能拿来量召回）。

    - 搜索后端整轮失败（引擎集体超时/限流）→ 候选池空是**后端抖动**的后果；
    - 计划层报错（主体都没认出来）→ 根本没跑到有意义的阶段。
    """
    if rec.get("search_failed"):
        return True
    ent = rec.get("entity") or {}
    if ent.get("source") == "error" or ent.get("error"):
        return True
    return False


async def run_with_retry(llm, search_tool, subject, style, query, attempts=3,
                         plan: Optional[rp.ResearchPlan] = None):
    """跑一次；数据无效就重试（最多 attempts 次），返回 (记录, 是否无效)。

    计划冻结时（plan 传入 ✓）重试也用同一份计划——无效只可能是搜索后端
    抖动，不会是"这次计划生成得不一样"（那会把两种失效混在一起 ✗）。
    """
    last = None
    for attempt in range(1, attempts + 1):
        rec = await run_once(llm, search_tool, subject, style, query, attempt,
                             plan=plan)
        last = rec
        if not _invalid(rec):
            return rec, False
        print(f"[recall] {subject}/{style} 第 {attempt} 次数据无效（搜索后端失败或计划报错），重试",
              flush=True)
        await asyncio.sleep(5)
    return last, True


# -- D-2 计划冻结（2026-11-01 拍板） --------------------------------------

def frozen_plan_path(frozen_dir: Path, subject: str, style: str) -> Path:
    return frozen_dir / f"{subject}_{style}.json"


async def load_or_build_plan(llm: Any, frozen_dir: Path,
                             subject: str, style: str, query: str,
                             regen: bool = False) -> tuple:
    """同一 (主体×句式) 的计划**只生成一次、落盘、跨轮复用**（D-2 ✓）。

    - 冻结粒度是 (主体×句式) 而不是主体 ✗：跨句式对照（task vs search）
      是在测的维度 ✗，冻结成一份就把它测没了 ✗。
    - 冻结文件入库（git ✓）：下一轮实跑直接复用这一份，
      计划波动从"每轮都变"变成"只在明确重生成时才变" ✓。
    - 副作用红利：一轮 12 次实跑的计划生成从 12 次降为 4 次 ✓
      （省配额，也消掉"中途 429 把计划生成打断"这个失效源 ✓）。
    """
    path = frozen_plan_path(frozen_dir, subject, style)
    if path.exists() and not regen:
        data = json.loads(path.read_text(encoding="utf-8"))
        return rp.ResearchPlan.from_dict(data), True
    plan = await rp.build_research_plan(llm, query)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return plan, False


def _tier_subsets(golden: rm.GoldenSet) -> Dict[str, rm.GoldenSet]:
    """按 source_tier 把金标准切成几个口径视图。

    为什么要分层：赛道全集里每条的来源不等价——
    独立来源（导航站/媒体/社区/英文榜单/Wikipedia/Reddit）中立，
    **厂商自写横评**有自我推荐的偏倚，应用商店层是**搜索 API 的抽样**
    （不可复现），而"只有本管线发现"的条目说明**外部来源也漏了**。
    同一批实跑在每个口径下的召回并列报，读者才能看清结论对口径有多敏感。
    """
    entries = list(golden.entries)
    if not any(e.get("source_tier") for e in entries):
        return {}
    def pick(tiers):
        return rm.GoldenSet([e for e in entries if (e.get("source_tier") or "") in tiers])
    subsets = {
        "口径：仅独立来源": pick({"external"}),
        "口径：独立来源+抽样核验": pick({"external", "sample_verified"}),
        "口径：全部（全集）": golden,
    }
    return {k: v for k, v in subsets.items() if len(v) > 0}


def _split_subsets(golden: rm.GoldenSet) -> Dict[str, rm.GoldenSet]:
    """按 split 切成"名册集 / 留出集"两个口径视图。

    为什么要有这两个视图：**名册是闸门的知识**（同赛道判据允许用名册 ✓），
    留出集是**闸门没见过的那一半**。两个数必须分开报——
    混在一起报，闸门的知识会把自己的成绩算进去（自证）。
    """
    entries = list(golden.entries)
    if not any(e.get("split") for e in entries):
        return {}
    subsets = {
        "口径：留出集（闸门没见过）": rm.GoldenSet(
            [e for e in entries if e.get("split") == "heldout"]),
        "口径：名册集（闸门见过）": rm.GoldenSet(
            [e for e in entries if e.get("split") == "registry"]),
    }
    return {k: v for k, v in subsets.items() if len(v) > 0}


def _self_serving_pages(runs: List[Dict[str, Any]]) -> List[str]:
    """所有通道读过的"答案页"（C3 目录页 ∪ C4 商店榜单页）——剔自证条目用 ✓。"""
    pages = set()
    for r in runs:
        pages.update(r.get("channel_pages") or [])
        pages.update(r.get("store_pages") or [])
    return sorted(pages)


def analyze_denominators(runs: List[Dict[str, Any]],
                         goldens: Dict[str, rm.GoldenSet]) -> List[Dict[str, Any]]:
    """**同一批实跑**在多份金标准下的召回对照（换分母，不重跑）。

    为什么要：把金标准从"抽样 18 个"扩成"赛道全集"之后，
    新旧口径的 R1 **不能直接相减**（分母变了）。要在**同一批 runs** 上
    并列算出来，才谈得上"换口径后召回是升是降"。
    判定线（R1 ≥0.70 等）**不动**，这里只并列报同一阈值下的两个数。
    """
    valid = [r for r in runs if not _invalid(r)]
    all_goldens: Dict[str, rm.GoldenSet] = dict(goldens)
    # 通道自证条目：来源正好是通道读过的页（C3 目录页 ∪ C4 商店榜单页）
    # → 不进任何口径的分母（读答案≠发现 ✓）
    channel_pages = _self_serving_pages(valid)
    if channel_pages:
        all_goldens = {k: v.exclude_by_source(channel_pages) for k, v in all_goldens.items()}

    def hostset(g: rm.GoldenSet):
        return frozenset(h for e in g.entries for h in (e.get("hosts") or []))

    # 带 source_tier 的金标准（赛道全集）自动补上分层视图：
    # 同一批实跑在"仅独立来源/独立来源+抽样/全部"三个口径下并列报。
    for label, g in list(goldens.items()):
        for sub_label, subset in _tier_subsets(g).items():
            if hostset(subset) in {hostset(x) for x in all_goldens.values()}:
                continue
            all_goldens[f"{label}·{sub_label}"] = subset
        # 带 split 的再加"留出集/名册集"两个视图（闸门见过没见过的区别）
        for sub_label, subset in _split_subsets(g).items():
            if hostset(subset) in {hostset(x) for x in all_goldens.values()}:
                continue
            all_goldens[f"{label}·{sub_label}"] = subset
    # 分层视图也要剔通道自证条目（否则"全集"视图含自证、留出集视图不含 ✗）
    if channel_pages:
        all_goldens = {k: v.exclude_by_source(channel_pages) for k, v in all_goldens.items()}
    # **含通道自证条目的对照口径**：通道找到的产品恰恰来自那些页，
    # 剔除后它的功劳在量测上就看不见了（第九轮：名额里的金标准命中
    # 0.76/轮 → 0.66/轮，差额几乎全是筑梦岛 8/12 轮 ✗）。
    # 所以额外并列报一个**含自证条目**的口径（明确标注，
    # 它不是判定口径，只用来回答"通道到底带来了多少" ✓）。
    if channel_pages:
        for label, g in list(goldens.items()):
            key = f"{label}·口径：含通道自证条目"
            if hostset(g) not in {hostset(x) for x in all_goldens.values()}:
                all_goldens[key] = g
    out: List[Dict[str, Any]] = []
    for label, golden in all_goldens.items():
        per_run = [rm.funnel_metrics(rec, golden) for rec in valid]
        recalls = _agg([m["recall"]["L0"] for m in per_run])
        keeps_l1 = _agg([m["keep"]["L1"] for m in per_run])
        keeps_l3 = _agg([m["keep"]["L3"] for m in per_run])
        # 口径不完整度：有 source_tier 字段的金标准里，
        # "只有本管线发现、外部来源没列"的条目数 = 这个口径至少还漏了多少。
        tiers: Dict[str, int] = {}
        for entry in golden.entries:
            tier = str(entry.get("source_tier") or "未标注")
            tiers[tier] = tiers.get(tier, 0) + 1
        out.append({
            "label": label,
            "golden_total": len(golden),
            "recall_L0": recalls,
            "keep_L1": keeps_l1,
            "keep_L3": keeps_l3,
            "r1_verdict": rm.verdict("R1", recalls, LINES["R1"][2]),
            # 实体（品牌）口径（非判定口径 ✗）
            "recall_brand_L0": _agg([m["recall_brand"]["L0"] for m in per_run]),
            "tiers": tiers,
            "per_run_recall": [m["recall"]["L0"] for m in per_run],
        })
    return out


def analyze(runs: List[Dict[str, Any]], golden: rm.GoldenSet) -> Dict[str, Any]:
    """算全部指标：每次运行的漏斗 + 每组的稳定性 + 跨句式对照。

    **通道自证条目要剔掉**：C3 目录页通道读的就是外部目录页，
    而金标准里有一部分条目也来自那些页 —— 通道读到那张表再"发现"上面的
    产品，那叫读答案。所以这些条目不进分母（分子分母一起剔 ✓）。
    """
    valid = [r for r in runs if not _invalid(r)]
    channel_pages = _self_serving_pages(valid)
    measured = golden.exclude_by_source(channel_pages)
    per_run = []
    for rec in valid:
        m = rm.funnel_metrics(rec, measured)
        per_run.append({
            "subject": rec["subject"], "style": rec["style"], "attempt": rec.get("attempt"),
            "elapsed_s": rec.get("elapsed_s"),
            "metrics": m,
            "slot_ledger": rec.get("slot_ledger") or {},
        })

    # 同一主体同一句式的重复稳定性
    stability: List[Dict[str, Any]] = []
    for subject in SUBJECTS:
        for style in ("task", "search"):
            same = [r for r in valid if r["subject"] == subject and r["style"] == style]
            if len(same) < 2:
                continue
            stability.append({
                "subject": subject,
                "style": style,
                "selected": rm.stability_metrics(same, key="selected"),
                "candidates": rm.stability_metrics(same, key="candidates"),
            })

    # 跨句式对照（Q3 的同款计算：同主体 task vs search 的入选名单主机重合度）
    cross = []
    for subject in SUBJECTS:
        task = [r for r in valid if r["subject"] == subject and r["style"] == "task"]
        search = [r for r in valid if r["subject"] == subject and r["style"] == "search"]
        if not task or not search:
            continue
        a = [i["url"] for i in task[0]["selected"]]
        b = [i["url"] for i in search[0]["selected"]]
        overlap = rm.host_overlap(a, b)
        # 多轮时取每一对的重合度均值
        pairs = []
        for x in task:
            for y in search:
                pairs.append(rm.host_overlap([i["url"] for i in x["selected"]],
                                             [i["url"] for i in y["selected"]])["jaccard"])
        cross.append({
            "subject": subject,
            "first_pair_jaccard": overlap["jaccard"],
            "mean_pair_jaccard": round(sum(pairs) / len(pairs), 4) if pairs else None,
            "common": overlap["common"],
            "only_task": overlap["only_a"],
            "only_search": overlap["only_b"],
        })
    # ---- 实体（品牌）口径：量化"产品被发现了、只是域名没对上"的缺口 ----
    # 品牌命中但 host 未命中的金标准产品逐轮计数（Talkie 案例 ✓）
    brand_l0: List[Optional[float]] = []
    brand_gap: Dict[str, int] = {}
    suspect_counts: List[int] = []
    for rec, item in zip(valid, per_run):
        m = item["metrics"]
        brand_l0.append(m["recall_brand"]["L0"])
        host_names = {h["name"] for h in m["hits"]["L0"]}
        for h in m["hits_brand"]["L0"]:
            if h["name"] not in host_names:
                brand_gap[h["name"]] = brand_gap.get(h["name"], 0) + 1
        suspect_counts.append(sum(
            1 for c in (rec.get("candidates") or [])
            if str(c.get("officiality") or "") == "suspect"))
    return {
        "golden_total": len(golden),
        "golden_measured": len(measured),
        "channel_excluded": len(golden) - len(measured),
        "channel_pages": channel_pages,
        "runs_total": len(runs),
        "runs_valid": len(valid),
        "runs_invalid": len(runs) - len(valid),
        "per_run": per_run,
        "stability": stability,
        "cross_phrasing": cross,
        # 实体口径（非判定口径 ✗——判定线仍用 host 口径 ✓）
        "recall_brand_L0": _agg(brand_l0),
        "brand_gap": sorted(brand_gap.items(), key=lambda kv: (-kv[1], kv[0])),
        # 官方性卫兵：每轮 suspect 均值（蹭名站已从品牌命中剔除 ✓）
        "suspect_mean_per_run": (round(sum(suspect_counts) / len(suspect_counts), 1)
                                 if suspect_counts else None),
        "tie": tie_analysis(valid, measured),
    }


def tie_analysis(valid: List[Dict[str, Any]], golden: rm.GoldenSet) -> Dict[str, Any]:
    """同分块（tie）的情况与**同分下的期望指标**。

    为什么要这块：候选分数几乎全是并列 0.5，同分块内按名字字母序 →
    **字母序在决定谁能进名额**。报某一次字母序结果等于报一个任意值
    （FRAME'26 @ RecSys'26：tie-break 规则就是排序规则的一部分）。
    这里报两样：① 同分流行度与"有没有同分块跨过名额边界"；
    ② 换 100 个种子（同分块换 100 次次序）后的 keep_L3 与稳定性分布。

    口径与单次结果对齐：分组（主体×句式）内各算一次，再按组取均值——
    和 line_results 的聚合方式一致，两个数才能并排比。
    """
    stats = [rm.tie_stats(rec) for rec in valid]
    tied_ratio = rm._mean([s["tied_ratio"] for s in stats if s.get("tied_ratio") is not None])
    crossing = rm._mean([float(s["blocks_crossing_cutoff"]) for s in stats])
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in valid:
        groups.setdefault(f"{rec.get('subject')}/{rec.get('style')}", []).append(rec)
    per_group: Dict[str, Any] = {}
    keep_l3_means: List[float] = []
    stability_means: List[float] = []
    computable = True
    reason = ""
    for gname, gruns in sorted(groups.items()):
        if len(gruns) < 2:
            continue
        exp = rm.tie_expected_metrics(gruns, golden, seeds=100)
        per_group[gname] = exp
        if not exp.get("computable"):
            computable = False
            reason = exp.get("reason") or ""
            continue
        k = exp["keep_l3"].get("mean")
        s = exp["stability"].get("mean")
        if k is not None:
            keep_l3_means.append(k)
        if s is not None:
            stability_means.append(s)
    return {
        "per_run": stats,
        "tied_ratio_mean": round(tied_ratio, 4) if tied_ratio is not None else None,
        "max_block": max((s["max_block"] for s in stats), default=0),
        "blocks_crossing_cutoff_mean": round(crossing, 2) if crossing is not None else None,
        "per_group": per_group,
        "computable": computable,
        "reason": reason,
        "keep_l3_mean": round(sum(keep_l3_means) / len(keep_l3_means), 4) if keep_l3_means else None,
        "keep_l3_sd": round(_sd(keep_l3_means), 4) if keep_l3_means else None,
        "stability_mean": round(sum(stability_means) / len(stability_means), 4) if stability_means else None,
        "stability_sd": round(_sd(stability_means), 4) if stability_means else None,
    }


def _sd(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _agg(values: List[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def line_results(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """按定稿的线判定（不改线）。"""
    per_run = analysis["per_run"]
    out: Dict[str, Any] = {}
    # R1/R2/R3：按"主体×句式"分组平均（重复次数之间取均值），再看线
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in per_run:
        groups.setdefault(f"{item['subject']}/{item['style']}", []).append(item)
    for code, (section, key, threshold, desc) in LINES.items():
        if code == "S1":
            continue
        by_group = {}
        for gname, items in groups.items():
            vals = [it["metrics"][section].get(key) for it in items]
            by_group[gname] = _agg(vals)
        overall = _agg(list(by_group.values()))
        out[code] = {
            "metric": LINE_LABELS[code],
            "threshold": threshold,
            "desc": desc,
            "by_group": by_group,
            "overall": overall,
            "verdict": rm.verdict(code, overall, threshold),
        }
    # S1：稳定性取各组均值
    stab = analysis["stability"]
    sel_vals = [s["selected"]["mean_jaccard"] for s in stab]
    overall = _agg(sel_vals)
    out["S1"] = {
        "metric": LINE_LABELS["S1"],
        "threshold": LINES["S1"][2],
        "desc": LINES["S1"][3],
        "by_group": {f"{s['subject']}/{s['style']}": s["selected"]["mean_jaccard"] for s in stab},
        "overall": overall,
        "verdict": rm.verdict("S1", overall, LINES["S1"][2]),
    }
    # ---- 同分下的期望值（决策：R3/keep_L3/S1 统一改报 tie 期望值） ----
    # 单次结果保留在 single_run_* 里，便于与历史轮次并排看；
    # 判定用期望值（线不变，**指标定义变了**，报告里写清楚）。
    tie = analysis.get("tie") or {}
    if tie.get("computable") and tie.get("keep_l3_mean") is not None:
        out["R3"]["single_run_overall"] = out["R3"]["overall"]
        out["R3"]["overall"] = tie["keep_l3_mean"]
        out["R3"]["tie_sd"] = tie.get("keep_l3_sd")
        out["R3"]["verdict"] = rm.verdict("R3", tie["keep_l3_mean"], LINES["R3"][2])
    if tie.get("computable") and tie.get("stability_mean") is not None:
        out["S1"]["single_run_overall"] = out["S1"]["overall"]
        out["S1"]["overall"] = tie["stability_mean"]
        out["S1"]["tie_sd"] = tie.get("stability_sd")
        out["S1"]["verdict"] = rm.verdict("S1", tie["stability_mean"], LINES["S1"][2])
    return out


def render_markdown(analysis: Dict[str, Any], lines: Dict[str, Any],
                    meta: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# 发现召回实跑报告")
    out.append("")
    out.append(f"- 生成时间：{meta['generated_at']}")
    out.append(f"- 金标准：`{meta['golden_file']}`（{analysis['golden_total']} 条，人核对过官网）")
    if analysis.get("channel_excluded"):
        out.append(f"- **通道自证条目已剔除 {analysis['channel_excluded']} 条**"
                   f"（来源正好是 C3 通道读过的目录页："
                   f"{'、'.join(analysis.get('channel_pages') or [])[:120]}）——"
                   f"实际分母 {analysis.get('golden_measured')} 条")
    out.append(f"- 运行：{analysis['runs_valid']} 次有效 / {analysis['runs_total']} 次"
               f"（无效 {analysis['runs_invalid']} 次：搜索后端失败或计划报错）")
    out.append(f"- 判定线来源：`docs/discovery-recall-measurement.md` §4（实跑前定稿）")
    out.append("")
    out.append("## 1. 判定结果")
    out.append("")
    out.append("| 线 | 指标 | 阈值 | 实测 | 结论 |")
    out.append("|---|---|---|---|---|")
    for code in ("R1", "R2", "R3", "S1"):
        item = lines[code]
        val = item["overall"]
        out.append(f"| {code} | {item['metric']} | ≥ {item['threshold']} | "
                   f"{val if val is not None else '不判'} | {item['verdict']['status']} |")
    out.append("")
    out.append("逐组：")
    out.append("")
    out.append("| 线 | " + " | ".join(sorted({g for it in lines.values()
                                       for g in it["by_group"]})) + " |")
    groups = sorted({g for it in lines.values() for g in it["by_group"]})
    out.append("|---" * (len(groups) + 1) + "|")
    for code in ("R1", "R2", "R3", "S1"):
        row = [code]
        for g in groups:
            v = lines[code]["by_group"].get(g)
            row.append(str(v) if v is not None else "不判")
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    # ---- 实体（品牌）口径：产品被发现了、只是域名没对上（Talkie 案例 ✓）----
    # 非判定口径 ✗：判定线仍用 host 口径（预注册不变 ✓）。
    brand_recall = analysis.get("recall_brand_L0")
    suspect_mean = analysis.get("suspect_mean_per_run")
    if brand_recall is not None:
        host_r1 = lines["R1"]["overall"]
        note = ""
        if host_r1 is not None:
            note = f"（同批 host 口径 R1={host_r1}，差 {round(brand_recall - host_r1, 4)} = 域名没对上的部分）"
        guard = (f"；官方性卫兵：suspect 均值 {suspect_mean}/轮"
                 "已从品牌命中中剔除（蹭名站不给记命中 ✓）") if suspect_mean is not None else ""
        out.append(f"- **实体口径（品牌匹配，非判定口径 ✗）**：recall_L0={brand_recall} {note}；"
                   f"判定线仍用 host 口径（预注册不变）✓{guard}")
        brand_gap = analysis.get("brand_gap") or []
        if brand_gap:
            total_runs = analysis.get("runs_valid") or 0
            out.append("")
            out.append("品牌命中但 host 未命中的产品（**实体归并缺口**，逐轮计数）：")
            out.append("")
            out.append("| 金标准产品 | 出现在 |")
            out.append("|---|---|")
            for name, cnt in brand_gap[:10]:
                out.append(f"| {name} | {cnt}/{total_runs} 轮 |")
    out.append("")
    out.append("## 2. 漏斗（每次运行）")
    out.append("")
    out.append("| 主体 | 句式 | 次数 | L0 发现池 | L1 官网池 | L2 裁决池 | L3 入选 | "
               "recall_L0 | keep_L1 | keep_L3 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in analysis["per_run"]:
        m = item["metrics"]
        c = m["counts"]
        out.append(
            f"| {item['subject']} | {item['style']} | {item['attempt']} | "
            f"{c['L0']} | {c['L1']} | {c['L2']} | {c['L3']} | "
            f"{m['recall']['L0']:.3f} | "
            f"{m['keep']['L1'] if m['keep']['L1'] is not None else '不判'} | "
            f"{m['keep']['L3'] if m['keep']['L3'] is not None else '不判'} |")
    out.append("")
    out.append("## 3. 召回丢在哪一步（每个金标准产品逐条归因）")
    out.append("")
    out.append("| 主体/句式 | 次数 | 金标准 | 丢在发现 | 丢在站点类型 | 丢在闸门 | 丢在同名合并 | 丢在截断 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for item in analysis["per_run"]:
        lost = item["metrics"]["lost"]
        buckets = {rm.LOST_NOT_FOUND: 0, rm.LOST_SITE_TYPE: 0,
                   rm.LOST_GATE: 0, rm.LOST_CUT: 0, rm.LOST_MERGE: 0}
        for entry in lost:
            buckets[entry["reason"]] = buckets.get(entry["reason"], 0) + 1
        out.append(
            f"| {item['subject']}/{item['style']} | {item['attempt']} | "
            f"{item['metrics']['golden_total']} | {buckets[rm.LOST_NOT_FOUND]} | "
            f"{buckets[rm.LOST_SITE_TYPE]} | {buckets[rm.LOST_GATE]} | "
            f"{buckets[rm.LOST_MERGE]} | {buckets[rm.LOST_CUT]} |")
    out.append("")
    out.append("### 3.1 逐条明细（第一次运行）")
    out.append("")
    out.append("| 主体/句式 | 金标准产品 | 丢在哪一步 | 说明 |")
    out.append("|---|---|---|---|")
    seen_groups = set()
    for item in analysis["per_run"]:
        key = f"{item['subject']}/{item['style']}"
        if key in seen_groups:
            continue
        seen_groups.add(key)
        for entry in item["metrics"]["lost"]:
            out.append(f"| {key} | {entry['name']} | {entry['reason']} | {entry['detail']} |")
    out.append("")
    out.append("## 4. 稳定性（同一句式重复跑）")
    out.append("")
    out.append("| 主体/句式 | 入选名单两两 Jaccard | 发现池两两 Jaccard | 各次入选数 | 名单并集 |")
    out.append("|---|---|---|---|---|")
    for s in analysis["stability"]:
        out.append(f"| {s['subject']}/{s['style']} | {s['selected']['mean_jaccard']} | "
                   f"{s['candidates']['mean_jaccard']} | {s['selected']['sizes']} | "
                   f"{s['selected']['union_size']} |")
    out.append("")
    out.append("## 5. 跨句式对照（Q3 的同款计算）")
    out.append("")
    out.append("| 主体 | 首对 Jaccard | 多对均值 | 共同 | 仅 task | 仅 search |")
    out.append("|---|---|---|---|---|---|")
    for c in analysis["cross_phrasing"]:
        out.append(f"| {c['subject']} | {c['first_pair_jaccard']} | {c['mean_pair_jaccard']} | "
                   f"{', '.join(c['common']) or '（无）'} | "
                   f"{', '.join(c['only_task']) or '（无）'} | "
                   f"{', '.join(c['only_search']) or '（无）'} |")
    out.append("")
    out.append("## 6. 同分（tie）稳健性")
    out.append("")
    tie = analysis.get("tie") or {}
    if not tie:
        out.append("（本轮没有可用的同分统计）")
    else:
        out.append(f"- 裁决池里同分候选占比（各轮均值）：{tie.get('tied_ratio_mean')}；"
                   f"最大同分块 {tie.get('max_block')} 个；"
                   f"**跨过名额边界的同分块**平均每轮 {tie.get('blocks_crossing_cutoff_mean')} 个")
        if tie.get("computable"):
            out.append(f"- 同分下的期望值（换 100 个种子 = 同分块换 100 次次序）："
                       f"keep_L3 = {tie.get('keep_l3_mean')}"
                       f"（组间标准差 {tie.get('keep_l3_sd')}）；"
                       f"入选稳定性 = {tie.get('stability_mean')}"
                       f"（组间标准差 {tie.get('stability_sd')}）")
            out.append("")
            out.append("| 主体/句式 | keep_L3 期望 | keep_L3 标准差 | 稳定性期望 | 稳定性标准差 |")
            out.append("|---|---|---|---|---|")
            for gname, exp in (tie.get("per_group") or {}).items():
                if not exp.get("computable"):
                    continue
                out.append(f"| {gname} | {exp['keep_l3'].get('mean')} | {exp['keep_l3'].get('sd')} | "
                           f"{exp['stability'].get('mean')} | {exp['stability'].get('sd')} |")
            out.append("")
            out.append("> **判定线未改，指标定义改了**：R3/S1 现在报的是"
                       "**同分下的期望值**（单次结果保留在上表对照）。"
                       "单次结果里的同分块成员由名字字母序决定，"
                       "换一种次序结果就会变——报单次数等于报一个任意值。")
        else:
            out.append(f"- 同分期望值**不可算**：{tie.get('reason') or '缺少 selection 口径'}")
    out.append("")
    denominators = meta.get("denominators") or []
    if denominators:
        out.append("## 7. 口径对照（同一批实跑，换分母）")
        out.append("")
        out.append("| 金标准口径 | 条目数 | recall_L0 | keep_L1 | keep_L3 | R1 判定（线 ≥0.70） |")
        out.append("|---|---|---|---|---|---|")
        for item in denominators:
            out.append(
                f"| {item['label']} | {item['golden_total']} | "
                f"{item['recall_L0'] if item['recall_L0'] is not None else '不判'} | "
                f"{item['keep_L1'] if item['keep_L1'] is not None else '不判'} | "
                f"{item['keep_L3'] if item['keep_L3'] is not None else '不判'} | "
                f"{item['r1_verdict']['status']} |")
        out.append("")
        for item in denominators:
            if item.get("tiers") and len(item["tiers"]) > 1:
                parts = "、".join(f"{k} {v} 条" for k, v in sorted(item["tiers"].items()))
                out.append(f"- {item['label']} 的来源构成：{parts}")
        out.append("")
        out.append("> 两个口径的分母不同，**数值不可直接相减**；"
                   "判定线（R1 ≥ 0.70）实跑前定稿，未改动。")
        out.append("")
    out.append(_render_slot_ledger(analysis))
    return "\n".join(out)


def _render_slot_ledger(analysis: Dict[str, Any]) -> str:
    """名额账：名额给了谁、依据是什么、谁被挤掉了（纯观测 ✗ 不改行为）。"""
    per_run = analysis.get("per_run") or []
    ledgers = [(item["subject"], item["style"], item["attempt"],
                item.get("slot_ledger") or {})
               for item in per_run]
    ledgers = [x for x in ledgers if x[3].get("slots")]
    if not ledgers:
        return ""
    out: List[str] = []
    out.append("## 8. 名额账（8 个名额给了谁）")
    out.append("")
    counter: Counter = Counter()
    named_hit = 0
    audited = 0
    for _, _, _, ledger in ledgers:
        counter.update(ledger.get("source_counter") or {})
        named_hit += int(ledger.get("must_include_hit") or 0)
        audited += sum(1 for s in (ledger.get("slots") or []) if s.get("audit_flag"))
    total = sum(counter.values()) or 1
    out.append(f"- 轮次 {len(ledgers)}，名额合计 {total} 个")
    out.append(f"- 名额来源：计划点名 {counter.get('计划点名', 0)}、"
               f"模型提名 {counter.get('模型提名', 0)}"
               f"（{counter.get('模型提名', 0) / total:.1%}）、"
               f"发现（搜索/通道） {counter.get('发现', 0)}"
               f"（{counter.get('发现', 0) / total:.1%}）")
    out.append(f"- 计划点名兑现：{named_hit} 个名额；"
               f"带审计标记（提名但无赛道证据，已降权）的名额：{audited} 个")
    out.append("")
    out.append("| 主体/句式 | 次数 | 名额 | 产品 | 分数 | 来源 | 依据 |")
    out.append("|---|---|---|---|---|---|---|")
    for subject, style, attempt, ledger in ledgers:
        for slot in (ledger.get("slots") or []):
            out.append(
                f"| {subject}/{style} | {attempt} | {slot.get('slot')} | "
                f"{slot.get('name')} | {slot.get('score')} | {slot.get('source')} | "
                f"{'；'.join(slot.get('reasons') or [])} |")
    out.append("")
    out.append("> 名额来源是**观测值**，不是判定线："
               "「模型提名占比高」说明模型的点名决定了名单，"
               "不代表名单不好（头部竞品本来就该在）。")
    out.append("")
    return "\n".join(out)


async def main_async(args) -> int:
    golden = rm.GoldenSet.from_file(str(args.golden), subject=None,
                                   split=getattr(args, "golden_split", None))
    llm = get_llm_client()
    search_tool = get_search_tool()
    runs: List[Dict[str, Any]] = []
    subjects = args.subjects or list(SUBJECTS)
    for subject in subjects:
        task_q, search_q = SUBJECTS[subject]
        for style, query in (("task", task_q), ("search", search_q)):
            # D-2 计划冻结：同一 (主体×句式) 只生成/加载一次，重复轮全复用 ✓
            plan, frozen = await load_or_build_plan(
                llm, Path(args.frozen_plans_dir), subject, style, query,
                regen=bool(args.regen_plans))
            print(f"[recall] {'复用冻结计划' if frozen else '生成新计划（已冻结落盘）'}"
                  f"：{subject}/{style} → {frozen_plan_path(Path(args.frozen_plans_dir), subject, style)}",
                  flush=True)
            for repeat in range(1, args.repeats + 1):
                print(f"[recall] {subject}/{style} 第 {repeat}/{args.repeats} 次 …", flush=True)
                rec, invalid = await run_with_retry(llm, search_tool, subject, style,
                                                    query, attempts=args.attempts,
                                                    plan=plan)
                rec["repeat"] = repeat
                rec["invalid"] = invalid
                runs.append(rec)
                print(f"[recall]   候选 {len(rec['candidates'])} / 入选 {len(rec['selected'])} "
                      f"/ {rec['elapsed_s']}s{'（数据无效）' if invalid else ''}", flush=True)
                await asyncio.sleep(args.pause)
    ts = time.strftime("%Y%m%d_%H%M%S")
    raw_path = Path("output") / f"recall_{ts}.json"
    md_path = Path("output") / f"recall_{ts}.md"
    analysis = analyze(runs, golden)
    lines = line_results(analysis)
    goldens = {"主口径：" + Path(args.golden).stem: golden}
    for extra in (args.golden_extra or []):
        # 对照口径是**别的文件**，不套主口径的 split 过滤（各文件各有自己的口径）
        goldens["对照口径：" + Path(extra).stem] = rm.GoldenSet.from_file(str(extra))
    denominators = analyze_denominators(runs, goldens)
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "golden_file": str(args.golden),
            "denominators": denominators}
    raw_path.write_text(json.dumps({"meta": meta, "runs": runs, "analysis": analysis,
                                    "lines": lines}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    md_path.write_text(render_markdown(analysis, lines, meta), encoding="utf-8")
    print(f"\n报告：{md_path.resolve()}")
    print(f"原始：{raw_path.resolve()}")
    print()
    for code in ("R1", "R2", "R3", "S1"):
        item = lines[code]
        val = item["overall"]
        print(f"  {code}: {item['metric']} = {val if val is not None else '不判'} "
              f"（线 ≥{item['threshold']}）→ {item['verdict']['status']}")
    print()
    print("口径对照（同一批实跑，换分母；判定线未改）：")
    for item in denominators:
        print(f"  {item['label']:34s} {item['golden_total']:3d} 条｜"
              f"recall_L0={item['recall_L0']} → R1 {item['r1_verdict']['status']}")
    failed = [c for c in ("R1", "R2", "R3", "S1") if lines[c]["verdict"]["pass"] is False]
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="发现召回实跑（只跑发现阶段）")
    parser.add_argument("--golden", default=str(GOLDEN_FILE), help="金标准文件")
    parser.add_argument("--golden-split", default=None,
                        choices=["heldout", "registry"],
                        help="只取金标准里的某一半：heldout=测量主口径（闸门没见过），"
                             "registry=名册那半（闸门见过）")
    parser.add_argument("--golden-extra", nargs="*", default=None,
                        help="对照口径的金标准文件（同一批实跑换分母，不重跑）")
    parser.add_argument("--repeats", type=int, default=3, help="每种句式重复几次")
    parser.add_argument("--attempts", type=int, default=3, help="数据无效时最多重试几次")
    parser.add_argument("--pause", type=float, default=3.0, help="两次运行之间的间隔秒数")
    parser.add_argument("--frozen-plans-dir", default=str(REPO / "evals" / "frozen_plans"),
                        help="D-2 计划冻结目录：同一 (主体×句式) 的计划存这里、跨轮复用")
    parser.add_argument("--regen-plans", action="store_true",
                        help="忽略已冻结的计划、重新生成并覆盖（显式动作，不默认）")
    parser.add_argument("--subjects", nargs="*", default=None, help="只跑这些主体")
    parser.add_argument("--analyze", default=None, help="只对已有 artifact 做分析")
    args = parser.parse_args()
    if args.analyze:
        data = json.loads(Path(args.analyze).read_text(encoding="utf-8"))
        golden = rm.GoldenSet.from_file(str(args.golden), subject=None,
                                   split=getattr(args, "golden_split", None))
        analysis = analyze(data["runs"], golden)
        lines = line_results(analysis)
        meta = data.get("meta") or {}
        goldens = {"主口径：" + Path(args.golden).stem: golden}
        for extra in (args.golden_extra or []):
            # 对照口径是**别的文件**，不套主口径的 split 过滤（各文件各有自己的口径）
            goldens["对照口径：" + Path(extra).stem] = rm.GoldenSet.from_file(str(extra))
        meta["denominators"] = analyze_denominators(data["runs"], goldens)
        md = Path(args.analyze).with_suffix(".md")
        md.write_text(render_markdown(analysis, lines, meta), encoding="utf-8")
        print(f"报告：{md.resolve()}")
        for code in ("R1", "R2", "R3", "S1"):
            item = lines[code]
            val = item["overall"]
            print(f"  {code}: {item['metric']} = {val if val is not None else '不判'} "
                  f"（线 ≥{item['threshold']}）→ {item['verdict']['status']}")
        print("口径对照：")
        for item in meta["denominators"]:
            print(f"  {item['label']:34s} {item['golden_total']:3d} 条｜"
                  f"recall_L0={item['recall_L0']} → R1 {item['r1_verdict']['status']}")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
