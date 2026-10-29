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
from typing import Any, Dict, List

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
                   query: str, attempt: int) -> Dict[str, Any]:
    """跑一次发现阶段，返回一条 run 记录。"""
    started = time.time()
    record: Dict[str, Any] = {
        "subject": subject,
        "style": style,
        "query": query,
        "attempt": attempt,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
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
         "aggregator": bool(c.is_aggregator)}
        for c in discovery.candidates
    ]
    record["scored"] = [
        {"url": item.get("url", ""), "name": item.get("name", ""),
         "score": item.get("score"), "gate_passed": bool(item.get("gate_passed")),
         "gate_reasons": list(item.get("gate_reasons") or [])}
        for item in discovery.scored
    ]
    record["selected"] = [
        {"url": item.get("url", ""), "name": item.get("name", ""),
         "score": item.get("score"), "reasons": list(item.get("reasons") or [])}
        for item in discovery.selected
    ]
    record["notes"] = list(discovery.notes)
    record["search_failed"] = bool(discovery.search_failed)
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


async def run_with_retry(llm, search_tool, subject, style, query, attempts=3):
    """跑一次；数据无效就重试（最多 attempts 次），返回 (记录, 是否无效)。"""
    last = None
    for attempt in range(1, attempts + 1):
        rec = await run_once(llm, search_tool, subject, style, query, attempt)
        last = rec
        if not _invalid(rec):
            return rec, False
        print(f"[recall] {subject}/{style} 第 {attempt} 次数据无效（搜索后端失败或计划报错），重试",
              flush=True)
        await asyncio.sleep(5)
    return last, True


def analyze(runs: List[Dict[str, Any]], golden: rm.GoldenSet) -> Dict[str, Any]:
    """算全部指标：每次运行的漏斗 + 每组的稳定性 + 跨句式对照。"""
    valid = [r for r in runs if not _invalid(r)]
    per_run = []
    for rec in valid:
        m = rm.funnel_metrics(rec, golden)
        per_run.append({
            "subject": rec["subject"], "style": rec["style"], "attempt": rec.get("attempt"),
            "elapsed_s": rec.get("elapsed_s"),
            "metrics": m,
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
    return {
        "golden_total": len(golden),
        "runs_total": len(runs),
        "runs_valid": len(valid),
        "runs_invalid": len(runs) - len(valid),
        "per_run": per_run,
        "stability": stability,
        "cross_phrasing": cross,
    }


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
    return out


def render_markdown(analysis: Dict[str, Any], lines: Dict[str, Any],
                    meta: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# 发现召回实跑报告")
    out.append("")
    out.append(f"- 生成时间：{meta['generated_at']}")
    out.append(f"- 金标准：`{meta['golden_file']}`（{analysis['golden_total']} 条，人核对过官网）")
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
    out.append("| 主体/句式 | 次数 | 金标准 | 丢在发现 | 丢在站点类型 | 丢在闸门 | 丢在截断 |")
    out.append("|---|---|---|---|---|---|---|")
    for item in analysis["per_run"]:
        lost = item["metrics"]["lost"]
        buckets = {rm.LOST_NOT_FOUND: 0, rm.LOST_SITE_TYPE: 0,
                   rm.LOST_GATE: 0, rm.LOST_CUT: 0}
        for entry in lost:
            buckets[entry["reason"]] = buckets.get(entry["reason"], 0) + 1
        out.append(
            f"| {item['subject']}/{item['style']} | {item['attempt']} | "
            f"{item['metrics']['golden_total']} | {buckets[rm.LOST_NOT_FOUND]} | "
            f"{buckets[rm.LOST_SITE_TYPE]} | {buckets[rm.LOST_GATE]} | {buckets[rm.LOST_CUT]} |")
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
    return "\n".join(out)


async def main_async(args) -> int:
    golden = rm.GoldenSet.from_file(str(args.golden), subject=None)
    llm = get_llm_client()
    search_tool = get_search_tool()
    runs: List[Dict[str, Any]] = []
    subjects = args.subjects or list(SUBJECTS)
    for subject in subjects:
        task_q, search_q = SUBJECTS[subject]
        for style, query in (("task", task_q), ("search", search_q)):
            for repeat in range(1, args.repeats + 1):
                print(f"[recall] {subject}/{style} 第 {repeat}/{args.repeats} 次 …", flush=True)
                rec, invalid = await run_with_retry(llm, search_tool, subject, style,
                                                    query, attempts=args.attempts)
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
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "golden_file": str(args.golden)}
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
    failed = [c for c in ("R1", "R2", "R3", "S1") if lines[c]["verdict"]["pass"] is False]
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="发现召回实跑（只跑发现阶段）")
    parser.add_argument("--golden", default=str(GOLDEN_FILE), help="金标准文件")
    parser.add_argument("--repeats", type=int, default=3, help="每种句式重复几次")
    parser.add_argument("--attempts", type=int, default=3, help="数据无效时最多重试几次")
    parser.add_argument("--pause", type=float, default=3.0, help="两次运行之间的间隔秒数")
    parser.add_argument("--subjects", nargs="*", default=None, help="只跑这些主体")
    parser.add_argument("--analyze", default=None, help="只对已有 artifact 做分析")
    args = parser.parse_args()
    if args.analyze:
        data = json.loads(Path(args.analyze).read_text(encoding="utf-8"))
        golden = rm.GoldenSet.from_file(str(args.golden), subject=None)
        analysis = analyze(data["runs"], golden)
        lines = line_results(analysis)
        meta = data.get("meta") or {}
        md = Path(args.analyze).with_suffix(".md")
        md.write_text(render_markdown(analysis, lines, meta), encoding="utf-8")
        print(f"报告：{md.resolve()}")
        for code in ("R1", "R2", "R3", "S1"):
            item = lines[code]
            val = item["overall"]
            print(f"  {code}: {item['metric']} = {val if val is not None else '不判'} "
                  f"（线 ≥{item['threshold']}）→ {item['verdict']['status']}")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
