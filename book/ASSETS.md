# 本卷资产登记（ASSETS）

> 这一页的数字**由门禁核对**，不是手写的自我描述。
> `book/tools/verify_book_numbers.py` 会逐行核：文件在不在、行数对不对、合计加不加得起来、
> 有没有漏登记。任何一个对不上，CI 红。

## 一、卷宗规模

| 文件 | 行数 |
|---|---|
| `book/README.md` | 45 |
| `book/前言.md` | 67 |
| `book/OUTLINE.md` | 29 |
| `book/chapters/ch01-定位与边界.md` | 86 |
| `book/chapters/ch02-双模式编排.md` | 105 |
| `book/chapters/ch03-预算与PARTIAL.md` | 113 |
| `book/chapters/ch04-引用校验闭环.md` | 118 |
| `book/chapters/ch05-提示注入纵深.md` | 119 |
| `book/chapters/ch06-离线评测与门禁.md` | 108 |
| `book/chapters/ch07-脚本化LLM的陷阱.md` | 165 |
| `book/chapters/ch08-MCP与防漂移.md` | 104 |
| `book/chapters/ch09-诚实账.md` | 108 |
| `book/crops/裁剪版.md` | 93 |
| `book/尾声.md` | 47 |
| `book/tools/_anchor.py` | 98 |
| `book/tools/audit_coords.py` | 145 |
| `book/tools/check_book_structure.py` | 73 |
| `book/tools/verify_crops_verbatim.py` | 81 |
| `book/tools/verify_book_numbers.py` | 99 |
| **合计** | **1803** |

> `book/ASSETS.md`（也就是本页）**不登记自己**：登记表里写自己的行数会陷入
> "改一个数字就改变这个数字"的循环。门禁对此有显式豁免，并把豁免写在这里。

## 二、四道门

| 门 | 管什么 | 不通过会怎样 |
|---|---|---|
| `audit_coords.py` | 每条坐标都能在锚点提交里读到那一行 | 越界 / 文件不存在 / 同名歧义 / 孤立简写 → 红 |
| `check_book_structure.py` | 每章三节齐全，章数与 OUTLINE 一致 | 缺节或缺章 → 红 |
| `verify_crops_verbatim.py` | 摘录逐字可回溯到章节 | 摘录被改写 → 红 |
| `verify_book_numbers.py` | 本页的数字与真实文件一致 | 行数、合计、覆盖率任一不符 → 红 |

四道门在 CI 里是**四个独立步骤**（`.github/workflows/ci.yml` 的 `book` 作业），
任一步失败整条红。

## 三、锚点

- 锚点：`research-agent@ef7204f`（定义在 `book/tools/_anchor.py`，全书唯一来源）
- 停机点 tag：`book-anchor-2026-10-25`
- 改锚只改一处，然后跑四道门；**锚点冻结不等于与代码同步**，改了代码要重锚。

## 四、变更记录

- 2026-10-25 起骨架：锚点工具、坐标审计、结构门、9 章大纲与第一章。
- 2026-10-25 第 2–9 章写完；坐标审计补上三种简写写法（坐标数 88 → 174，此前的简写从不进检查）。
- 2026-10-25 加前言、尾声、裁剪件；裁剪门与数字门接入 CI；本页开始受数字门约束。
