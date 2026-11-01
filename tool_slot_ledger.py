"""名额账：把"8 个名额给了谁、为什么、挤掉了谁"逐轮算清楚（纯观测）。

为什么先做它（而不是先上 MMR）：
名额分配是**结构性问题**——现在 8 个名额几乎全给了头部产品
（第九轮实测：留出集 keep_L3 只有 0.0377，名册集 0.6994）。
在不知道"名额到底被谁按什么规则拿走"之前改分配算法，等于盲改。

这个工具**只读已有 artifact**（`output/recall_*.json`），不联网、不改管线行为，
可以直接在旧产物上复算（第九轮、第八轮都行）。

用法：
    python tool_slot_ledger.py output/recall_20261101_101613.json
    python tool_slot_ledger.py output/recall_20261101_101613.json --golden-split heldout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT))

from src.tools import recall_metrics as rm  # noqa: E402
from src.workflow.site_types import normalized_brand  # noqa: E402

GOLDEN_FILE = ROOT / "evals" / "discovery_golden" / "track_universe.json"


def _host(url: str) -> str:
    return rm.host_key(url)


def _tokens(name: str) -> set:
    """名字的词元（中文按字、英文按词）。用于"同赛道/同质"的粗粒度比较。"""
    text = (name or "").lower()
    cn = set(re.findall(r"[一-鿿]", text))
    en = set(re.findall(r"[a-z0-9]{2,}", text))
    return cn | en


def _sim(a: str, b: str) -> float:
    """两个产品名的粗粒度相似度（Jaccard 词元）。

    注意：**这是代理指标**，不是语义相似度。真正的 MMR 相似度应该用
    名称 + 描述 + 赛道维度（实现阶段再定），这里先用名字做可复算的近似。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _nominated(brand: str, nominated: set) -> bool:
    return bool(brand) and brand in nominated


def build_ledger(run: Dict[str, Any], golden: rm.GoldenSet,
                 channel_urls: Optional[set] = None) -> Dict[str, Any]:
    """算一轮的名额账：每个名额给了谁、依据是什么、谁被挤掉。

    优先用**管线自己记的**名额账（`run["slot_ledger"]` ✓，第十轮起有 ✓）；
    旧产物没有这个字段时，按 recorded 的 scored/selected **离线重建** ✓
    （第九轮就是这么算出来的 ✓ —— 见 docs/slot-allocation-plan.md §2 ✓）。
    """
    channel_urls = channel_urls or set()
    selection = run.get("selection") or {}
    max_count = int(selection.get("max_count") or 8)
    min_score = float(selection.get("min_admission_score") or 0.0)
    must_include = [t for t in (selection.get("must_include") or []) if t]
    plan = run.get("plan") or {}
    nominated = {normalized_brand(n) for n in (plan.get("competitor_candidates") or [])}

    recorded = run.get("slot_ledger") or {}
    if recorded.get("slots"):
        slots = []
        for slot in recorded["slots"]:
            match = golden.match(str(slot.get("url") or ""))
            slots.append({
                "slot": slot.get("slot"),
                "url": slot.get("url"),
                "name": slot.get("name"),
                "host": slot.get("host"),
                "score": slot.get("score"),
                "reasons": list(slot.get("reasons") or []),
                "source": slot.get("source") or "发现",
                "audit_flag": slot.get("audit_flag"),
                "golden": (match.get("name") if match else None),
                "golden_split": (match.get("split") if match else None),
            })
        displaced = []
        for item in (recorded.get("displaced") or []):
            match = golden.match(str(item.get("url") or ""))
            displaced.append({
                "url": item.get("url"),
                "name": item.get("name"),
                "score": item.get("score"),
                "why": item.get("why"),
                "source": item.get("source") or "发现",
                "golden": (match.get("name") if match else None),
                "golden_split": (match.get("split") if match else None),
            })
        return {
            "subject": run.get("subject"),
            "style": run.get("style"),
            "attempt": run.get("attempt"),
            "max_count": recorded.get("max_count") or max_count,
            "min_score": recorded.get("min_score") or min_score,
            "must_include_tokens": recorded.get("must_include") or must_include,
            "must_include_hit_count": recorded.get("must_include_hit") or 0,
            "slots": slots,
            "displaced": displaced,
            "slot_source_counter": Counter(s["source"] for s in slots),
            "golden_in_slots": [s["golden"] for s in slots if s["golden"]],
        }

    selected = list(run.get("selected") or [])
    scored = list(run.get("scored") or [])
    selected_urls = {s.get("url") for s in selected}

    slots: List[Dict[str, Any]] = []
    for i, item in enumerate(selected, 1):
        name = str(item.get("name") or "")
        brand = normalized_brand(name)
        match = golden.match(str(item.get("url") or ""))
        slots.append({
            "slot": i,
            "url": item.get("url"),
            "name": name,
            "host": _host(str(item.get("url") or "")),
            "score": item.get("score"),
            "reasons": list(item.get("reasons") or []),
            "source": ("C3目录页通道" if item.get("url") in channel_urls
                       else "模型提名" if _nominated(brand, nominated)
                       else "搜索"),
            "golden": (match.get("name") if match else None),
            "golden_split": (match.get("split") if match else None),
        })

    # 被挤掉的：过了闸门但没拿到名额的
    displaced: List[Dict[str, Any]] = []
    selected_hosts = {s.get("host") for s in slots}
    for item in scored:
        url = str(item.get("url") or "")
        if url in selected_urls:
            continue
        if not item.get("gate_passed", True):
            continue
        score = float(item.get("score") or 0.0)
        host = _host(url)
        if host and host in selected_hosts:
            why = "同一站点重复"
        elif score < min_score:
            why = f"分数低于地板 {min_score}"
        else:
            why = "名额已满"
        match = golden.match(url)
        displaced.append({
            "url": url,
            "name": item.get("name"),
            "score": score,
            "why": why,
            "golden": (match.get("name") if match else None),
            "golden_split": (match.get("split") if match else None),
            "source": ("C3目录页通道" if url in channel_urls
                       else "模型提名" if _nominated(normalized_brand(str(item.get("name") or "")), nominated)
                       else "搜索"),
        })

    return {
        "subject": run.get("subject"),
        "style": run.get("style"),
        "attempt": run.get("attempt"),
        "max_count": max_count,
        "min_score": min_score,
        "must_include_tokens": must_include,
        "must_include_hit_count": sum(
            1 for s in slots if any(tok and tok in str(s.get("name") or "") for tok in must_include)),
        "slots": slots,
        "displaced": displaced,
        "slot_source_counter": Counter(s["source"] for s in slots),
        "golden_in_slots": [s["golden"] for s in slots if s["golden"]],
    }


def aggregate(ledgers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把多轮的名额账汇总成"名额都给了谁"的全局画像。"""
    source_counter: Counter = Counter()
    golden_counter: Counter = Counter()
    split_counter: Counter = Counter()
    names: Counter = Counter()
    n_runs = len(ledgers)
    for led in ledgers:
        source_counter.update(led["slot_source_counter"])
        for s in led["slots"]:
            names[s["name"]] += 1
            if s["golden"]:
                golden_counter[s["golden"]] += 1
                split_counter[s["golden_split"] or "未标注"] += 1
    return {
        "runs": n_runs,
        "slots_total": sum(source_counter.values()),
        "source_share": {k: f"{v} ({v / max(1, sum(source_counter.values())):.1%})"
                         for k, v in source_counter.most_common()},
        "golden_in_slots": golden_counter.most_common(),
        "golden_split_share": dict(split_counter),
        "top_names": names.most_common(15),
    }


def render_markdown(ledgers: List[Dict[str, Any]], agg: Dict[str, Any],
                    golden_label: str) -> str:
    out: List[str] = []
    out.append("# 名额账：8 个名额给了谁")
    out.append("")
    out.append(f"- 金标准口径：{golden_label}")
    out.append(f"- 轮次：{agg['runs']} 轮，共 {agg['slots_total']} 个名额")
    out.append("")
    out.append("## 1. 名额的来源构成（所有轮次合计）")
    out.append("")
    out.append("| 名额来源 | 数量 | 占比 |")
    out.append("|---|---|---|")
    for k, v in agg["source_share"].items():
        out.append(f"| {k} | {v.split(' ')[0]} | {v.split(' ')[1]} |")
    out.append("")
    out.append("## 2. 进了名额的金标准产品（按出现次数）")
    out.append("")
    if agg["golden_in_slots"]:
        out.append("| 产品 | 出现轮次 |")
        out.append("|---|---|")
        for name, cnt in agg["golden_in_slots"]:
            out.append(f"| {name} | {cnt} |")
    else:
        out.append("（无）")
    out.append("")
    out.append("## 3. 名额里的金标准产品按 split 分布")
    out.append("")
    out.append("| split | 次数 |")
    out.append("|---|---|")
    for k, v in agg["golden_split_share"].items():
        out.append(f"| {k} | {v} |")
    out.append("")
    out.append("## 4. 拿到名额最多的产品名")
    out.append("")
    out.append("| 产品名 | 出现轮次 |")
    out.append("|---|---|")
    for name, cnt in agg["top_names"]:
        out.append(f"| {name} | {cnt} |")
    out.append("")
    out.append("## 5. 逐轮明细")
    out.append("")
    for led in ledgers:
        out.append(f"### {led['subject']}/{led['style']} 第 {led['attempt']} 次")
        out.append("")
        out.append(f"must_include 命中名额数：{led['must_include_hit_count']}"
                   f"（must_include = {led['must_include_tokens']}）")
        out.append("")
        out.append("| 名额 | 产品 | 域名 | 分数 | 来源 | 理由 | 金标准命中 |")
        out.append("|---|---|---|---|---|---|---|")
        for s in led["slots"]:
            out.append(f"| {s['slot']} | {s['name']} | {s['host']} | {s['score']} | "
                       f"{s['source']} | {'；'.join(s['reasons'])} | "
                       f"{s['golden'] or '—'} |")
        out.append("")
        out.append(f"被挤掉的（{len(led['displaced'])} 个）：")
        out.append("")
        out.append("| 产品 | 分数 | 原因 | 来源 | 金标准命中 |")
        out.append("|---|---|---|---|---|")
        for d in led["displaced"][:20]:
            out.append(f"| {d['name']} | {d['score']} | {d['why']} | {d['source']} | "
                       f"{d['golden'] or '—'} |")
        if len(led["displaced"]) > 20:
            out.append(f"| … | | 其余 {len(led['displaced']) - 20} 个略 | | |")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="名额账（纯观测，不联网）")
    parser.add_argument("artifact", help="output/recall_*.json")
    parser.add_argument("--golden", default=str(GOLDEN_FILE))
    parser.add_argument("--golden-split", default=None,
                        choices=["heldout", "registry"],
                        help="只看某一半金标准")
    parser.add_argument("--out", default=None, help="Markdown 输出路径（默认写到 artifact 同目录）")
    args = parser.parse_args()

    data = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    runs = data.get("runs") or []
    golden = rm.GoldenSet.from_file(str(args.golden), split=args.golden_split)
    # 通道自证条目不进分母（与主报告同一把尺）
    pages = sorted({p for r in runs for p in (r.get("channel_pages") or [])})
    if pages:
        golden = golden.exclude_by_source(pages)

    ledgers = []
    for run in runs:
        channel_urls = set(run.get("channel_candidates") or [])
        ledgers.append(build_ledger(run, golden, channel_urls))
    agg = aggregate(ledgers)
    label = f"`{Path(args.golden).name}`" + (f" · {args.golden_split}" if args.golden_split else "")
    md = render_markdown(ledgers, agg, label)
    out_path = Path(args.out) if args.out else Path(args.artifact).with_name(
        Path(args.artifact).stem + "_slot_ledger.md")
    out_path.write_text(md, encoding="utf-8")
    print(f"名额账：{out_path.resolve()}")
    print(f"  轮次 {agg['runs']}，名额 {agg['slots_total']} 个")
    for k, v in agg["source_share"].items():
        print(f"  {k}: {v}")
    print(f"  名额里的金标准产品：{agg['golden_in_slots'][:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
