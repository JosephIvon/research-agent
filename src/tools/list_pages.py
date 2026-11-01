"""目录分类页/集合页的抓取与条目抽取（召回通道 C3）。

为什么要有这个通道：赛道全集 289 条里，模型能提名的那部分（头部）
早就被找到了，"仅独立来源"口径的召回只有 0.0044——差的是**长尾**。
长尾在哪？在导航站/目录站的**分类页**里：一页几十个产品，
而且这些页面是**服务端渲染**的（实测 aigc.cn 的 OneNav 主题：
`<strong>产品名</strong>` + `title` 属性写描述）。

实测（2026-11-01）：
- aigc.cn/favorites/ai-role-play-chatbot → 200，57 KB，22 条站外链接，带产品名与描述 ✓
- ai-bot.cn/favorites/ai-chatbots/ → 200，151 KB ✓
- aigc.cn 首页的赛道分类是**锚点/JS 加载**的（`#term-6373-6381`），
  首页 HTML 里没有那些产品链接 ✗ → 要抓**集合页/分类页**，别啃首页 tab。
- 目录站**普遍没有 JSON-LD**（aigc.cn / ai-bot.cn / xingyeai.com 实测 4/4 无）✗
  → 判"这是不是一个列表页"不能靠 schema.org，只能靠路径形态 + 链接结构。

口径纪律（重要）：这个通道读的就是**外部目录页**，
而赛道金标准里有一部分条目**也来自这些页面**。
所以通道读过的页所对应的条目，不能算作"发现"（那叫读答案）。
见 tool_build_track_split.py 的切分规则。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

# ----------------------------------------------------------------------
# 目录站主机（通道的取材范围）
# ----------------------------------------------------------------------
# 只读这些站上的**分类页/集合页**。这份名单是"生产侧的取材范围"，
# 同时也是金标准切分规则的输入（切分脚本 import 同一份名单，
# 避免"通道读哪些页"与"测量剔除哪些条目"两处各写一遍而走偏）。
DIRECTORY_HOSTS = (
    "www.aigc.cn",
    "aigc.cn",
    "ai-bot.cn",
    "www.ai-bot.cn",
    "www.aitoolnet.com",
    "aitoolnet.com",
    "seektool.ai",
    "www.seektool.ai",
    "theaisurf.com",
    "www.theaisurf.com",
    "app.aibase.com",
    "aibase.com",
    "www.aicpb.com",
    "aicpb.com",
    "www.similarlabs.com",
    "similarlabs.com",
    "www.aiangels.io",
    "aiangels.io",
    "xix.ai",
    "www.xix.ai",
    "www.nieta.art",
    "nieta.art",
    "www.unite.ai",
    "unite.ai",
    "www.aitoolsoasis.com",
    "aitoolsoasis.com",
    "www.toolmage.com",
    "toolmage.com",
    "www.reverie.im",
    "reverie.im",
    "ai.fly63.com",
    "www.ai-bot.cn",
    "www.aitoollab.com",
    "aitoollab.com",
)

# 路径形态：分类页/集合页/榜单页（不是产品自己的页面）
LISTING_PATH_HINTS = (
    "/category/", "/categories/", "/category", "/favorites/", "/favorites",
    "/class/", "/class", "/cate/", "/cate", "/tools/", "/tools", "/s/",
    "/rank", "/ranking", "/list", "/collection", "/collections", "/best-",
    "/top-", "/ai-", "/zh/", "/en/",
)

# 明显不是"产品列表页"的路径（厂商写的横评/指南/新闻）
NOT_LISTING_PATH_HINTS = (
    "/blog/", "/news/", "/post/", "/posts/", "/guide", "/guides/", "/article",
    "/articles/", "/review", "/reviews/", "/how-to", "/tutorial",
)

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_DEFAULT_TIMEOUT = 20


@dataclass
class ListEntry:
    """列表页里的一条产品条目。"""
    name: str
    url: str
    description: str = ""
    source_page: str = ""


@dataclass
class ListPageResult:
    url: str
    ok: bool = False
    error: str = ""
    entries: List[ListEntry] = field(default_factory=list)
    elapsed_s: float = 0.0


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _bare_host(host: str) -> str:
    """去掉 www. 前缀的主机名（比较主机时用）。"""
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


# 目录站给的链接常带来源标记（?ref=aitoolnet.com&utm_source=…），
# 留着会让同一个产品出现成好几个 URL，白占名额 ✗
_TRACKING_PARAMS = ("ref", "from", "source", "cp_id", "spm",
                    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _strip_tracking_params(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    # 有些目录站给的 href 里 query 后面又带了一个 "?"（"?cp_id=x?ref=y"），
    # 取第一个问号之后的部分，剩下的当噪声丢掉
    query = parts.query.split("?", 1)[0]
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in _TRACKING_PARAMS or key.startswith("utm_"):
            continue
        kept.append(pair)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


def is_directory_host(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def is_listing_shape(url: str) -> bool:
    """这个 URL 的**路径**像"分类/集合/榜单页"吗？（不看主机）"""
    path = (urlparse(url).path or "").lower()
    if not path or path == "/":
        return False
    if any(hint in path for hint in NOT_LISTING_PATH_HINTS):
        return False
    return any(hint in path for hint in LISTING_PATH_HINTS)


def is_listing_url(url: str) -> bool:
    """这个 URL 像"目录站的分类/集合页"吗？

    判据（都来自实测）：路径像分类页 + 主机在已知目录站名单里。
    名单只是**快路径**——不在名单里的站也能用（见 looks_like_directory）。
    """
    if not is_directory_host(url):
        return False
    return is_listing_shape(url)


def looks_like_directory(html_text: str, base_url: str, min_entries: int = 12) -> bool:
    """结构性判断"这是不是一个目录站"。

    目录站的招牌：首页列着一大批**站外产品**（每条都有名字）——
    实测 aigc.cn 首页有 291 个站外域名、ai-bot.cn 首页同理 ✓。
    比硬编码主机名单更通用（名单会过期），也不用猜。
    """
    return len(extract_entries(html_text, base_url)) >= min_entries


# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------
def fetch_html(url: str, timeout: int = _DEFAULT_TIMEOUT) -> tuple[str, str]:
    """抓一个页面，返回 (html, error)。抓不到不抛异常——通道不该拖垮整轮。

    先直连；直连在 TLS/连接层失败时，用环境里的 HTTP(S)_PROXY 再试一次
    （实测 seektool.ai 直连 SSL 失败、经本地代理可取到 ✓）。
    """
    import os
    import time

    import requests

    started = time.time()
    headers = {"User-Agent": _USER_AGENT}
    attempts: List[Dict[str, Any]] = [{"headers": headers}]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        attempts.append({
            "headers": headers,
            "proxies": {"http": proxy, "https": proxy},
        })
    last_error = ""
    for kwargs in attempts:
        try:
            response = requests.get(url, timeout=timeout, **kwargs)
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                continue
            response.encoding = response.apparent_encoding or "utf-8"
            return response.text, ""
        except Exception as exc:  # noqa: BLE001 —— 网络异常一律按"这页没抓到"处理
            last_error = f"{type(exc).__name__}: {exc}"
    _ = time.time() - started
    return "", last_error


# ----------------------------------------------------------------------
# 条目抽取
# ----------------------------------------------------------------------
_ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_STRONG_RE = re.compile(r"<(strong|h3|h4|title)\b[^>]*>(.*?)</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 常见的"标题属性"（很多目录站把描述放在 title 里）
_DESC_ATTR_RE = re.compile(r"title=[\"']([^\"']{4,})[\"']", re.I)

# 页脚/备案/友链这类"不是产品"的链接宿主
_BOILERPLATE_HOSTS = (
    "beian.miit.gov.cn",
    "miit.gov.cn",
    "beianx.cn",
    "gitee.com",
    "github.com",
    "qq.com",
    "weibo.com",
    "bilibili.com",
    "zhihu.com",
)

# 目录站里当按钮用的通用锚文本（不是产品名）
_GENERIC_ANCHOR_NAMES = {
    "访问", "点击", "点击进入", "进入", "官网", "官方网站", "打开", "详情", "查看",
    "立即访问", "更多", "read more", "visit", "click here", "open", "link",
    "more", "enter", "website", "official", "home", "详情页", "前往",
}

# 页脚起始标记：只解析正文里的链接（页脚的友链/备案链接不是产品条目）
_FOOTER_RE = re.compile(r"<footer\b|</footer\b|<div[^>]+class=[\"'][^\"']*footer", re.I)

# 目录站的廉价预筛标记（标题/路径里出现这些字样才值得抓首页验一验）
_DIRECTORY_MARKERS = (
    "导航", "目录", "工具集", "工具箱", "导航站", "收录", "分类",
    "nav", "directory", "toolbox", "collection",
)

# ---- 进程内缓存 -------------------------------------------------------
# 通道每次跑都要重复做同一件事（同样的 site: 检索 + 同样的页面抓取），
# 而一轮实跑要跑 12 次（2 主体 × 2 句式 × 3 重复）。
# 不缓存的话，12 次重复检索会多花十几分钟 ✗。
# 缓存的键：分类页 = (host, keyword)；抓取结果 = 页面 URL。
_CATEGORY_CACHE: Dict[str, List[str]] = {}
_ENTRIES_CACHE: Dict[str, List[ListEntry]] = {}


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _split_name_desc(anchor_html: str, page_url: str = "") -> tuple[str, str]:
    """从一段 <a> 里拆出 (产品名, 描述)。

    实测 aigc.cn（OneNav 主题）：产品名在 <strong> 里，描述在 title 属性里，
    <p> 里也有一份描述。其它站点常见的是 <h3>/<h4>/<img alt>。
    """
    strong = _STRONG_RE.search(anchor_html)
    if strong:
        name = _clean_text(strong.group(2))
    else:
        img_alt = re.search(r'<img[^>]+alt=["\']([^"\']{2,})["\']', anchor_html, re.I)
        if img_alt:
            name = _clean_text(img_alt.group(1))
        else:
            # 退而求其次：去掉标签后的前几个词
            flat = _clean_text(anchor_html)
            name = flat.split(" ")[0] if flat else ""
            if len(name) > 40:
                name = name[:40]
    desc_match = _DESC_ATTR_RE.search(anchor_html)
    desc = _clean_text(desc_match.group(1)) if desc_match else ""
    if not desc:
        p = re.search(r"<p\b[^>]*>(.*?)</p>", anchor_html, re.S | re.I)
        if p:
            desc = _clean_text(p.group(1))
    # 锚文本是"访问/Visit"这种按钮词 → 名字要从 title 描述或域名里取
    if name.strip().lower() in _GENERIC_ANCHOR_NAMES:
        fallback = ""
        if desc:
            fallback = re.split(r"[-–—|:：，,]", desc)[0].strip()
        if not fallback or fallback.lower() in _GENERIC_ANCHOR_NAMES:
            fallback = ""
        if not fallback:
            label = (urlparse(page_url).netloc or "").lower()
            if label.startswith("www."):
                label = label[4:]
            fallback = label.split(".")[0]
        name = fallback
    return name, desc


def _devalue_resolve(payload: List[Any], index: Any, depth: int = 0) -> Any:
    """解 devalue 扁平数组里的引用（值是整数 → 数组下标）。

    Nuxt 把页面数据序列化成一个扁平数组：{"productName":18} 里的 18 是下标，
    真正的名字在 payload[18]。这不是标准 JSON 引用，所以要自己解。
    """
    if depth > 6:
        return None
    if isinstance(index, bool):
        return index
    if isinstance(index, int):
        if 0 <= index < len(payload):
            return payload[index]
        return None
    return index


def extract_entries_from_payload(payload_text: str, page_url: str) -> List[ListEntry]:
    """从页面内嵌的 JSON 载荷里抽产品条目（Nuxt / Next 那种"数据在脚本里"的站）。

    实测 aibase 的 /zh/class/情感陪伴 就是这种：产品名和链接在
    `<script type="application/json">` 的 devalue 扁平数组里，页面上没有 <a>。
    """
    import json

    try:
        payload = json.loads(payload_text)
    except Exception:  # noqa: BLE001 —— 不是合法 JSON 就当没有载荷
        return []
    if not isinstance(payload, list):
        return []
    entries: List[ListEntry] = []
    seen: set = set()
    name_keys = ("productName", "name", "title", "appName", "toolName")
    url_keys = ("url", "link", "website", "homepage", "siteUrl", "officialUrl")
    for item in payload:
        if not isinstance(item, dict):
            continue
        resolved = {}
        for key, value in item.items():
            if isinstance(value, int) and not isinstance(value, bool):
                resolved[key] = _devalue_resolve(payload, value)
            else:
                resolved[key] = value
        name = next((str(resolved[k]) for k in name_keys
                     if resolved.get(k) and isinstance(resolved.get(k), (str, int))), "")
        url = next((str(resolved[k]) for k in url_keys
                    if resolved.get(k) and isinstance(resolved.get(k), str)
                    and str(resolved[k]).startswith("http")), "")
        if not name or not url:
            continue
        host = host_of(url)
        if not host or host in seen:
            continue
        seen.add(host)
        desc = str(resolved.get("info") or resolved.get("description") or resolved.get("summary") or "")
        entries.append(ListEntry(name=name.strip(), url=url.strip(),
                                 description=desc.strip(), source_page=page_url))
    return entries


_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I)


def extract_entries(html_text: str, page_url: str) -> List[ListEntry]:
    """从列表页 HTML 里抽出产品条目（站外链接 + 产品名 + 描述）。

    实测踩过的坑（都要挡掉，否则抽出来一堆"不是产品"的链接）：
    - 页脚友链/备案链接 → 只解析 <footer> 之前的正文 ✓
    - 一页里同一个站多个链接（/ 与 /about） → 按主机去重 ✓
    - 锚文本是一整句描述 → 优先取 <strong>/<h3>/<h4> 里的产品名 ✓
    - 备案号被当成产品名（"蜀ICP备2022019184号-2"）→ 备案/友链宿主直接跳过 ✓
    """
    base_host = host_of(page_url)
    footer = _FOOTER_RE.search(html_text or "")
    body = html_text[: footer.start()] if footer else (html_text or "")
    entries: List[ListEntry] = []
    seen: set = set()
    for match in _ANCHOR_RE.finditer(body):
        href = html.unescape((match.group(1) or "").strip())
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(page_url, href)
        full = _strip_tracking_params(full)
        host = host_of(full)
        if not host or host == base_host:
            continue          # 站内链接不是产品条目
        if host.endswith("aigc.cn"):
            continue          # 同站的不同域名仍算站内
        if any(host == b or host.endswith("." + b) for b in _BOILERPLATE_HOSTS):
            continue          # 备案/友链/代码托管不是产品
        key = host
        if key in seen:
            continue
        anchor_html = match.group(0) or ""
        name, desc = _split_name_desc(anchor_html, full)
        if not name or len(name) < 2:
            continue
        if re.search(r"[A-Za-z]{0,4}ICP备|ICP证|公安备案", name):
            continue          # 备案号不是产品名
        seen.add(key)
        entries.append(ListEntry(name=name, url=full, description=desc, source_page=page_url))
    # 数据在脚本里的站（aibase 就是）：走 JSON 载荷那条路，**排在锚文本前面**
    # ——页面顶部的导航链接（news./geo./mcp.）不是产品条目，结构化数据更可信。
    payload_entries: List[ListEntry] = []
    for match in _JSON_SCRIPT_RE.finditer(body):
        payload_entries.extend(extract_entries_from_payload(match.group(1), page_url))
    if payload_entries:
        merged = list(payload_entries)
        merged_hosts = {host_of(e.url) for e in payload_entries}
        for entry in entries:
            if host_of(entry.url) not in merged_hosts:
                merged.append(entry)
        entries = merged
    return entries


def harvest(page_urls: Sequence[str],
            track_keywords: Sequence[str] = (),
            timeout: int = _DEFAULT_TIMEOUT,
            max_entries: int = 40) -> List[ListPageResult]:
    """抓若干个列表页，抽条目，并按赛道词做**粗过滤**。

    粗过滤的理由：目录站的分类页里常混着别的品类（AI 写作/AI 绘画）。
    这里只按**赛道词**粗筛（名字或描述命中），真正的"是不是同赛道"
    交给准入闸门（名册 + 赛道词 + 跨行业负例）判——分工要清楚。
    """
    results: List[ListPageResult] = []
    for url in page_urls:
        html, error = fetch_html(url, timeout=timeout)
        result = ListPageResult(url=url, ok=bool(html), error=error)
        if html:
            entries = extract_entries(html, url)
            if track_keywords:
                entries = [
                    e for e in entries
                    if any(kw and kw.lower() in (e.name + " " + e.description).lower()
                           for kw in track_keywords)
                ]
            result.entries = entries[:max_entries]
        results.append(result)
    return results


def find_listing_pages(search_results: Iterable[Dict[str, Any]],
                        limit: int = 5) -> List[str]:
    """从搜索结果里挑出"像目录分类页"的 URL（按主机+路径形态筛）。"""
    out: List[str] = []
    seen: set = set()
    for item in search_results or []:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        if not is_listing_url(url):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def _slug_text(url: str, anchor_text: str) -> str:
    """把 URL 路径与锚文本压成可匹配的文本（连字符/下划线当空格）。"""
    path = urlparse(url).path or ""
    text = f"{path} {anchor_text}".lower().replace("-", " ").replace("_", " ").replace("/", " ")
    return re.sub(r"\s+", " ", text)


def keyword_hit(text: str, keywords: Sequence[str]) -> Optional[str]:
    """文本里命中哪个赛道词（路径/锚文本都算）。

    比对时**去掉所有空白**：赛道词写的是「AI 角色扮演」，
    页面锚文本写的是「AI角色扮演」、slug 写的是「ai-role-play」——
    空白不一致不该算没命中 ✓。
    """
    lowered = (text or "").lower()
    squashed = re.sub(r"\s+", "", lowered)
    for keyword in keywords:
        kw = (keyword or "").strip().lower()
        if not kw:
            continue
        if kw in lowered:
            return keyword
        if re.sub(r"\s+", "", kw) in squashed:
            return keyword
    return None


def find_category_pages(
    html_text: str,
    base_url: str,
    track_keywords: Sequence[str],
    limit: int = 4,
) -> List[str]:
    """从目录站首页里找**跟本赛道相关**的分类页链接。

    实测：目录站的分类页在首页 HTML 里是普通 <a>（aigc.cn 首页有 24 个
    /favorites/xxx 链接 ✓），所以"首页 → 分类页"这一步可以直接解析 HTML 做，
    不用浏览器。赛道相关性用**路径/锚文本里的赛道词**判（slug 里
    ai-role-play-chatbot 这类命名 ✓）。
    """
    base_host = host_of(base_url)
    footer = _FOOTER_RE.search(html_text or "")
    body = html_text[: footer.start()] if footer else (html_text or "")
    scored: List[Tuple[int, str]] = []
    seen: set = set()
    for match in _ANCHOR_RE.finditer(body):
        href = (match.group(1) or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base_url, href)
        if host_of(full) != base_host:
            continue                      # 只看站内链接（分类页在站内）
        path = (urlparse(full).path or "").lower()
        if not path or path == "/":
            continue
        if any(hint in path for hint in NOT_LISTING_PATH_HINTS):
            continue                      # 文章页不是分类页
        if not any(hint in path for hint in LISTING_PATH_HINTS):
            continue                      # 路径不像分类页
        anchor_text = _clean_text(match.group(2) or "")[:80]
        hit = keyword_hit(_slug_text(full, anchor_text), track_keywords)
        if not hit:
            continue
        if full in seen:
            continue
        seen.add(full)
        scored.append((len(hit), full))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [url for _, url in scored[:limit]]


def collect_channel_items(
    search_fn,
    track_keywords: Sequence[str],
    max_homepages: int = 3,
    max_category_pages: int = 4,
    max_site_searches: int = 8,
    max_entries_per_page: int = 40,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """C3 通道：搜索找目录站 → 抓首页 → 找赛道分类页 → 抓分类页 → 抽条目。

    返回 (条目列表, 过程说明, 读过的分类页)。条目是 {title, url, content}
    —— 与搜索结果**同形**，可以直接并进发现阶段的候选池 ✓。
    读过的页要记下来：**金标准里来源正好是这些页的条目不能算"发现"**
    （那叫读答案，测量时要把它们从分母里剔掉）。

    为什么要这条通道：赛道全集里"模型不知道、搜索也搜不到"的长尾产品，
    大量只出现在目录站的分类页上（一页几十个），而这些页面是服务端渲染的
    （实测 aigc.cn 的 /favorites/* 页带 <strong> 产品名 + title 描述 ✓）。
    """
    notes: List[str] = []
    keywords = [k for k in (track_keywords or []) if k]
    if not keywords:
        return [], ["C3 通道跳过：本轮没有赛道词"], []
    items: List[Dict[str, Any]] = []
    pages_read: List[str] = []

    # 1) 目录站候选：
    #    主路 = **已知目录站名单**（生产资产：这些是公开的 AI 工具目录站，
    #    名单里只有"去哪儿找"，没有"哪些产品在那儿"——不构成答案泄漏）；
    #    备路 = 搜索"赛道词 + 导航"再按结构认目录站。
    hosts: List[str] = []
    for host in DIRECTORY_HOSTS:
        if host not in hosts:
            hosts.append(host)
    if len(hosts) < 4:
        for query in (f"{keywords[0]} 导航 分类", "AI 工具导航站 分类"):
            try:
                results = search_fn(query) or []
            except Exception as exc:  # noqa: BLE001 —— 通道失败不能拖垮整轮
                notes.append(f"C3 通道搜索失败（{query}）：{type(exc).__name__}")
                continue
            for result in results:
                url = str(result.get("url") or "")
                host = _bare_host(host_of(url))
                title = str(result.get("title") or "")
                if not host or host in ("baike.baidu.com", "zh.wikipedia.org"):
                    continue
                marker = f"{url} {title}".lower()
                if not any(k in marker for k in _DIRECTORY_MARKERS):
                    continue
                if host not in hosts:
                    hosts.append(host)
    notes.append(f"C3 通道：目录站候选 {len(hosts)} 个")

    # 2) 找赛道分类页：
    #    `site:<host> <赛道词>` —— 实测能搜到目录站挂着的分类页
    #    （site:aigc.cn 角色扮演 → /favorites/ai-role-play-chatbot ✓）；
    #    再补一层：首页里挂着的分类页（有些站首页就有分类链接 ✓）。
    category_pages: List[str] = []
    for host in hosts[:max_site_searches]:
        if len(category_pages) >= max_category_pages:
            break
        for keyword in keywords[:2]:          # 两个赛道词都试（缓存后只多花一次）
            cache_key = f"{host}|{keyword}"
            if cache_key in _CATEGORY_CACHE:
                found = _CATEGORY_CACHE[cache_key]
            else:
                try:
                    results = search_fn(f"site:{host} {keyword}") or []
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"C3 通道 site: 检索失败（{host} {keyword}）：{type(exc).__name__}")
                    continue
                found = []
                for result in results:
                    url = str(result.get("url") or "")
                    if _bare_host(host_of(url)) != _bare_host(host) or not is_listing_shape(url):
                        continue
                    if url not in found:
                        found.append(url)
                _CATEGORY_CACHE[cache_key] = found
            for url in found:
                if url not in category_pages:
                    category_pages.append(url)
            if found:
                break
    notes.append(f"C3 通道：找到 {len(category_pages)} 个赛道分类页")

    # 3) 抓分类页 → 抽条目
    for page in category_pages[:max_category_pages]:
        pages_read.append(page)
        if page in _ENTRIES_CACHE:
            entries = _ENTRIES_CACHE[page]
        else:
            html, error = fetch_html(page, timeout=timeout)
            if error:
                notes.append(f"C3 通道：{page} 抓取失败（{error[:40]}）")
                _ENTRIES_CACHE[page] = []
                continue
            entries = extract_entries(html, page)
            _ENTRIES_CACHE[page] = entries
        kept = 0
        for entry in entries:
            # **不在条目层再过赛道词**：这一页本身就是赛道分类页
            # （URL/标题命中了赛道词才被选进来 ✓），
            # 页里的条目再按词筛一遍会把正常产品全筛掉
            # （实测：小冰岛/筑梦岛/万话的描述里没有"AI 情感陪伴"这类字样 ✗）。
            # "是不是同赛道"由**准入闸门**判（名册 + 跨行业词），分工在这里。
            items.append({
                "title": entry.name,
                "url": entry.url,
                "content": entry.description or entry.name,
            })
            kept += 1
            if kept >= max_entries_per_page:
                break
        notes.append(f"C3 通道：{page} → {kept} 条入池")
    return items, notes, pages_read


def listing_page_hosts() -> set:
    """通道取材范围里的主机集合（切分脚本用同一份名单）。"""
    return set(DIRECTORY_HOSTS)
