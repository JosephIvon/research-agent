"""报告对比工具 - 比较两个版本的差异"""
import difflib
from typing import List, Dict, Any, Tuple


def compare_reports(old_report: str, new_report: str) -> Dict[str, Any]:
    """
    对比两份报告，返回差异摘要

    ``changes`` 由 difflib.SequenceMatcher.get_opcodes() 的 ``replace`` 操作码生成：
    每一项表示"同一处内容被改写"，形如 ``{"old": "旧片段", "new": "新片段"}``，
    多行片段以换行拼接。纯新增（``insert``）与纯删除（``delete``）只计入
    additions / deletions，不重复出现在 changes 中。两份报告完全相同时 changes 为空列表。

    Returns:
        {
            "additions": [...],   # 新增行（已去掉行首 "+"）
            "deletions": [...],   # 删除行（已去掉行首 "-"）
            "changes": [{"old": "...", "new": "..."}],  # 修改内容 (old -> new)，最多20条
            "similarity": 0.85,    # 相似度 0-1
            "summary": "报告更新摘要"
        }
    """
    old_lines = old_report.split("\n")
    new_lines = new_report.split("\n")

    differ = difflib.unified_diff(old_lines, new_lines, lineterm="")

    additions = []
    deletions = []

    # Parse diff lines
    for line in differ:
        if line.startswith("+") and not line.startswith("+++"):
            additions.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            deletions.append(line[1:])

    # 行级修改检测：只有 replace 操作码代表"同一处内容被改写"
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    similarity = matcher.ratio()

    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        changes.append({
            "old": "\n".join(old_lines[i1:i2]),
            "new": "\n".join(new_lines[j1:j2]),
        })

    return {
        "additions": additions[:20],  # Limit to 20 items
        "deletions": deletions[:20],
        "changes": changes[:20],
        "similarity": round(similarity, 3),
        "summary": _generate_summary(similarity, len(additions), len(deletions)),
    }


def _generate_summary(similarity: float, additions: int, deletions: int) -> str:
    if similarity >= 0.95:
        return "报告几乎无变化"
    elif similarity >= 0.8:
        return f"报告有小幅更新，新增约{additions}行，删除约{deletions}行"
    else:
        return f"报告有较大变化，新增约{additions}行，删除约{deletions}行"