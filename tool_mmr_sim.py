"""MMR / 名额规则 what-if 模拟（离线，只读已有 artifact，不改管线）。

为什么要先模拟再改代码：
名额分配是结构性问题（第九轮：留出集 keep_L3 = 0.0377）。
改分配算法**会改变 L3 的内容** ⚠️ → 必须先把口径写死、再拿数据说话，
否则就是"看着结果调算法"（那叫过拟合，不是改进）。

本脚本比较几种分配规则在**同一批实跑产物**上的表现：
  1. 现行规则（按分数截断 + 同主机去重）
  2. MMR（λ 扫描）：λ·分数 − (1−λ)·与已选集合的最大相似度
  3. MMR + "模型提名名额上限"：提名的产品最多占 N 个名额，
     其余名额留给别的候选（证据分相同时的确定性问题）

相似度两种模式：**产品名词元 Jaccard**（`name`，对本赛道实测是空操作 ⚠️）与
**名字+描述词元 Jaccard**（`description`，第十轮起 scored 记录带 description ✓）。
真实现应该用 名称 + 描述 + 维度；这里是可复算的近似。

用法：
    python tool_mmr_sim.py output/recall_20261101_154601.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT))

from src.tools import recall_metrics as rm  # noqa: E402
from src.workflow.research_plan import _path_depth, _tie_key  # noqa: E402
from src.workflow.site_types import normalized_brand  # noqa: E402

GOLDEN_FILE = ROOT / "evals" / "discovery_golden" / "track_universe.json"


def _tokens(name: str) -> Set[str]:
    text = (name or "").lower()
    cn = set(re.findall(r"[一-鿿]", text))
    en = set(re.findall(r"[a-z0-9]{2,}", text))
    return cn | en


def _sim_tokens(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _host(url: str) -> str:
    return rm.host_key(url)


def select_mmr(scored: Sequence[Dict[str, Any]],
               max_count: int,
               min_score: float,
               lam: float = 1.0,
               tie_break: str = "name",
               seed: int = 0,
               nominated_cap: Optional[int] = None,
               nominated: Optional[Set[str]] = None,
               similarity: str = "name") -> List[Dict[str, Any]]:
    """MMR 选择：λ·归一化分数 − (1−λ)·与已选集合的最大相似度。

    - 分数先按本轮的最高分归一化到 [0,1]（不同轮的分数尺度不一样 ✗）。
    - 相似度：`name` = 名字词元 Jaccard（空操作 ⚠️）；
      `description` = 名字+描述词元 Jaccard（第十轮起产物带描述 ✓）。
    - `nominated_cap`：模型提名的候选最多占几个名额（None = 不限 ✓）。
    - 同分仍走可插拔的 tie_break ✓（评测侧换种子算期望值 ✓）。
    """
    pool = [i for i in scored if i.get("gate_passed", True)]
    if not pool:
        return []
    top = max(float(i.get("score") or 0.0) for i in pool) or 1.0
    nominated = nominated or set()

    def item_toks(item: Dict[str, Any]) -> Set[str]:
        text = str(item.get("name") or "")
        if similarity == "description":
            text += " " + str(item.get("description") or "")
        return _tokens(text)

    def order_key(item):
        return (-float(item.get("score") or 0.0),
                _path_depth(str(item.get("url") or "")),
                _tie_key(item, tie_break, seed))

    selected: List[Dict[str, Any]] = []
    selected_hosts: Set[str] = set()
    used_nominated = 0
    remaining = sorted(pool, key=order_key)
    while remaining and len(selected) < max_count:
        best = None
        best_key = None
        for item in remaining:
            url = str(item.get("url") or "")
            host = _host(url)
            if host and host in selected_hosts:
                continue
            score = float(item.get("score") or 0.0)
            if score < min_score:
                continue
            brand = normalized_brand(str(item.get("name") or ""))
            is_nom = brand in nominated
            if nominated_cap is not None and is_nom and used_nominated >= nominated_cap:
                continue
            toks = item_toks(item)
            max_sim = 0.0
            for chosen in selected:
                max_sim = max(max_sim, _sim_tokens(toks, item_toks(chosen)))
            mmr = lam * (score / top) - (1.0 - lam) * max_sim
            # 排序键：MMR 值降序 → 分数降序 → 稳定次序（tie_break ✓）
            key = (-round(mmr, 6), -score, order_key(item)[2])
            if best_key is None or key < best_key:
                best, best_key = item, key
        if best is None:
            break
        selected.append(best)
        remaining = [i for i in remaining if i is not best]
        host = _host(str(best.get("url") or ""))
        if host:
            selected_hosts.add(host)
        brand = normalized_brand(str(best.get("name") or ""))
        if brand in nominated:
            used_nominated += 1
    return selected


def evaluate(selected: List[Dict[str, Any]], golden: rm.GoldenSet,
             channel_urls: Set[str], nominated: Set[str]) -> Dict[str, Any]:
    hits = []
    for item in selected:
        match = golden.match(str(item.get("url") or ""))
        if match:
            hits.append({"name": match.get("name"), "split": match.get("split")})
    sources = Counter()
    for item in selected:
        url = str(item.get("url") or "")
        if url in channel_urls:
            sources["C3通道"] += 1
        elif normalized_brand(str(item.get("name") or "")) in nominated:
            sources["模型提名"] += 1
        else:
            sources["搜索"] += 1
    return {
        "n_selected": len(selected),
        "golden_hits": len(hits),
        "golden_names": [h["name"] for h in hits],
        "golden_splits": dict(Counter(h["split"] or "未标注" for h in hits)),
        "sources": dict(sources),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MMR / 名额规则 what-if（离线）")
    parser.add_argument("artifact")
    parser.add_argument("--golden", default=str(GOLDEN_FILE))
    parser.add_argument("--seeds", type=int, default=20,
                        help="同分块换几次次序（算期望）")
    args = parser.parse_args()

    data = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    runs = data.get("runs") or []
    golden = rm.GoldenSet.from_file(str(args.golden))
    pages = sorted({p for r in runs for p in (r.get("channel_pages") or [])})
    if pages:
        golden = golden.exclude_by_source(pages)

    variants = [
        ("现行（按分数截断）", dict(lam=1.0, nominated_cap=None)),
        ("MMR λ=0.8", dict(lam=0.8, nominated_cap=None)),
        ("MMR λ=0.6", dict(lam=0.6, nominated_cap=None)),
        ("MMR λ=0.4", dict(lam=0.4, nominated_cap=None)),
        ("MMR λ=0.2", dict(lam=0.2, nominated_cap=None)),
        ("MMR λ=0.6 + 提名上限4", dict(lam=0.6, nominated_cap=4)),
        ("MMR λ=0.6 + 提名上限5", dict(lam=0.6, nominated_cap=5)),
        ("MMR λ=0.4 + 提名上限5", dict(lam=0.4, nominated_cap=5)),
        # 描述相似度（第十轮起产物带 description ✓）
        ("MMR λ=0.7 描述相似", dict(lam=0.7, similarity="description")),
        ("MMR λ=0.5 描述相似", dict(lam=0.5, similarity="description")),
        ("MMR λ=0.3 描述相似", dict(lam=0.3, similarity="description")),
    ]

    print(f"artifact: {args.artifact}")
    print(f"轮次 {len(runs)}；金标准 {len(golden)} 条（已剔通道自证条目）")
    print()
    header = f"{'变体':28s} {'金标准命中':>10s} {'留出集':>8s} {'名册集':>8s} {'C3通道':>8s} {'模型提名':>9s} {'搜索':>6s}"
    print(header)
    print("-" * len(header))
    for label, kwargs in variants:
        totals: Counter = Counter()
        split_hits: Counter = Counter()
        srcs: Counter = Counter()
        for run in runs:
            sel = run.get("selection") or {}
            max_count = int(sel.get("max_count") or 8)
            min_score = float(sel.get("min_admission_score") or 0.0)
            plan = run.get("plan") or {}
            nominated = {normalized_brand(n) for n in (plan.get("competitor_candidates") or [])}
            channel_urls = set(run.get("channel_candidates") or [])
            scored = list(run.get("scored") or [])
            # 现行规则（λ=1）就是 select_competitors 的口径；
            # 但为了口径一致，下面所有变体都用同一个选择器实现。
            chosen = select_mmr(scored, max_count, min_score,
                                tie_break="name", seed=0,
                                nominated=nominated, **kwargs)
            ev = evaluate(chosen, golden, channel_urls, nominated)
            totals["golden"] += ev["golden_hits"]
            for s, c in ev["golden_splits"].items():
                split_hits[s] += c
            for s, c in ev["sources"].items():
                srcs[s] += c
        n = len(runs)
        print(f"{label:28s} {totals['golden']/n:>10.2f} {split_hits.get('heldout',0)/n:>8.2f} "
              f"{split_hits.get('registry',0)/n:>8.2f} {srcs.get('C3通道',0)/n:>8.2f} "
              f"{srcs.get('模型提名',0)/n:>9.2f} {srcs.get('搜索',0)/n:>6.2f}")
    print()
    print("（每行 = 平均每轮的名额数；金标准命中 = 命中数/轮；C3通道 = 通道产品占的名额数/轮）")
    print("注意：未标注「描述相似」的行用**名字词元 Jaccard**（对本赛道是空操作 ⚠️）；"
          "「描述相似」行用 名字+描述 词元 Jaccard（第十轮起产物带 description ✓）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
