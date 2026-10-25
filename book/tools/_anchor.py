#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全书的**唯一**锚点定义。改锚只改这里。

书里每条事实性主张都挂坐标：`research-agent@<commit> <路径>:<行号>`。
`<commit>` 就是下面这一个提交——**不是**"最新 main"，也不是"写作当天的 HEAD"：
读者 clone 下来能 checkout 到它，书里的每一行才读得到。

改锚的规矩（其他几卷踩过的坑）：
  · 改了代码就要重锚，否则坐标指向旧内容——"锚点冻结"不等于"与代码同步"；
  · 重锚是**显式动作**：改这个文件 → 跑 `python book/tools/audit_coords.py` → 全绿才算完；
  · 历史记载里的旧 SHA 保持原样（那是当时的事），只有**活坐标**跟着改。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# ---- 锚点（改这一行 = 全书重锚）----
ANCHOR_COMMIT = "ef7204f"
ANCHOR_LABEL = f"research-agent@{ANCHOR_COMMIT}"

REPO_NAME = "research-agent"
VOLUME_NAME = "《AI 产品工程卷》"
BOOK_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BOOK_DIR.parent


def git(*args: str, check: bool = True) -> str:
    """在仓根跑 git，统一 UTF-8 解码（中文路径在 Windows 上默认会炸）。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败: {proc.stderr.decode('utf-8', 'replace')[:400]}"
        )
    return proc.stdout.decode("utf-8", "replace")


def read_at(commit: str, path: str) -> list[str] | None:
    """读某个提交里的某个文件，返回行列表；文件不存在返回 None。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{commit}:{path}"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").splitlines()


def commit_exists(commit: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-t", commit],
        capture_output=True,
    )
    return proc.returncode == 0 and proc.stdout.decode().strip() == "commit"


_TREE_CACHE: dict[str, list[str]] = {}


def tree_files(commit: str) -> list[str]:
    """某个提交里的全部文件路径（缓存：一次审计会反复用）。"""
    if commit not in _TREE_CACHE:
        out = git("-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", commit)
        _TREE_CACHE[commit] = [ln for ln in out.splitlines() if ln]
    return _TREE_CACHE[commit]


def resolve_path(commit: str, rel: str) -> tuple[str | None, list[str]]:
    """把书里写的路径解析成提交里真实存在的路径。

    书正文里常写短路径（`injection.py:22`），读者能懂，机器不该猜。
    规则：**后缀唯一匹配**才算解析成功；重名就当错——猜一个等于给"指错了"开门。

    返回 (真实路径 或 None, 候选列表)。
    """
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        # 只剥**前缀** `./`，不能写 lstrip("./")——那会把 `.github/` 的点也吃掉
        rel = rel[2:]
    files = tree_files(commit)
    if rel in files:
        return rel, [rel]
    candidates = [f for f in files if f.endswith("/" + rel)]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


if __name__ == "__main__":
    print(f"锚点: {ANCHOR_LABEL}")
    print(f"仓:   {REPO_ROOT}")
    print(f"卷:   {VOLUME_NAME}")
    print(f"提交存在: {'是' if commit_exists(ANCHOR_COMMIT) else '否（未 fetch？）'}")
