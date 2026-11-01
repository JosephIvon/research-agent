"""C4 应用商店通道：商店 Web 版的分类/榜单页是服务端渲染的产品清单。

为什么要这条通道（2026-11-01 拍板，第十一轮主攻）：
赛道全集里 **store_only 来源有 93 条（32%）**——这些产品在商店里有页、
在开放 Web 上几乎搜不到（搜索 API 的抽样不可复现，教训 #见 measurement §8.5），
是 R1（0.3982 FAIL）最大的已知增量空间。

形态与 C3（目录页通道）同构 ✓：
1. 已知商店名单（**只有"去哪儿找"，没有"哪些产品"——不构成答案泄漏 ✓**）；
2. `site:<host> <赛道词>` 找分类/榜单页（失败单独记账，不作废整轮 ✓）；
3. 抓页抽条目（复用 list_pages 的锚文本抽取 + JSON-LD ✓）；
4. 读过的页记下来 → 测量时把"来源正好是这些页"的金标准条目剔出分母
   （**读答案 ≠ 发现** ✓；store_only 条目尤其要防这个 ✗）。

与 C3 的差别：
- 分类页形态判据不同（genre/charts/category/榜单 vs 目录站的 favorites/category）；
- 条目要过一层**商店 App 详情页形态**过滤（商店页里导航链接很多 ✗）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from src.tools.list_pages import (
    _DEFAULT_TIMEOUT,
    fetch_html,
    host_of,
)

# 商店 Web 版主机 → 该店"App 详情页"的路径形态。
# 只收**服务端渲染**的店（JS 渲染的店抓不到条目，列进来只会浪费配额 ✗）。
STORE_HOSTS: Dict[str, Tuple[str, ...]] = {
    # App Store Web：genre 页 / charts 页都是 SSR，App 详情在 /<store>/app/<名>/id<> ✓
    "apps.apple.com": ("/app",),
    # 小米应用商店 Web：分类页 SSR，详情是 details.html ✓
    "app.mi.com": ("details.html", "/detail"),
    # 豌豆荚 Web：分类页 SSR，详情是 /apps/<包名> ✓
    "www.wandoujia.com": ("/apps/",),
}

# 分类/榜单页的路径形态（site: 命中后用这个挑"哪些页值得抓"）
_STORE_LISTING_RE = re.compile(
    r"/(genre|charts|category|categories|ranking|rank|top|list|classification)"
    r"|分类|榜单|排行|热门",
    re.I,
)

# 进程内缓存（同 C3：一轮 12 次实跑不重复抓同一页 ✓）
_STORE_SEARCH_CACHE: Dict[str, List[str]] = {}
_STORE_ENTRIES_CACHE: Dict[str, list] = {}
_OFFICIAL_SITE_CACHE: Dict[str, Optional[str]] = {}

# 官网链接抽取：商店详情页里的"开发者网站 / 官网 / 点击查看"外链。
# 实测（2026-11-01）：小米 details.html 与 App Store 详情页都是 SSR ✓，
# 筑梦岛的详情页直接带 h5.zhumengdao.com（官方域）✓✓——
# **商店 URL 本身既匹配不上金标准（金标准记的是产品自己的域）✗、
# 也过不了"必须有官网"的闸门 ✗**，所以这条富化是 C4 有 R1 信用的前提 ✓。
_APP_ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_GENERIC_ANCHOR = {"下载", "查看", "详情", "更多", "进入", "打开", "app", "下载app"}

# 这些域不可能是"产品的官网"（平台自身/应用分发/社交/备案）
_NON_OFFICIAL_HOSTS = (
    "apple.com", "icloud.com", "mi.com", "xiaomi.com", "miui.com", "duokan.com",
    "wandoujia.com", "25pp.com", "qq.com", "weibo.com", "weixin.qq.com",
    "baidu.com", "tieba.baidu.com", "zhihu.com", "douyin.com", "bilibili.com",
    "kuaishou.com", "coolapk.com", "huawei.com", "dbankcloud", "hicloud",
    "google.com", "play.google.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "tiktok.com", "reddit.com",
    "discord.com", "telegram.org", "12377.cn", "beian.miit.gov.cn",
    "appgallery.huawei.com",
)

# Apple 的 App 详情路径里带 app id（同 App 的多地区 URL 共享一个 id ✓）
_APP_ID_RES = {
    "apps.apple.com": re.compile(r"/app/(?:[^/?#]*/)?(id\d+)"),
    "app.mi.com": re.compile(r"details\?id=([\w.]+)"),
    "www.wandoujia.com": re.compile(r"/apps/([\w.]+)"),
}


def store_app_key(url: str) -> Optional[Tuple[str, str]]:
    """App 的稳定键 (商店主机, app id)——同一 App 的多地区 URL 共享一键 ✓。"""
    host = store_host_of(url)
    if not host:
        return None
    match = _APP_ID_RES.get(host).search(url) if _APP_ID_RES.get(host) else None
    if not match:
        return None
    return (host, match.group(1))


def _clean_anchor_text(raw: str) -> str:
    text = _TAG_RE.sub("", raw or "")
    return _WS_RE.sub(" ", text).strip()


def extract_app_anchors(html_text: str, page_url: str, host: str,
                        limit: int = 60) -> List[Dict[str, str]]:
    """商店分类/榜单页 → App 详情条目（名字 + 详情 URL）。

    list_pages.extract_entries 对商店页太严（实测豌豆荚 152 个 /apps/ 链接
    只出 1 条 ✗），商店页用它自己的锚形态抽 ✓。
    """
    from urllib.parse import urljoin, unquote

    out: List[Dict[str, str]] = []
    seen: set = set()
    pattern = _APP_ID_RES.get(host)
    if pattern is None:
        return out
    for match in _APP_ANCHOR_RE.finditer(html_text or ""):
        href = (match.group(1) or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(page_url, href)
        if not is_app_detail_url(full, host) or not pattern.search(full):
            continue
        key = store_app_key(full)
        if key and key in seen:
            continue
        name = _clean_anchor_text(match.group(2) or "")
        if not name or name.lower() in _GENERIC_ANCHOR:
            # 锚文本是按钮词 → 从 URL slug 还原（App Store 的路径带 %编码名 ✓）
            m2 = re.search(r"/app/([^/?#]+)", full)
            name = unquote(m2.group(1)) if m2 else ""
        if not name:
            continue
        if key:
            seen.add(key)
        out.append({"name": name, "url": full, "description": ""})
        if len(out) >= limit:
            break
    return out


def extract_official_site(html_text: str, store_host: str,
                          limit_hosts: int = 6) -> Optional[str]:
    """商店详情页 → 开发者官网（外链启发式）。

    优先级：锚文本/URL 带 官网/开发者网站/website 提示的 > 其余外链。
    平台/分发/社交域一律不算（_NON_OFFICIAL_HOSTS ✓）。
    返回 scheme://host 形式（产品自己的站点根 ✓）。
    """
    from urllib.parse import urljoin

    if not html_text:
        return None
    hints = ("官网", "官方网站", "开发者网站", "developer website", "website",
             "homepage", "主页", "点击查看", "official")
    fallback: Optional[str] = None
    seen: set = set()
    for match in _APP_ANCHOR_RE.finditer(html_text):
        href = (match.group(1) or "").strip()
        if not href.startswith(("http://", "https://")):
            full = urljoin(f"https://{store_host}/", href)
        else:
            full = href
        try:
            from urllib.parse import urlparse
            host = (urlparse(full).netloc or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if not host or host in seen:
            continue
        seen.add(host)
        bare = host[4:] if host.startswith("www.") else host
        if any(bad in bare for bad in _NON_OFFICIAL_HOSTS):
            continue
        text = _clean_anchor_text(match.group(2) or "").lower()
        if any(h in text or h in bare for h in hints):
            return f"{urlparse(full).scheme}://{host}"
        if fallback is None:
            fallback = f"{urlparse(full).scheme}://{host}"
        if len(seen) >= limit_hosts * 3:
            break
    return fallback


def store_host_of(url: str) -> Optional[str]:
    """这个 URL 是不是已知商店的页？返回规范化主机（www. 前缀折叠）。"""
    host = (host_of(url) or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    for known in STORE_HOSTS:
        if host == known or bare == known:
            return known
    return None


def is_app_detail_url(url: str, host: Optional[str] = None) -> bool:
    """这个 URL 是不是该商店的**App 详情页**（不是导航/首页/分类页）。"""
    host = host or store_host_of(url)
    if not host:
        return False
    path = (urlparse(url).path or "").lower()
    return any(marker in path for marker in STORE_HOSTS[host])


def is_store_listing_url(url: str) -> bool:
    """这个 URL 是不是商店的**分类/榜单页**形态。"""
    if not store_host_of(url):
        return False
    path = (urlparse(url).path or "").lower()
    if not path or path == "/":
        return False
    return bool(_STORE_LISTING_RE.search(path))


def find_store_pages(search_results: Sequence[Dict[str, Any]]) -> List[str]:
    """从 site: 搜索结果里挑出**商店分类/榜单页**。"""
    found: List[str] = []
    for result in search_results:
        url = str((result or {}).get("url") or "")
        if not url or not is_store_listing_url(url):
            continue
        if url not in found:
            found.append(url)
    return found


def collect_store_items(
    search_fn,
    track_keywords: Sequence[str],
    max_hosts: int = 3,
    max_keywords: int = 2,
    max_pages: int = 4,
    max_entries_per_page: int = 40,
    max_detail_fetches: int = 10,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """C4 通道：商店名单 → `site:` 找分类/榜单页 → 抓页 → 抽 App 条目。

    两条路（实测 2026-11-01）：
    - **列表页收割**：site: 命中分类/榜单页 → 抓一页收几十个 App
      （豌豆荚 /category/6006 ✓，App Store genre 页 ✓）——这是通道的本钱 ✓；
    - **详情页直取**：Apple/小米的 site: 命中**直接就是 App 详情页**
      （1 查询 = 1 产品 ✓）——商店深页在常规查询里根本不出现 ✓，
      直取进池；详情页是"一查一得"的正常搜索，**不算读答案** ✓
      （读答案专指"一页几十个答案"的列表页 ✗）。

    返回 (条目, 过程说明, 读过的页)——条目 {title,url,content} 与搜索结果同形 ✓；
    读过的**列表页**必须由调用方记进产物（测量剔"通道自证条目"用 ✓；
    详情页不是答案页，不进这个清单 ✓）。
    """
    notes: List[str] = []
    keywords = [k for k in (track_keywords or []) if k]
    if not keywords:
        return [], ["C4 通道跳过：本轮没有赛道词"], []

    items: List[Dict[str, Any]] = []
    pages_read: List[str] = []
    seen_urls: set = set()
    seen_app_keys: set = set()

    def _add(title: str, url: str, content: str) -> bool:
        if not url or url in seen_urls:
            return False
        # 同一 App 的多地区 URL 只留一条（实测：同一产品在 Apple 有
        # cn/us/tw/hk/ga/sc 六个区，六条 URL 会互相白占去重与名额 ✗）
        key = store_app_key(url)
        if key:
            if key in seen_app_keys:
                return False
            seen_app_keys.add(key)
        seen_urls.add(url)
        items.append({"title": title, "url": url,
                      "content": content or title})
        return True

    for host in list(STORE_HOSTS)[:max_hosts]:
        for keyword in keywords[:max_keywords]:
            if len(pages_read) >= max_pages:
                break
            cache_key = f"{host}|{keyword}"
            if cache_key in _STORE_SEARCH_CACHE:
                results = _STORE_SEARCH_CACHE[cache_key]
            else:
                try:
                    results = search_fn(f"site:{host} {keyword}") or []
                except Exception as exc:  # noqa: BLE001 —— 通道失败不拖垮整轮
                    notes.append(f"C4 通道 site: 检索失败（{host} {keyword}）：{type(exc).__name__}")
                    continue
                _STORE_SEARCH_CACHE[cache_key] = results

            # 路 (b)：详情页直取（Apple/小米的 site: 命中形态 ✓）
            direct = 0
            pages = find_store_pages(results)
            page_set = set(pages)
            for result in results:
                url = str((result or {}).get("url") or "")
                if not url or url in page_set:
                    continue
                if not is_app_detail_url(url, host):
                    continue
                if _add(str(result.get("title") or ""), url,
                        str(result.get("content") or "")):
                    direct += 1
            if direct:
                notes.append(f"C4 通道：site:{host} {keyword} 详情页直取 {direct} 条")

            # 路 (a)：列表页收割（一页几十个 ✓）
            for page in pages:
                if page in pages_read or len(pages_read) >= max_pages:
                    continue
                pages_read.append(page)
                if page in _STORE_ENTRIES_CACHE:
                    entries = _STORE_ENTRIES_CACHE[page]
                else:
                    html, error = fetch_html(page, timeout=timeout)
                    if error:
                        notes.append(f"C4 通道：{page} 抓取失败（{error[:40]}）")
                        _STORE_ENTRIES_CACHE[page] = []
                        continue
                    entries = extract_app_anchors(html, page, host)
                    _STORE_ENTRIES_CACHE[page] = entries
                kept = 0
                for entry in entries:
                    if kept >= max_entries_per_page:
                        break
                    # 不在条目层再过赛道词（同 C3 的教训：分类页本身就是
                    # 赛道相关页 ✗ 再筛会把正常产品全筛掉）；"同赛道"由闸门判 ✓。
                    if _add(entry["name"], entry["url"], entry["description"] or ""):
                        kept += 1
                notes.append(f"C4 通道：{page} → {kept} 条入池")

    # 官网富化：商店 URL 匹配不上金标准（金标准记产品自己的域 ✗）、
    # 也过不了"必须有官网"的闸门 ✗ → 从详情页抽开发者官网并**替换 URL** ✓。
    # 有界（max_detail_fetches ✓）+ 进程内缓存（12 次实跑只抓一次 ✓）。
    fetched = 0
    for item in items:
        if fetched >= max_detail_fetches:
            break
        store_url = str(item.get("url") or "")
        host = store_host_of(store_url)
        if not host or store_url in _OFFICIAL_SITE_CACHE:
            continue
        html, error = fetch_html(store_url, timeout=timeout)
        official = None
        if not error:
            official = extract_official_site(html, host)
        _OFFICIAL_SITE_CACHE[store_url] = official
        fetched += 1
    enriched = 0
    for item in items:
        official = _OFFICIAL_SITE_CACHE.get(str(item.get("url") or ""))
        if official and item.get("url") != official:
            item["url"] = official
            item["content"] = f"{item.get('content') or item.get('title') or ''}（来自商店页）"
            enriched += 1
    if enriched:
        notes.append(f"C4 通道：{enriched} 条替换为开发者官网链接（详情页抓取 {fetched} 次）")
    if not items:
        notes.append("C4 通道：没有可用的商店分类/榜单页或详情页（命中 0）")
    return items, notes, pages_read


def store_page_hosts() -> set:
    """通道取材范围里的商店主机集合（切分脚本/测量对账用）。"""
    return set(STORE_HOSTS)
