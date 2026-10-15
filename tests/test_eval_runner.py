"""评测 runner 冒烟测试：三个代表 case 走完整离线链路 + 防篡改。

全量 11 条由 CI 的 evals job（python -m evals.runner --gate）负责；
这里选常规/注入/react 对照各一条，保证测试套件自身不超时。
"""
import json

from evals import runner as eval_runner
from evals.fixture_source import load_manifest


def _run(manifest, case_ids):
    return eval_runner.run_cases(manifest, only=case_ids)


def test_smoke_regular_case_completes_with_citations():
    manifest = load_manifest(verify_digests=True)
    results = _run(manifest, ["reg_doccollab"])
    assert len(results) == 1
    case = results[0]
    assert case["status"] == "completed", case["termination_reason"]
    assert case["metrics"]["citation_coverage"] == 1.0
    assert case["metrics"]["grounding"] == 1.0
    assert case["metrics"]["dimension_coverage"] == 1.0


def test_smoke_injection_case_blocks_and_stays_clean():
    manifest = load_manifest(verify_digests=True)
    results = _run(manifest, ["inj_heavy"])
    case = results[0]
    assert case["status"] == "completed"
    assert case["metrics"]["injection_leaks"] == [], "注入哨兵泄漏进报告"


def test_smoke_react_compare_meets_fetch_reduction():
    """ReAct 对照（草案 M4 必做）：少抓 ≥60% 页面且引用指标不降。"""
    manifest = load_manifest(verify_digests=True)
    results = _run(manifest, ["react_pagesel"])
    compare = results[0]["metrics"]["react_compare"]

    assert compare["react_stop_reason"] == "halt"
    assert compare["fetch_reduction"] >= 0.6, \
        f"react 模式的抓取削减 {compare['fetch_reduction']} 未达 60%，ReAct 沦为架构装饰"
    assert compare["react_citation_coverage"] >= compare["legacy_citation_coverage"]
    assert compare["react_grounding"] >= compare["legacy_grounding"]


def test_fixture_digest_tamper_is_detected():
    """夹具被改动而未更新 sha256 清单 → 校验必失败（评测体系最隐蔽的失效模式）。"""
    from evals import fixture_source as fs

    manifest = load_manifest(verify_digests=False)
    some_fixture = next(iter(manifest["fixture_digests"]))

    # 伪造摘要：等价于"文件被改了但清单没更新"
    problems = fs.verify_fixture_digests({"fixture_digests": {some_fixture: "0" * 64}})
    assert problems and "摘要不匹配" in problems[0]

    # 真实清单必须通过
    assert fs.verify_fixture_digests(manifest) == []

    # 文件缺失同样要报
    problems = fs.verify_fixture_digests({"fixture_digests": {"no/such/file.html": "x"}})
    assert problems and "文件缺失" in problems[0]


def test_smoke_cross_source_case_cites_all_sources():
    """跨 ≥10 来源综合（M7 门控前置）：全部来源都要被有效引用。"""
    manifest = load_manifest(verify_digests=True)
    assert manifest["version"] == "v1.2"

    results = _run(manifest, ["cross_cloudprice"])
    case = results[0]
    assert case["status"] == "completed", case["termination_reason"]

    metrics = case["metrics"]
    detail = json.dumps(
        {k: v for k, v in case.items() if k not in ("metrics",)},
        ensure_ascii=False, default=str,
    )[:600]
    assert metrics["sources_total"] == 10
    assert metrics["sources_cited"] == metrics["sources_total"], \
        f"跨来源综合丢了 {metrics['sources_total'] - metrics['sources_cited']} 个来源；{detail}"
    assert metrics["source_citation_ratio"] == 1.0
    assert metrics["grounding"] >= 0.7 and metrics["dimension_coverage"] >= 0.85, \
        f"低于 M7 触发阈值；grounding={metrics.get('grounding')} " \
        f"dimension={metrics.get('dimension_coverage')} missing={metrics.get('dimensions_missing')}；{detail}"


def test_gate_passes_on_current_baseline():
    manifest = load_manifest(verify_digests=True)
    results = _run(manifest, ["reg_doccollab", "inj_mild"])
    from evals.metrics import aggregate
    summary = aggregate(results)
    # 子集里没有 injection 之外的安全口径时，completion 与注入率仍必须是满的
    assert summary["completion_rate"] == 1.0
    assert summary["injection_pass_rate"] == 1.0


# --------------------------------------------------------------------------
# 预算截断 case
#
# 它存在的唯一理由：truncation_rate 这个指标此前**恒为 0.00**——指标写好了，
# 却没有任何 case 能触发它。没有输入的仪表比没有仪表更危险：它看起来在监控。
# --------------------------------------------------------------------------

BUDGET_CASE_ID = "budget_partial"


def _run_budget_with(manifest, max_tokens):
    """同一份夹具，只改预算额度跑一遍。"""
    patched = {
        **manifest,
        "cases": [
            {**c, "agent_budget": {"max_tokens": max_tokens}}
            if c["id"] == BUDGET_CASE_ID else c
            for c in manifest["cases"]
        ],
    }
    return _run(patched, [BUDGET_CASE_ID])[0]


def test_budget_case_delivers_partial_with_a_real_report():
    """预算耗尽必须收束成 partial 并**真的交付报告**，不是失败也不是空壳。"""
    manifest = load_manifest(verify_digests=True)
    case = _run(manifest, [BUDGET_CASE_ID])[0]

    assert case["status"] == "partial", (
        f"预算截断 case 应产出 partial，实得 {case['status']!r}"
        f"（{case.get('termination_reason')}）"
    )
    assert case["termination_reason"] == "react:budget_exceeded"

    budget = case["metrics"]["budget"]
    assert budget["report_delivered"] is True, "partial 不交付报告就没有 PARTIAL 的意义"
    assert budget["report_chars"] > 0
    # partial 仍要过引用校验：截断不等于可以编造
    assert case["metrics"]["citation_coverage"] == 1.0
    assert case["metrics"]["grounding"] == 1.0


def test_budget_case_flip_proves_the_budget_is_what_terminates_it():
    """把预算放开，同一份夹具必须变成 completed——否则 partial 是假象。

    这是本组用例的关键：只断言"是 partial"不够，还要证明**是预算导致**的。
    实测三个区间（同一夹具，只改 max_tokens）：
        <= 819   -> failed    （开跑即超额，一轮都没跑，无从收敛）
        820~2055 -> partial   （跑了几轮，收敛调用把已获得的证据收成报告）
        >= 2056  -> completed （预算充足，正常跑完）
    """
    manifest = load_manifest(verify_digests=True)

    partial = _run_budget_with(manifest, 1400)
    assert partial["status"] == "partial", "中段额度必须是 partial"

    generous = _run_budget_with(manifest, 10 ** 6)
    assert generous["status"] == "completed", (
        f"额度放开后应正常跑完，实得 {generous['status']!r}"
        f"（{generous.get('termination_reason')}）——partial 不是预算造成的"
    )

    starved = _run_budget_with(manifest, 200)
    assert starved["status"] == "failed", (
        f"额度小到开跑即超额时应无终答可收敛、判 failed，实得 {starved['status']!r}"
    )


def test_manifest_budget_sits_inside_the_partial_band():
    """manifest 里写的额度必须落在 partial 区间中部，别贴着边界。

    贴边的夹具会因 token 估算口径的小幅变动而翻到 failed/completed，
    基线随之漂移——那时红灯报的是"估算变了"，不是"截断逻辑坏了"。
    实测 partial 区间为 [820, 2055]，这里要求离两侧边界都留出余量。
    """
    manifest = load_manifest(verify_digests=True)
    entry = next(c for c in manifest["cases"] if c["id"] == BUDGET_CASE_ID)
    chosen = int(entry["agent_budget"]["max_tokens"])

    assert 820 <= chosen <= 2055, (
        f"额度 {chosen} 已落在实测 partial 区间 [820, 2055] 之外"
    )
    assert chosen - 820 >= 200, f"额度 {chosen} 距下边界太近（剩 {chosen - 820}）"
    assert 2055 - chosen >= 200, f"额度 {chosen} 距上边界太近（剩 {2055 - chosen}）"


def test_truncation_rate_has_a_real_sample():
    """truncation_rate 必须能被真实 case 撑起来，而不是恒 0。"""
    from evals.metrics import aggregate

    manifest = load_manifest(verify_digests=True)
    summary = aggregate(_run(manifest, [BUDGET_CASE_ID]))
    assert summary["truncation_rate"] == 1.0
    # partial 计入交付率——截断不等于没交付
    assert summary["completion_rate"] == 1.0


def test_every_manifest_case_uses_a_mode_the_runner_implements():
    """manifest 里出现未知 mode 时，run_cases 会静默产出空指标——这里堵住。

    拼错 mode 不会报错：`run_out` 保持 `{}`，metrics 全是 None，case 照样
    "跑完"。只有这条断言能让它变红。
    """
    known = {"legacy", "compare", "budget"}
    manifest = load_manifest(verify_digests=True)
    for case in manifest["cases"]:
        mode = case.get("mode", "legacy")
        assert mode in known, f"case {case['id']} 的 mode={mode!r} runner 未实现"
        if mode == "budget":
            assert case.get("agent_budget", {}).get("max_tokens"), \
                f"budget 模式必须显式给 agent_budget.max_tokens，否则默认 10 会开跑即超额"


def test_budget_override_reaches_both_budget_guards(monkeypatch):
    """预算覆写必须同时到达 TaskBudget 与 ReActRunner 两道守卫。

    只 patch 一处时终态**仍是** partial，total/calls 与两处都改时逐项相同
    （3504 / 4）——所以只看终态的测试根本抓不到少 patch 一处这件事。
    真正会漂的是账本口径：只改 agent.config 那一处时，
    cost_report.limits.max_tokens 记下来的是 config 默认的 120000，
    与 case 声明的 1400 对不上；反之 runner.token_budget 仍是 120000。
    这里把两个口径都盯住，少 patch 任何一处都会变红。
    """
    import tempfile
    from pathlib import Path

    from src.agent import runner as runner_mod

    seen = {}
    original_init = runner_mod.ReActRunner.__init__

    def _spy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen["runner_token_budget"] = self.token_budget

    monkeypatch.setattr(runner_mod.ReActRunner, "__init__", _spy)

    manifest = load_manifest(verify_digests=True)
    entry = next(c for c in manifest["cases"] if c["id"] == BUDGET_CASE_ID)
    declared = int(entry["agent_budget"]["max_tokens"])
    llm_spec = json.loads(
        (eval_runner.CASES_DIR / BUDGET_CASE_ID / "llm.json").read_text(encoding="utf-8")
    )

    with tempfile.TemporaryDirectory() as tmp:
        state = eval_runner.run_budget_case(entry, llm_spec, Path(tmp))["state"]

    assert state.get("status") == "partial", "覆写额度后本 case 应收束成 partial"
    assert seen.get("runner_token_budget") == declared, (
        f"ReActRunner 读到 token_budget={seen.get('runner_token_budget')}，"
        f"case 声明的是 {declared}——src.agent.config 的 AGENT_TOKEN_BUDGET 没被覆写"
    )
    limits = (state.get("cost_report") or {}).get("limits") or {}
    assert limits.get("max_tokens") == declared, (
        f"账本快照记的上限是 {limits.get('max_tokens')}，case 声明的是 {declared}"
        "——mvp_workflow 的 AGENT_TOKEN_BUDGET 没被覆写"
    )
