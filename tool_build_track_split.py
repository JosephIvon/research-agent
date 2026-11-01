"""把赛道全集切成**名册**（生产侧知识）与**留出集**（测量侧真值）。

为什么必须切（决策 2 的代价，说清楚）：
用户同意"同赛道判据允许用赛道名册"——名册是**闸门的知识**。
如果名册同时又是**测量的分母**，那么闸门只要把名册里的产品放行，
keep 率就会自己涨——那是自证，不是改进。
所以名册与测量集必须**互斥**，而且切分要**可复现**（固定哈希，不随机）。

切分规则（固定，2026-11-01）：
1. **有官网且有独立来源证据**的条目才有资格进名册
   （source_tier ∈ {external, sample_verified, vendor_article}）：
   - store_only（只有应用商店页）→ 对网页闸门没有用；
   - pipeline（本管线自己发现的）→ 进名册就是循环论证。
2. 资格条目按 `sha1(切分种子|规范化名) % 100 < 50` 进名册，其余进留出集。
   哈希与来源层级无关 → 两边的层级构成自然保持一致。
3. **留出集 = 测量的主口径**（名册那半闸门见过，不能当"新发现"）。
   全集口径仍然报，作为对照（更保守，因为分母里含名册与通道自证条目）。

另外：**通道自证条目**（金标准来源页正好是召回通道 C3 读过的那些目录页）
不进测量分母——通道读到那张表再"发现"上面的产品，那叫读答案。
这条是运行时算的（通道读了哪些页由当轮决定），在 tool_e2e_recall.py 里执行。

用法：
    uv run python tool_build_track_split.py            # 生成并写回
    uv run python tool_build_track_split.py --dry-run  # 只看切分结果
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

UNIVERSE_FILE = Path("evals/discovery_golden/track_universe.json")
REGISTRY_FILE = Path("assets/track_registry.json")
SPLIT_SEED = "research-agent/track-split/v1"
REGISTRY_BUCKET = 50           # % 0–49 → 名册
REGISTRY_TIERS = ("external", "sample_verified", "vendor_article")


def bucket_of(name: str) -> int:
    """固定哈希分桶（0–99）。跨进程、跨机器一致。"""
    digest = hashlib.sha1(f"{SPLIT_SEED}|{(name or '').strip().casefold()}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def normalize_name(name: str) -> str:
    from src.workflow.site_types import normalized_brand

    return normalized_brand(name)


def build(universe: Dict[str, Any]) -> Dict[str, Any]:
    """给每条金标准条目打 split 标记，并挑出名册条目。"""
    entries = list(universe.get("golden") or [])
    registry_entries: List[Dict[str, Any]] = []
    counts = Counter()
    tiers = Counter()
    for entry in entries:
        name = str(entry.get("name") or "")
        tier = str(entry.get("source_tier") or "")
        hosts = [h for h in (entry.get("hosts") or []) if h]
        eligible = tier in REGISTRY_TIERS and bool(hosts)
        bucket = bucket_of(name)
        if eligible and bucket < REGISTRY_BUCKET:
            entry["split"] = "registry"
            registry_entries.append({
                "name": name,
                "aliases": list(entry.get("aliases") or []),
                "hosts": list(hosts),
                "source_tier": tier,
                "evidence": str(entry.get("evidence") or ""),
                "sources": list(entry.get("sources") or []),
            })
            counts["registry"] += 1
        else:
            entry["split"] = "heldout"
            counts["heldout"] += 1
        tiers[f"{entry['split']}:{tier}"] += 1
    return {
        "universe": universe,
        "registry_entries": registry_entries,
        "counts": counts,
        "tiers": tiers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="切分赛道名册与测量留出集")
    parser.add_argument("--universe", default=str(UNIVERSE_FILE))
    parser.add_argument("--registry", default=str(REGISTRY_FILE))
    parser.add_argument("--dry-run", action="store_true", help="只看结果，不写文件")
    args = parser.parse_args()

    universe_path = Path(args.universe)
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    result = build(universe)

    print(f"条目总数：{len(universe.get('golden') or [])}")
    print(f"  名册（生产侧知识）：{result['counts']['registry']}")
    print(f"  留出集（测量主口径）：{result['counts']['heldout']}")
    print("层级构成：")
    for key, value in sorted(result["tiers"].items()):
        print(f"  {key:34s} {value}")
    if args.dry_run:
        print("（--dry-run：未写文件）")
        return 0

    universe["split_policy"] = {
        "version": 1,
        "seed": SPLIT_SEED,
        "registry_bucket_lt": REGISTRY_BUCKET,
        "registry_tiers": list(REGISTRY_TIERS),
        "rule": "有官网 + 独立来源证据的条目，按 sha1(种子|规范化名) 分桶，"
                "桶号 <50 进名册（生产侧），其余进留出集（测量主口径）。",
        "why": "名册是闸门的知识，不能同时当测量的分母——否则闸门放行名册产品"
               "就会自己把 keep 率涨上去（自证）。",
    }
    universe_path.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    registry = {
        "version": 1,
        "track": universe.get("track") or "AI 情感陪伴 / AI 角色扮演",
        "built_at": "2026-11-01",
        "split_seed": SPLIT_SEED,
        "note": "生产侧的赛道名册：同赛道闸门用它判『这个品牌/站点确实在这个赛道』。"
                "它与测量集（evals/discovery_golden/）互斥——切分规则见 "
                "evals/discovery_golden/track_universe.json 的 split_policy。",
        "entries": result["registry_entries"],
    }
    registry_path = Path(args.registry)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写：{universe_path}")
    print(f"已写：{registry_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
