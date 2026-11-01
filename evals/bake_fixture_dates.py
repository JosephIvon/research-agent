"""给夹具页面写入「数据口径日期」——评测语料的时间锚点。

为什么需要：freshness 维度要判的是"这些数据是什么时候的"，唯一外部证据是
**页面自己声明的日期**。语料里没有日期 → 下游只能判"无时效支撑"，该维度
退化成常量、等于废掉。本脚本按**档位**给每个 case 的页面写入日期：

    fresh  ≤ 90 天（近）
    mid    90 ~ 180 天
    stale  > 180 天（过时）

日期是**固定值**（不按"今天"实时计算）：报告正文会进下游数据集当样本，语料
必须可复算——相对日期会让同一条 case 每次跑出的报告都不一样，A/B 对比就
失去"同样本"这个前提。代价是语料会变旧：到期后用 `--reference` 换基准日重跑
本脚本，再重算 fixture 摘要（`--update-digests`）即可。

用法:
    python evals/bake_fixture_dates.py                        # 默认基准日写入
    python evals/bake_fixture_dates.py --reference 2027-02-01  # 语料变旧后重烘焙
    python evals/bake_fixture_dates.py --dry-run              # 只看要写什么

写进页面的写法与上游口径一致：`<p>数据口径日期：YYYY-MM-DD</p>`。
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"
MANIFEST = CASES_DIR / "manifest.json"

# 基准日：写死而非取今天——脚本默认必须幂等（同参数跑两次结果一致）。
DEFAULT_REFERENCE = "2026-11-01"

# 档位 → 距基准日的天数。fresh 取 5 天（90 天上限还有 85 天余量），
# mid 取 95 天（离 180 天上限还有 85 天余量），stale 取 382 天（永久过时）。
BAND_OFFSETS = {"fresh": 5, "mid": 95, "stale": 382}

# 每个 case 归到哪一档。**显式表**：档位分布是语料的设计属性（新 case 必须
# 显式归档，否则语料会在某档上缺样本，freshness 的判别力就悄悄退化了）。
BANDS = {
    "fresh": [
        "reg_doccollab", "reg_aicoding", "reg_cloudvm", "conf_pricing",
        "cross_cloudprice", "inj_mild", "cov_gap_declared", "gap_ambiguous",
        "react_pagesel", "matrix_b2b", "plan_clarify",
    ],
    "mid": [
        "reg_single_devtool", "conf_specs", "miss_api", "inj_heavy",
        "gap_ungrounded", "gap_multi_site", "gap_both", "cov_gap_undeclared",
    ],
    "stale": [
        "miss_enterprise", "budget_partial", "gap_mixed_unknown", "gap_refetch",
        "gap_dimension_missing", "cov_gap_partial_declared",
    ],
}

_DATE_LINE = re.compile(r"\n\s*<p>\s*(?:数据口径日期|页面最后更新)[^<]*</p>", re.IGNORECASE)
_DATE_IN_TEXT = re.compile(r"(数据口径日期|页面最后更新)[：:]\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def band_dates(reference: date) -> dict:
    """档位 → 基准日往前推的日期字符串。"""
    return {
        band: (reference - timedelta(days=days)).isoformat()
        for band, days in BAND_OFFSETS.items()
    }


def band_of(case_id: str) -> str:
    for band, ids in BANDS.items():
        if case_id in ids:
            return band
    raise SystemExit(
        f"case 未归档：{case_id}。把它加进 evals/bake_fixture_dates.py 的 BANDS "
        f"（fresh/mid/stale），否则语料会在某一档上缺样本。"
    )


def apply_date(path: Path, iso_date: str) -> bool:
    """把页面里的口径日期写成给定日期；返回是否改动了文件。"""
    text = path.read_text(encoding="utf-8")
    line = f"<p>数据口径日期：{iso_date}</p>"
    if _DATE_LINE.search(text):
        new_text = _DATE_LINE.sub(f"\n  {line}", text)
    elif _DATE_IN_TEXT.search(text):
        # 已有别的写法（如「页面最后更新：…」）：只换日期，保留原有措辞
        new_text = _DATE_IN_TEXT.sub(lambda m: f"{m.group(1)}：{iso_date}", text)
    elif "</body>" in text:
        new_text = text.replace("</body>", f"  {line}\n</body>", 1)
    else:
        new_text = text.rstrip() + f"\n{line}\n"
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="给夹具页面写入数据口径日期")
    ap.add_argument("--reference", default=DEFAULT_REFERENCE,
                    help=f"基准日 YYYY-MM-DD（默认 {DEFAULT_REFERENCE}）；语料变旧后换它重烘焙")
    ap.add_argument("--dry-run", action="store_true", help="只报告要写什么，不落盘")
    args = ap.parse_args()

    try:
        reference = date.fromisoformat(args.reference)
    except ValueError:
        raise SystemExit(f"--reference 不是合法日期: {args.reference}")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    dates = band_dates(reference)
    changed = 0
    plan = []
    for case in manifest.get("cases", []):
        case_id = case["id"]
        band = band_of(case_id)
        for site in case.get("sites", []):
            for page in site.get("pages", []):
                path = CASES_DIR / case_id / "sites" / site["key"] / page
                if not path.exists():
                    raise SystemExit(f"夹具页面缺失: {path}")
                plan.append((case_id, band, dates[band], str(path)))
                if not args.dry_run and apply_date(path, dates[band]):
                    changed += 1

    for case_id, band, iso, path in plan:
        print(f"{case_id:24s} {band:5s} {iso}  {path}")
    counts = {}
    for _, band, _, _ in plan:
        counts[band] = counts.get(band, 0) + 1
    print(f"\n档位分布（页面数）：{counts}")
    print(f"基准日 {reference.isoformat()}；fresh 档 {BAND_OFFSETS['fresh']} 天前、"
          f"mid 档 {BAND_OFFSETS['mid']} 天前、stale 档 {BAND_OFFSETS['stale']} 天前")
    # 语料有效期：fresh 档跨过 90 天、mid 档跨过 180 天都会掉档。
    expiry = reference + timedelta(days=min(90 - BAND_OFFSETS["fresh"],
                                             180 - BAND_OFFSETS["mid"]))
    print(f"档位有效期至 {expiry.isoformat()}（之后必须用 --reference 重烘焙）")
    if args.dry_run:
        print("dry-run：未写盘")
    else:
        print(f"已改动 {changed} 个页面；记得重算 fixture 摘要（--update-digests）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
