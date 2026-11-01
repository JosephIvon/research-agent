"""赛道口径：名册 + 赛道词 + 跨行业词（生产侧的"同赛道"判据）。

为什么要这一层：实测算门"同赛道"这条**判不了**（规则解析把它归到
"不是可执行条件"），于是闸门只看站点类型与 must_exclude，
跨行业的候选就混进了名额——实测 talkie.ai（AI 医疗分诊）
和 hoshinoresorts.com（星野集团度假村）都占过名额。

现在的判据（决策：允许用**赛道名册**）：
1. **名册命中** → 放行（"这个品牌确实在这个赛道"）
2. **跨行业词命中** → 拦（"这是医疗/酒店/股票，不是同一个赛道"）
3. **赛道词命中** → 放行
4. 都没有 → 放行但记"未判定"（宁可漏杀也不误杀——闸门收紧会直接压召回，
   召回线的容错空间只有 0.10）

**口径纪律（重要）**：名册与测量集必须**互斥**。
名册来自 `assets/track_registry.json`（生产资产），
测量集来自 `evals/discovery_golden/`（评测真值）；
两者由 tool_build_track_split.py 按固定哈希切分生成，名册那半永远不进测量分母。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.workflow.site_types import normalized_brand

ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"
SCOPE_FILE = ASSET_DIR / "track_scope.json"
REGISTRY_FILE = ASSET_DIR / "track_registry.json"


@dataclass
class TrackScope:
    """赛道词 + 跨行业词。"""
    track: str = ""
    positive_keywords: Tuple[str, ...] = ()
    off_track_keywords: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackScope":
        return cls(
            track=str(data.get("track") or ""),
            positive_keywords=tuple(str(k) for k in (data.get("positive_keywords") or []) if k),
            off_track_keywords=tuple(str(k) for k in (data.get("off_track_keywords") or []) if k),
        )


@dataclass
class TrackRegistry:
    """赛道名册：品牌键 + 站点主机（生产侧的知识）。"""
    track: str = ""
    entries: List[Dict[str, Any]] = field(default_factory=list)
    brands: Tuple[str, ...] = ()
    hosts: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackRegistry":
        entries = list(data.get("entries") or [])
        brands = []
        hosts = []
        for entry in entries:
            for alias in ([entry.get("name")] + list(entry.get("aliases") or [])):
                brand = normalized_brand(str(alias or ""))
                if brand:
                    brands.append(brand)
            for host in (entry.get("hosts") or []):
                host = str(host or "").strip().lower()
                if host:
                    hosts.append(host)
        return cls(
            track=str(data.get("track") or ""),
            entries=entries,
            brands=tuple(sorted(set(brands))),
            hosts=tuple(sorted(set(hosts))),
        )


@lru_cache(maxsize=1)
def load_scope(path: Optional[str] = None) -> TrackScope:
    """读赛道口径资产（读不到就返回空口径——闸门不能因为缺文件而崩）。"""
    target = Path(path) if path else SCOPE_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 资产缺失/坏掉都不该让管线挂掉
        return TrackScope()
    return TrackScope.from_dict(data)


@lru_cache(maxsize=1)
def load_registry(path: Optional[str] = None) -> TrackRegistry:
    """读赛道名册资产（读不到就是空名册）。"""
    target = Path(path) if path else REGISTRY_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return TrackRegistry()
    return TrackRegistry.from_dict(data)


def _hit(text: str, keywords: Sequence[str]) -> Optional[str]:
    lowered = (text or "").lower()
    for keyword in keywords:
        if keyword and keyword.lower() in lowered:
            return keyword
    return None


def check_same_track(
    name: str,
    url: str = "",
    description: str = "",
    extra_keywords: Sequence[str] = (),
    scope: Optional[TrackScope] = None,
    registry: Optional[TrackRegistry] = None,
) -> Tuple[bool, str]:
    """判一个候选是不是同赛道。返回 (是否放行, 理由)。

    理由要能落到人话上——闸门的判定理由会写进报告，
    "为什么星野集团度假村被拦"必须说得清。
    """
    scope = scope if scope is not None else load_scope()
    registry = registry if registry is not None else load_registry()
    text = " ".join([name or "", url or "", description or ""])
    from urllib.parse import urlparse

    host = (urlparse(url).netloc or "").lower()
    bare = host[4:] if host.startswith("www.") else host

    # 1) 名册里的**站点** → 放行（这个站点就是那个产品，不用再判）
    if host and (bare in registry.hosts or host in registry.hosts):
        return True, f"同赛道（名册里的站点：{host}）"

    # 2) 跨行业词命中 → 拦
    #    注意要在"名册品牌"之前判：talkie.ai 的标题是
    #    "AI Medical Receptionist for Better Patient Access"——
    #    "Talkie" 这个**品牌**在名册里（真竞品 talkie-ai.com 也在名册里），
    #    但 talkie.ai 这个**站点**是医疗分诊，不是同一个赛道。
    off = _hit(text, scope.off_track_keywords)
    if off:
        return False, f"跨行业（命中跨行业词：{off}）"

    # 3) 名册里的**品牌** → 放行
    brand = normalized_brand(name or "")
    if brand and brand in registry.brands:
        return True, f"同赛道（名册里的品牌：{name}）"

    # 4) 赛道词命中 → 放行
    positive = list(scope.positive_keywords) + [k for k in extra_keywords if k]
    hit = _hit(text, positive)
    if hit:
        return True, f"同赛道（命中赛道词：{hit}）"

    # 5) 没证据 → 放行但记账（不误杀）
    return True, "未判定（无名册/赛道词/跨行业证据）"
