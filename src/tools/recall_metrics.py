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
# 同名合并：产品**找到了**，但管线把它并进了另一个域的 URL，
# 金标准记的域不在里面 → 匹配不上（不是闸门挡的 ✗）
LOST_MERGE = "同名合并"


def _norm_name(name: str) -> str:
    """名字归一化（判断"是不是同一个产品名"用）。

    只做保守归一：小写 + 去空白 + 去非字母数字。
    刻意**不做**拼音/罗马化（那是实体归并三段式的事，F 项还没做），
    这里只用来区分"闸门挡的"和"同名合并的"两种丢法。
    """
    import re as _re

    return _re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (name or "").strip().lower())


def _norm_page(url: str) -> str:
    """页面 URL 归一化（比来源用）：去协议差异、去 www、去尾部斜杠、去查询串。"""
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "http://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return f"{host}{path}"


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
        # 品牌（实体）索引：规范化品牌键 → 金标准条目。
        # 背景（第十一轮实测 ✗）：Talkie 被发现在 talkieai.cc（官方域 ✓），
        # 但金标准只记了 talkie-ai.com —— host 口径把"已发现的产品"记 0 分；
        # 12 轮同批数据：host 口径 R1=0.4074 vs 品牌 口径=0.5139（+0.1065）。
        # 冲突键（两个不同产品共用一个品牌键）自动排除，保证无歧义 ✓
        # （实测两个口径 0 冲突 ✓，见 2026-11-01 探针）。
        self.brand_index: Dict[str, Dict[str, Any]] = {}
        self.ambiguous_brands: set = set()
        self._build_brand_index()

    def _build_brand_index(self) -> None:
        from src.workflow.site_types import normalized_brand

        # 歧义检测的"条目身份"必须用**位置**而不是名字：同名异站的两条
        # （Muse@muse1.example vs Muse@muse2.example）恰恰是最需要歧义
        # 保护的形态 ✗——按名字判会把它们误当成同一条 ✓ 改按下标判 ✓。
        pending: Dict[str, set] = {}
        for pos, entry in enumerate(self.entries):
            keys = {normalized_brand(str(entry.get("name") or ""))}
            for alias in (entry.get("aliases") or []):
                key = normalized_brand(str(alias or ""))
                if key:
                    keys.add(key)
            keys.discard("")
            for key in keys:
                pending.setdefault(key, set()).add(pos)
                self.brand_index[key] = entry
        # 一个键指向多个不同条目 → 有歧义 → 从索引里剔掉（brand 匹配宁缺毋滥 ✓）
        for key, positions in pending.items():
            if len(positions) > 1:
                self.ambiguous_brands.add(key)
                self.brand_index.pop(key, None)

    def match_brand(self, name: str) -> Optional[Dict[str, Any]]:
        """这个名字的规范化品牌键命中金标准里的哪一条？无歧义才命中 ✓。"""
        from src.workflow.site_types import normalized_brand

        key = normalized_brand(str(name or ""))
        if not key or key in self.ambiguous_brands:
            return None
        return self.brand_index.get(key)

    @classmethod
    def from_file(cls, path: str, subject: Optional[str] = None,
                  split: Optional[str] = None) -> "GoldenSet":
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # 顶层 "golden" = **与主体无关**的金标准（例如"该赛道全集"）：
        # 两个主体（猫箱、百度搭子）共用同一份赛道全集时用它。
        top = data.get("golden")
        if isinstance(top, list) and top:
            entries = list(top)
            if split:
                entries = [e for e in entries if str(e.get("split") or "") == split]
            return cls(entries)
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
        if split:
            entries = [e for e in entries if str(e.get("split") or "") == split]
        return cls(entries)

    def exclude_by_source(self, urls: Iterable[str]) -> "GoldenSet":
        """剔掉"来源正好是这些页"的条目（通道自证条目）。

        为什么要有这一步：C3 目录页通道读的就是外部目录页，
        而赛道金标准里有一部分条目**也来自那些页**。
        通道读到那张表再"发现"上面的产品，那叫**读答案**，不是发现。
        所以这些条目不能留在测量分母里（分子分母都剔 ✓）。
        """
        wanted = {_norm_page(u) for u in urls or [] if u}
        if not wanted:
            return GoldenSet(self.entries)
        kept: List[Dict[str, Any]] = []
        for entry in self.entries:
            sources = {_norm_page(str(s.get("source_url") or ""))
                       for s in (entry.get("sources") or []) if isinstance(s, dict)}
            if sources & wanted:
                continue
            kept.append(entry)
        return GoldenSet(kept)

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
    # 实体（品牌）口径：候选的规范化品牌命中金标准 name/aliases 即算发现——
    # 产品被找到了、只是域名跟金标准记的不同（镜像域/官方域并存）不该记 0 分 ✓。
    # **非判定口径** ✗：判定线（R1≥0.70）仍用 host 口径（预注册不变）；
    # 这里只是把"实体归并缺口"量化出来（host 与 brand 的差 = 域名没对上）。
    brand_hits: Dict[str, Dict[str, Any]] = {}
    for level in LEVELS:
        found: Dict[str, Dict[str, Any]] = {}
        for item in levels[level]:
            entry = golden.match_brand(str(item.get("name") or ""))
            if entry is None:
                continue
            name = entry.get("name") or ""
            if name not in found:
                found[name] = {
                    "name": name,
                    "hosts": entry.get("hosts") or [],
                    "url": item.get("url", ""),
                    "matched_by": "brand",
                }
        brand_hits[level] = found
    brand_counts = {level: len(brand_hits[level]) for level in LEVELS}
    brand_recalls = {
        level: (brand_counts[level] / total) if total else None for level in LEVELS
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
            # 这里有两种完全不同的丢法，必须分开：
            # ① **闸门真的挡了它**（scored 里有它且 gate_passed=False）；
            # ② **同名合并把它并进了另一个域**（scored 里有同名的另一条 URL，
            #    而金标准记的是另一个域 → 匹配不上）。
            # 把②算成①会得出"新闸门误杀了金标准产品"的错误结论 ✗✗。
            site_type = None
            gate_reasons: List[str] = []
            merged_item = None
            key = _norm_name(name)
            for item in all_scored:
                got = golden.match(item.get("url", ""))
                if got and (got.get("name") or "") == name:
                    gate_reasons = list(item.get("gate_reasons") or [])
                    site_type = item.get("site_type")
                    break
                if _norm_name(item.get("name", "")) == key and not got:
                    merged_item = item
            if not gate_reasons and merged_item is not None:
                reason = LOST_MERGE
                detail = (f"同名合并到了另一个域（{merged_item.get('url')}），"
                          f"金标准记的域不在其中 → 匹配不上（不是闸门挡的）")
            else:
                reason = LOST_GATE
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
        # 实体（品牌）口径（非判定口径 ✗——判定线仍用 host 口径 ✓）
        "found_brand": brand_counts,
        "recall_brand": brand_recalls,
        "hits_brand": {level: sorted(brand_hits[level].values(), key=lambda x: x["name"]) for level in LEVELS},
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


# ----------------------------------------------------------------------
# 同分块（tie）稳健性
# ----------------------------------------------------------------------
# 为什么要有这块：排序键是 (-分数, 路径深度, 名字)，而候选分数几乎全是
# 并列 0.5 → **同分块内按名字字母序**，也就是字母序在决定谁能进名额。
# 评测文献（FRAME'26 @ RecSys'26）把这件事说得很直白：
# **tie-break 规则就是排序规则的一部分**——同分块跨过 top-k 边界时，
# 谁进榜由这条规则决定；报某一次字母序结果，等于报一个任意值。
# 那篇论文给出的诚实口径是"同分下的期望指标"：
# 解析式（单相关文档时 E[Hit@k]=max{0,min(t,k-a)}/t），
# 或"重复随机 tie-break 取均值±方差"（论文实测两者一致到 0.0012）。
# 这里实现后者：换种子 = 换一次同分块次序，跑 N 次取分布。

def _path_depth(url: str) -> int:
    path = urlparse(url).path or ""
    return len([seg for seg in path.split("/") if seg])


def _tie_blocks(scored: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """把已打分候选按 (分数, 路径深度) 分成同分块（按当前排序键的次序排好）。"""
    ordered = sorted(
        scored,
        key=lambda item: (
            -float(item.get("score") or 0.0),
            _path_depth(str(item.get("url") or "")),
            str(item.get("name") or ""),
        ),
    )
    blocks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_key: Optional[Tuple[float, int]] = None
    for item in ordered:
        key = (float(item.get("score") or 0.0), _path_depth(str(item.get("url") or "")))
        if current_key is not None and key == current_key:
            current.append(item)
            continue
        if current:
            blocks.append(current)
        current = [item]
        current_key = key
    if current:
        blocks.append(current)
    return blocks


def tie_stats(run: Dict[str, Any]) -> Dict[str, Any]:
    """一次运行的同分情况：同分流行度、最大同分块、有没有同分块跨过名额边界。"""
    scored = [s for s in (run.get("scored") or []) if s.get("gate_passed")]
    blocks = _tie_blocks(scored)
    selected_urls = {str(item.get("url") or "") for item in (run.get("selected") or [])}
    tied_candidates = sum(len(b) for b in blocks if len(b) > 1)
    crossing = 0
    for block in blocks:
        if len(block) < 2:
            continue
        inside = [b for b in block if str(b.get("url") or "") in selected_urls]
        outside = [b for b in block if str(b.get("url") or "") not in selected_urls]
        if inside and outside:
            crossing += 1
    return {
        "candidates": len(scored),
        "tie_blocks": sum(1 for b in blocks if len(b) > 1),
        "tied_candidates": tied_candidates,
        "tied_ratio": round(tied_candidates / len(scored), 4) if scored else None,
        "max_block": max((len(b) for b in blocks), default=0),
        "blocks_crossing_cutoff": crossing,
        "selected": len(selected_urls),
    }


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def tie_expected_metrics(
    runs: Sequence[Dict[str, Any]],
    golden: GoldenSet,
    seeds: int = 100,
    seed_base: int = 0,
) -> Dict[str, Any]:
    """同分下的**期望指标**：换种子就换一次同分块次序，跑 N 次取分布。

    只有 L3（入选名单）依赖 tie-break，所以：
    - R1/R2（L0/L1 的量）与 tie 无关，不重算；
    - R3（keep_L3）与 S1（入选名单稳定性）重算。

    需要 run 记录里有 `selection`（max_count / must_include / min_score）——
    那是"当时按什么口径选的"；缺了就**不判**（computable=False），
    不能拿现在的默认值假装能复算。
    """
    from src.workflow.research_plan import select_competitors

    if not runs:
        return {"computable": False, "reason": "没有可用的运行记录"}
    missing = [i for i, r in enumerate(runs) if not (r.get("selection") or {}).get("max_count")]
    if missing:
        return {
            "computable": False,
            "reason": f"{len(missing)} 条运行记录缺 selection 口径（max_count），无法复算同分下的期望值",
        }

    # L2 的命中数与 tie 无关（闸门在排序之前就判完了）
    l2_hits: List[int] = []
    for run in runs:
        golden_l2 = set()
        for item in (run.get("scored") or []):
            if not item.get("gate_passed"):
                continue
            entry = golden.match(str(item.get("url") or ""))
            if entry:
                golden_l2.add(entry.get("name") or "")
        l2_hits.append(len(golden_l2))

    per_seed: List[Dict[str, Optional[float]]] = []
    for k in range(seeds):
        seed = seed_base + k
        selected_sets: List[set] = []
        l3_hits: List[int] = []
        for run in runs:
            scored = [s for s in (run.get("scored") or []) if s.get("gate_passed")]
            meta = run.get("selection") or {}
            chosen = select_competitors(
                scored,
                max_count=int(meta.get("max_count") or 0),
                must_include=list(meta.get("must_include") or []),
                min_score=float(meta.get("min_admission_score") or 0.0),
                tie_break="random",
                seed=seed,
                # **重放要用当时的 MMR 配置**（第十轮起 selection 带 diversity ✓；
                # 旧产物没有这个键 → None → 现行按分数截断 ✓，口径不串 ✗）
                diversity=meta.get("diversity"),
            )
            selected_sets.append({str(c.get("url") or "") for c in chosen})
            hit_names = set()
            for c in chosen:
                entry = golden.match(str(c.get("url") or "") or "")
                if entry:
                    hit_names.add(entry.get("name") or "")
            l3_hits.append(len(hit_names))
        keep_l3: List[float] = []
        for l2, l3 in zip(l2_hits, l3_hits):
            keep_l3.append(l3 / l2 if l2 else None)
        pairs = [
            _jaccard(selected_sets[i], selected_sets[j])
            for i in range(len(selected_sets))
            for j in range(i + 1, len(selected_sets))
        ]
        per_seed.append({
            "keep_l3": _mean([v for v in keep_l3 if v is not None]),
            "stability": (sum(pairs) / len(pairs)) if pairs else None,
        })

    def _dist(key: str) -> Dict[str, Any]:
        values = [row[key] for row in per_seed if row.get(key) is not None]
        if not values:
            return {"mean": None, "sd": None, "min": None, "max": None, "n": 0}
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        return {
            "mean": round(mean, 4),
            "sd": round(var ** 0.5, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "n": len(values),
        }

    return {
        "computable": True,
        "seeds": seeds,
        "keep_l3": _dist("keep_l3"),
        "stability": _dist("stability"),
    }
