"""网页爬虫模块 - 支持公开URL抓取和登录场景"""
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Optional, Dict
from urllib.parse import urljoin, urlparse, urlunparse
from src.config.settings import TIMEOUT_SECONDS, MAX_RETRIES, CRAWL_REQUEST_INTERVAL
from src.security.url_validation import validate_fetch_url
import asyncio
import re


# --------------------------------------------------------------------------
# 站内链接发现策略
#
# ## 这组默认权重是**先验，不是真理**
#
# `DEFAULT_PRIORITY_TIERS` 编码的是"SaaS 风格竞品调研"这一种场景的判断：
# 定价 > 文档 > 模型 > 接口 > 工作流。对医疗、金融、工业软件、工具类产品
# 并不成立——它们的 `/适应症`、`/利率`、`/许可`、`/规格` 一律落在最低档。
# 所以它是**可被覆盖的默认值**，而不是写死在函数里的常量。
#
# ## 为什么零分不再是硬边界
#
# 原实现是 `if score <= 0: continue`：匹配不到任何词条的链接**永远不会被考虑**。
# `/sla`、`/security`、`/compliance`、`/enterprise-seats` 全是 0 分——
# 表外的页面不是排在后面，是**不存在**。这等于把"我不知道"伪装成"没有"。
#
# 改为：打分只决定**顺序**，不决定**有无**。真正的过滤交给 `excluded_segments`
# ——那是一组**可枚举的**噪音路径（登录、购物车、法务、招聘），
# 与不可穷举的"有用页面"词表性质不同，适合做硬排除。
# --------------------------------------------------------------------------

DEFAULT_PRIORITY_TIERS = (
    (100, ("pricing", "price", "plans", "billing", "cost",
           "定价", "价格", "计费", "套餐")),
    (90, ("docs", "doc", "quickstart", "guide", "developer",
          "文档", "开发", "接入")),
    (85, ("models", "model", "providers", "engines",
          "模型", "供应商")),
    (80, ("api", "sdk", "integration", "integrations",
          "接口", "集成")),
    (65, ("workflow", "flow", "route", "routing",
          "工作流", "流程", "路由")),
    (55, ("console", "dashboard", "控制台", "仪表盘")),
    (45, ("faq", "help", "support", "常见问题", "帮助", "支持")),
)

# 多档命中加成：首匹配即 break 是原实现的一处顺序依赖——同时命中
# pricing 和 api 的 URL 只按先命中的算，与实际价值无关。改成取最高档 +
# 每多命中一档加一点分，顺序就不再影响结果。
_MULTI_TIER_BONUS = 3

# 与"竞品调研"无关的噪音路径。与优先级词表不同，这组是**封闭**的：
# 登录/购物车/法务/招聘在任何行业都与调研无关，可以硬排除。
DEFAULT_EXCLUDED_SEGMENTS = frozenset({
    "login", "logout", "signin", "signout", "signup", "register",
    "auth", "account", "profile", "cart", "checkout", "basket",
    "privacy", "terms", "legal", "cookie", "cookies", "gdpr",
    "careers", "career", "jobs", "job", "press", "media",
    "newsletter", "subscribe", "unsubscribe", "rss", "sitemap",
    "sitemap.xml", "robots.txt", "wp-admin", "wp-login",
})

# 默认排除集内部的拆分，依据是 2026-10-15 的真实站点实测（方案文档 §5.5.1）：
# 19 个真实竞品站里有 2 个**产品页**被误伤——
#   plaid.com/products/auth      （Plaid 的 Auth 产品页，竞品能力核心）
#   brex.com/product/business-account（Brex Business 产品页）
# 根因：`auth` / `account` 既是账户状态词，**又是常见产品名**，
# 而 `_is_excluded` 原本对路径任意深度的 token 一视同仁。
#
# 所以这两类词必须区别对待：
#   文档/板块类（login / legal / careers …）—— 任意深度都排除，
#       否则 /pricing/login 这类从定价页链过去的登录入口会混进候选。
#   账户状态类（auth / account）—— 只在**首段**排除，
#       否则 /products/auth、/product/business-account 这类产品页被静默丢掉。
#
# 只移动**实测到碰撞**的词。`profile` / `register` 同样可疑，但本轮
# 24 站实测里没有观察到碰撞，没有证据就不动——留待有实测时再调整。
_EXCLUDE_FIRST_SEGMENT_ONLY = frozenset({"auth", "account"})


def _term_hit(term: str, haystack: str) -> bool:
    """关键词是否命中。

    **ASCII 词按词边界匹配，中文词按子串匹配。**
    中文没有词边界，`定价` 只能子串；但英文必须加 `\\b`——裸子串会让
    `api` 命中 `rapid` / `capital` / `therapist`，`model` 命中 `remodeling`，
    把无关页面顶进高分区。
    """
    if term.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])"
        return re.search(pattern, haystack) is not None
    return term in haystack


def _segment_tokens(seg: str) -> set:
    """把一个路径段拆成可与排除集比对的 token 集合（含紧凑写法与去扩展名形式）。

    `sign-in` → {signin, sign, in}，`terms.html` → {terms.html, terms}，
    `accountability-report` → {accountability-report, accountability, report}。
    """
    tokens = {seg}
    compact = seg.replace("-", "").replace("_", "")
    if compact and compact != seg:
        tokens.add(compact)
    for part in seg.replace("%20", " ").replace("_", "-").split("-"):
        part = part.strip()
        if not part:
            continue
        tokens.add(part)
        stem = part.split(".", 1)[0]
        if stem and stem != part:
            tokens.add(stem)
    return tokens


def _is_excluded(url_path: str, excluded: frozenset) -> bool:
    """路径是否落在噪音段里。

    按 **token 精确比对**，token 由 `/ - _ .` 切出：
    `privacy-policy` → {privacy, policy}，命中排除集；
    而 `accountability-report` → {accountability, report}，**不**命中——
    子串匹配会把 accountability 误判成 account 从而丢掉合规报告页。

    切分是必需的：连字符路径（`privacy-policy`、`sign-in`、`add-to-cart`）
    在真实站点里比裸单词常见得多，只按整段比对等于排除集形同虚设。

    但切分必须**与紧凑写法并存**：`sign-in` 切成 `{sign, in}` 谁都不匹配，
    而排除集里写的是 `signin`。所以额外比对一次去掉分隔符的紧凑形式。

    **首段与非首段区别对待**：默认排除集里的 `auth` / `account` 既是账户状态词
    又是常见产品名，只在**首段**排除（`/auth` 排除，`/products/auth` 保留）；
    其余词任意深度都排除（`/pricing/login` 仍排除）。依据见
    `_EXCLUDE_FIRST_SEGMENT_ONLY` 的注释——真实站点实测出的产品页误伤。

    调用方传入**自定义**排除集时不做这个拆分，一律按任意深度排除：
    自定义集合没有经过这次分类论证，不该继承默认集的语义。
    """
    split_by_depth = excluded == DEFAULT_EXCLUDED_SEGMENTS
    seen = 0
    for raw in url_path.split("/"):
        seg = raw.strip().lower()
        if not seg:
            continue
        # 首段要按**非空段**计数：路径开头的 `/` 会切出一个空串，
        # 直接用下标会把 `/auth` 的首段算成 index 1，于是首段规则整个失效。
        is_first = seen == 0
        seen += 1
        for token in _segment_tokens(seg):
            if token not in excluded:
                continue
            if split_by_depth and not is_first and token in _EXCLUDE_FIRST_SEGMENT_ONLY:
                continue
            return True
    return False


class WebCrawler:
    """网页内容抓取工具"""

    def __init__(self, request_interval: float = None):
        self.browser = None
        self.context = None
        self.request_interval = request_interval if request_interval is not None else CRAWL_REQUEST_INTERVAL
        self.default_selectors = {
            "username": [
                'input[name="username"]', 'input[name="user"]', 'input[name="login"]',
                'input[id="username"]', 'input[id="user"]', 'input[type="email"]',
                'input[placeholder*="用户"]', 'input[placeholder*="账号"]'
            ],
            "password": [
                'input[name="password"]', 'input[id="password"]', 'input[type="password"]',
                'input[placeholder*="密码"]'
            ],
            "submit": [
                'button[type="submit"]', 'input[type="submit"]', 'button.login',
                'button:has-text("登录")', 'button:has-text("Sign In")',
                'button:has-text("登 录")', 'a:has-text("登录")'
            ]
        }
        self.content_selectors = [
            "article", "main", "[role='main']", ".content", ".main-content",
            "#content", "#main", ".container", ".page-content", "body"
        ]

    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """异步上下文管理器退出"""
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()

    async def _find_element_with_fallback(self, page, selectors: List[str]) -> Optional[str]:
        """尝试多个选择器，找到第一个可用的元素"""
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=5000)
                return selector
            except Exception:
                continue
        return None

    async def _wait_for_content(self, page, timeout: int = 5000) -> None:
        """等待页面内容加载完成（SPA支持）"""
        for selector in self.content_selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout)
                return
            except Exception:
                continue

    async def fetch_page(
        self,
        url: str,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None,
        wait_for_content: bool = True
    ) -> str:
        """
        抓取网页内容

        Args:
            url: 目标URL
            auth_credentials: 登录凭据 {"username": "", "password": ""}
            login_url: 登录页面URL（可选，默认使用目标URL）
            custom_selectors: 自定义选择器 {"username": [...], "password": [...], "submit": [...]}
            wait_for_content: 是否等待内容加载（SPA页面需要）
        """
        validate_fetch_url(url)
        if login_url:
            validate_fetch_url(login_url)

        snapshot = await self._fetch_page_snapshot(
            url,
            auth_credentials=auth_credentials,
            login_url=login_url,
            custom_selectors=custom_selectors,
            wait_for_content=wait_for_content,
        )
        return snapshot["content"]

    async def fetch_site_profile(
        self,
        url: str,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None,
        wait_for_content: bool = True,
        max_pages: int = 3,
        discovered_page_timeout: int = 15,
        priority_tiers: Optional[tuple] = None,
    ) -> Dict[str, object]:
        """Fetch homepage and high-value internal pages as one research profile.

        priority_tiers：可选。透传给 `discover_research_links` 的站内发现排序。
        默认是 SaaS 风格先验；维度补抓用它注入维度意图（见 dimension_refetch），
        让 discover 优先命中该维度对应的页面。
        """
        homepage = await self._fetch_page_snapshot(
            url,
            auth_credentials=auth_credentials,
            login_url=login_url,
            custom_selectors=custom_selectors,
            wait_for_content=wait_for_content,
        )
        pages = [homepage]
        discovered_urls = self.discover_research_links(
            url, homepage["html"], max_links=max(0, max_pages - 1),
            priority_tiers=priority_tiers,
        )

        for discovered_url in discovered_urls:
            try:
                page = await self._fetch_page_snapshot(
                    discovered_url,
                    auth_credentials=auth_credentials,
                    login_url=login_url,
                    custom_selectors=custom_selectors,
                    wait_for_content=wait_for_content,
                    timeout_seconds=discovered_page_timeout,
                    max_attempts=1,
                )
                pages.append(page)
            except Exception:
                continue

        return {
            "url": url,
            "content": self.combine_page_texts(pages),
            "pages": pages,
            "discovered_urls": discovered_urls,
        }

    async def _fetch_page_snapshot(
        self,
        url: str,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None,
        wait_for_content: bool = True,
        timeout_seconds: Optional[int] = None,
        max_attempts: Optional[int] = None,
    ) -> Dict[str, str]:
        validate_fetch_url(url)
        if login_url:
            validate_fetch_url(login_url)

        page = None
        retry_delay = 1

        attempts = max_attempts or MAX_RETRIES
        page_timeout_ms = (timeout_seconds or TIMEOUT_SECONDS) * 1000

        for attempt in range(attempts):
            try:
                page = await self.browser.new_page()
                await page.route("**/*", self._guard_outbound_request)
                page.set_default_timeout(page_timeout_ms)

                if auth_credentials and login_url:
                    await page.goto(login_url, wait_until="domcontentloaded", timeout=page_timeout_ms)

                    selectors = {**self.default_selectors, **(custom_selectors or {})}

                    username_selector = await self._find_element_with_fallback(page, selectors["username"])
                    if not username_selector:
                        raise Exception("无法找到用户名输入框")

                    password_selector = await self._find_element_with_fallback(page, selectors["password"])
                    if not password_selector:
                        raise Exception("无法找到密码输入框")

                    await page.fill(username_selector, auth_credentials["username"])
                    await page.fill(password_selector, auth_credentials["password"])

                    submit_selector = await self._find_element_with_fallback(page, selectors["submit"])
                    if submit_selector:
                        await page.click(submit_selector)
                    else:
                        await page.press(password_selector, "Enter")

                    await page.wait_for_timeout(2000)

                await page.goto(url, wait_until="domcontentloaded", timeout=page_timeout_ms)

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass

                if wait_for_content:
                    await self._wait_for_content(page)

                await asyncio.sleep(self.request_interval)

                content = await page.content()
                text_content = self._parse_html(content)

                try:
                    await page.close()
                except Exception:
                    pass
                return {"url": url, "content": text_content, "html": content}

            except asyncio.CancelledError:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                raise
            except Exception as e:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

                if attempt < attempts - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue

                raise e

    async def fetch_multiple_pages(
        self,
        urls: List[str],
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None
    ) -> List[Dict[str, str]]:
        """批量抓取多个页面"""
        results = []

        for url in urls:
            try:
                content = await self.fetch_page(url, auth_credentials, login_url, custom_selectors)
                results.append({
                    "url": url,
                    "content": content,
                    "timestamp": datetime.now(),
                    "status": "success"
                })
            except Exception as e:
                results.append({
                    "url": url,
                    "content": "",
                    "timestamp": datetime.now(),
                    "status": "failed",
                    "error": str(e)
                })

        return results

    @staticmethod
    def discover_research_links(
        base_url: str,
        html_content: str,
        max_links: int = 4,
        priority_tiers: Optional[tuple] = None,
        min_score: int = 0,
        excluded_segments: Optional[frozenset] = None,
    ) -> List[str]:
        """发现同源页面，按「可能含调研证据」排序后取前 `max_links` 条。

        参数
        ----
        priority_tiers   : `((权重, (关键词, ...)), ...)`，默认 `DEFAULT_PRIORITY_TIERS`。
                           **默认值是 SaaS 风格调研的先验，换行业应整表覆盖。**
        min_score        : 低于此分丢弃。默认 0 = 不丢，只排序。
                           想恢复旧行为传 1，但这会把表外页面重新变成"不存在"。
        excluded_segments: 噪音路径段（登录/购物车/法务/招聘…），默认见模块常量。

        打分只决定**顺序**，过滤只由 `excluded_segments` 决定——
        两者分工明确，不再让"没匹配到关键词"兼职"不值得抓"。
        """
        tiers = DEFAULT_PRIORITY_TIERS if priority_tiers is None else priority_tiers
        excluded = (DEFAULT_EXCLUDED_SEGMENTS if excluded_segments is None
                    else excluded_segments)
        base = urlparse(validate_fetch_url(base_url))
        soup = BeautifulSoup(html_content, "html.parser")
        candidates = []
        seen = set()

        base_normalized = urlunparse((base.scheme, base.netloc, base.path.rstrip("/") or "/", "", base.query, ""))
        for order, anchor in enumerate(soup.find_all("a", href=True)):
            href = anchor.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            absolute_url = urljoin(base_url, href)
            parsed = urlparse(absolute_url)
            if parsed.netloc != base.netloc or parsed.scheme not in ("http", "https"):
                continue

            normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, ""))
            if normalized in seen or normalized == base_normalized:
                continue

            if excluded and _is_excluded(parsed.path, excluded):
                seen.add(normalized)
                continue

            evidence = f"{anchor.get_text(' ', strip=True)} {parsed.path} {parsed.query}".lower()
            # 累加所有命中的档位，而不是首命中即 break：
            # 原实现让**词表顺序**决定结果，同时命中 pricing 和 api 的 URL
            # 只按先命中的算。改成"取最高档 + 每多命中一档加一点分"后，
            # 顺序不再影响打分，多主题页（如 /docs/pricing-api）也能体现价值。
            matched = [priority for priority, terms in tiers
                       if any(_term_hit(term, evidence) for term in terms)]
            score = 0 if not matched else (
                max(matched) + _MULTI_TIER_BONUS * (len(matched) - 1)
            )
            if score < min_score:
                continue

            try:
                validate_fetch_url(normalized)
            except ValueError:
                continue

            seen.add(normalized)
            candidates.append((score, order, normalized))

        # 同分按文档出现顺序，保证同一份 HTML 永远给出同一结果
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return [url for _, _, url in candidates[:max_links]]

    @staticmethod
    def combine_page_texts(pages: List[Dict[str, str]]) -> str:
        sections = []
        for page in pages:
            content = page.get("content", "")
            if not content:
                continue
            sections.append(f"### Source URL: {page.get('url', '')}\n{content}")
        return "\n\n".join(sections)

    def _parse_html(self, html_content: str) -> str:
        """将HTML转换为纯文本"""
        soup = BeautifulSoup(html_content, "html.parser")

        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.decompose()

        text = soup.get_text(separator="\n")
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())

        return text

    async def _guard_outbound_request(self, route, request):
        """Abort browser requests that would target unsafe networks."""
        try:
            validate_fetch_url(request.url)
        except ValueError:
            await route.abort()
            return
        await route.continue_()


async def _fetch_page_async(
    url: str,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None
) -> str:
    """异步页面抓取内部函数"""
    async with WebCrawler() as crawler:
        return await crawler.fetch_page(url, auth_credentials, login_url)
