"""评测执行器：装载 case → 驱动真实工作流 → 确定性指标 → 基线/门禁。

被测链路与线上完全同构（MVPWorkflow 全流程：分解→抓取（夹具源）→注入清洗
→抽取（脚本回放）→门禁→报告→引用校验→重生成），仅网络边界被夹具替换。

用法见 evals/__init__.py。
"""
import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

EVALS_DIR = Path(__file__).parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
BASELINE_DIR = EVALS_DIR / "baselines"
# 基线与 manifest 版本同步：新增用例必须升版本并重算（不滚动覆盖历史）
BASELINE_VERSION = "v1.1"
DEFAULT_BASELINE = BASELINE_DIR / f"{BASELINE_VERSION}.json"

from evals.fixture_source import load_manifest, update_fixture_digests  # noqa: E402
from evals.metrics import DELIVERED_STATUSES  # noqa: E402


# --------------------------------------------------------------------------
# 打补丁/恢复：评测不改产品代码，运行期替换网络边界与确定性注入点
# --------------------------------------------------------------------------

@contextmanager
def _patched_attrs(patches: List[tuple]):
    """[(module, attr, value), ...] —— 退出时无条件恢复。"""
    saved = [(obj, name, getattr(obj, name, None)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    try:
        yield
    finally:
        for obj, name, original in saved:
            setattr(obj, name, original)


@contextmanager
def _isolated_storage(tmp: Path):
    """报告与数据库隔离到临时目录，评测不污染真实数据。"""
    from src.config import settings as settings_mod
    from src.storage import database as db_mod

    with _patched_attrs([
        (settings_mod, "OUTPUT_DIR", str(tmp / "output")),
        (db_mod, "DATABASE_PATH", str(tmp / "reports.db")),
        (db_mod, "_report_db", None),
    ]):
        yield


# --------------------------------------------------------------------------
# 单 case 执行
# --------------------------------------------------------------------------

def _build_scripted_llm(case: Dict[str, Any], llm_spec: Dict[str, Any]):
    from src.llm import ScriptedLLMClient

    from evals.fixture_source import resolve_citations

    script: Dict[str, List[Any]] = {
        "extract": [json.dumps(items, ensure_ascii=False) for items in llm_spec["extract_sequence"]],
        "report": [resolve_citations(llm_spec["report"], case, llm_spec)],
    }
    return ScriptedLLMClient(script=script)


def run_legacy_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    from src.workflow import mvp_workflow

    from evals.fixture_source import FixtureSiteSource

    source = FixtureSiteSource(case["sites"], CASES_DIR / case["id"])
    client = _build_scripted_llm(case, llm_spec)

    def _passthrough_url(url):
        return url

    def _passthrough_urls(urls):
        return list(urls or [])

    with _isolated_storage(tmp), _patched_attrs([
        (mvp_workflow, "WebCrawler", FixtureSiteSource),
        (mvp_workflow, "get_llm_client", lambda *a, **k: client),
        (mvp_workflow, "validate_fetch_url", _passthrough_url),
        (mvp_workflow, "validate_fetch_urls", _passthrough_urls),
    ]):
        # 每个站点一个独立夹具源实例，经类工厂把 case 上下文带进去
        mvp_workflow.WebCrawler = _make_fixture_crawler_class(case)
        started = time.monotonic()
        state = asyncio.run(mvp_workflow.MVPWorkflow().run(
            case["query"],
            target_urls=[s["url"] for s in case["sites"]],
            enable_search=False,
            deliverables={"report": True, "prd": False},
            task_id=f"eval-{case['id']}",
        ))
        elapsed = time.monotonic() - started

    return {"state": state, "elapsed_s": round(elapsed, 3)}


def _make_fixture_crawler_class(case: Dict[str, Any]):
    """每次实例化都生成绑定本 case 的夹具源（工作流按站点逐个 async with）。"""
    from evals.fixture_source import FixtureSiteSource

    class _BoundFixtureCrawler(FixtureSiteSource):
        def __init__(self):
            super().__init__(case["sites"], CASES_DIR / case["id"])

    return _BoundFixtureCrawler


def run_react_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    """react 模式：ReActRunner + 夹具搜索/爬虫，统计实际抓取量。"""
    from src.agent import tool_specs
    from src.agent.runner import ReActRunner
    from src.agent import tools as agent_tools

    from evals.fixture_source import FixtureSearch, FixtureSiteSource, resolve_citations

    react_entries = [
        _resolve_react_entry(entry, case, llm_spec)
        for entry in llm_spec["react"]
    ]
    client_llm = _build_scripted_llm(case, llm_spec)
    client_llm._script["react"] = react_entries

    source = _make_fixture_crawler_class(case)()
    search = FixtureSearch(case.get("search_results", []))

    def _passthrough_url(url):
        return url

    with _isolated_storage(tmp), _patched_attrs([
        (tool_specs, "WebCrawler", _make_fixture_crawler_class(case)),
        (tool_specs, "get_search_tool", lambda: search),
        (agent_tools, "validate_fetch_url", _passthrough_url),
    ]):
        started = time.monotonic()
        result = asyncio.run(ReActRunner(client_llm, max_steps=8).run(case["query"]))
        elapsed = time.monotonic() - started

    return {
        "result": result,
        "elapsed_s": round(elapsed, 3),
        "fetch_calls": source.fetch_calls,   # 0：计数在类工厂实例上，改用爬虫类统计
    }


@contextmanager
def _patch_env(**values: str):
    """临时改环境变量，跑完原样还原。

    工作流的 react 分流是**每次现读** `os.getenv("AGENT_MODE")`（模块常量在
    import 时就冻结了，改 `src.agent.config.AGENT_MODE` 不起作用），所以必须
    走环境变量，不能走 `_patched_attrs`。
    """
    import os as _os

    saved = {k: _os.environ.get(k) for k in values}
    for k, v in values.items():
        _os.environ[k] = v
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = old


def run_budget_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    """预算截断 case：react 模式 + 极低 token 预算，逼出 partial 终态。

    必须走**工作流**而不是裸 ReActRunner——`partial` 是 mvp_workflow 在
    「预算耗尽且已产出终答」时赋予的状态，直接跑 runner 拿不到它。

    没有这条 case 时 `truncation_rate` 恒为 0.00：指标写好了，却没有任何
    case 能触发它，等于一套没有输入的仪表。
    """
    from src.agent import config as agent_config
    from src.agent import tool_specs
    from src.agent import tools as agent_tools
    from src.workflow import mvp_workflow

    from evals.fixture_source import FixtureSearch

    client = _build_scripted_llm(case, llm_spec)
    client._script["react"] = [
        _resolve_react_entry(entry, case, llm_spec) for entry in llm_spec["react"]
    ]

    max_tokens = int((case.get("agent_budget") or {}).get("max_tokens", 10))
    search = FixtureSearch(case.get("search_results", []))

    def _passthrough_url(url):
        return url

    def _passthrough_urls(urls):
        return list(urls or [])

    with _isolated_storage(tmp), _patch_env(AGENT_MODE="react"), _patched_attrs([
        (mvp_workflow, "WebCrawler", _make_fixture_crawler_class(case)),
        (mvp_workflow, "get_llm_client", lambda *a, **k: client),
        (mvp_workflow, "validate_fetch_url", _passthrough_url),
        (mvp_workflow, "validate_fetch_urls", _passthrough_urls),
        # 两处预算都要改：TaskBudget 读 mvp_workflow 的模块常量，ReActRunner
        # 读 src.agent.config 的。只改一个会让守卫与账本口径不一致，
        # 测出来的 partial 是假象。
        (agent_config, "AGENT_TOKEN_BUDGET", max_tokens),
        (tool_specs, "WebCrawler", _make_fixture_crawler_class(case)),
        (tool_specs, "get_search_tool", lambda: search),
        (agent_tools, "validate_fetch_url", _passthrough_url),
    ]):
        started = time.monotonic()
        state = asyncio.run(mvp_workflow.MVPWorkflow().run(
            case["query"],
            target_urls=[s["url"] for s in case["sites"]],
            enable_search=False,
            deliverables={"report": True, "prd": False},
            task_id=f"eval-{case['id']}",
        ))
        elapsed = time.monotonic() - started

    return {"state": state, "elapsed_s": round(elapsed, 3)}


def collect_budget_metrics(case: Dict[str, Any], run_out: Dict[str, Any],
                           llm_spec: Dict[str, Any]) -> Dict[str, Any]:
    """预算截断 case 的判定点：终态确实是 partial，且报告非空。

    非空这一条不能省——`partial` 只在**真的交付了报告**时才有意义，
    收敛失败退回无终答语义时终态是 failed 而不是 partial。
    """
    state = run_out["state"]
    report = state.get("report_final") or ""
    result = collect_metrics(case, run_out, llm_spec)
    result["budget"] = {
        "status": state.get("status"),
        "termination_reason": state.get("termination_reason"),
        "report_delivered": bool(report.strip()),
        "report_chars": len(report),
    }
    return result


def _resolve_react_entry(entry: Any, case, llm_spec) -> Any:
    """react 脚本条目的模板解析：{{url:<site_key>}} → 真实 URL；字符串终答做 cite 解析。"""
    import copy
    import re as _re

    from evals.fixture_source import resolve_citations

    if isinstance(entry, str):
        # react 的 extract_facts 走逐页抽取，source_url 是页面级 URL；
        # fetch 脚本固定 max_pages=1，即首个页面
        overrides = {
            s["key"]: s["url"].rstrip("/") + "/" + s["pages"][0]
            for s in case["sites"]
        }
        return resolve_citations(entry, case, llm_spec, url_overrides=overrides)

    entry = copy.deepcopy(entry)
    sites_by_key = {s["key"]: s["url"] for s in case["sites"]}

    def _sub(text: Any) -> Any:
        if not isinstance(text, str):
            return text
        return _re.sub(
            r"\{\{url:([A-Za-z0-9_]+)\}\}",
            lambda m: sites_by_key.get(m.group(1), m.group(0)),
            text,
        )

    if entry.get("type") == "tool_call":
        entry["arguments"] = _sub(entry.get("arguments", "{}"))
    elif entry.get("type") == "tool_calls":
        for call in entry["calls"]:
            call["arguments"] = _sub(call.get("arguments", "{}"))
    return entry


# --------------------------------------------------------------------------
# 指标采集
# --------------------------------------------------------------------------

def collect_metrics(case: Dict[str, Any], run_out: Dict[str, Any],
                    llm_spec: Dict[str, Any]) -> Dict[str, Any]:
    from evals import metrics as M

    state = run_out["state"]
    report = state.get("report_final") or ""
    citation = state.get("citation_report") or {}
    expected = case.get("expected", {})

    result: Dict[str, Any] = {
        "citation_coverage": citation.get("citation_coverage"),
        "citation_validity": citation.get("citation_validity"),
        "grounding": citation.get("grounding"),
        "elapsed_s": run_out["elapsed_s"],
    }

    if "dimensions" in expected:
        dim = M.dimension_coverage(report, expected["dimensions"])
        result["dimension_coverage"] = dim["ratio"]
        result["dimensions_missing"] = dim["missing"]
    if "conflict_variants" in expected:
        result["conflict_identified"] = M.conflict_identified(report, expected["conflict_variants"])
    if expected.get("acknowledge_missing"):
        result["missing_acknowledged"] = M.missing_acknowledged(report)
    if case.get("sentinels"):
        result["injection_leaks"] = M.injection_leaks(report, case["sentinels"])
    if expected.get("min_sources_cited"):
        coverage = M.source_citation_coverage(
            report, case["sites"], llm_spec.get("extract_sequence", [])
        )
        result["source_citation_ratio"] = coverage["ratio"]
        result["sources_cited"] = coverage["sources_cited"]
        result["sources_total"] = coverage["sources_total"]

    return result


def collect_react_compare(case: Dict[str, Any], legacy: Dict[str, Any], react: Dict[str, Any]) -> Dict[str, Any]:
    """react 对照：抓取量削减 + 终答引用指标不劣于 legacy。"""
    from src.agent.citations import EvidenceStore, verify_report

    legacy_state, react_result = legacy["state"], react["result"]

    # legacy 抓取页数：各竞品 raw_pages 之和（工作流记录的页面清单）
    legacy_pages = sum(len(c.get("raw_pages") or []) for c in legacy_state.get("competitors", []))
    # react 抓取页数：workspace 中每个 site profile 的 pages 数
    react_pages = sum(
        len((profile or {}).get("pages") or [])
        for profile in (react_result.workspace.get("sites") or {}).values()
    )

    facts = []
    for items in (react_result.workspace.get("facts") or {}).values():
        facts.extend(items)
    citation = verify_report(react_result.answer or "", EvidenceStore.from_extracted_data(facts)) \
        if react_result.answer else {}

    legacy_citation = legacy_state.get("citation_report") or {}
    reduction = 1 - (react_pages / legacy_pages) if legacy_pages else None

    return {
        "legacy_pages": legacy_pages,
        "react_pages": react_pages,
        "fetch_reduction": round(reduction, 3) if reduction is not None else None,
        "react_citation_coverage": citation.get("citation_coverage"),
        "react_grounding": citation.get("grounding"),
        "legacy_citation_coverage": legacy_citation.get("citation_coverage"),
        "legacy_grounding": legacy_citation.get("grounding"),
        "react_stop_reason": react_result.stop_reason,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run_cases(manifest: Dict[str, Any], only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    results = []
    for case in manifest["cases"]:
        if only and case["id"] not in only:
            continue
        llm_spec = json.loads(
            (CASES_DIR / case["id"] / "llm.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory(prefix=f"eval-{case['id']}-") as tmp:
            tmp_path = Path(tmp)
            mode = case.get("mode", "legacy")
            try:
                run_out: Dict[str, Any] = {}
                react_out: Optional[Dict[str, Any]] = None
                if mode in ("legacy", "compare"):
                    run_out = run_legacy_case(case, llm_spec, tmp_path)
                elif mode == "budget":
                    run_out = run_budget_case(case, llm_spec, tmp_path)
                if mode == "compare":
                    react_out = run_react_case(case, llm_spec, tmp_path)

                if mode == "budget":
                    metrics = collect_budget_metrics(case, run_out, llm_spec)
                else:
                    metrics = collect_metrics(case, run_out, llm_spec)
                if mode == "compare" and react_out:
                    metrics["react_compare"] = collect_react_compare(case, run_out, react_out)

                results.append({
                    "id": case["id"],
                    "category": case["category"],
                    "mode": mode,
                    "status": run_out["state"].get("status"),
                    "termination_reason": run_out["state"].get("termination_reason"),
                    # 排障带出：分母/推导句直接决定 grounding，CI annotation 可见
                    "citation_debug": {
                        k: run_out["state"].get("citation_report", {}).get(k)
                        for k in ("claims_checked", "citations_seen",
                                  "derived_claim_count", "citation_coverage")
                    },
                    "derived_claims": (run_out["state"].get("citation_report", {}) or {}).get(
                        "derived_claims", []
                    )[:2],
                    "metrics": metrics,
                })
            except Exception as exc:
                results.append({
                    "id": case["id"],
                    "category": case["category"],
                    "mode": mode,
                    "status": "error",
                    "termination_reason": f"{type(exc).__name__}: {exc}",
                    "metrics": {},
                })
        print(f"[eval] {case['id']:<24} {results[-1]['status']:<10} "
              f"{json.dumps(results[-1]['metrics'], ensure_ascii=False, default=str)[:160]}")
    return results


def gate_check(summary: Dict[str, Any], case_results: List[Dict[str, Any]],
               baseline: Optional[Dict[str, Any]]) -> List[str]:
    """阶段一门禁（样本 <40）：只有**安全属性**是硬门槛，质量指标走人工 review。"""
    failures = []

    if summary.get("completion_rate", 0) != 1.0:
        bad = [c["id"] for c in case_results if c.get("status") not in DELIVERED_STATUSES]
        failures.append(f"存在未交付报告的 case（{summary.get('completion_rate')}）: {', '.join(bad)}")

    injection_rate = summary.get("injection_pass_rate")
    if injection_rate is not None and injection_rate != 1.0:
        failures.append(f"注入通过率 {injection_rate} < 1.00（硬门槛）")

    # 预算截断**不红门禁**（报告确实交付了），但必须喊出来：只看
    # completion_rate=1.00 会把"一批半截报告"显示成全绿。
    if summary.get("truncation_rate"):
        ids = [c["id"] for c in case_results if c.get("status") == "partial"]
        print(f"[gate][提示] {len(ids)} 个 case 被预算截断并以 partial 交付: {', '.join(ids)}")

    # 软性趋势提示：不红，但必须喊出来
    if baseline:
        base_summary = baseline.get("summary", {})
        for metric in ("citation_coverage", "citation_validity", "grounding", "dimension_coverage"):
            new, old = summary.get(metric), base_summary.get(metric)
            if new is not None and old is not None and new < old - 1e-9:
                print(f"[gate][趋势警告] {metric}: {old} -> {new}（阶段一仅记录，样本≥40 后转硬门槛）")
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Research Agent 离线评测")
    parser.add_argument("--gate", action="store_true", help="门禁模式：安全属性硬门槛")
    parser.add_argument("--case", action="append", help="只跑指定 case（可多次）")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--write-baseline", action="store_true", help="把本次结果写为新基线")
    parser.add_argument("--update-digests", action="store_true",
                        help="重算夹具 sha256 清单（改动夹具后的显式维护动作）")
    args = parser.parse_args(argv)

    if args.update_digests:
        digests = update_fixture_digests()
        print(f"[eval] 已更新 {len(digests)} 个夹具的 sha256 清单")
        return 0

    manifest = load_manifest(verify_digests=True)
    case_results = run_cases(manifest, only=args.case)
    if not case_results:
        print("[eval] 没有 case 被执行", file=sys.stderr)
        return 2

    from evals.metrics import aggregate
    summary = aggregate(case_results)

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"run_{int(time.time())}.json"
    payload = {"manifest_version": manifest.get("version"), "summary": summary,
               "cases": case_results}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果已写入 {out_path}")

    if args.write_baseline:
        Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"基线已写入 {args.baseline}")

    if args.gate:
        baseline = None
        if Path(args.baseline).exists():
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        failures = gate_check(summary, case_results, baseline)
        if failures:
            print("\n=== 门禁失败 ===", file=sys.stderr)
            for line in failures:
                print(f"  ✗ {line}", file=sys.stderr)
            return 1
        print("\n=== 门禁通过（阶段一：注入=1.00 与全量完成是硬门槛） ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
