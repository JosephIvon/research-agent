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
BASELINE_VERSION = "v1.8"
DEFAULT_BASELINE = BASELINE_DIR / f"{BASELINE_VERSION}.json"

from evals.fixture_source import load_manifest, update_fixture_digests  # noqa: E402
from evals.metrics import DELIVERED_STATUSES  # noqa: E402

# 模块级导入：`_SiteKeyedScriptedLLM` 要拿它当基类，类定义时就得可见。
# （原先只在函数里惰性导入，改成模块级后 §extract 分派才有地方挂。）
from src.llm import ScriptedLLMClient, detect_llm_intent  # noqa: E402


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

def _site_host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url or "").netloc or "").lower()


def _host_in_messages(messages: Any, by_host: Dict[str, Any]) -> str:
    """从提示词里认出这次抽取是哪个站点，认不出就**判错而不是猜**。"""
    parts: List[str] = []
    for m in messages or []:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            parts.append(m["content"])
    text = "\n".join(parts)
    hits = [h for h in by_host if h and h in text]
    if len(hits) != 1:
        raise ValueError(
            f"[fake] extract 意图无法唯一确定站点（命中 {hits or '无'}；已知 "
            f"{sorted(by_host)}）。按调用顺序取响应会在并发抓取下把 A 站的条目"
            "配给 B 站，证据 id 对不上——所以这里必须判错，不能退回按序取。"
        )
    return hits[0]


def _build_scripted_llm(case: Dict[str, Any], llm_spec: Dict[str, Any]):
    from evals.fixture_source import _site_versions, resolve_citations

    # report 可以是单条（字符串）或**按调用顺序的多条**（列表）。
    # 多条的用途：脚本化「首版报告有违规 → 重生成修正」。ScriptedLLMClient 在
    # 同一意图的脚本耗尽后重复最后一条（src/llm/__init__.py:760-761），
    # 所以 [错版, 对版] 天然就是「第 1 次校验不过、第 2 次通过」。
    reports = llm_spec["report"]
    if isinstance(reports, str):
        reports = [reports]

    # extract 响应**按站点主机**分派，不按调用顺序：`extract_key_info` 跑在并发
    # 抓取分支里（`mvp_workflow` 用 `asyncio.gather` 抓多站），完成顺序不定；
    # 按序取会把 A 站的抽取结果配给 B 站的 URL，`make_evidence_id` 于是对不上，
    # 引用校验的 validity/grounding 假红。CI 上实测过一次
    # `citation_coverage=1.0 / validity=0.333 / grounding=0.0` 而同一提交本地全绿，
    # 正是并发顺序翻车的签名——这不是抖动，是**脚本夹具的语义错**。
    versions = [_site_versions(sv) for sv in llm_spec["extract_sequence"]]
    sites = case.get("sites") or []
    blocked = {i for i, s in enumerate(sites) if s.get("blocked")}
    # 约定：被整页丢弃的站点（manifest 里标 `blocked: true`）**不占用**抽取条目，
    # 所以条目按「非 blocked 站点」的声明顺序一一对应。
    live_sites = [s for i, s in enumerate(sites) if i not in blocked]
    extract_by_host: Dict[str, List[str]] = {}
    for idx, site in enumerate(live_sites):
        if idx >= len(versions):
            break
        extract_by_host[_site_host(site.get("url", ""))] = [
            json.dumps(v, ensure_ascii=False) for v in versions[idx]
        ]

    script: Dict[str, List[Any]] = {
        "report": [resolve_citations(r, case, llm_spec) for r in reports],
    }
    return _SiteKeyedScriptedLLM(script=script, extract_by_host=extract_by_host)


class _SiteKeyedScriptedLLM(ScriptedLLMClient):
    """`extract` 意图按站点主机取脚本条目；其余意图沿用父类的按序语义。

    父类 `_next_entry` 用「同意图第几次调用」定位条目（`src/llm/__init__.py:841`），
    那要求调用顺序稳定——而真实工作流的抓取是**并发**的。评测夹具不能靠
    "线程调度恰好按声明顺序"来保证正确性。
    """

    def __init__(self, script: Dict[str, List[Any]], extract_by_host: Dict[str, List[str]]):
        super().__init__(script=script)
        self._extract_by_host = {
            str(h).lower(): list(v) for h, v in extract_by_host.items()
        }
        self._extract_step: Dict[str, int] = {}

    def _next_entry(self, messages, tools):
        intent = detect_llm_intent(messages)
        if intent != "extract":
            return super()._next_entry(messages, tools)
        host = _host_in_messages(messages, self._extract_by_host)
        step = self._extract_step.get(host, 0)
        self._extract_step[host] = step + 1
        entries = self._extract_by_host[host]
        entry = entries[min(step, len(entries) - 1)]
        self.calls.append(
            {"intent": intent, "step": step, "messages": messages, "tools": tools}
        )
        return entry


def _make_fixture_crawler_class(case: Dict[str, Any]):
    """每次实例化都生成绑定本 case 的夹具源（工作流按站点逐个 async with）。"""
    from evals.fixture_source import FixtureSiteSource

    class _BoundFixtureCrawler(FixtureSiteSource):
        def __init__(self):
            super().__init__(case["sites"], CASES_DIR / case["id"])

    return _BoundFixtureCrawler


class EvalHarness:
    """case → 被测对象的构造器：剧本 LLM + 夹具网络边界 + 隔离存储收拢到一处。

    曾散在三个 run_* 里的 12+ 个 monkeypatch 点（mvp_workflow/tool_specs/
    agent_tools 的模块属性 + 模块常量 + AGENT_MODE 环境变量）已全部清零：
    依赖经 MVPWorkflow / build_default_tools / ReActRunner 的构造注入口传入。
    新增网络边界时只需在这里补参数——漏传会直接 TypeError，而不是旧 patch
    模式下的静默假绿。
    """

    def __init__(self, case: Dict[str, Any], llm_spec: Dict[str, Any]):
        self.case = case
        self.llm_spec = llm_spec
        self._crawler_cls = _make_fixture_crawler_class(case)
        from evals.fixture_source import FixtureSearch
        self.search = FixtureSearch(case.get("search_results", []))

    def scripted_llm(self, with_react: bool = False):
        """剧本客户端；with_react=True 时附加 react 轮次脚本（react/budget 模式）。"""
        client = _build_scripted_llm(self.case, self.llm_spec)
        if with_react:
            client._script["react"] = [
                _resolve_react_entry(entry, self.case, self.llm_spec)
                for entry in self.llm_spec["react"]
            ]
        return client

    def workflow(self, react_script: bool = False, **overrides):
        """legacy/budget 模式的被测工作流：依赖全注入，无 monkeypatch。"""
        from src.workflow.mvp_workflow import MVPWorkflow

        return MVPWorkflow(
            llm_client=self.scripted_llm(with_react=react_script),
            crawler_factory=self._crawler_cls,
            url_validator=lambda u: u,
            **overrides,
        )

    def react_runner(self, max_steps: int = 8):
        """裸 ReActRunner（compare 模式）：工具集显式装配夹具网络边界。"""
        from src.agent.runner import ReActRunner
        from src.agent.tool_specs import build_default_tools

        client = self.scripted_llm(with_react=True)
        workspace: Dict[str, Any] = {}
        tools = build_default_tools(
            client, workspace,
            crawler_factory=self._crawler_cls,
            search_factory=lambda: self.search,
            url_validator=lambda u: u,
        )
        return ReActRunner(client, tools=tools, workspace=workspace, max_steps=max_steps)


def run_legacy_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    harness = EvalHarness(case, llm_spec)

    with _isolated_storage(tmp):
        started = time.monotonic()
        state = asyncio.run(harness.workflow().run(
            case["query"],
            target_urls=[s["url"] for s in case["sites"]],
            enable_search=False,
            deliverables={"report": True, "prd": False},
            task_id=f"eval-{case['id']}",
        ))
        elapsed = time.monotonic() - started

    return {"state": state, "elapsed_s": round(elapsed, 3)}


def run_react_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    """react 模式：ReActRunner + 夹具搜索/爬虫，统计实际抓取量。"""
    harness = EvalHarness(case, llm_spec)
    runner = harness.react_runner(max_steps=8)

    with _isolated_storage(tmp):
        started = time.monotonic()
        result = asyncio.run(runner.run(case["query"]))
        elapsed = time.monotonic() - started

    return {
        "result": result,
        "elapsed_s": round(elapsed, 3),
        # 0：真实抓取计数发生在工具闭包内的爬虫实例上，这里不重复统计
        "fetch_calls": 0,
    }


def run_budget_case(case: Dict[str, Any], llm_spec: Dict[str, Any], tmp: Path) -> Dict[str, Any]:
    """预算截断 case：react 模式 + 极低 token 预算，逼出 partial 终态。

    必须走**工作流**而不是裸 ReActRunner——`partial` 是 mvp_workflow 在
    「预算耗尽且已产出终答」时赋予的状态，直接跑 runner 拿不到它。

    没有这条 case 时 `truncation_rate` 恒为 0.00：指标写好了，却没有任何
    case 能触发它，等于一套没有输入的仪表。

    token_budget 一个参数同时驱动 TaskBudget（工作流侧）和 ReActRunner
    的 token_budget（config 侧）——二者本就是同源双口径，只喂一处会让
    cost_report.limits 与 case 声明对不上（旧 monkeypatch 时代的坑，
    见 git 历史）。agent_mode="react" 显式注入，不再借道环境变量。
    """
    harness = EvalHarness(case, llm_spec)
    max_tokens = int((case.get("agent_budget") or {}).get("max_tokens", 10))

    workflow = harness.workflow(
        react_script=True,
        search_factory=lambda: harness.search,
        token_budget=max_tokens,
        agent_mode="react",
    )

    with _isolated_storage(tmp):
        started = time.monotonic()
        state = asyncio.run(workflow.run(
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

                first_attempt = run_out["state"].get("citation_first_attempt") or {}

                results.append({
                    "id": case["id"],
                    "category": case["category"],
                    "mode": mode,
                    "status": run_out["state"].get("status"),
                    "termination_reason": run_out["state"].get("termination_reason"),
                    # 排障带出：分母/推导句直接决定 grounding，CI annotation 可见
                    "citation_debug": {
                        **{
                            k: (run_out["state"].get("citation_report") or {}).get(k)
                            for k in ("claims_checked", "citations_seen",
                                      "derived_claim_count", "citation_coverage")
                        },
                        # 引用违规的**触发率**分母。第一次 verify 的违规明细在重生成
                        # 后会被覆盖（mvp_workflow.py 落 state["citation_violations"]
                        # 那行只保留终判），所以这里取的是工作流另存的首轮留档。
                        # 没有这几个字段，「有多少报告需要补救」这个问题无法回答。
                        "attempts": run_out["state"].get("citation_attempts"),
                        "first_violation_count": first_attempt.get("violation_count", 0),
                        "first_violation_types": first_attempt.get("violation_types", []),
                        "final_violation_count": (run_out["state"].get("citation_report") or {}).get(
                            "violation_count"
                        ),
                    },
                    "derived_claims": (run_out["state"].get("citation_report", {}) or {}).get(
                        "derived_claims", []
                    )[:2],
                    # 补抓/交付裁决观测：数值补抓与维度补抓各自是否发生、落地多少新证据，
                    # 以及最终交付就绪裁决——「报告自洽 ≠ 研究完整」在此可事后测量。
                    "gap_refetch_result": (run_out["state"].get("gap_refetch_result") or None),
                    "dimension_refetch_result": (run_out["state"].get("dimension_refetch_result") or None),
                    "delivery_readiness": (run_out["state"].get("delivery_readiness") or {}).get("verdict"),
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
