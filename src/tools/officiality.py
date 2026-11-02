"""官方性检查：候选域是否是**开发者自己的站**（App Store 元数据交叉验证）。

背景（2026-11-02 行核 ✗）：Talkie 的金标准官方域是 talkie-ai.com（App Store
sellerUrl 权威证实），但候选池里排"talkie 官网"第一的是 talkieai.cc——
**蹭名站**：无开发者印记、联盟导流链接。品牌口径把这类站记成"发现了产品"，
是假阳性 ✗。

方法：对候选品牌查 iTunes Search API（`term=<品牌> &entity=software`），
取**同品牌 App** 的 sellerUrl/developerUrl 主机作"开发者站"参照：
- 候选域 == 开发者站（或其子域/父域）→ **verified** ✓
- 存在开发者站但候选域不是 → **suspect**（蹭名嫌疑：不占名额、品牌口径不给命中 ✓）
- 查不到同品牌 App / sellerUrl 缺失 / sellerUrl 不含品牌词（linktree 类）→
  **unknown**（不罚 ✓ 宁缺毋滥）

纪律：进程级缓存（12 轮只查一次 ✓）+ 失败一律 unknown（不作废整轮 ✓）+
有界（max_lookups ✓）+ 判定只用**发现阶段拿得到的信息** ✓。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib import request as urlrequest
from urllib.parse import urlparse

from src.tools.list_pages import host_of
from src.workflow.site_types import normalized_brand

# 进程级缓存：brand → 同品牌 App 的开发者站主机列表（空列表 = 查过但没有 ✓）
_SELLER_CACHE: Dict[str, List[str]] = {}
# 本次进程里查询失败的品牌（失败结果**不落盘** ✓——间歇失败不能固化成 unknown）
_FETCH_FAILED: set = set()

_TIMEOUT = 10
_LIMIT = 10

# 落盘缓存（第十八轮，预注册 §15 ✓）：跨进程复用 + iTunes 间歇失败的兜底。
# 路径放 golden 目录（与行核审计同源 ✓）；None = 不启用落盘（测试默认 ✓）。
DEFAULT_CACHE_PATH = str(Path(__file__).resolve().parents[2]
                        / "evals" / "discovery_golden" / "officiality_cache.json")
_DISK_PATH: Optional[str] = None


def set_disk_cache(path: Optional[str]) -> None:
    """启用/关闭落盘缓存（None 关闭 ✓）。进程内先读盘（load），写穿（save ✓）。"""
    global _DISK_PATH
    _DISK_PATH = path
    if path:
        load_disk_cache(path)


def load_disk_cache(path: str) -> None:
    """把落盘缓存并入进程缓存（进程内已有/已失败的键不覆盖 ✓）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 没有缓存文件/损坏 = 冷启动 ✓
        return
    for brand, sellers in (data.get("brands") or {}).items():
        if brand not in _SELLER_CACHE and brand not in _FETCH_FAILED:
            _SELLER_CACHE[brand] = [str(h) for h in (sellers or [])]
    for host, target in (data.get("redirects") or {}).items():
        if host not in _REDIRECT_CACHE:
            _REDIRECT_CACHE[host] = (str(target) if target else None)


def save_disk_cache(path: Optional[str] = None) -> None:
    """把进程缓存写盘（原子替换 ✓；**跳过失败品牌** ✓；失败静默——缓存坏了
    不能拖垮测量 ✓）。"""
    target = path or _DISK_PATH
    if not target:
        return
    brands = {b: hosts for b, hosts in _SELLER_CACHE.items()
              if b not in _FETCH_FAILED}
    redirects = {h: t for h, t in _REDIRECT_CACHE.items() if t}
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "officiality 判定缓存：brands=品牌→开发者站主机；redirects=域→重定向终点。"
                "来源 App Store sellerUrl/developerUrl + HTTP 重定向跟随（§14/§15 ✓）。"
                "脚本可重生成（删掉本文件即可）。",
        "brands": brands,
        "redirects": redirects,
    }
    try:
        tmp = f"{target}.tmp"
        Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001
        pass


def _itunes_search(brand: str, country: str = "us") -> List[Dict[str, Any]]:
    url = (f"https://itunes.apple.com/search?term={brand}"
           f"&entity=software&limit={_LIMIT}&country={country}")
    req = urlrequest.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlrequest.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    return list(data.get("results") or [])


def _host_has_brand(host: str, brand: str) -> bool:
    """开发者站主机要**含品牌词**——linktree/落地页不算开发者站 ✗。"""
    bare = (host or "").lower()
    bare = bare[4:] if bare.startswith("www.") else bare
    return bool(brand) and bool(brand in bare)


def _hosts_related(a: str, b: str) -> bool:
    """同站判定：相等或互为子域（studio.hangjam.ai ↔ hangjam.ai ✓）。"""
    a = (a or "").lower().lstrip(".")
    b = (b or "").lower().lstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# 重定向终点缓存：host → 终点主机（None = 不可达/无重定向 ✓）
_REDIRECT_CACHE: Dict[str, Optional[str]] = {}


def redirect_target_host(host: str, timeout: int = 8) -> Optional[str]:
    """这个域的重定向终点主机（一次 HTTP，进程缓存 ✓；失败 None = 无豁免 ✓）。"""
    key = (host or "").lower()
    if not key:
        return None
    if key in _REDIRECT_CACHE:
        return _REDIRECT_CACHE[key]
    target: Optional[str] = None
    for scheme in ("https", "http"):
        try:
            opener = urlrequest.build_opener()  # 默认就跟随重定向 ✓
            opener.addheaders = [("User-Agent", "Mozilla/5.0")]
            with opener.open(f"{scheme}://{host}/", timeout=timeout) as resp:
                target = (urlparse(resp.geturl()).hostname or "").lower()
                break
        except Exception:  # noqa: BLE001 —— 探不到就没有豁免，按 suspect 继续 ✓
            continue
    _REDIRECT_CACHE[key] = target
    return target


def seller_hosts_for_brand(brand: str, fetch: Optional[Any] = None) -> List[str]:
    """这个品牌的开发者站主机列表（进程级缓存 ✓；失败返回 [] → unknown ✓）。

    fetch 可注入（测试/离线复算用 ✓）；生产默认 iTunes Search API。
    只认**同品牌 App**（trackName 归一化品牌一致 ✓）且 sellerUrl 主机
    含品牌词的（linktree 类不算 ✓）。
    """
    key = normalized_brand(brand)
    if not key:
        return []
    if key in _SELLER_CACHE:
        return _SELLER_CACHE[key]
    sellers: List[str] = []
    ok = True
    try:
        results = (fetch or _itunes_search)(key)
        for item in results or []:
            name = normalized_brand(str((item or {}).get("trackName") or ""))
            # 同品牌 App：App Store 的标题常带后缀（真实案例 ✗：
            # "Talkie Lab - AI Playground" 的规范化是 talkielab ≠ talkie）——
            # 精确等值会漏掉本尊 ✗；用**品牌词包含**（key ⊆ 名字，len≥3 防
            # "AI" 这类通用词通配 ✗）。异品牌（TalkAI练口语/Character AI）不含
            # talkie ✓ 仍被过滤。
            if len(key) < 3 or key not in name:
                continue
            for field in ("sellerUrl", "developerUrl"):
                host = host_of(str((item or {}).get(field) or ""))
                if host and _host_has_brand(host, key):
                    if host not in sellers:
                        sellers.append(host)
    except Exception:  # noqa: BLE001 —— 查询失败按 unknown 处理，不拖垮链路 ✓
        sellers = []
        ok = False
    _SELLER_CACHE[key] = sellers
    if not ok:
        # **失败不落盘**（第十七轮教训 #106 ✗）：查不到 ≠ 没有开发者站，
        # 失败结果进盘会把"间歇失败"固化成"永久 unknown"✗。
        _FETCH_FAILED.add(key)
    return sellers


def classify_officiality(candidate_host: str, brand: str,
                         fetch: Optional[Any] = None,
                         redirect_lookup: Optional[Any] = None) -> str:
    """verified / suspect / unknown（unknown 永不罚 ✓）。

    **重定向豁免**（2026-11-02 行核误伤案例 ✗）：旧官方域 replika.ai
    200 重定向到新官方域 replika.com（= sellerUrl）——直接比对会把它
    误标 suspect ✗。所以 direct 不中时，跟一次候选域的重定向终点
    （进程缓存 ✓，fetch 注入测试 ✓），落在开发者站上即 verified ✓。
    """
    key = normalized_brand(brand)
    if not key:
        return "unknown"
    host = (host_of(candidate_host) or "").lower()
    if not host:
        return "unknown"
    sellers = seller_hosts_for_brand(key, fetch=fetch)
    if not sellers:
        return "unknown"
    if any(_hosts_related(host, s) for s in sellers):
        return "verified"
    lookup = redirect_lookup or redirect_target_host
    target = lookup(host)
    if target and any(_hosts_related(target, s) for s in sellers):
        return "verified"
    return "suspect"


def annotate_officiality(items: Sequence[Any], max_lookups: int = 16,
                         fetch: Optional[Any] = None,
                         cache_path: Optional[str] = None) -> Dict[str, Any]:
    """给候选批量标注 officiality（verified/suspect/unknown），返回统计。

    items 是候选对象（有 .name/.url/.officiality）或等价 dict——用 setattr
    同时支持两者 ✓。有界：最多查 max_lookups 个**不同品牌**（缓存命中不算 ✓）。
    cache_path 给出时：先读盘（跨进程复用 ✓），结束后写穿（下轮免查 ✓，
    iTunes 间歇失败也有兜底 ✓——失败品牌不落盘 ✗ 见 seller_hosts_for_brand）。
    """
    if cache_path:
        set_disk_cache(cache_path)
    counts = {"verified": 0, "suspect": 0, "unknown": 0}
    looked_up = 0
    for item in items:
        name = str(getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else "") or "")
        url = str(getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else "") or "")
        brand = normalized_brand(name) or name.strip().casefold()
        if looked_up < max_lookups or _SELLER_CACHE.get(normalized_brand(brand)):
            verdict = classify_officiality(url, brand, fetch=fetch)
            if not _SELLER_CACHE.get(normalized_brand(brand)):
                looked_up += 1
        else:
            verdict = "unknown"
        counts[verdict] = counts.get(verdict, 0) + 1
        try:
            setattr(item, "officiality", verdict)
        except Exception:  # noqa: BLE001 —— dict 候选走 setdefault
            if isinstance(item, dict):
                item["officiality"] = verdict
    if cache_path:
        save_disk_cache(cache_path)
    return {"counts": counts, "lookups": looked_up,
            "suspects": [str(getattr(i, "name", None) or (i.get("name") if isinstance(i, dict) else ""))
                         for i in items
                         if (getattr(i, "officiality", None)
                             or (i.get("officiality") if isinstance(i, dict) else "")) == "suspect"]}
