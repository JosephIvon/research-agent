"""缺口定向补抓（gap refetch）接线集成测试。

真实链路「引用校验仍不过 → 判定层 → 补抓 → 带新证据重生成 → 第三次校验」
在测试里用 ScriptedLLMClient + 两阶段爬虫替身完整走一遍：

- extract 脚本按调用轮次推进：[初次(无定价), 补抓(含定价)] 表达「初次缺、补抓得」
- report 脚本按轮次推进：[首版错, 二版仍错, 补抓后对] 表达三次生成
- 爬虫替身：首次调用返回无定价页，之后返回定价页

隔离手法与 test_agent_mode_wiring.py 一致（monkeypatch 工作流网络边界 +
临时输出目录 + 临时 SQLite），不经过 evals 夹具框架——因为
`resolve_citations` 只能把报告引用解析到 `extract_sequence[site_idx]`，
表达不了「报告引用补抓新增证据」的 id，所以这里直接手算 `make_evidence_id`。
"""
import asyncio
import json

from src.agent.citations import make_evidence_id
from src.llm import ScriptedLLMClient

SITE_URL = "https://acme.example.com/pricing"

# 初次抽取：覆盖 6 个质量维度但**不含定价**（过 quality/verify 门禁，触发真缺口）
INITIAL_ITEMS = [
    {"key": "features", "value": "日志检索", "confidence": 0.9},
    {"key": "models", "value": "SaaS 多租户", "confidence": 0.9},
    {"key": "target_users", "value": "中大型企业", "confidence": 0.9},
    {"key": "integrations", "value": "支持 Prometheus 导出", "confidence": 0.9},
    {"key": "limitations", "value": "私有化部署未公开", "confidence": 0.9},
    {"key": "reviews", "value": "用户评价检索速度快", "confidence": 0.9},
]
PRICING_ITEM = {"key": "pricing", "value": "79 元/月", "confidence": 0.9}

_FEATURES_ID = make_evidence_id(SITE_URL, "features", "日志检索")
_PRICING_ID = make_evidence_id(SITE_URL, "pricing", "79 元/月")


class _TwoPhaseCrawler:
    """首次抓取返回无定价页，之后（补抓）返回定价页。site_url 可选：
    工作流批量/就地构造都走无参 `WebCrawler()`，测试里单站场景无需绑定。"""

    def __init__(self, site_url=None):
        self.site_url = site_url or "https://placeholder.example.com"
        self.calls = 0
        self.profile_kwargs = []  # 记录每次 fetch_site_profile 的实参（透传断言用）

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_site_profile(self, url, max_pages=3, **kwargs):
        self.calls += 1
        self.profile_kwargs.append({"url": url, "max_pages": max_pages, **kwargs})
        if self.calls == 1:
            page = {"url": url, "content": "Acme 官网首页（未披露定价）", "html": ""}
        else:
            page = {"url": url, "content": "Acme 专业版定价页：79 元/月", "html": ""}
        return {
            "url": url,
            "content": page["content"],
            "pages": [page],
            "discovered_urls": [],
        }


class _EventCollector:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _wire_offline(monkeypatch, tmp_path, llm_client, crawler_cls):
    from src.config import settings
    from src.storage import database as dbmod
    from src.workflow import mvp_workflow

    monkeypatch.setattr(mvp_workflow, "get_llm_client", lambda *a, **k: llm_client)
    monkeypatch.setattr(mvp_workflow, "WebCrawler", crawler_cls)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_url", lambda u: u)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_urls", lambda us: list(us))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)


def _run_legacy(monkeypatch, tmp_path, script, crawler_cls=None):
    from src.workflow import mvp_workflow

    llm = ScriptedLLMClient(script=script)
    _wire_offline(monkeypatch, tmp_path, llm, crawler_cls or _TwoPhaseCrawler)

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector
    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=[SITE_URL],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="gap-flow-0001",
    ))
    return state, collector


def test_refetch_turns_third_draft_green(tmp_path, monkeypatch):
    """真缺口闭环：首版/二版 ungrounded → 补抓得定价 → 第三次校验通过。

    这是「补抓 → 带新证据重生成 → 第三次校验」顺序的实锤——如果只重建证据库
    不重生成（评审问题 A 的坑），报告仍引用旧证据，第三次必然不过。
    """
    crawler = _TwoPhaseCrawler(SITE_URL)
    script = {
        "decompose": ['[{"id": "1", "description": "分析目标网站内容", "priority": 1}]'],
        "extract": [
            json.dumps(INITIAL_ITEMS, ensure_ascii=False),
            json.dumps([PRICING_ITEM], ensure_ascii=False),
        ],
        "report": [
            f"## 定价\n\nAcme 专业版 99 元/月 [e:{_FEATURES_ID}]。\n",
            f"## 定价\n\nAcme 专业版 99 元/月 [e:{_FEATURES_ID}]。\n",
            f"## 定价\n\nAcme 专业版 79 元/月 [e:{_PRICING_ID}]。\n",
        ],
    }
    state, collector = _run_legacy(monkeypatch, tmp_path, script, lambda: crawler)

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["citation_attempts"] == 3, (
        f"attempts={state.get('citation_attempts')} "
        f"decision={state.get('gap_refetch_decision')} "
        f"result={state.get('gap_refetch_result')}"
    )
    assert state["citation_trusted"] is True, state.get("citation_violations")

    # 判定层判 eligible，补抓拿到定价证据
    decision = state["gap_refetch_decision"]
    assert decision["eligible"] is True, decision
    assert [c["competitor_url"] for c in decision["candidates"]] == ["acme.example.com"]
    result = state["gap_refetch_result"]
    assert result["new_items"] == 1 and result["candidates"] == 1
    assert state["refetched_evidence"][0]["key"] == "pricing"
    # 分布观测的口径钉死：只记**缺口数字**（99）的出现条数，补抓带回的 79
    # 不是缺口数字，即便进了证据也不进分布——这条观测为阈值决策攒样本，
    # 口径漂移会让攒出的分布失去可比性（见 refetchable_gaps 的 shortcut 注释）。
    assert result["refetchable_gap_evidence_spread"] == {}, result

    # 「缺口解决」双信号：数字进证据是观测、grounding 提升是裁决。
    # gap_refetch 形态：缺口 99 从未进证据（refetchable 计数低），
    # 但补抓带回 79、报告改用 79 → grounding 从不过变为通过。
    assert result["grounding_before"] < result["grounding_after"], result
    assert result["grounding_after"] == 1.0, result

    # 交付就绪统一裁决：补抓闭环后引用通过且无核心缺口 → ready
    readiness = state["delivery_readiness"]
    assert readiness["verdict"] == "ready", readiness["reasons"]
    assert "pricing" not in readiness["critical_gaps"], readiness["dimensions"]

    # 报告交付的是修正后的定价
    assert "79 元/月" in state["report_final"]
    assert "99 元/月" not in state["report_final"]

    # 事件流：gap_refetch running → completed（判定层触发的证据）
    gap_events = [e for e in collector.events if e.event == "gap_refetch"]
    statuses = [e.status for e in gap_events]
    assert "running" in statuses and "completed" in statuses, statuses


def test_dimension_gap_triggers_refetch_and_adds_assertion(tmp_path, monkeypatch):
    """报告**没写**的维度（missing）→ 维度补抓 → 重生成新增断言 → 通过。

    v0 报告不含定价断言（引用通过、无 ungrounded）→ 数值判定层看不到 → readiness
    判 pricing missing → 维度补抓（带维度意图选页）→ 补抓得定价 → 重生成 v1 含
    定价断言 → 校验通过。这是「引用自洽 ≠ 研究完整」的闭环补抓路径。
    """
    crawler = _TwoPhaseCrawler(SITE_URL)
    script = {
        "decompose": ['[{"id": "1", "description": "分析目标网站内容", "priority": 1}]'],
        "extract": [
            json.dumps(INITIAL_ITEMS, ensure_ascii=False),
            json.dumps([PRICING_ITEM], ensure_ascii=False),
        ],
        "report": [
            f"## 功能\n\nAcme 支持日志检索 [e:{_FEATURES_ID}]。\n",
            (
                f"## 功能\n\nAcme 支持日志检索 [e:{_FEATURES_ID}]。\n"
                f"## 定价\n\nAcme 专业版 79 元/月 [e:{_PRICING_ID}]。\n"
            ),
        ],
    }
    state, collector = _run_legacy(monkeypatch, tmp_path, script, lambda: crawler)

    assert state["status"] == "completed", state.get("termination_reason")
    # v0 引用通过（attempts=1）→ 维度补抓 → v1（attempts=2），未走数值补抓
    assert state["citation_attempts"] == 2, state.get("citation_attempts")
    assert state["citation_trusted"] is True, state.get("citation_violations")
    assert state.get("gap_refetch_result") is None, "无数值缺口，不应触发数值补抓"

    # 维度补抓决策：pricing 缺失 → 候选
    dim = state["dimension_refetch_decision"]
    assert dim["eligible"] is True, dim
    assert dim["candidates"][0]["dims"] == ["pricing"]
    assert dim["candidates"][0]["competitor_url"] == SITE_URL

    # 维度补抓拿到定价证据，报告新增定价断言
    result = state["dimension_refetch_result"]
    assert result["new_items"] == 1, result
    assert result["grounding_before"] <= result["grounding_after"], result
    assert "79 元/月" in state["report_final"]

    # readiness：补抓后 pricing completed → ready
    readiness = state["delivery_readiness"]
    assert readiness["verdict"] == "ready", readiness["reasons"]
    assert "pricing" not in readiness["critical_gaps"], readiness["dimensions"]

    # 事件流：维度补抓 running → completed
    gap_events = [e for e in collector.events if e.event == "gap_refetch"]
    statuses = [e.status for e in gap_events]
    assert "running" in statuses and "completed" in statuses, statuses

    # 真实爬虫链路透传：维度补抓必须带 page_tiers（意图选页）+ max_pages=2
    # （首页 + 1 意图页）。夹具替身若不透传，per_site_pages=1 只抓首页的
    # 真实 bug 会被掩盖——这里直接钉住透传实参。
    refetch_kwargs = [
        kw for kw in crawler.profile_kwargs if kw.get("priority_tiers")
    ]
    assert refetch_kwargs, "维度补抓必须透传 priority_tiers（维度意图）给爬虫"
    assert refetch_kwargs[-1]["max_pages"] == 2, (
        "每站 2 页 = 首页 + 1 意图页；1 页会让 fetch_site_profile 只抓首页"
    )


def test_dimension_refetch_skipped_when_budget_exhausted(tmp_path, monkeypatch):
    """数值补抓用满共享预算 → 维度补抓不触发（budget_insufficient）。

    预算合并口径的防回归：remaining < 每站下限 2 时强行触发会退化回
    per_site=1 的坑（只剩首页、选页意图失效），必须显式跳过并记录原因。
    """
    from src.agent.citations import EvidenceStore
    from src.workflow import mvp_workflow

    llm = ScriptedLLMClient(script={"extract": ["[]"], "report": ["占位"], "decompose": ["[]"]})
    _wire_offline(monkeypatch, tmp_path, llm, _TwoPhaseCrawler)
    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = {"target_sites": [{"url": SITE_URL}]}
    workflow.state = state  # _emit 内部经 _state() 读状态
    store = EvidenceStore.from_extracted_data([
        {"key": "features", "value": "日志检索", "source_url": SITE_URL},
    ])
    decision = asyncio.run(workflow._maybe_refetch_dimensions(
        state, store, remaining_budget=1,
    ))
    assert decision is None
    assert state["dimension_refetch_decision"]["reason"] == "budget_insufficient"
    # 未触发抓取：只有 skipped 事件，没有 running/completed
    gap_events = [e for e in collector.events if e.event == "gap_refetch"]
    assert gap_events and all(e.status == "skipped" for e in gap_events), [
        e.status for e in gap_events
    ]


def test_refetch_runs_through_single_competitor_fetch(tmp_path, monkeypatch):
    """补抓复用 `_fetch_single_competitor`：注入防护与抽取链路天然复用。

    同时验证判定层分到的页数额度（per_candidate_pages）被透传给爬虫——
    这是补抓的成本刹车。
    """
    crawler = _TwoPhaseCrawler(SITE_URL)
    script = {
        "extract": [
            json.dumps(INITIAL_ITEMS, ensure_ascii=False),
            json.dumps([PRICING_ITEM], ensure_ascii=False),
        ],
        "report": [
            f"## 定价\n\nAcme 专业版 99 元/月 [e:{_FEATURES_ID}]。\n",
            f"## 定价\n\nAcme 专业版 99 元/月 [e:{_FEATURES_ID}]。\n",
            f"## 定价\n\nAcme 专业版 79 元/月 [e:{_PRICING_ID}]。\n",
        ],
    }

    from src.workflow import mvp_workflow

    llm = ScriptedLLMClient(script=script)
    _wire_offline(monkeypatch, tmp_path, llm, lambda: crawler)
    calls = []
    original = mvp_workflow.MVPWorkflow._fetch_single_competitor

    async def _spy(self, url, *args, **kwargs):
        calls.append({"url": url, "max_pages": kwargs.get("max_pages"),
                      "crawler": kwargs.get("crawler")})
        return await original(self, url, *args, **kwargs)

    monkeypatch.setattr(mvp_workflow.MVPWorkflow, "_fetch_single_competitor", _spy)

    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(workflow.run(
        "Acme 定价调研", target_urls=[SITE_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id="gap-spy-0001",
    ))

    assert state["status"] == "completed"
    # 初次抓取 + 补抓各 1 次；补抓那次带共享爬虫与判定层额度
    assert len(calls) == 2, [c["url"] for c in calls]
    refetch_call = calls[1]
    assert refetch_call["max_pages"] == 1, "页数额度必须透传（per_candidate_pages）"
    assert refetch_call["crawler"] is not None, "补抓必须复用共享爬虫实例"


def test_refetch_skipped_when_only_unknown_citations(tmp_path, monkeypatch):
    """只有 unknown_citation（幻觉 id）→ 判定层不触发补抓 → 低可信交付。

    幻觉 id 补页面补不出，交给重生成；这里二版仍幻觉 → 明确不补抓、挂低可信。
    """
    script = {
        "extract": [json.dumps(INITIAL_ITEMS, ensure_ascii=False)],
        "report": [
            "## 定价\n\nAcme 专业版 99 元/月 [e:deadbeef1234]。\n",
            "## 定价\n\nAcme 专业版 99 元/月 [e:deadbeef1234]。\n",
        ],
    }
    state, collector = _run_legacy(monkeypatch, tmp_path, script)

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["citation_trusted"] is False, "二版仍幻觉 → 低可信交付"
    assert "低可信" in state["report_final"], "必须显式标记而非静默交付"

    decision = state["gap_refetch_decision"]
    assert decision["eligible"] is False
    assert decision["reason"] == "no_ungrounded_claim", decision["reason"]
    assert "gap_refetch_result" not in state, "判定不触发就不该有补抓动作"

    gap_events = [e for e in collector.events if e.event == "gap_refetch"]
    assert gap_events and gap_events[0].status == "skipped", [
        (e.event, e.status) for e in collector.events
    ]


def test_gap_counter_is_observability_only_and_never_blocks(tmp_path, monkeypatch):
    """`refetchable_gap_count = 0` **不阻止**重生成——这正是它该被钉住的行为。

    这条用例是为了钉死一个被证伪的假设而存在：一度想让「缺口数字不在任何证据里」
    直接拦住那次最贵的报告生成，理由是实测里 42% 的缺口在站点上压根不存在、
    重生成只会让模型再编一个。

    **那个理由是错的，而下面的夹具就是反证**：缺口数字 99 从未出现在任何证据里
    （补抓带回的是 79），模型却在第三次生成时拿到新证据表、自己把数字换成 79，
    校验转绿。也就是说 `absent` 类的缺口恰恰是重生成能修的那一类。

    真实运行不可复现，拿不到修复率数据之前没有任何证据支持抑制规则，
    所以 `refetchable_gap_count` 只做观测。将来若要用它当闸门，
    必须先拿出「抑制 vs 不抑制」的对照数据，并同步改掉这条用例。
    """
    crawler = _TwoPhaseCrawler(SITE_URL)
    wrong = f"## 定价\n\nAcme 专业版 99 元/月 [e:{_FEATURES_ID}]。\n"
    script = {
        "extract": [
            json.dumps(INITIAL_ITEMS, ensure_ascii=False),
            json.dumps([PRICING_ITEM], ensure_ascii=False),
        ],
        "report": [
            wrong,
            wrong,
            f"## 定价\n\nAcme 专业版 79 元/月 [e:{_PRICING_ID}]。\n",
        ],
    }
    state, _ = _run_legacy(monkeypatch, tmp_path, script, lambda: crawler)

    # 计数口径正确：99 不在任何证据里（补抓带回的是 79）
    result = state["gap_refetch_result"]
    assert result["refetchable_gap_count"] == 0, result

    # 但计数为 0 不影响闭环：仍然重生成，模型自己把数字改成有据可引的 79
    assert state["citation_attempts"] == 3, (
        f"计数为 0 不得拦下重生成：attempts={state.get('citation_attempts')}"
    )
    assert state["citation_trusted"] is True, state.get("citation_violations")
    assert "79 元/月" in state["report_final"]

