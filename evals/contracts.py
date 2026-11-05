"""评测里的契约/生命周期子检查（mode="contract"）。

这些不是报告质量 case，而是把 v2 的四条硬契约变成可重复执行的评测项：
断点续跑、取消后无副作用、折叠证据分页回读、只读/写声明拦截。
每条都驱动真实产品类（MVPWorkflow / ToolRegistry / EvidenceIndex），
只用离线夹具替换网络边界；判定全部确定性，无模型参与。
"""
import asyncio
import hashlib
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src.agent.context import EvidenceIndex
from src.agent.tools import Tool, ToolRegistry
from src.agent.tool_specs import build_default_tools
from src.llm import ScriptedLLMClient
from src.storage import database as dbmod
from src.workflow import mvp_workflow
from src.workflow.mvp_workflow import MVPWorkflow
from tests.test_scripted_llm import EXTRACT_ITEMS, TARGET_URL, _FakeWebCrawler


class _Crash(Exception):
    pass


class _CrashAfterStage(MVPWorkflow):
    def __init__(self, crash_stage: str, **kwargs):
        self._crash_stage = crash_stage
        super().__init__(**kwargs)

    def _save_checkpoint(self, state, stage):
        super()._save_checkpoint(state, stage)
        if stage == self._crash_stage:
            raise _Crash(stage)


def _scripted_client():
    from src.agent.citations import EvidenceStore

    expected = [dict(item, source_url=TARGET_URL) for item in EXTRACT_ITEMS]
    store = EvidenceStore.from_extracted_data(expected)
    cite = store.evidences[0].citation
    report = f"## 定价\n\n标准版 $49/席位/月 {cite}，企业版 $99/席位/月 {cite}。\n"
    return ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report],
    }), cite


def _wire(tmp_path: Path, client) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(mvp_workflow, "WebCrawler", _FakeWebCrawler))
    stack.enter_context(patch.object(mvp_workflow, "get_llm_client", lambda *a, **k: client))
    stack.enter_context(patch.object(mvp_workflow, "validate_fetch_url", lambda u: u))
    stack.enter_context(patch.object(mvp_workflow, "validate_fetch_urls", lambda us: list(us)))
    stack.enter_context(patch("src.config.settings.OUTPUT_DIR", str(tmp_path / "output")))
    stack.enter_context(patch.object(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db")))
    stack.enter_context(patch.object(dbmod, "_report_db", None))
    return stack


def _check_resume_across_crash() -> str:
    """跑到 verify 中段猝死 → 重启 resume → completed，且报告真被续出。"""
    with tempfile.TemporaryDirectory(prefix="contract-resume-") as td:
        tmp = Path(td)
        client, cite = _scripted_client()
        with _wire(tmp, client):
            crashed = _CrashAfterStage(crash_stage="verify")
            bad = asyncio.run(crashed.run(
                "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
                deliverables={"report": True, "prd": False}, task_id="contract-resume",
            ))
            assert bad["status"] == "failed", "注入崩溃必须让首跑失败"
            checkpoint = dbmod.get_report_db().load_latest_checkpoint("contract-resume")
            assert checkpoint and checkpoint["stage"] == "verify"

            resumed = MVPWorkflow()
            state = asyncio.run(resumed.run(
                "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
                deliverables={"report": True, "prd": False}, task_id="contract-resume",
                resume=True,
            ))
            assert state["status"] == "completed", state.get("termination_reason")
            assert state.get("_resume_stage") == "verify"
            assert cite in state.get("report_final", "")
    return "crash@verify -> checkpoint -> resume -> completed"


def _check_cancel_has_no_side_effects() -> str:
    """取消后终态 cancelled，且输出目录里没有新报告文件。"""
    with tempfile.TemporaryDirectory(prefix="contract-cancel-") as td:
        tmp = Path(td)
        client, _ = _scripted_client()
        with _wire(tmp, client):
            event = asyncio.Event()
            event.set()
            state = asyncio.run(MVPWorkflow().run(
                "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
                deliverables={"report": True, "prd": False}, task_id="contract-cancel",
                cancel_event=event,
            ))
            assert state["status"] == "cancelled"
            reports = list((tmp / "output").glob("research_report_*.md"))
            assert not reports, f"取消后不得落报告，实际 {reports}"
    return "cancelled; 0 report files"


def _check_evidence_pagination_readback() -> str:
    """折叠指针 paginated 回读，拼回原文后 sha256 与折叠前一致。"""
    content = "契约证据 " + "A" * 5000
    workspace = {}
    registry = build_default_tools(None, workspace)
    index = workspace["_evidence_index"]
    key = index.put(content)["key"]

    chunks, offset, pages = [], 0, 0
    while offset is not None:
        result = asyncio.run(
            registry.execute("read_evidence", {"key": key, "offset": offset, "limit": 2})
        )
        assert result["ok"], result
        chunks.extend(result["data"]["chunks"])
        offset = result["nextOffset"]
        pages += 1

    joined = "".join(chunk["text"] for chunk in chunks)
    assert joined == content
    actual = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    assert actual == index.digest(key)
    return f"{pages} pages; sha256 match"


def _check_read_write_declaration_blocked() -> str:
    """注册表拒绝「只读但声明会写」的工具，合法只读工具可正常注册。"""
    async def _noop():
        return {}

    registry = ToolRegistry()
    try:
        registry.add(Tool(
            name="bad_write", description="should be rejected",
            parameters={"type": "object", "properties": {}}, handler=_noop,
            read_only=True, side_effects="write",
        ))
    except ValueError:
        pass
    else:
        raise AssertionError("read_only + side_effects=write 未被拦截")

    registry.add(Tool(
        name="good_read", description="legal read tool",
        parameters={"type": "object", "properties": {}}, handler=_noop,
        read_only=True, side_effects="read",
    ))
    assert "good_read" in registry.names()
    return "write-on-read-only rejected; legal read accepted"


_CASE_CHECKS = {
    "resume_across_crash": _check_resume_across_crash,
    "cancel_no_side_effects": _check_cancel_has_no_side_effects,
    "evidence_pagination_readback": _check_evidence_pagination_readback,
    "read_write_declaration_blocked": _check_read_write_declaration_blocked,
}


def run_contract(case: dict) -> dict:
    check = _CASE_CHECKS.get(case.get("check"))
    if check is None:
        return {"passed": False, "detail": f"未知契约检查: {case.get('check')!r}"}
    try:
        detail = check()
    except Exception as exc:
        return {"passed": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"passed": True, "detail": detail}
