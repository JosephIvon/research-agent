"""矩阵构建与抓后准入闸门（含留池补位）。

从 ``mvp_workflow`` 抽出的叶子职责，原方法保留薄委托：
- ``derive_and_build_matrix``：推导可借鉴点并重建竞品矩阵（失败退回规则表）。
- ``apply_admission_gate``：抓后淘汰 + 未采信清单 + 从留池补位。
- ``backfill_from_pool``：补抓候选（受上限约束），抓取入口由调用方注入。
- ``plan_subject``：从研究计划取主体/口径/角度摘要。

依赖注入而非反向导入：``emit`` 与 ``fetch_all`` 由调用方传入，
``max_backfill`` 由调用方传入（常量仍以 ``mvp_workflow`` 为唯一来源）。
"""
from typing import Any, Dict, List, Optional

from src.tools.quality_scorer import QualityScorer
from src.workflow import borrowable_derivation
from src.workflow.admission_rules import evaluate_post_crawl, parse_admission_rule
from src.workflow.competitor_matrix import build_matrix
from src.workflow.research_plan import MIN_ADMISSION_SCORE, _host_key


async def derive_and_build_matrix(llm_client: Any, state: Dict[str, Any], user_query: str):
    """推导「可借鉴点」并重建竞品矩阵。

    推导走**域无关**路径：模型给出「锚点 key + 逐字片段 + 借鉴动作」，
    代码机械校验（锚点存在、片段逐字命中、动作里的数字有据），
    不需要为每个行业维护一张规则表。推导失败不拖垮链路——
    矩阵退回规则表兜底，再不行就留「待产品判断」。

    针对性来自 ``self_context``（我方是谁、要解决什么）：
    没有它，模型只能给通用话——那是上下文缺失，不是规则缺失。
    """
    derived_map: Dict[str, List[Dict[str, Any]]] = {}
    try:
        result = await borrowable_derivation.derive(
            llm_client,
            user_query,
            str(state.get("self_context") or ""),
            state.get("competitors") or [],
        )
        state["borrowable_derivation"] = result.to_dict()
        if result.ok:
            derived_map = {
                platform: [item.to_dict() for item in items]
                for platform, items in result.items_by_platform.items()
            }
    except Exception as exc:  # noqa: BLE001 —— 推导是增强项，不能让它炸掉整条链路
        state["borrowable_derivation"] = {
            "items_by_platform": {}, "rejected": [], "error": f"{type(exc).__name__}: {exc}",
        }
    matrix_obj = build_matrix(
        user_query,
        competitors=state.get("competitors") or [],
        derived_by_platform=derived_map,
        subject=plan_subject(
            state.get("research_plan"), state.get("rejected_competitors")
        ),
    )
    state["competitor_matrix"] = matrix_obj.to_dict()
    return matrix_obj


def plan_subject(
    plan_data: Optional[Dict[str, Any]],
    rejected: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """从研究计划里取"主体/口径/角度"摘要，供矩阵章节展示。"""
    if not plan_data:
        return {}
    entity = plan_data.get("entity") or {}
    criteria = plan_data.get("competitor_criteria") or {}
    angles = plan_data.get("angles") or []
    return {
        "entity_name": entity.get("name", ""),
        "entity_type": entity.get("entity_type", ""),
        "what_it_is": entity.get("what_it_is", ""),
        "resolved_confidence": entity.get("resolved_confidence", 0),
        "disambiguation_candidates": entity.get("disambiguation_candidates") or [],
        "layers": criteria.get("layers") or [],
        "min_count": criteria.get("min_count"),
        "max_count": criteria.get("max_count"),
        "angles": angles,
        # 实际执行的准入条件 + 未采信清单：矩阵章节要能说清"按什么口径选的"
        "admission_rule": plan_data.get("admission_rule") or {},
        "rejected_competitors": list(rejected or []),
    }


async def apply_admission_gate(
    state: Dict[str, Any],
    *,
    emit,
    backfill,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None,
    custom_selectors: Optional[Dict[str, List[str]]] = None,
) -> None:
    """抓后准入闸门 + 从留池补位。

    三件事，按顺序：

    1. **淘汰**：按口径里"至少 N 个维度有数据"这一条（抓后才判得了），
       把数据不足的竞品从矩阵里剔出去。它们**进"未采信"清单**——
       既不污染结论，也不隐藏"我们试过它"这件事。
    2. **补位**：数量不够时，从候选留池（``discovery.scored``）里按分
       取下一批候选抓一轮。补位有上限（``max_backfill``，由调用方注入），
       补完仍不够就如实标"未达口径下限"，不假装够了。
    3. **记账**：把规则、淘汰清单、补位记录写进 state，供报告与前端展示。
    """
    plan_data = state.get("research_plan") or {}
    criteria = plan_data.get("competitor_criteria") or {}
    rule = parse_admission_rule(criteria.get("admission_rule"), criteria)

    competitors = list(state.get("competitors") or [])
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for comp in competitors:
        passed, reasons = evaluate_post_crawl(comp, rule)
        if passed:
            accepted.append(comp)
        else:
            rejected.append({
                "name": comp.get("name") or comp.get("url") or "",
                "url": comp.get("url") or "",
                "status": comp.get("status"),
                "reasons": reasons,
                "dimension_count": QualityScorer.count_dimensions(comp),
            })
    state["competitors"] = accepted
    state["rejected_competitors"] = rejected

    if rejected:
        emit(
            "crawl_complete", "crawl", "completed",
            f"准入闸门淘汰 {len(rejected)} 家数据不足的竞品（未采信清单）",
            payload={"rejected": [r.get("name") for r in rejected]},
        )

    # 补位：口径下限没达到时，从留池补抓
    min_count = int(criteria.get("min_count") or 0)
    need = max(0, min_count - len(accepted))
    backfilled: List[Dict[str, Any]] = []
    if need:
        backfilled = await backfill(
            state,
            rule,
            need=need,
            auth_credentials=auth_credentials,
            login_url=login_url,
            custom_selectors=custom_selectors,
        )
    state["admission_gate"] = {
        "rule": rule.to_dict(),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "backfilled": backfilled,
        "final_count": len(state.get("competitors") or []),
        "min_count": min_count,
        "met_minimum": len(state.get("competitors") or []) >= min_count if min_count else True,
    }


async def backfill_from_pool(
    state: Dict[str, Any],
    rule,
    need: int,
    *,
    emit,
    fetch_all,
    max_backfill: int,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None,
    custom_selectors: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """从候选留池补抓下一批候选（受上限约束）。"""
    discovery = state.get("discovery") or {}
    pool = discovery.get("scored") or []
    if not pool:
        return []
    seen_hosts = {
        _host_key(c.get("url")) for c in (state.get("competitors") or [])
    }
    seen_hosts |= {
        _host_key(c.get("url")) for c in (state.get("rejected_competitors") or [])
    }
    picked: List[Dict[str, Any]] = []
    for item in pool:
        if len(picked) >= min(need, max_backfill):
            break
        if not item.get("gate_passed", True):
            continue
        if float(item.get("score") or 0) < MIN_ADMISSION_SCORE:
            continue
        host = _host_key(item.get("url"))
        if host and host in seen_hosts:
            continue
        picked.append(item)
        if host:
            seen_hosts.add(host)
    if not picked:
        return []

    emit(
        "crawl_start", "crawl", "running",
        f"准入闸门后补抓 {len(picked)} 家候选（口径下限还差 {need} 家）",
    )
    sites = [
        {"url": item["url"], "auth_credentials": None, "login_url": None}
        for item in picked
    ]
    fetched = await fetch_all(
        sites,
        auth_credentials,
        login_url,
        custom_selectors,
        page_tiers=state.get("page_tiers"),
    )
    state["target_sites"].extend(sites)
    state["target_urls"].extend(site["url"] for site in sites)

    accepted: List[Dict[str, Any]] = []
    backfilled: List[Dict[str, Any]] = []
    for comp in fetched:
        passed, reasons = evaluate_post_crawl(comp, rule)
        entry = {
            "name": comp.get("name") or comp.get("url") or "",
            "url": comp.get("url") or "",
            "status": comp.get("status"),
            "reasons": reasons,
            "dimension_count": QualityScorer.count_dimensions(comp),
        }
        backfilled.append(entry)
        if passed:
            accepted.append(comp)
        else:
            state.setdefault("rejected_competitors", []).append(entry)
    state.setdefault("competitors", []).extend(accepted)
    return backfilled
