"""报告落盘与命名：任务级固定文件名、原子写、草稿读写。

从 mvp_workflow 抽出的纯叶子职责：只依赖 task_id、正文与存储层，
不读工作流的共享 state，也不依赖工作流实例。
"""
import hashlib
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def report_filename_for_task(task_id: str) -> str:
    """同一任务固定文件名：崩溃在“文件已写、状态未落库”窗口后续跑只覆盖不新增。

    文件名映射先于正文落盘写库，因此进程被杀后仍能复用同一个名字。
    库不可用时退回按任务后缀的秒级名（仍然同秒不同任务不撞车）。
    """
    try:
        from src.storage import get_report_db

        existing = get_report_db().get_task_draft(task_id, "report_filename")
        if existing:
            return existing
    except Exception as exc:  # 缓存失败不得阻断报告落盘
        logger.warning("读取报告文件名映射失败: %s", exc)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = hashlib.sha256(task_id.encode()).hexdigest()[:8]
    filename = f"research_report_{timestamp}_{suffix}.md"
    try:
        from src.storage import get_report_db

        get_report_db().save_task_draft(task_id, "report_filename", filename)
    except Exception as exc:
        logger.warning("保存报告文件名映射失败: %s", exc)
    return filename


def save_report(content: str, task_id: Optional[str] = None, *, atomic: bool = True) -> str:
    """保存报告到本地文件。

    默认按 task 固定文件名 + 临时文件原子替换：续跑重写同一路径，
    不会出现“崩溃前一份、续跑后两份”的重复报告。
    `atomic=False` 回到旧的“时间戳命名 + 直接写”。
    """
    from src.config.settings import OUTPUT_DIR

    if not task_id or not atomic:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{task_id[:8]}" if task_id else ""
        filename = f"research_report_{timestamp}{suffix}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    filename = report_filename_for_task(task_id)
    filepath = os.path.join(OUTPUT_DIR, filename)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, filepath)
    return filepath


def load_report_draft(task_id: Optional[str], *, enabled: bool = True) -> Optional[str]:
    """按任务读取报告草稿；未启用或没有任务时返回 None。"""
    if not task_id or not enabled:
        return None
    try:
        from src.storage import get_report_db

        return get_report_db().get_task_draft(task_id, "report")
    except Exception as exc:
        logger.warning("读取报告草稿失败: %s", exc)
        return None


def save_report_draft(task_id: Optional[str], content: str, *, enabled: bool = True) -> None:
    """按任务保存报告草稿；未启用、没有任务或正文为空时静默跳过。"""
    if not task_id or not content or not enabled:
        return
    try:
        from src.storage import get_report_db

        get_report_db().save_task_draft(task_id, "report", content)
    except Exception as exc:
        logger.warning("保存报告草稿失败: %s", exc)
