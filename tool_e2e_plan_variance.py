"""量一量**计划本身**有多不稳定（发现召回的上游）。

实测背景（2026-10-29 发现召回测量）：同一句式重复跑，入选名单两两 Jaccard
只有 0.04–0.23（Q3 测的跨句式 Jaccard 更低）。一开始以为是"搜索引擎抖动"，
后来发现更靠上游的一层：**每次生成的调研计划都不一样**——

- 实体锚定/发现阶段的**查询词**每次都换（"猫箱 AI 官网" / "猫箱 官网" / "猫箱 App 官网"）
- 模型提名的**竞品数量与名单**也在变（5 个 / 6 个 / 4 个）

计划一变 → 搜到的东西就变 → 候选池就变 → 入选名单当然不重合。
也就是说：拿"同一句式跑两次"去测稳定性，**测的是计划抽签，不是管线行为**。

本工装把这一层单独量出来：
- 计划级：查询词集合的 Jaccard、提名名单的 Jaccard、计划全文是否逐字相同
- 只跑计划，不搜索（省时：一次约 30–40 秒）

用法：
    uv run python tool_e2e_plan_variance.py --repeats 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.llm import get_llm_client  # noqa: E402
from src.workflow.research_plan import build_research_plan  # noqa: E402


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _plan_queries(plan: Any) -> List[str]:
    return [q for stage in (plan.search_plan or []) for q in (stage.queries or [])]


def _discovery_queries(plan: Any) -> List[str]:
    out: List[str] = []
    for stage in (plan.search_plan or []):
        if str(getattr(stage, "stage", "")) in ("entity_anchor", "competitor_discovery"):
            out.extend(stage.queries or [])
    return out


def _nominee_queries(plan: Any) -> List[str]:
    """发现阶段**实际会发**的竞品官网查询词（由代码按模板拼，见 search_stages.py）。"""
    out: List[str] = []
    for name in (plan.competitor_candidates or []):
        out.append(f"{name} 官网")
        out.append(f"{name} 官方网站")
    return out


def _pool_queries(plan: Any) -> List[str]:
    """决定候选池的**全部**输入：发现阶段的品类词 + 提名竞品的官网词。"""
    out: List[str] = []
    for stage in (plan.search_plan or []):
        if str(getattr(stage, "stage", "")) == "competitor_discovery":
            out.extend(stage.queries or [])
    out.extend(_nominee_queries(plan))
    return out


def run(subject: str, repeats: int) -> Dict[str, Any]:
    llm = get_llm_client()
    plans: List[Dict[str, Any]] = []
    for i in range(repeats):
        t0 = time.time()
        plan = asyncio.run(build_research_plan(llm, subject))
        plans.append({
            "index": i + 1,
            "seconds": round(time.time() - t0, 1),
            "source": plan.source,
            "error": plan.error,
            "candidates": list(plan.competitor_candidates or []),
            "queries": _plan_queries(plan),
            "discovery_queries": _discovery_queries(plan),
            "nominee_queries": _nominee_queries(plan),
            "pool_queries": _pool_queries(plan),
        })
    return {"subject": subject, "repeats": repeats, "plans": plans}


def analyze(result: Dict[str, Any]) -> Dict[str, Any]:
    plans = result["plans"]
    pairs: List[Dict[str, Any]] = []
    for i in range(len(plans)):
        for j in range(i + 1, len(plans)):
            a, b = plans[i], plans[j]
            same_nominees = a["candidates"] == b["candidates"]
            pairs.append({
                "pair": f"{i+1}-{j+1}",
                "query_jaccard": round(_jaccard(a["queries"], b["queries"]), 4),
                "discovery_query_jaccard": round(_jaccard(a["discovery_queries"], b["discovery_queries"]), 4),
                "candidate_jaccard": round(_jaccard(a["candidates"], b["candidates"]), 4),
                "pool_query_jaccard": round(_jaccard(a["pool_queries"], b["pool_queries"]), 4),
                "same_nominee_set": same_nominees,
                "identical_plan": a["queries"] == b["queries"] and a["candidates"] == b["candidates"],
            })
    n = len(plans)

    def _mean(key: str) -> Optional[float]:
        if not pairs:
            return None
        return round(sum(p[key] for p in pairs) / len(pairs), 4)

    same_set_pairs = [p for p in pairs if p["same_nominee_set"]]
    return {
        "subject": result["subject"],
        "runs": n,
        "sources": sorted({p["source"] for p in plans}),
        "errors": [p["error"] for p in plans if p["error"]],
        "mean_query_jaccard": _mean("query_jaccard"),
        "mean_discovery_query_jaccard": _mean("discovery_query_jaccard"),
        "mean_candidate_jaccard": _mean("candidate_jaccard"),
        "mean_pool_query_jaccard": _mean("pool_query_jaccard"),
        "same_nominee_set_pairs": len(same_set_pairs),
        "pool_query_jaccard_same_nominees": (
            round(sum(p["pool_query_jaccard"] for p in same_set_pairs) / len(same_set_pairs), 4)
            if same_set_pairs else None
        ),
        "identical_plan_pairs": sum(1 for p in pairs if p["identical_plan"]),
        "total_pairs": len(pairs),
        "pairs": pairs,
        "plans": plans,
    }


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "# 调研计划稳定性实跑报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 主体：{result['subject']}｜重复次数：{result['runs']}",
        "",
        "## 1. 每次生成的计划",
        "",
        "| # | 用时 | source | 提名竞品 | 查询词 |",
        "|---|---|---|---|---|",
    ]
    for p in result["plans"]:
        lines.append(
            f"| {p['index']} | {p['seconds']}s | {p['source']} | "
            f"{'、'.join(p['candidates'])} | {' / '.join(p['queries'])} |"
        )
    lines += [
        "",
        "## 2. 两两比较",
        "",
        "| 对 | 全量查询词 Jaccard | 发现/锚定阶段 Jaccard | 提名名单 Jaccard | 候选池输入 Jaccard | 同一份提名 | 计划逐字相同 |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in result["pairs"]:
        lines.append(
            f"| {p['pair']} | {p['query_jaccard']} | {p['discovery_query_jaccard']} | "
            f"{p['candidate_jaccard']} | {p['pool_query_jaccard']} | "
            f"{'是' if p['same_nominee_set'] else '否'} | {'是' if p['identical_plan'] else '否'} |"
        )
    lines += [
        "",
        "## 3. 结论",
        "",
        f"- 全量查询词平均 Jaccard：**{result['mean_query_jaccard']}**",
        f"- 发现/锚定阶段查询词平均 Jaccard：**{result['mean_discovery_query_jaccard']}**"
        "（这两个阶段由代码拼装，应当 = 1.0）",
        f"- 提名竞品平均 Jaccard：**{result['mean_candidate_jaccard']}**",
        f"- 候选池输入（发现阶段实际会发的查询）平均 Jaccard：**{result['mean_pool_query_jaccard']}**",
        f"- 提名名单相同的计划对：{result['same_nominee_set_pairs']}/{result['total_pairs']}"
        f"（这些对的候选池输入 Jaccard = {result['pool_query_jaccard_same_nominees']}）",
        f"- 逐字相同的计划对：{result['identical_plan_pairs']}/{result['total_pairs']}",
        f"- 计划来源：{', '.join(result['sources'])}"
        + (f"｜错误：{'; '.join(result['errors'])}" if result["errors"] else ""),
        "",
        "> 判读：**候选池输入**才是决定候选池的东西。",
        "> 发现/锚定阶段的查询词由代码拼装（应当恒等），",
        "> 剩下的差异全部来自**提名名单**——那是模型知识，不是措辞抽签。",
        "> 提名名单相同时，候选池输入应当完全相同。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subjects", nargs="*", default=["猫箱", "百度搭子"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--out-dir", default="output")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_results = []
    for subject in args.subjects:
        print(f"[plan] {subject} × {args.repeats} …", flush=True)
        result = analyze(run(subject, args.repeats))
        all_results.append(result)
        print(
            f"[plan]   查询词 Jaccard={result['mean_query_jaccard']} "
            f"发现阶段={result['mean_discovery_query_jaccard']} "
            f"提名={result['mean_candidate_jaccard']} "
            f"逐字相同 {result['identical_plan_pairs']}/{result['total_pairs']}",
            flush=True,
        )
    payload = {"generated_at": stamp, "results": all_results}
    json_path = out_dir / f"plan_variance_{stamp}.json"
    md_path = out_dir / f"plan_variance_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = "\n\n".join(render_markdown(r) for r in all_results)
    md_path.write_text(md, encoding="utf-8")
    print(f"\n报告：{md_path}")
    print(f"原始：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
