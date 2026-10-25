#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁剪件校验：摘录必须是**逐字摘录**，不能是第二份正文。

为什么单设一道门：裁剪版最容易腐坏的方式不是写错字，而是**慢慢被重写**——
今天顺手改通一句、明天补一句过渡，两个月后它就成了与正文平行的另一个版本，
读者不知道哪个算数。

规则
----
`book/crops/*.md` 里每个 `<!-- crop: 章节路径 -->` 段之后，以 `>` 开头的行都是摘录；
去掉引用前缀后，必须能在该章节里**原样找到**（子串匹配，长度下限 15 字，防抖机灵）。

用法
----
    python book/tools/verify_crops_verbatim.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _anchor import BOOK_DIR  # noqa: E402

CROP_MARK = re.compile(r"^<!--\s*crop:\s*(.+?)\s*-->$")
MIN_LEN = 15


def main() -> int:
    problems: list[str] = []
    checked = 0
    crops = sorted((BOOK_DIR / "crops").glob("*.md")) if (BOOK_DIR / "crops").is_dir() else []
    if not crops:
        print("  ✗ 没有裁剪件（book/crops/*.md）")
        return 1

    for crop in crops:
        target: Path | None = None
        target_text = ""
        for lineno, line in enumerate(crop.read_text(encoding="utf-8").splitlines(), 1):
            m = CROP_MARK.match(line.strip())
            if m:
                rel = m.group(1)
                target = BOOK_DIR / rel
                if not target.is_file():
                    problems.append(f"{crop.name}:{lineno} 摘录来源不存在: {rel}")
                    target, target_text = None, ""
                else:
                    target_text = target.read_text(encoding="utf-8")
                continue
            if not line.startswith(">"):
                continue
            quote = line[1:].strip()
            if not quote:
                continue  # 空引用行当作分隔
            checked += 1
            if target is None:
                problems.append(f"{crop.name}:{lineno} 摘录之前没有 `<!-- crop: ... -->` 声明来源")
                continue
            if len(quote) < MIN_LEN:
                problems.append(f"{crop.name}:{lineno} 摘录过短（{len(quote)} 字）：{quote[:20]}")
                continue
            if quote not in target_text:
                problems.append(
                    f"{crop.name}:{lineno} 摘录在 {target.name} 里找不到（可能已失配或被改写）：{quote[:44]}…"
                )

    print(f"  摘录 {checked} 段")
    if problems:
        print("  ✗ 失败：")
        for p in problems:
            print("    - " + p)
        return 1
    print("  ✓ 全部逐字可回溯到对应章节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
