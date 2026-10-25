#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产数字门：ASSETS.md 里的数字必须与真实文件一致，且不许漏登记。

为什么单设一道门：手写的"本卷 30 文件 7000 行"是最容易腐坏的一句话——
加一章忘了改合计、删一节忘了改行数，读者拿到的规模描述就是假的。
（本仓在 README 上已经吃过一次这个亏：写着 14 条评测 case，夹具早已 20 条。）

三件事
------
1. **行数对**：表里每个文件的行数 = 真实行数。
2. **合计对得起**：`合计` 行 = 各行之和。
3. **没有漏登记**：`book/` 下的 `.md`/`.py` 文件都必须出现在表里
   （`book/ASSETS.md` 自身豁免——登记自己会陷入"改数字就改数字"的循环）。

用法
----
    python book/tools/verify_book_numbers.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _anchor import BOOK_DIR  # noqa: E402

ASSETS = BOOK_DIR / "ASSETS.md"
ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([\d,]+)\s*\|\s*$")
TOTAL_RE = re.compile(r"^\|\s*\*\*合计\*\*\s*\|\s*\*\*([\d,]+)\*\*\s*\|\s*$")


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def main() -> int:
    if not ASSETS.is_file():
        print("  ✗ 缺 book/ASSETS.md")
        return 1

    rows: dict[str, int] = {}
    total_declared: int | None = None
    for line in ASSETS.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows[m.group(1)] = _num(m.group(2))
            continue
        m = TOTAL_RE.match(line.strip())
        if m:
            total_declared = _num(m.group(1))

    problems: list[str] = []

    # 1) 行数对
    for rel, declared in rows.items():
        path = (BOOK_DIR.parent / rel).resolve() if rel.startswith("book/") else (BOOK_DIR / rel)
        if not path.is_file():
            problems.append(f"登记了不存在的文件: {rel}")
            continue
        actual = len(path.read_text(encoding="utf-8").splitlines())
        if actual != declared:
            problems.append(f"{rel}: 登记 {declared} 行，实际 {actual} 行")

    # 2) 合计
    if total_declared is None:
        problems.append("没有 `**合计**` 行")
    else:
        s = sum(rows.values())
        if s != total_declared:
            problems.append(f"合计登记 {total_declared}，各行之和 {s}")

    # 3) 漏登记
    on_disk = {
        str(p.relative_to(BOOK_DIR.parent)).replace("\\", "/")
        for p in BOOK_DIR.rglob("*")
        if p.is_file() and p.suffix in (".md", ".py") and "__pycache__" not in p.parts
    }
    on_disk.discard("book/ASSETS.md")  # 自身豁免（见文件头说明）
    missing = sorted(on_disk - set(rows))
    extra = sorted(set(rows) - on_disk)
    for m in missing:
        problems.append(f"漏登记: {m}（在 book/ 里存在，但表里没有）")
    for e in extra:
        problems.append(f"多余登记: {e}（表里有，磁盘上没有）")

    print(f"  登记 {len(rows)} 个文件，声明合计 {total_declared}")
    if problems:
        print("  ✗ 失败：")
        for p in problems:
            print("    - " + p)
        return 1
    print(f"  ✓ 行数、合计（{sum(rows.values())}）、覆盖率全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
