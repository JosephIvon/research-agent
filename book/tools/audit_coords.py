#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""坐标审计：书里每条 `research-agent@<sha> <路径>:<行号>` 都必须**真的读得到**。

这道门回答的问题只有一个：**读者按书里的坐标去 checkout，能不能看到书里说的那一行。**
它**不**回答"那一行的内容对不对"——那是写作时的责任，机器只能盯住"可解析、未越界"。

为什么单独一道门：坐标写错是这本书最容易犯、也最难被读者原谅的错。
四个学科（其他三卷）的共同教训是——**"范围合法"不等于"指对了目标"**，
所以这道门逐条 `git show <sha>:<path>` 真读，而不是只做正则匹配。

用法
----
    python book/tools/audit_coords.py            # 逐条核（默认）
    python book/tools/audit_coords.py --print    # 顺带把读到的那一行打出来
    python book/tools/audit_coords.py --list     # 只列坐标，不核
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _anchor import (  # noqa: E402
    ANCHOR_COMMIT, ANCHOR_LABEL, BOOK_DIR, REPO_NAME, commit_exists, read_at, resolve_path,
)

# `research-agent@ef7204f src/agent/citations.py:168` 或 `research-agent@ef7204f path:168-175`
COORD_RE = re.compile(
    r"`?" + re.escape(REPO_NAME) + r"@([0-9a-f]{7,40})\s+([^\s`<>\[\]]+):(\d+)(?:-(\d+))?`?"
)

# 简写坐标：只写 `路径:行号`（不带 `repo@sha` 前缀），默认按当前锚点解释。
# 为什么要认它：正文里同一句后面常常接着一个简写（"`…ci.yml:300`（作业名）、`:317`"），
# 如果工具只认完整形式，这些简写就**根本不会被统计**——
# 于是"漏掉的行"从来没进过检查范围，门禁照样全绿。
# 这正是这本卷反复讲的一种失败模式：只核对已出现的行，等于悄悄放过没出现的行。
BARE_RE = re.compile(
    r"(?<![@\w/])((?:[\w.\-]+/)*[\w.\-]+\.(?:py|md|json|ya?ml|toml|txt|html|js|ts|cfg|ini)):(\d+)(?:-(\d+))?"
)

# 简写坐标：`` `:317` `` 这种只有行号、路径继承**同一行里前一个坐标**的写法。
# 正文里很常见（"`…ci.yml:300`（作业名）、`:317`（跑门禁）"），
# 如果工具不认它，这些行号就永远不在检查范围内——同 BARE_RE 的理由。
SHORTHAND_RE = re.compile(r"`:(\d+)(?:-(\d+))?`")


def book_files() -> list[Path]:
    files = sorted(p for p in BOOK_DIR.rglob("*.md") if p.is_file())
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true", help="打印读到的那一行")
    ap.add_argument("--list", action="store_true", help="只列坐标")
    args = ap.parse_args()

    if not commit_exists(ANCHOR_COMMIT):
        print(f"  !! 锚点提交 {ANCHOR_COMMIT} 不在本仓（先 git fetch --tags）")
        return 1

    total = ok = 0
    problems: list[str] = []
    seen_shas: set[str] = set()

    def check(sha: str, rel: str, start: int, end, where: str) -> None:
        nonlocal total, ok
        total += 1
        seen_shas.add(sha)
        if args.list:
            print(f"  [{where}] {sha} {rel}:{start}{'-' + end if end else ''}")
            return
        real, candidates = resolve_path(sha, rel)
        if real is None:
            if candidates:
                problems.append(
                    f"{where}  {sha} {rel} —— 有 {len(candidates)} 个同名文件，请写全路径: "
                    + "、".join(candidates[:4])
                )
            else:
                problems.append(f"{where}  {sha} {rel} —— 该提交里没有这个文件")
            return
        rel = real
        lines = read_at(sha, rel)
        if lines is None:
            problems.append(f"{where}  {sha} {rel} —— 该提交里没有这个文件")
            return
        last = int(end) if end else start
        if start < 1 or last > len(lines):
            problems.append(
                f"{where}  {sha} {rel}:{start}-{last} —— 越界（该文件共 {len(lines)} 行）"
            )
            return
        ok += 1
        if args.show:
            print(f"  [{where}] {rel}:{start}  {lines[start - 1].strip()[:96]}")

    for path in book_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            where = f"{path.name}:{lineno}"
            spans: list[tuple[int, int]] = []
            inherited: tuple[str, str] | None = None
            for m in COORD_RE.finditer(line):
                spans.append(m.span())
                check(m.group(1), m.group(2), int(m.group(3)), m.group(4), where)
                inherited = (m.group(1), m.group(2))
            for m in BARE_RE.finditer(line):
                # 跳过已经被完整坐标覆盖的那一段，避免同一条坐标被数两遍
                if any(a <= m.start() < b for a, b in spans):
                    continue
                check(ANCHOR_COMMIT, m.group(1), int(m.group(2)), m.group(3), where)
                inherited = (ANCHOR_COMMIT, m.group(1))
            for m in SHORTHAND_RE.finditer(line):
                if any(a <= m.start() < b for a, b in spans):
                    continue
                if inherited is None:
                    problems.append(
                        f"{where}  `:{m.group(1)}` 是简写坐标，但同一行里没有可继承的完整坐标"
                    )
                    total += 1
                    continue
                check(inherited[0], inherited[1], int(m.group(1)), m.group(2), where)

    if args.list:
        print(f"\n  坐标 {total} 条")
        return 0

    print(f"\n  锚点 {ANCHOR_LABEL}")
    if seen_shas - {ANCHOR_COMMIT}:
        print(f"  ⚠ 书里出现了**非当前锚点**的提交: {sorted(seen_shas - {ANCHOR_COMMIT})}")
    print(f"  坐标 {total} 条：可解析 {ok} ｜ 失败 {len(problems)}")
    if problems:
        print("\n  失败清单：")
        for p in problems:
            print("    ✗ " + p)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
