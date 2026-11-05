"""证据页归并：反证页挂回所属竞品、补抓数据回填、外站页封顶。

从 mvp_workflow 抽出的纯叶子职责：只对传入的列表做筛选/归并，
不读写工作流的共享 state，也不依赖工作流实例。
"""
from typing import Any, Dict, List, Tuple

# 反证页（缺点/评价）抓取封顶：每家最多几页、整批最多几页。
# 反证页是**外站**，抓一个页就是爬一个站；不封顶的话一次调研能变成
# 几十个站点的爬取，预算与墙钟都扛不住。
MAX_COUNTER_EVIDENCE_PER_COMPETITOR = 2
MAX_COUNTER_EVIDENCE_TOTAL = 5


def counter_evidence_sites(
    verification_pages: List[Dict[str, Any]],
    existing_urls: List[str],
) -> List[Dict[str, Any]]:
    """从反证结果里挑出要抓的证据页（每家封顶、总数封顶）。

    封顶的原因：反证页是**外站**，抓一次就是抓一个站。放开会让
    一次调研变成几十个站点的爬取，预算与时间都扛不住。
    """
    seen = set(existing_urls or [])
    per_competitor: Dict[str, int] = {}
    sites: List[Dict[str, Any]] = []
    for page in verification_pages or []:
        if page.get("stage") != "counter_evidence":
            continue
        name = str(page.get("competitor") or "").strip()
        url = str(page.get("url") or "").strip()
        if not name or not url or url in seen:
            continue
        if per_competitor.get(name, 0) >= MAX_COUNTER_EVIDENCE_PER_COMPETITOR:
            continue
        if len(sites) >= MAX_COUNTER_EVIDENCE_TOTAL:
            break
        per_competitor[name] = per_competitor.get(name, 0) + 1
        seen.add(url)
        sites.append({
            "url": url,
            "auth_credentials": None,
            "login_url": None,
            "parent_platform": name,
            # 只取这一页：反证页是证据来源，不是要建站的竞品
            "max_pages": 1,
        })
    return sites


def merge_evidence_pages(
    sites: List[Dict[str, Any]],
    competitors: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把反证/验证页归并进它所属的竞品条目。

    不归并的话，这些外站页面会各自成为矩阵里的一行——一行叫
    "快驴进货 App"、一行叫"知乎"，它们根本不是竞品。
    返回 (归并后的竞品条目, 无主证据页)。无主证据页**不进竞品列表**
    （它们不是竞品），单独留档备查。
    """
    if not sites or len(sites) != len(competitors):
        return competitors, []
    parents = {
        str(comp.get("name") or "").strip(): comp
        for comp, site in zip(competitors, sites)
        if not site.get("parent_platform")
    }
    merged: List[Dict[str, Any]] = []
    orphans: List[Dict[str, Any]] = []
    for site, comp in zip(sites, competitors):
        parent = str(site.get("parent_platform") or "").strip()
        if not parent:
            merged.append(comp)
            continue
        target = parents.get(parent)
        if target is None:
            # 父竞品没抓到（抓取失败）：证据页不是竞品，不能自成一行
            orphans.append({
                "url": site.get("url"),
                "evidence_page_for": parent,
                "extracted_data": comp.get("extracted_data") or [],
                "error_message": comp.get("error_message"),
            })
            continue
        target.setdefault("extracted_data", []).extend(comp.get("extracted_data") or [])
        if comp.get("raw_search_results"):
            target.setdefault("raw_search_results", []).extend(comp["raw_search_results"])
        target.setdefault("evidence_pages", []).append({
            "url": site.get("url"),
            "stage": "counter_evidence",
        })
    return merged, orphans


def merge_refetched_into_competitors(
    competitors: List[Dict[str, Any]],
    competitor_url: str,
    items: List[Dict[str, Any]],
) -> None:
    """把补抓到的数据回填到对应竞品。

    补抓的产出只进了 state["extracted_data"]（报告正文读它），没进
    state["competitors"][i]["extracted_data"]（评分器按竞品统计维度读它）——
    两边不一致，附录自评表就会说「定价缺失」而正文里正写着定价。

    归属按**站点**（site_of，小写 netloc）判定：competitor_url 是站点标识，
    竞品条目里存的是整 URL，拿整串比永远比不上。
    """
    if not items:
        return
    from src.agent.gap_refetch import site_of

    target = site_of(str(competitor_url or ""))
    if not target:
        return
    for comp in competitors:
        if site_of(str(comp.get("url") or "")) == target:
            comp.setdefault("extracted_data", []).extend(items)
            return
