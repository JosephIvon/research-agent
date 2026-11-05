"""研究计划注入与 ReAct facts 还原。

从 mvp_workflow 抽出的纯叶子职责：只依赖入参与 research_plan 的常量，
不读写工作流的共享 state，也不依赖工作流实例。
"""
from typing import Any, Dict, List, Optional

from src.workflow.research_plan import ANGLE_LABELS


def plan_aware_query(user_query: str, plan: Optional[Any]) -> str:
    """把研究计划写进任务描述：模型看得见"研究对象与重点角度"。

    只在计划**确实带来了新信息**时才追加——默认计划（无模型消歧结果）
    追加进去只是噪声，反而稀释了用户的原始诉求。
    """
    if plan is None or plan.source != "model":
        return user_query
    lines = [f"【已确定的研究计划】", f"研究主体：{plan.entity_name}（{plan.what_it_is or '未说明'}）"]
    criteria = plan.criteria or {}
    layers = "、".join(criteria.get("layers") or [])
    # 入选标准给**可执行**的那几条：模型按没被执行的条款去取舍，
    # 就会与代码裁决出两套结果（声明与执行脱节）。
    rule = plan.admission_rule or {}
    executable = [str(c) for c in (rule.get("executable") or []) if str(c).strip()]
    rule_text = "；".join(executable) if executable else (criteria.get("admission_rule") or "默认")
    lines.append(
        f"竞品口径：{layers}；数量 {criteria.get('min_count')}-{criteria.get('max_count')} 家；"
        f"入选标准（按此执行）：{rule_text}"
    )
    weights = "、".join(
        f"{ANGLE_LABELS.get(a.angle, a.angle)}{round(a.weight * 100)}%"
        for a in sorted(plan.angles, key=lambda x: -x.weight)
    )
    lines.append(f"重点角度（权重）：{weights}")
    for stage in plan.search_plan:
        if stage.queries:
            lines.append(f"{stage.stage} 查询：{'；'.join(stage.queries[:4])}")
    lines.append("请按此边界取证；找不到证据就如实写明，不要用行业常识补。")
    return user_query + "\n\n" + "\n".join(lines)


def competitors_from_facts(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 ReAct 抓到的 facts 按来源站点还原成"竞品"条目。

    ReAct 的产出是 {url: [facts]}，没有"一家竞品一条"的边界；
    矩阵要按竞品成行，就得在这里把同一站点的证据归到一行。
    """
    by_url: Dict[str, List[Dict[str, Any]]] = {}
    for item in facts or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url", "") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, []).append(item)
    competitors = []
    for url, items in by_url.items():
        name = next(
            (str(i.get("value", "")).strip()
             for i in items
             if str(i.get("key", "")).strip().lower() == "name" and str(i.get("value", "")).strip()),
            url.split("//")[-1].split("/")[0],
        )
        competitors.append({
            "name": name,
            "url": url,
            "status": "success",
            "extracted_data": items,
        })
    return competitors
