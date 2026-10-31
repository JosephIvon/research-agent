"""发现召回的度量：漏斗分层 + 丢在哪一步的归因 + 重复稳定性。

为什么需要它：现有判定线只看**末尾**（入选名单），中间的
"搜索 → 站点类型过滤 → 准入闸门 → 截 top-N" 全程不可见。
第六/七轮实测到"两种句式选出的都是真竞品官网却完全不重合"，
只测末尾分不开"搜索没搜到"和"截断按任意顺序挑"这两种原因。

口径与判定线见 `docs/discovery-recall-measurement.md`（实跑前定稿）。

本模块是**纯函数**：不联网、不碰全局状态，全部可单测。
金标准必须是外部真值（人核对过的官网），不能由被测代码的站点类型表生成——
那正是 Q2 自证失败的原因。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

# 漏斗层级（与文档 §2 的 L0–L3 一一对应）
LEVELS = ("L0", "L1", "L2", "L3")
LEVEL_NAMES = {
    "L0": "发现池",
    "L1": "官网池",
    "L2": "裁决池",
    "L3": "入选名单",
}

# 归因（文档 §2.1）
LOST_NOT_FOUND = "未被发现"
LOST_SITE_TYPE = "站点类型过滤"
LOST_GATE = "准入闸门"
LOST_CUT = "名额截断"


def host_key(url: str) -> str:
    """取主机名（去 www.、端口、小写）。

    金标准按**主机**匹配候选：同一个产品的官网与移动站主机不同
    （xingyeai.com 与 m.xingyeai.com），但都属于金标准里的一条。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "//" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, golden_hosts: Sequence[str]) -> bool:
    """主机是否命中金标准里的某个域名。

    - 完全相等（xingyeai.com ↔ xingyeai.com）
    - **子域相等**（m.xingyeai.com ↔ xingyeai.com；docs.doudou.fun ↔ doudou.fun）
      —— 但要防"父域相同即算命中"（evil.xingyeai.com 也算），
      所以子域方向是**金标准域 → 候选域**（候选是金标准域的子域，或反之），
      且两边至少有一段是标签。
    """
    if not host:
        return False
    for golden in golden_hosts:
        g = (golden or "").lower().lstrip(".")
        if not g:
            continue
        if host == g:
            return True
        if host.endswith("." + g) or g.endswith("." + host):
            return True
    return False


class GoldenSet:
    """一份金标准（人核对过的真竞品官网）。"""

    def __init__(self, entries: Sequence[Dict[str, Any]]):
        self.entries = list(entries)

    @classmethod
    def from_file(cls, path: str, subject: Optional[str] = None) -> "GoldenSet":
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # 顶层 "golden" = **与主体无关**的金标准（例如"该赛道全集"）：
        # 两个主体（猫箱、百度搭子）共用同一份赛道全集时用它。
        top = data.get("golden")
        if isinstance(top, list) and top:
            return cls(list(top))
        subjects = data.get("subjects") or {}
        if subject and subject in subjects:
            entries = (subjects[subject] or {}).get("golden") or []
        else:
            # 没指定主体时把所有主体的金标准合起来（去重按 name）
            merged: Dict[str, Dict[str, Any]] = {}
            for block in subjects.values():
                for entry in (block or {}).get("golden") or []:
                    key = entry.get("name") or ""
                    if key and key not in merged:
                        merged[key] = entry
            entries = list(merged.values())
        return cls(entries)

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, url: str) -> Optional[Dict[str, Any]]:
        """这个 URL 命中金标准里的哪一条？命不中返回 None。"""
        host = host_key(url)
        if not host:
            return None
        for entry in self.entries:
            if _host_matches(host, entry.get("hosts") or []):
                return entry
        return None

    def match_host(self, host: str) -> Optional[Dict[str, Any]]:
        for entry in self.entries:
            if _host_matches(host, entry.get("hosts") or []):
                return entry
        return None


def _level_sets(run: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """从一次运行的记录里取出四层的候选。

    输入是 tool_e2e_recall.py 写出的 run 记录：
      candidates: [{url, name, site_type, aggregator}]
      scored:     [{url, name, score, gate_passed, gate_reasons}]
      selected:   [{url, name}]
    """
    candidates = list(run.get("candidates") or [])
    scored = list(run.get("scored") or [])
    selected = list(run.get("selected") or [])
    l0 = candidates
    l1 = [c for c in candidates if not c.get("aggregator")]
    l2 = [c for c in scored if c.get("gate_passed")]
    l3 = list(selected)
    return {"L0": l0, "L1": l1, "L2": l2, "L3": l3}


def funnel_metrics(run: Dict[str, Any], golden: GoldenSet) -> Dict[str, Any]:
    """算一次运行的漏斗指标 + 逐个金标准产品的丢失归因。"""
    levels = _level_sets(run)
    # 每层命中了哪些金标准产品（按金标准条目聚合，一个产品多个域名只算一条）
    hits: Dict[str, Dict[str, Any]] = {}
    for level in LEVELS:
        found: Dict[str, Dict[str, Any]] = {}
        for item in levels[level]:
            entry = golden.match(item.get("url", ""))
            if entry is None:
                continue
            name = entry.get("name") or ""
            if name not in found:
                found[name] = {
                    "name": name,
                    "hosts": entry.get("hosts") or [],
                    "url": item.get("url", ""),
                    "matched_url": item.get("url", ""),
                    "site_type": item.get("site_type"),
                }
        hits[level] = found

    total = len(golden)
    found_counts = {level: len(hits[level]) for level in LEVELS}
    recalls = {
        level: (found_counts[level] / total) if total else None for level in LEVELS
    }
    # 逐层保留率（分母是上一层的命中数）
    keeps: Dict[str, Optional[float]] = {}
    for prev, level in zip(LEVELS, LEVELS[1:]):
        prev_n = found_counts[prev]
        keeps[level] = (found_counts[level] / prev_n) if prev_n else None
    # 精确率（分母是该层的候选条数）
    precisions: Dict[str, Optional[float]] = {}
    for level in LEVELS:
        n = len(levels[level])
        precisions[level] = (found_counts[level] / n) if n else None

    # 逐个金标准产品的丢失归因
    all_scored = list(run.get("scored") or [])
    lost: List[Dict[str, Any]] = []
    for entry in golden.entries:
        name = entry.get("name") or ""
        if name in hits["L3"]:
            continue
        if name not in hits["L0"]:
            reason, detail = LOST_NOT_FOUND, "搜索阶段没捞到（或被去重/合并挤掉）"
            site_type = None
            gate_reasons: List[str] = []
        elif name not in hits["L1"]:
            reason = LOST_SITE_TYPE
            # 找出它被判成了什么类型
            site_type = None
            for item in levels["L0"]:
                got = golden.match(item.get("url", ""))
                if got and (got.get("name") or "") == name:
                    site_type = item.get("site_type")
                    break
            detail = f"被站点类型判定剔掉（判成 {site_type or '未知'}）"
            gate_reasons = []
        elif name not in hits["L2"]:
            reason = LOST_GATE
            site_type = None
            gate_reasons = []
            # 闸门的理由记在 scored 上（gate_passed=False 的那些也在里面）
            for item in all_scored:
                got = golden.match(item.get("url", ""))
                if got and (got.get("name") or "") == name:
                    gate_reasons = list(item.get("gate_reasons") or [])
                    site_type = item.get("site_type")
                    break
            detail = "被准入闸门挡下" + (f"（{'; '.join(gate_reasons)}）" if gate_reasons else "")
        else:
            reason = LOST_CUT
            site_type = None
            gate_reasons = []
            detail = "合格但排不进前 N（名额截断）"
        lost.append({
            "name": name,
            "hosts": entry.get("hosts") or [],
            "reason": reason,
            "detail": detail,
            "site_type": site_type,
            "gate_reasons": gate_reasons,
        })

    return {
        "golden_total": total,
        "counts": {level: len(levels[level]) for level in LEVELS},
        "found": found_counts,
        "recall": recalls,
        "keep": keeps,
        "precision": precisions,
        "hits": {level: sorted(hits[level].values(), key=lambda x: x["name"]) for level in LEVELS},
        "lost": sorted(lost, key=lambda x: (x["reason"], x["name"])),
    }


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def stability_metrics(runs: Sequence[Dict[str, Any]],
                      key: str = "selected") -> Dict[str, Any]:
    """同一主体同一句式**重复跑**的稳定性（两两 Jaccard）。

    key="selected" 入选名单；key="candidates" 发现池。
    这是把"句式差异"与"引擎抖动"分开的关键量：同句式重复跑都不重合，
    说明抖动就足以让跨句式的 Jaccard 掉到 0（Q3 的线在这种条件下不可能达标）。
    """
    sets: List[set] = []
    for run in runs:
        items = run.get(key) or []
        sets.append({item.get("url", "") for item in items if item.get("url")})
    pairs: List[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            pairs.append(_jaccard(sets[i], sets[j]))
    return {
        "runs": len(sets),
        "pairwise_jaccard": [round(p, 4) for p in pairs],
        "mean_jaccard": round(sum(pairs) / len(pairs), 4) if pairs else None,
        "min_jaccard": round(min(pairs), 4) if pairs else None,
        "union_size": len(set().union(*sets)) if sets else 0,
        "sizes": [len(s) for s in sets],
    }


def host_overlap(a: Sequence[str], b: Sequence[str]) -> Dict[str, Any]:
    """两个名单按主机算的重合度（跨句式对照用）。"""
    ha = {host_key(u) for u in a if host_key(u)}
    hb = {host_key(u) for u in b if host_key(u)}
    return {
        "jaccard": round(_jaccard(ha, hb), 4),
        "common": sorted(ha & hb),
        "only_a": sorted(ha - hb),
        "only_b": sorted(hb - ha),
    }


def verdict(name: str, value: Optional[float], threshold: float,
            higher_is_better: bool = True) -> Dict[str, Any]:
    """按线判定一个指标。value 为 None → 不判（分母为 0，不是 0%）。"""
    if value is None:
        return {"line": name, "value": None, "threshold": threshold, "pass": None,
                "status": "不判"}
    ok = value >= threshold if higher_is_better else value <= threshold
    return {"line": name, "value": value, "threshold": threshold, "pass": ok,
            "status": "PASS" if ok else "FAIL"}
