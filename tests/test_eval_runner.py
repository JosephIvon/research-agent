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
    # 守的是「manifest 与基线同版本」这条约定（新增用例必须升版本并重算），
    # 而不是某个具体版本号。此前这里硬编码 "v1.2"，每升一次基线就得改一次——
    # 久而久之它只会变成一个必须手工同步的字符串，早就不再检查任何东西了。
    assert manifest["version"] == eval_runner.BASELINE_VERSION, (
        f"manifest 版本 {manifest['version']} 与基线版本 "
        f"{eval_runner.BASELINE_VERSION} 不同步：新增用例必须升版本并重算基线"
    )

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


# --------------------------------------------------------------------------
# 引用违规记账
#
# 这组字段（attempts / first_violation_count / first_violation_types /
# final_violation_count）回答的是「有多少报告需要补救、被补救成功了吗」。
#
# 它们的来历和 truncation_rate 一模一样：指标写好了，却**没有任何 case 能触发**
# ——13 条旧 case 全部 citation_validity=1.0，因为夹具的抽取脚本与报告模板是配套
# 设计的，引用必然对得上。所以补了 gap_ungrounded：首版报告把 59 写成 89
# 并引用了正确的证据 id（正是模型「记错数字」的真实形态），重生成修正。
#
# 下面三条测试的作用是**防止这个样本被悄悄改没**：样本一消失，指标就退回恒零，
# 而恒零看起来和「全部合规」完全一样。
# --------------------------------------------------------------------------

UNGROUNDED_CASE_ID = "gap_ungrounded"


def test_ungrounded_case_triggers_a_violation_then_repairs_it():
    """这条 case 必须真的触发 ungrounded_claim 并在重生成后修好。

    断言精确到类型与次数，而不是「最终通过」——只看终态的话，
    把首版报告改回正确数字也能让测试变绿，而样本就没了。
    """
    manifest = load_manifest(verify_digests=True)
    case = _run(manifest, [UNGROUNDED_CASE_ID])[0]
    dbg = case["citation_debug"]

    assert case["status"] == "completed", (
        f"重生成后应交付完整报告，实得 {case['status']!r}（{case.get('termination_reason')}）"
    )
    assert dbg["attempts"] == 2, f"应走过一次重生成，实得 attempts={dbg['attempts']!r}"
    assert dbg["first_violation_count"] == 1, (
        f"首轮应恰好 1 处违规，实得 {dbg['first_violation_count']!r}"
    )
    assert dbg["first_violation_types"] == ["ungrounded_claim"], (
        f"首轮违规类型必须是 ungrounded_claim（数值无据），"
        f"实得 {dbg['first_violation_types']!r}"
    )
    assert dbg["final_violation_count"] == 0, "重生成后不应残留违规"


def test_ungrounded_case_fails_if_the_offending_number_disappears():
    """反向守卫：首版报告的错数字必须真的与证据不符。

    数字归一化若被改动（例如把 89 也认成 59），违规会消失、样本静默失效。
    这里直接断言「首版文本里的数字不在所引证据原文中」，
    把夹具与校验器之间的契约钉死。
    """
    from evals.fixture_source import CASES_DIR, resolve_citations
    from src.agent.citations import extract_numbers

    case_spec = next(
        c for c in load_manifest(verify_digests=True)["cases"]
        if c["id"] == UNGROUNDED_CASE_ID
    )
    llm_spec = json.loads(
        (CASES_DIR / UNGROUNDED_CASE_ID / "llm.json").read_text(encoding="utf-8")
    )
    first_report = llm_spec["report"][0]
    resolved = resolve_citations(first_report, case_spec, llm_spec)

    evidence_text = llm_spec["extract_sequence"][0][0]["value"]
    report_numbers = set(extract_numbers(resolved))
    evidence_numbers = set(extract_numbers(evidence_text))

    assert report_numbers, "首版报告必须含数值，否则测不到 ungrounded_claim"
    assert report_numbers - evidence_numbers, (
        "首版报告必须存在**证据里没有**的数字（错数字），"
        f"实得报告数字 {report_numbers} 全部包含于证据数字 {evidence_numbers}"
    )
    assert len(llm_spec["report"]) == 2, "需要「错版 + 修正版」两条脚本才能模拟重生成"


def test_violation_accounting_is_present_on_every_case_and_not_vacuous():
    """每条 case 都必须带齐 4 个记账字段，且全集中至少有一条真的非零。

    前半句堵字段消失，后半句堵样本消失——只有后半句能防住「删掉 gap_ungrounded
    后所有测试仍然全绿」这种事，因为恒零和合规在字段层面长得一模一样。
    """
    manifest = load_manifest(verify_digests=True)
    results = _run(manifest, [c["id"] for c in manifest["cases"]])

    fields = ("attempts", "first_violation_count",
              "first_violation_types", "final_violation_count")
    for row in results:
        dbg = row["citation_debug"]
        missing = [f for f in fields if f not in dbg]
        assert not missing, f"case {row['id']} 的 citation_debug 缺字段: {missing}"

    nonzero = [r["id"] for r in results
               if (r["citation_debug"]["first_violation_count"] or 0) > 0]
    assert nonzero, (
        "全集中没有任何 case 触发过引用违规——'引用违规'这项指标已退回恒零，"
        "和 truncation_rate 曾经踩的坑同构。检查 gap_ungrounded 夹具是否还在、"
        "或数字归一化是否变了。"
    )


def test_gap_refetch_case_closes_the_loop(tmp_path):
    """gap_refetch 端到端：初次缺定价 → 判定层补抓 → 带新证据重生成 → 第三次校验通过。

    attempts=3 是关键证据：若补抓只重建证据库不重生成（评审问题 A 的坑），
    报告仍引用旧证据、第三次校验必然不过——attempts 会停在 2 且 final_count>0。
    """
    manifest = load_manifest(verify_digests=True)
    entry = next(c for c in manifest["cases"] if c["id"] == "gap_refetch")
    llm_spec = json.loads(
        (eval_runner.CASES_DIR / "gap_refetch" / "llm.json").read_text(encoding="utf-8")
    )
    run_out = eval_runner.run_legacy_case(entry, llm_spec, tmp_path)
    state = run_out["state"]

    assert state["status"] == "completed", state.get("termination_reason")
    dbg = state.get("citation_report") or {}
    assert state["citation_attempts"] == 3, (
        "必须走补抓后的第三次校验（初次→重生成→补抓重生成）"
    )
    first = state.get("citation_first_attempt") or {}
    assert first.get("violation_types") == ["ungrounded_claim"], first
    assert state["citation_trusted"] is True, "补抓得定价后应通过"
    assert dbg.get("grounding") == 1.0, dbg

    # 「缺口解决」双信号（评测口径）：grounding 提升是裁决、数字进证据只观测。
    # 本 case 缺口 99 从不进证据（补抓带回 79），靠报告改用 79 转绿。
    result = state.get("gap_refetch_result") or {}
    assert result.get("grounding_before", 0) < result.get("grounding_after", 0), result
    assert result.get("grounding_after") == 1.0, result

    # 交付就绪统一裁决：补抓闭环后引用通过且无核心缺口 → ready
    readiness = state.get("delivery_readiness") or {}
    assert readiness.get("verdict") == "ready", readiness.get("reasons")

    # 预算合并 + 维度闭合：数值补抓已修定价 → 维度检查不再补抓
    dim_decision = state.get("dimension_refetch_decision") or {}
    assert dim_decision.get("eligible") is False, dim_decision


def test_gap_dimension_missing_case_closes_dimension_gap(tmp_path):
    """gap_dimension_missing 端到端：报告**没写**的维度 → 维度补抓 → 重生成新增断言。

    与 gap_refetch（数值缺口）互补：v0 报告无定价断言（引用通过、无违规）→
    数值判定层看不到 → readiness 判 pricing missing → 维度补抓 → 重生成含定价
    断言 → 校验通过。attempts=2、dimension_refetch_result 存在是关键证据。
    """
    manifest = load_manifest(verify_digests=True)
    entry = next(c for c in manifest["cases"] if c["id"] == "gap_dimension_missing")
    llm_spec = json.loads(
        (eval_runner.CASES_DIR / "gap_dimension_missing" / "llm.json").read_text(encoding="utf-8")
    )
    run_out = eval_runner.run_legacy_case(entry, llm_spec, tmp_path)
    state = run_out["state"]

    assert state["status"] == "completed", state.get("termination_reason")
    # v0 引用通过（attempts=1）→ 维度补抓 → v1（attempts=2），未走数值补抓
    assert state["citation_attempts"] == 2, state.get("citation_attempts")
    assert state.get("gap_refetch_result") is None, "无数值缺口，不应触发数值补抓"
    assert state["citation_trusted"] is True, state.get("citation_violations")

    # 维度补抓决策：pricing 缺失 → 候选
    dim = state.get("dimension_refetch_decision") or {}
    assert dim.get("eligible") is True, dim
    assert dim["candidates"][0]["dims"] == ["pricing"]

    # 补抓落地 + 报告新增定价断言
    result = state.get("dimension_refetch_result") or {}
    assert result.get("new_items", 0) >= 1, result
    assert result.get("grounding_before", 1) <= result.get("grounding_after", 1), result
    assert "79 元/月" in state.get("report_final", ""), "维度补抓后报告必须新增定价断言"

    # readiness：补抓后 pricing completed → ready
    readiness = state.get("delivery_readiness") or {}
    assert readiness.get("verdict") == "ready", readiness.get("reasons")
    assert "pricing" not in readiness.get("critical_gaps", []), readiness.get("dimensions")


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


# --------------------------------------------------------------------------
# 多版本抽取框架（缺口补抓 case 的地基）：向后兼容是硬约束
# --------------------------------------------------------------------------

def test_site_versions_adapts_single_and_multi():
    """站点抽取条目：单版本（dict 元素）包成 [版本0]，多版本（list 元素）原样。"""
    from evals.fixture_source import _site_versions

    single = [{"key": "pricing", "value": "59 元/月"}]
    assert _site_versions(single) == [single], "旧格式必须包成单版本"

    multi = [
        [{"key": "features", "value": "日志检索"}],
        [{"key": "pricing", "value": "79 元/月"}],
    ]
    assert _site_versions(multi) == multi, "多版本原样透传"


def test_resolve_citations_versioned_syntax_is_backward_compatible():
    """报告模板版本索引：旧语法解析版本0、新语法解析指定版本，id 各自正确。"""
    from evals.fixture_source import resolve_citations
    from src.agent.citations import make_evidence_id

    site_url = "https://auditx.test/"
    case = {"sites": [{"key": "auditx", "url": site_url}]}
    llm = {
        "extract_sequence": [
            [
                [{"key": "features", "value": "日志检索"}],
                [{"key": "pricing", "value": "79 元/月"}],
            ]
        ]
    }
    # 旧语法 → 版本0
    old = resolve_citations("{{cite:auditx:0}}", case, llm)
    assert old == f"[e:{make_evidence_id(site_url, 'features', '日志检索')}]"
    # 新语法 → 版本1
    new = resolve_citations("{{cite:auditx:1:0}}", case, llm)
    assert new == f"[e:{make_evidence_id(site_url, 'pricing', '79 元/月')}]"
    assert old != new, "两个版本引用必须指向不同证据"


def test_build_scripted_llm_flattens_versions_version_major():
    """extract 脚本按「版本优先、站点其次」平铺；旧格式平铺与旧行为逐字一致。"""
    site_url = "https://auditx.test/"
    case = {"sites": [{"key": "auditx", "url": site_url}]}

    # 新格式（多版本）：[版本0, 版本1]
    multi_llm = {
        "extract_sequence": [
            [
                [{"key": "features", "value": "日志检索"}],
                [{"key": "pricing", "value": "79 元/月"}],
            ]
        ],
        "report": ["报告占位"],
    }
    client = eval_runner._build_scripted_llm(case, multi_llm)
    assert len(client._script["extract"]) == 2, "两版必须各占一条脚本"
    assert json.loads(client._script["extract"][0])[0]["key"] == "features"
    assert json.loads(client._script["extract"][1])[0]["key"] == "pricing"

    # 旧格式（单版本）：与「每站一条」的旧行为一致
    single_llm = {
        "extract_sequence": [
            [{"key": "features", "value": "日志检索"}],
        ],
        "report": ["报告占位"],
    }
    client = eval_runner._build_scripted_llm(case, single_llm)
    assert len(client._script["extract"]) == 1
    assert json.loads(client._script["extract"][0])[0]["key"] == "features"
