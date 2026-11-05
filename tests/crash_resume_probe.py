"""崩溃续跑探针：真实进程被杀后，从 SQLite 检查点重新跑完。

用法：
    uv run python tests/crash_resume_probe.py --trials 10

父进程为每个 trial：
1. 启动子进程跑离线工作流，在指定阶段检查点落盘后写 marker 并挂起；
2. 父进程用 Popen.kill()（Windows TerminateProcess，无 finally/atexit）强杀；
3. 启动新的子进程，带 resume=True 从检查点续跑；
4. 子进程写 result.json，父进程统计 completed 次数。

离线：复用 tests/test_scripted_llm.py 的 FakeWebCrawler + ScriptedLLM，零网络零密钥。
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

STAGES = ["crawl", "extract", "verify", "report", "citation"]


def _wire(tmp_root: Path, task_id: str, stage: str):
    from src.agent.citations import EvidenceStore
    from src.llm import ScriptedLLMClient
    from src.storage import database as dbmod
    from src.workflow import mvp_workflow
    from tests.test_scripted_llm import EXTRACT_ITEMS, TARGET_URL, _FakeWebCrawler

    expected = [dict(item, source_url=TARGET_URL) for item in EXTRACT_ITEMS]
    cite = EvidenceStore.from_extracted_data(expected).evidences[0].citation
    report = f"## 定价\n\n标准版 $49/席位/月 {cite}，企业版 $99/席位/月 {cite}。\n"
    client = ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report],
    })
    mvp_workflow.WebCrawler = _FakeWebCrawler
    mvp_workflow.get_llm_client = lambda *a, **k: client
    mvp_workflow.validate_fetch_url = lambda u: u
    mvp_workflow.validate_fetch_urls = lambda us: list(us)
    dbmod.DATABASE_PATH = str(tmp_root / "reports.db")
    dbmod._report_db = None
    from src.config import settings
    settings.OUTPUT_DIR = str(tmp_root / "output")
    return mvp_workflow, task_id, stage, TARGET_URL


def _run_child(mode: str, tmp_root: Path, task_id: str, stage: str):
    mvp_workflow, task_id, stage, target_url = _wire(tmp_root, task_id, stage)
    from src.workflow.mvp_workflow import MVPWorkflow

    class _StopAfterStage(MVPWorkflow):
        def _save_checkpoint(self, state, stage_name):
            super()._save_checkpoint(state, stage_name)
            if stage_name != stage:
                return
            (tmp_root / "marker.json").write_text(
                json.dumps({"stage": stage_name, "checkpoint": True}), encoding="utf-8"
            )
            # 真实进程被强杀前的“仍在运行”状态：阻塞等待父进程 TerminateProcess。
            while True:
                time.sleep(0.2)

    workflow = _StopAfterStage() if mode == "crash" else MVPWorkflow()
    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=[target_url],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id=task_id,
        resume=(mode == "resume"),
    ))
    (tmp_root / "result.json").write_text(
        json.dumps({"mode": mode, "status": state["status"],
                    "reason": state.get("termination_reason")}, ensure_ascii=False),
        encoding="utf-8",
    )


def _run_trial(index: int, stage: str) -> bool:
    tmp_root = Path(tempfile.mkdtemp(prefix=f"resume-probe-{index}-{stage}-"))
    task_id = f"probe-{index}-{stage}"
    cmd = [sys.executable, str(Path(__file__).resolve()), "--mode"]
    crash = subprocess.Popen(
        cmd + ["crash", "--child", stage, "--root", str(tmp_root), "--task-id", task_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    marker = tmp_root / "marker.json"
    deadline = time.time() + 60
    while not marker.exists() and time.time() < deadline:
        if crash.poll() is not None:
            return False
        time.sleep(0.1)
    if not marker.exists():
        crash.kill()
        return False
    # kill -9 等价：TerminateProcess，无 Python 清理钩子
    crash.kill()
    crash.wait(timeout=10)

    resume = subprocess.run(
        cmd + ["resume", "--child", stage, "--root", str(tmp_root), "--task-id", task_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    result = tmp_root / "result.json"
    if resume.returncode != 0 or not result.exists():
        return False
    payload = json.loads(result.read_text(encoding="utf-8"))
    return payload.get("status") == "completed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--child", default="", help="child mode: crash stage name")
    parser.add_argument("--mode", choices=["crash", "resume"], default="crash")
    parser.add_argument("--root", default="")
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()

    if args.child:
        _run_child(args.mode, Path(args.root), args.task_id, args.child)
        return

    trials = [(i, STAGES[i % len(STAGES)]) for i in range(args.trials)]
    ok = 0
    for index, stage in trials:
        passed = _run_trial(index, stage)
        print(f"trial {index + 1:02d} crash@{stage:9s} -> {'PASS' if passed else 'FAIL'}")
        ok += int(passed)
    print(f"resume success: {ok}/{len(trials)}")
    return 0 if ok == len(trials) else 1


if __name__ == "__main__":
    raise SystemExit(main())
