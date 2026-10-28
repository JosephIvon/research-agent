"""真实搜索引擎端到端：澄清与定界 + 搜索四段式 + 代码裁决的入选。

判定线写在 docs/decision-contract-search-e2e-2026-10.md（实跑前定稿），
本脚本只负责**取数与按线判定**，不改线。

用法：
    uv run python tool_e2e_search_real.py                    # 2 主体 × 2 句式
    uv run python tool_e2e_search_real.py --with-crawl       # 额外跑 1 组整站抓取
    uv run python tool_e2e_search_real.py --subject "猫箱" --styles task

产出：output/search_e2e_<ts>.md（人读）+ output/search_e2e_<ts>.json（原始）
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.llm import get_llm_client  # noqa: E402
from src.tools.search_tool import get_search_tool  # noqa: E402
from src.workflow import research_plan as rp  # noqa: E402
from src.workflow import search_stages as ss  # noqa: E402

# 主体 → (任务句式, 搜索词句式)。任务句式是用户原话里的写法。
SUBJECTS = {
    "百度搭子": (
        "找一下百度搭子的全部竞品信息",
        "百度搭子 竞品",
    ),
    "猫箱": (
        "帮我看看猫箱这个产品的全部竞品信息",
        "猫箱 竞品",
    ),
}

# 赛道判定（Q4）：人工认定的"同赛道"关键词——只用于人工核对，不参与管线逻辑
TRACK_HINTS = (
    "ai", "智能体", "搭子", "陪伴", "角色", "对话", "chat", "companion",
    "角色扮演", "社交", "二次元",
)


def _is_track(name: str, description: str) -> bool:
    text = (name + " " + description).lower()
    return any(hint in text for hint in TRACK_HINTS)


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


async def run_group(
    llm: Any,
    search_tool: Any,
    subject: str,
    style: str,
    query: str,
    with_crawl: bool = False,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "subject": subject,
        "style": style,
        "query": query,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # ---- 1. 澄清与定界（真实模型）----------------------------------
    plan = await rp.build_research_plan(llm, query)
    entity = {
        "name": plan.entity_name,
        "type": plan.entity_type,
        "what_it_is": plan.what_it_is,
        "confidence": plan.resolved_confidence,
        "alternatives": plan.disambiguation_candidates,
        "source": plan.source,
        "error": plan.error,
    }
    record["entity"] = entity
    record["criteria"] = {
        "layers": plan.layers,
        "min_count": plan.min_count,
        "max_count": plan.max_count,
        "admission_rule": plan.criteria.get("admission_rule", ""),
    }
    record["angles"] = [
        {"angle": a.angle, "label": rp.ANGLE_LABELS.get(a.angle, a.angle),
         "priority": a.priority, "weight": round(a.weight, 3),
         "hints": a.page_hints}
        for a in plan.angles
    ]

    # ---- 2. 搜索四段式（真实搜索引擎）------------------------------
    runner = ss.SearchStageRunner(search_tool)
    stage_log: List[Dict[str, Any]] = []

    def _queries(name: str) -> List[str]:
        return plan.queries_for(name)

    anchor_pages = runner.run_entity_anchor(plan)
    stage_log.append({
        "stage": "entity_anchor",
        "queries": _queries("entity_anchor"),
        "unique_urls": len({p["url"] for p in anchor_pages}),
        "kept": len(anchor_pages),
        "titles": [p["title"][:60] for p in anchor_pages],
    })

    discovery = runner.run_competitor_discovery(plan)
    stage_log.append({
        "stage": "competitor_discovery",
        "queries": _queries("competitor_discovery"),
        "candidates": len(discovery.candidates),
        "selected": len(discovery.selected),
    })

    verification = runner.run_verification(
        plan, [item.get("name") or "" for item in discovery.selected]
    )
    counter = [v for v in verification if v.get("stage") == "counter_evidence"]
    stage_log.append({
        "stage": "per_competitor_verify",
        "queries": [],
        "results": len([v for v in verification if v.get("stage") == "per_competitor_verify"]),
    })
    stage_log.append({
        "stage": "counter_evidence",
        "queries": [],
        "results": len(counter),
        "titles": [v["title"][:60] for v in counter[:8]],
    })
    record["stages"] = stage_log

    record["candidates"] = [
        {
            "name": c.name,
            "url": c.url,
            "aggregator": c.is_aggregator,
            "in_track": _is_track(c.name, c.description),
        }
        for c in discovery.candidates
    ]
    record["selected"] = [
        {
            "name": item["name"],
            "url": item["url"],
            "score": item["score"],
            "reasons": item["reasons"],
        }
        for item in discovery.selected
    ]
    record["notes"] = discovery.notes
    record["counter_evidence"] = [
        {"title": v["title"][:80], "url": v["url"], "competitor": v["competitor"]}
        for v in counter[:10]
    ]

    # ---- 3.（可选）整站抓取，确认端到端能落地 ----------------------
    if with_crawl:
        from src.workflow import mvp_workflow

        workflow = mvp_workflow.MVPWorkflow()
        workflow._search_factory = lambda: search_tool
        state = await workflow.run(
            query, enable_search=True, task_id=f"search-e2e-{subject}-{style}",
        )
        matrix = state.get("competitor_matrix") or {}
        record["crawl"] = {
            "status": state.get("status"),
            "target_urls": state.get("target_urls"),
            "matrix_rows": [
                {
                    "platform": row.get("platform"),
                    "positioning": row.get("positioning"),
                    "content_complete": row.get("content_complete"),
                }
                for row in (matrix.get("rows") or [])
            ],
            "report_chars": len(state.get("report_final") or ""),
        }
    record["elapsed_s"] = round(time.time() - _t0, 1)
    return record


def _judge(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按预先写死的判定线给结论。阈值来自决策契约文档，不在这里改。"""
    verdict: Dict[str, Any] = {}

    # Q1 主体消歧
    q1 = []
    for rec in records:
        ent = rec["entity"]
        q1.append({
            "subject": rec["subject"],
            "style": rec["style"],
            "name": ent["name"],
            "confidence": ent["confidence"],
            "source": ent["source"],
            "pass": bool(ent["name"]) and float(ent["confidence"] or 0) >= 0.5,
        })
    verdict["Q1"] = {
        "question": "计划层能否把主体认成具体实体（置信度 ≥ 0.5）",
        "detail": q1,
        "pass": all(item["pass"] for item in q1),
    }

    # Q2 发现质量
    q2 = []
    for rec in records:
        selected = rec["selected"]
        pool = rec["candidates"]
        official_ratio = (
            sum(1 for s in selected if not ss.looks_like_aggregator(s["url"])) / len(selected)
            if selected else 0.0
        )
        q2.append({
            "subject": rec["subject"],
            "style": rec["style"],
            "pool": len(pool),
            "selected": len(selected),
            "official_ratio": round(official_ratio, 3),
            "pass": len(pool) >= 5 and official_ratio >= 0.8,
        })
    verdict["Q2"] = {
        "question": "候选池 ≥ 5 家 且 入选中非媒体域占比 ≥ 80%",
        "detail": q2,
        "pass": all(item["pass"] for item in q2),
    }

    # Q3 句式不敏感（同一主体两句式的入选名单 Jaccard ≥ 0.5）
    q3 = []
    by_subject: Dict[str, Dict[str, List[str]]] = {}
    for rec in records:
        by_subject.setdefault(rec["subject"], {})[rec["style"]] = [
            s["name"] for s in rec["selected"]
        ]
    for subject, styles in by_subject.items():
        if len(styles) < 2:
            continue
        a = styles.get("task", [])
        b = styles.get("search", [])
        score = _jaccard(a, b)
        q3.append({
            "subject": subject,
            "task": a,
            "search": b,
            "jaccard": round(score, 3),
            "pass": score >= 0.5,
        })
    verdict["Q3"] = {
        "question": "任务句式 vs 搜索词句式：入选名单 Jaccard ≥ 0.5",
        "detail": q3,
        "pass": all(item["pass"] for item in q3) if q3 else False,
    }

    # Q4 入选正确性（同赛道 ≥ 3/4）
    q4 = []
    for rec in records:
        selected = rec["selected"]
        in_track = sum(
            1 for s in selected
            if _is_track(s["name"], next(
                (c.get("name", "") for c in rec["candidates"] if c["url"] == s["url"]), ""
            ))
        )
        ratio = in_track / len(selected) if selected else 0.0
        q4.append({
            "subject": rec["subject"],
            "style": rec["style"],
            "selected": len(selected),
            "in_track": in_track,
            "ratio": round(ratio, 3),
            "pass": ratio >= 0.75,
        })
    verdict["Q4"] = {
        "question": "入选名单里 ≥ 75% 属于主体所在赛道（代理判定，AI/陪伴/角色赛道）",
        "detail": q4,
        "pass": all(item["pass"] for item in q4),
    }

    # Q5 反证段有产出
    q5 = [
        {
            "subject": rec["subject"],
            "style": rec["style"],
            "counter_evidence": len(rec["counter_evidence"]),
            "pass": len(rec["counter_evidence"]) >= 1,
        }
        for rec in records
    ]
    verdict["Q5"] = {
        "question": "counter_evidence 阶段至少 1 条结果",
        "detail": q5,
        "pass": all(item["pass"] for item in q5),
    }
    return verdict


def _render(records: List[Dict[str, Any]], verdict: Dict[str, Any], meta: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# 真实搜索引擎端到端：实跑记录\n")
    lines.append(f"- 时间：{meta['ts']}")
    lines.append(f"- 搜索后端：{meta['provider']}（{meta['searxng_url']}）")
    lines.append(f"- 计划模型：{meta['provider_name']} / {meta['model']}")
    lines.append(f"- 判定线来源：docs/decision-contract-search-e2e-2026-10.md（实跑前定稿）\n")

    for rec in records:
        ent = rec["entity"]
        lines.append(f"## {rec['subject']} · {rec['style']} 句式\n")
        lines.append(f"- 查询原话：{rec['query']}")
        lines.append(
            f"- 计划：主体 **{ent['name']}**（{ent['type']}），置信度 {ent['confidence']}，"
            f"来源 {ent['source']}"
        )
        if ent["what_it_is"]:
            lines.append(f"- 主体释义：{ent['what_it_is']}")
        if ent["alternatives"]:
            lines.append(f"- 备选：{'、'.join(ent['alternatives'][:4])}")
        if ent["error"]:
            lines.append(f"- 计划降级原因：{ent['error']}")
        lines.append(
            f"- 口径：层级 {'/'.join(rec['criteria']['layers'])}，"
            f"数量 {rec['criteria']['min_count']}-{rec['criteria']['max_count']}，"
            f"标准 {rec['criteria']['admission_rule'][:60]}"
        )
        lines.append(
            "- 角度权重：" + "、".join(
                f"{a['label']} {a['weight']}" for a in rec["angles"]
            )
        )
        lines.append("\n**四段式**\n")
        lines.append("| 阶段 | 查询词 | 产出 |")
        lines.append("|---|---|---|")
        for st in rec["stages"]:
            queries = "；".join(st.get("queries") or []) or "（由竞品名 + 意图词拼出）"
            out = []
            for key in ("unique_urls", "kept", "candidates", "selected", "results"):
                if key in st:
                    out.append(f"{key}={st[key]}")
            lines.append(f"| {st['stage']} | {queries[:110]} | {', '.join(out)} |")
        lines.append("\n**候选池**\n")
        for c in rec["candidates"]:
            flag = "媒体/社区" if c["aggregator"] else "官网候选"
            lines.append(f"- {c['name']} — {c['url']} [{flag}]")
        lines.append("\n**入选（代码裁决）**\n")
        for s in rec["selected"]:
            lines.append(
                f"- {s['name']} — {s['url']}｜分 {s['score']}｜{'；'.join(s['reasons'])}"
            )
        if rec["notes"]:
            lines.append("\n**过程说明**\n")
            for note in rec["notes"]:
                lines.append(f"- {note}")
        if rec["counter_evidence"]:
            lines.append("\n**反证段结果**\n")
            for c in rec["counter_evidence"]:
                lines.append(f"- {c['competitor']}：{c['title']} — {c['url']}")
        if rec.get("crawl"):
            cr = rec["crawl"]
            lines.append("\n**整站抓取**\n")
            lines.append(f"- 状态：{cr['status']}")
            lines.append(f"- 抓取目标：{cr['target_urls']}")
            for row in cr["matrix_rows"]:
                lines.append(
                    f"- 矩阵行：{row['platform']}｜定位 {str(row['positioning'])[:40]}｜"
                    f"内容完整 {row['content_complete']}"
                )
        lines.append(f"\n耗时 {rec['elapsed_s']}s\n")

    lines.append("\n## 判定\n")
    lines.append("| 编号 | 问题 | 结论 | 明细 |")
    lines.append("|---|---|---|---|")
    for key in sorted(verdict):
        item = verdict[key]
        mark = "✅ PASS" if item["pass"] else "❌ FAIL"
        detail = "；".join(
            f"{d.get('subject', '')}/{d.get('style', '')}"
            f"{d.get('pass')}" for d in item["detail"]
        )
        lines.append(f"| {key} | {item['question']} | {mark} | {detail} |")
    return "\n".join(lines) + "\n"


_t0 = time.time()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", action="append", help="只跑指定主体（可多次）")
    parser.add_argument("--styles", default="task,search", help="句式：task,search")
    parser.add_argument("--with-crawl", action="store_true", help="额外跑整站抓取")
    args = parser.parse_args()

    subjects = args.subject or list(SUBJECTS)
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]

    llm = get_llm_client()
    search_tool = get_search_tool()

    records: List[Dict[str, Any]] = []
    for subject in subjects:
        task_q, search_q = SUBJECTS.get(subject, (subject, subject))
        for style in styles:
            query = task_q if style == "task" else search_q
            print(f"[e2e] {subject} / {style}: {query}", flush=True)
            try:
                rec = asyncio.run(run_group(
                    llm, search_tool, subject, style, query,
                    with_crawl=args.with_crawl and style == "task" and subject == subjects[0],
                ))
            except Exception as exc:  # noqa: BLE001
                rec = {
                    "subject": subject, "style": style, "query": query,
                    "entity": {"name": "", "type": "", "what_it_is": "", "confidence": 0.0,
                               "alternatives": [], "source": "error", "error": f"{type(exc).__name__}: {exc}"},
                    "criteria": {"layers": [], "min_count": 0, "max_count": 0, "admission_rule": ""},
                    "angles": [], "stages": [], "candidates": [], "selected": [],
                    "notes": [], "counter_evidence": [], "elapsed_s": 0,
                }
            records.append(rec)
            print(
                f"[e2e]   主体={rec['entity']['name']!r} 候选={len(rec['candidates'])} "
                f"入选={len(rec['selected'])} 反证={len(rec['counter_evidence'])} "
                f"{rec['elapsed_s']}s",
                flush=True,
            )

    verdict = _judge(records)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = REPO / "output"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"search_e2e_{ts}.md"
    json_path = out_dir / f"search_e2e_{ts}.json"
    meta = {
        "ts": ts,
        "provider": search_tool.default_provider,
        "searxng_url": search_tool.searxng_url,
        "provider_name": getattr(llm, "provider_name", "?"),
        "model": getattr(llm, "default_model", "?"),
    }
    md_path.write_text(_render(records, verdict, meta), encoding="utf-8")
    json_path.write_text(
        json.dumps({"meta": meta, "records": records, "verdict": verdict},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n报告：{md_path}")
    print(f"原始：{json_path}")
    for key in sorted(verdict):
        print(f"  {key}: {'PASS' if verdict[key]['pass'] else 'FAIL'} — {verdict[key]['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
