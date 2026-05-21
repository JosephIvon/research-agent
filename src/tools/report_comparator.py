"""报告对比工具 - 比较两个版本的差异"""
import difflib
from typing import List, Dict, Any, Tuple


def compare_reports(old_report: str, new_report: str) -> Dict[str, Any]:
    """
    对比两份报告，返回差异摘要

    Returns:
        {
            "additions": [...],   # 新增内容
            "deletions": [...],   # 删除内容
            "changes": [...],     # 修改内容 (old -> new)
            "similarity": 0.85,    # 相似度 0-1
            "summary": "报告更新摘要"
        }
    """
    old_lines = old_report.split("\n")
    new_lines = new_report.split("\n")

    differ = difflib.unified_diff(old_lines, new_lines, lineterm="")

    additions = []
    deletions = []
    changes = []

    # Parse diff lines
    for line in differ:
        if line.startswith("+") and not line.startswith("+++"):
            additions.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            deletions.append(line[1:])

    # Find changes (lines that appear in both but at different positions)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    similarity = matcher.ratio()

    return {
        "additions": additions[:20],  # Limit to 20 items
        "deletions": deletions[:20],
        "changes": [],  # Simplified - could do line-by-line
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