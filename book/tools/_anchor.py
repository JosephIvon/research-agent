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


if __name__ == "__main__":
    print(f"锚点: {ANCHOR_LABEL}")
    print(f"仓:   {REPO_ROOT}")
    print(f"卷:   {VOLUME_NAME}")
    print(f"提交存在: {'是' if commit_exists(ANCHOR_COMMIT) else '否（未 fetch？）'}")
