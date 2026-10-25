#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书的结构门禁：每章必须有「能回答什么 / 可验证主张 / 证据与坐标」三节。

为什么要有这道门：书不是散文集。三节缺任何一节，那一章就退化成"看着挺有道理"——
  · 缺「能回答什么」→ 读者不知道读完能拿去干什么；
  · 缺「可验证主张」→ 一堆形容词，没有一条能被第三方当场核；
  · 缺「证据与坐标」→ 主张没有落点，`audit_coords.py` 也就无从下手。

用法
----
    python book/tools/check_book_structure.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _anchor import BOOK_DIR  # noqa: E402

REQUIRED = {
    "能回答什么": re.compile(r"^##\s+本章能回答什么", re.M),
    "可验证主张": re.compile(r"^##\s+可验证主张", re.M),
    "证据与坐标": re.compile(r"^##\s+证据与坐标", re.M),
}
CHAPTER_DIR = BOOK_DIR / "chapters"
# 章不多不少：改章节数要同时改这里与 OUTLINE，防止"写了但没登记"
EXPECTED_CHAPTERS = 9


def main() -> int:
    problems: list[str] = []

    if not CHAPTER_DIR.is_dir():
        print(f"  ✗ 缺章节目录: {CHAPTER_DIR}")
        return 1

    chapters = sorted(p for p in CHAPTER_DIR.glob("ch*.md") if p.is_file())
    if len(chapters) != EXPECTED_CHAPTERS:
        problems.append(
            f"章节数 {len(chapters)}，约定 {EXPECTED_CHAPTERS}（改章节数请同步 OUTLINE 与本文件）"
        )

    for path in chapters:
        text = path.read_text(encoding="utf-8")
        for name, pat in REQUIRED.items():
            if not pat.search(text):
                problems.append(f"{path.name}: 缺「## {name}」")
        # 主张必须挂坐标：本章里至少有一条可解析坐标，否则"可验证"是空话
        if not re.search(r"research-agent@[0-9a-f]{7,40}\s+\S+:\d+", text):
            problems.append(f"{path.name}: 「可验证主张」一节没有挂任何坐标")

    outline = BOOK_DIR / "OUTLINE.md"
    if not outline.is_file():
        problems.append("缺 OUTLINE.md")
    else:
        listed = len(re.findall(r"^\|\s*\d+\s*\|", outline.read_text(encoding="utf-8"), re.M))
        if listed != EXPECTED_CHAPTERS:
            problems.append(f"OUTLINE.md 登记了 {listed} 章，约定 {EXPECTED_CHAPTERS}")

    if problems:
        print("  ✗ 结构问题：")
        for p in problems:
            print("    - " + p)
        return 1
    print(f"  ✓ 结构完整：{len(chapters)} 章，每章三节齐全且主张挂坐标；OUTLINE 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
