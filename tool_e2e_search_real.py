"""真实搜索引擎端到端：澄清与定界 + 搜索四段式 + 代码裁决的入选。

判定线写在 docs/decision-contract-search-e2e-2026-10.md（实跑前定稿），
本脚本只负责**取数与按线判定**，不改线。

用法：
    uv run python tool_e2e_search_real.py                    # 2 主体 × 2 句式
    uv run python tool_e2e_search_real.py --with-crawl       # 额外跑 1 组整站抓取
    uv run python tool_e2e_search_real.py --subject "猫箱" --styles task
    uv run python tool_e2e_search_real.py --judge-review output/search_e2e_<ts>.review.json

产出：output/search_e2e_<ts>.md（人读）+ output/search_e2e_<ts>.json（原始）
      output/search_e2e_<ts>.review.json（**人工核对表**：Q2 的判定依据）

关于 Q2：判定线已改成"人工核对入选项里真竞品官网的占比"。
代理判定（用被测代码自己的站点类型表去判"是不是官网"）是**自证**，
上一轮实测里它给基线打了 PASS，人工核对却是 19%。
所以工装不再自己给 Q2 下结论：它只负责**产出核对表**，
人工填完 `--judge-review` 才有结论。没填就是"待人工核对"，不写 PASS 也不写 FAIL。
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
from src.workflow.site_types import classify_site, site_type_label  # noqa: E402

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


def _is_product_site(url: str, title: str = "") -> bool:
    """Q4 代理判据的第二层：**必须是产品官网**。

    上一轮的代理判据只有"名字/摘要里有没有赛道词"，
    于是"15个AI虚拟女友…社交软件，专属恋爱聊天和情感陪伴"（一篇文章）也算同赛道。
    这里补上站点类型这一层：文章页、媒体站、应用商店、收录目录一律不算竞品官网。
    """
    return classify_site(url, title) == "product_site"


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
            "site_type": classify_site(c.url, c.name),
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
            # 站点类型与赛道词是 Q4 代理判据的两层依据（产品官网 + 同赛道）
            "site_type": classify_site(item["url"], item["name"]),
            "in_track": _is_track(
                item["name"],
                next(
                    (c.description for c in discovery.candidates
                     if c.url == item["url"]), ""
                ),
            ),
        }
        for item in discovery.selected
    ]
    record["notes"] = discovery.notes
    record["counter_evidence"] = [
        {"title": v["title"][:80], "url": v["url"], "competitor": v["competitor"]}
        for v in counter[:10]
    ]
    # 搜索后端是否整轮失败过：失败过的这一轮**数据无效**，
    # 不能当成"没找到竞品"来判线（实测 2026-10-29：SearXNG 引擎集体超时会返回 0 条）。
    record["search_failed"] = bool(discovery.search_failed)
    record["search_failures"] = discovery.search_failures

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


def _group_is_invalid(rec: Dict[str, Any]) -> bool:
    """这一组的数据是否无效（不能拿来判线）。

    两种情况：
    - 搜索后端整轮失败（引擎集体超时/限流）→ 候选池空是**后端抖动的后果**，
      不是"这个赛道没有竞品"；
    - 计划层抛错（主体都没认出来）→ 这一组根本没跑到有意义的阶段。
    """
    if rec.get("search_failed"):
        return True
    ent = rec.get("entity") or {}
    if ent.get("source") == "error" or ent.get("error"):
        return True
    return False


async def _run_group_with_retry(llm, search_tool, subject, style, query, attempts=3,
                                with_crawl=False):
    """跑一组；数据无效就重试。

    搜索引擎的抖动是**暂时的**（实测：SearXNG 引擎集体超时会让整轮返回 0 条）。
    直接把这种 0 写进结论，等于把后端故障记成"没找到竞品"——
    上一轮实测里就有整组是这样归零的。
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            rec = await run_group(llm, search_tool, subject, style, query,
                                  with_crawl=with_crawl and attempt == 1)
        except Exception as exc:  # noqa: BLE001
            rec = {
                "subject": subject, "style": style, "query": query,
                "entity": {"name": "", "type": "", "what_it_is": "", "confidence": 0.0,
                           "alternatives": [], "source": "error",
                           "error": f"{type(exc).__name__}: {exc}"},
                "criteria": {"layers": [], "min_count": 0, "max_count": 0, "admission_rule": ""},
                "angles": [], "stages": [], "candidates": [], "selected": [],
                "notes": [], "counter_evidence": [], "elapsed_s": 0,
                "search_failed": False, "search_failures": [],
            }
        rec["attempts"] = attempt
        if not _group_is_invalid(rec) or attempt == attempts:
            if attempt > 1:
                rec["notes"].append(f"（第 {attempt} 次尝试才拿到可用数据）")
            return rec
        last = rec
        print(
            f"[e2e]   {subject}/{style} 本轮数据无效"
            f"（{rec.get('entity', {}).get('error') or '搜索后端失败'}），重试…",
            flush=True,
        )
        await asyncio.sleep(5)
    return last or rec


def _judge(records: List[Dict[str, Any]],
           review: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """按预先写死的判定线给结论。阈值来自决策契约文档，不在这里改。

    ``review`` 是人工核对结果（``--judge-review`` 读进来的 JSON）：
    ``{"verdicts": [{"url": "...", "verdict": true/false, "note": "..."}]}``。
    没给就 Q2 记"待人工核对"——不写 PASS 也不写 FAIL。
    """
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

    # Q2 发现质量——**人工核对**才是判定依据（代理判定是自证的）
    q2 = []
    manual = _manual_verdicts(review)
    for rec in records:
        selected = rec["selected"]
        pool = rec["candidates"]
        item = {
            "subject": rec["subject"],
            "style": rec["style"],
            "pool": len(pool),
            "selected": len(selected),
        }
        checked = _manual_check(manual, selected)
        if checked is None:
            item.update({
                "manual_checked": 0,
                "manual_real": 0,
                "manual_ratio": None,
                "status": "待人工核对",
                "pass": None,
            })
        else:
            checked_count, real_count = checked
            ratio = real_count / checked_count if checked_count else 0.0
            item.update({
                "manual_checked": checked_count,
                "manual_real": real_count,
                "manual_ratio": round(ratio, 3),
                "status": "已人工核对",
                # 判定线：人工核对入选项里真竞品官网的占比 ≥ 80%
                "pass": checked_count > 0 and ratio >= 0.8,
            })
        q2.append(item)
    q2_states = [item["status"] for item in q2]
    if all(state == "已人工核对" for state in q2_states):
        verdict["Q2"] = {
            "question": "人工核对：入选名单里真竞品官网占比 ≥ 80%",
            "detail": q2,
            "pass": all(item["pass"] for item in q2),
            "status": "已人工核对",
        }
    else:
        verdict["Q2"] = {
            "question": "人工核对：入选名单里真竞品官网占比 ≥ 80%",
            "detail": q2,
            "pass": None,
            "status": "待人工核对（用 --judge-review 填完核对表再判）",
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
        # 两边都空 → Jaccard 算出来是 1.0，那是**假的 PASS**：
        # 什么也没选出来，"两种句式一致"是真的吗？不能这么判。
        both_empty = not a and not b
        q3.append({
            "subject": subject,
            "task": a,
            "search": b,
            "jaccard": round(score, 3),
            "empty": both_empty,
            "pass": (not both_empty) and score >= 0.5,
        })
    verdict["Q3"] = {
        "question": "任务句式 vs 搜索词句式：入选名单 Jaccard ≥ 0.5（两边都空不算通过）",
        "detail": q3,
        "pass": all(item["pass"] for item in q3) if q3 else False,
    }

    # Q4 入选正确性：**产品官网** + 同赛道，两层都要满足
    q4 = []
    for rec in records:
        selected = rec["selected"]
        in_track = sum(
            1 for s in selected
            if s.get("in_track") and s.get("site_type") == "product_site"
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
        "question": "入选名单里 ≥ 75% 是「产品官网且同赛道」（代理判定加站点类型层）",
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


def _manual_verdicts(review: Dict[str, Any] | None) -> Dict[str, bool]:
    """把人工核对表压成 {url: 是否真竞品官网}。

    只认 ``verdict`` 明确为 true/false 的条目——没填的不算"通过"，
    也不当成"不通过"，而是"没核对"。
    """
    out: Dict[str, bool] = {}
    if not review:
        return out
    for item in review.get("verdicts") or []:
        url = (item or {}).get("url")
        flag = (item or {}).get("verdict")
        if not url or not isinstance(flag, bool):
            continue
        out[str(url).strip()] = flag
    return out


def _manual_check(manual: Dict[str, bool], selected: List[Dict[str, Any]]):
    """返回 (核对条数, 其中真竞品官网的条数)；没核对过任何一条就返回 None。"""
    checked = 0
    real = 0
    for item in selected:
        url = str(item.get("url") or "").strip()
        if url not in manual:
            continue
        checked += 1
        if manual[url]:
            real += 1
    if not checked:
        return None
    return checked, real


def _review_sheet(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """生成人工核对表（Q2 的判定依据）。

    每条入选项给一行：名称、URL、站点类型（代码的判断，供人参考）、
    得分与理由、以及一个待填的 ``verdict`` 字段。
    人只需要回答一个问题：**这个 URL 是不是该主体的真竞品官网**。
    """
    entries = []
    for rec in records:
        for item in rec["selected"]:
            entries.append({
                "subject": rec["subject"],
                "style": rec["style"],
                "name": item.get("name") or "",
                "url": item.get("url") or "",
                "site_type": item.get("site_type") or "",
                "site_type_label": site_type_label(item.get("site_type")),
                "score": item.get("score"),
                "reasons": item.get("reasons") or [],
                # null = 待核对；true = 是真竞品官网；false = 不是
                "verdict": None,
                "note": "",
            })
    return {
        "_说明": (
            "逐条判断：这个 URL 是不是该主体的真竞品官网（产品自己的站点）。"
            "把 verdict 改成 true / false，note 可写理由。"
            "填完运行：uv run python tool_e2e_search_real.py "
            "--judge-review <本文件路径>"
        ),
        "verdicts": entries,
        # 原始记录一起带上：--judge-review 要按组算占比，脱离网络也能复算
        "records": records,
    }


def _render_review_sheet(review: Dict[str, Any]) -> str:
    lines = ["# 人工核对表（Q2 的判定依据）", ""]
    lines.append(review.get("_说明", ""))
    lines.append("")
    lines.append("| # | 主体 | 句式 | 名称 | URL | 站点类型（代码判断） | 得分 | 核对 | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for index, item in enumerate(review.get("verdicts") or [], start=1):
        mark = {True: "是", False: "否", None: "待核对"}[item.get("verdict")]
        lines.append(
            f"| {index} | {item.get('subject', '')} | {item.get('style', '')} | "
            f"{item.get('name', '')} | {item.get('url', '')} | "
            f"{item.get('site_type_label', '')} | {item.get('score', '')} | "
            f"{mark} | {item.get('note', '')} |"
        )
    return "\n".join(lines) + "\n"


def _judge_review_file(path: Path) -> int:
    """只读核对表、按线判 Q2，不联网。"""
    review = json.loads(path.read_text(encoding="utf-8"))
    records = review.get("records") or []
    if not records:
        # 核对表里只存了逐条判断；Q2 还要按组算占比，所以原始记录也得带上
        print("核对表里没有 records，无法按组统计。请用实跑产出的 .json + 核对表合并后的文件。",
              file=sys.stderr)
        return 2
    verdict = _judge(records, review)
    q2 = verdict["Q2"]
    print(f"Q2 {q2['status']}")
    for item in q2["detail"]:
        state = {True: "PASS", False: "FAIL"}.get(item["pass"], "不判（该组没有可核对的入选项）")
        print(
            f"  {item['subject']}/{item['style']}: 核对 {item['manual_checked']} 条，"
            f"真竞品官网 {item['manual_real']} 条，占比 {item['manual_ratio']} → {state}"
        )
    judged = [item for item in q2["detail"] if item["pass"] is not None]
    failed = [item for item in judged if not item["pass"]]
    if q2["pass"] is True:
        print("结论：PASS")
        return 0
    if q2["pass"] is False:
        print("结论：FAIL")
        return 1
    # 有组没判（一条入选都没有）：整条线不能下结论，但已判的组里若有 FAIL，要说出来
    if failed:
        print(f"结论：待人工核对（已判 {len(judged)} 组，其中 {len(failed)} 组 FAIL）")
        return 1
    print(f"结论：待人工核对（已判 {len(judged)} 组，暂无 FAIL；缺 {len(q2['detail']) - len(judged)} 组）")
    return 2


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
        state = item.get("pass")
        mark = "✅ PASS" if state is True else ("❌ FAIL" if state is False else "⏳ 待人工核对")
        detail = "；".join(
            f"{d.get('subject', '')}/{d.get('style', '')}"
            f"{d.get('pass')}" for d in item["detail"]
        )
        lines.append(f"| {key} | {item['question']} | {mark} | {detail} |")
    if verdict.get("Q2", {}).get("status", "").startswith("待"):
        lines.append(
            "\n> Q2 的判定依据是**人工核对**：代理判定（用被测代码的站点类型表去判"
            "「是不是官网」）是自证的。请填 `output/search_e2e_<ts>.review.json` 里的 "
            "`verdicts`，再跑 `--judge-review`。"
        )
    return "\n".join(lines) + "\n"


_t0 = time.time()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", action="append", help="只跑指定主体（可多次）")
    parser.add_argument("--styles", default="task,search", help="句式：task,search")
    parser.add_argument("--with-crawl", action="store_true", help="额外跑整站抓取")
    parser.add_argument(
        "--judge-review",
        metavar="PATH",
        help="只按已填的人工核对表判 Q2（不联网、不重跑）",
    )
    args = parser.parse_args()

    if args.judge_review:
        return _judge_review_file(Path(args.judge_review))

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
            rec = asyncio.run(_run_group_with_retry(
                llm, search_tool, subject, style, query,
                with_crawl=args.with_crawl and style == "task" and subject == subjects[0],
            ))
            records.append(rec)
            print(
                f"[e2e]   主体={rec['entity']['name']!r} 候选={len(rec['candidates'])} "
                f"入选={len(rec['selected'])} 反证={len(rec['counter_evidence'])} "
                f"{rec['elapsed_s']}s"
                + ("  ⚠ 本轮数据无效" if _group_is_invalid(rec) else ""),
                flush=True,
            )

    verdict = _judge(records)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = REPO / "output"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"search_e2e_{ts}.md"
    json_path = out_dir / f"search_e2e_{ts}.json"
    review_path = out_dir / f"search_e2e_{ts}.review.json"
    review_md_path = out_dir / f"search_e2e_{ts}.review.md"
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
    # 人工核对表：Q2 的判定依据。人填完 verdict 字段后再跑 --judge-review。
    review = _review_sheet(records)
    review_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    review_md_path.write_text(_render_review_sheet(review), encoding="utf-8")

    print(f"\n报告：{md_path}")
    print(f"原始：{json_path}")
    print(f"人工核对表：{review_path}（填 verdict 后跑 --judge-review）")
    for key in sorted(verdict):
        state = verdict[key].get("pass")
        mark = "PASS" if state is True else ("FAIL" if state is False else "待人工核对")
        print(f"  {key}: {mark} — {verdict[key]['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
