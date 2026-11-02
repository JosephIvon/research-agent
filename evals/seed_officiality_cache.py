"""官方性判定预计算（seed）：把金标准里的品牌批量查一遍落盘。

为什么（第十八轮实测 ✗）：e2e 进程里逐品牌现查 iTunes，批量触发限流
（52 个品牌只成功 1 个 → 全 unknown 降级 ✓ 设计行为，但覆盖率归零）。
判定是**低频变化的事实**（开发者站不常换），一次性预计算 + 跨进程缓存
是正确形态 ✓；判定与行核审计同源（officiality_cache.json 在 golden 目录 ✓）。

用法：uv run python evals/seed_officiality_cache.py
纪律：只读金标准 ✓；间隔 1.5s + 失败重试 1 次 ✓；结果写 officiality_cache.json
（失败品牌不落盘 ✓，lessons #106 同源）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import officiality as off  # noqa: E402
from src.tools.recall_metrics import GoldenSet  # noqa: E402

GOLDEN_FILES = [
    "evals/discovery_golden/companion_apps.json",
    "evals/discovery_golden/track_universe.json",
]


def golden_brands(limit: int = 80) -> list[str]:
    """金标准条目名；**判定口径（companion_apps）优先**——赛道全集的 ASCII
    名会按排序把名额吃满，把 Talkie/Replika 这类判定条目截掉 ✗（首跑踩过）。"""
    companion: list[str] = []
    universe: list[str] = []
    seen: set[str] = set()
    for path, bucket in (("evals/discovery_golden/companion_apps.json", companion),
                         ("evals/discovery_golden/track_universe.json", universe)):
        gs = GoldenSet.from_file(str(ROOT / path))
        for entry in gs.entries:
            name = str(entry.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                bucket.append(name)
    ordered = sorted(companion) + sorted(universe)
    return ordered[:limit]


def main() -> int:
    names = golden_brands()
    print(f"预计算 {len(names)} 个品牌 → {off.DEFAULT_CACHE_PATH}")
    # 启动时载盘（保住 e2e 已落的判定 ✓）+ 结束**显式带路径**写盘——
    # save_disk_cache() 无参是 no-op（_DISK_PATH 未设 ✗ 首跑踩过：24 个判定
    # 一个没写，盘里还是 e2e 的旧 10 条 ✗）。
    off.set_disk_cache(off.DEFAULT_CACHE_PATH)
    ok, fail = 0, 0
    for i, name in enumerate(names, 1):
        sellers = off.seller_hosts_for_brand(name)
        if name in off._FETCH_FAILED:  # 失败重试一次（限流大多是瞬时的 ✓）
            time.sleep(3.0)
            off._FETCH_FAILED.discard(name)
            off._SELLER_CACHE.pop(name, None)
            sellers = off.seller_hosts_for_brand(name)
        if name in off._FETCH_FAILED:
            fail += 1
            print(f"  [{i:>2}] {name:<22} 查询失败（不落盘 ✓）")
        else:
            ok += 1
            print(f"  [{i:>2}] {name:<22} sellers={sellers}")
        time.sleep(1.5)
    off.save_disk_cache(off.DEFAULT_CACHE_PATH)
    data = json.loads(Path(off.DEFAULT_CACHE_PATH).read_text(encoding="utf-8"))
    brands = data.get("brands") or {}
    print(f"落盘品牌 {len(brands)} 个（成功 {ok} / 失败 {fail}）；"
          f"talkie={brands.get('talkie')} replika={brands.get('replika')}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
