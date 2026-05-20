"""网页爬虫模块 - 支持公开URL抓取和登录场景"""
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Optional, Dict
from urllib.parse import urljoin, urlparse, urlunparse
from src.config.settings import TIMEOUT_SECONDS, MAX_RETRIES
from src.security.url_validation import validate_fetch_url
import asyncio


class WebCrawler:
    """网页内容抓取工具"""

    def __init__(self, request_interval: float = 1.0):
        self.browser = None
        self.context = None
        self.request_interval = request_interval
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
    ) -> Dict[str, object]:
        """Fetch homepage and high-value internal pages as one research profile."""
        homepage = await self._fetch_page_snapshot(
            url,
            auth_credentials=auth_credentials,
            login_url=login_url,
            custom_selectors=custom_selectors,
            wait_for_content=wait_for_content,
        )
        pages = [homepage]
        discovered_urls = self.discover_research_links(url, homepage["html"], max_links=max(0, max_pages - 1))

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
    def discover_research_links(base_url: str, html_content: str, max_links: int = 4) -> List[str]:
        """Discover same-origin pages likely to contain research evidence."""
        base = urlparse(validate_fetch_url(base_url))
        soup = BeautifulSoup(html_content, "html.parser")
        candidates = []
        seen = set()

        priority_terms = [
            (100, ["pricing", "price", "plans", "billing", "cost", "定价", "价格", "计费", "套餐"]),
            (90, ["docs", "doc", "quickstart", "guide", "developer", "文档", "开发", "接入"]),
            (85, ["models", "model", "providers", "engines", "模型", "供应商"]),
            (80, ["api", "sdk", "integration", "integrations", "接口", "集成"]),
            (65, ["workflow", "flow", "route", "routing", "工作流", "流程", "路由"]),
            (55, ["console", "dashboard", "控制台", "仪表盘"]),
            (45, ["faq", "help", "support", "常见问题", "帮助", "支持"]),
        ]

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

            evidence = f"{anchor.get_text(' ', strip=True)} {parsed.path} {parsed.query}".lower()
            score = 0
            for priority, terms in priority_terms:
                if any(term.lower() in evidence for term in terms):
                    score = priority
                    break
            if score <= 0:
                continue

            try:
                validate_fetch_url(normalized)
            except ValueError:
                continue

            seen.add(normalized)
            candidates.append((score, order, normalized))

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


def fetch_page_sync(
    url: str,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None
) -> str:
    """同步版本的页面抓取"""
    return asyncio.run(_fetch_page_async(url, auth_credentials, login_url))


async def _fetch_page_async(
    url: str,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None
) -> str:
    """异步页面抓取内部函数"""
    async with WebCrawler() as crawler:
        return await crawler.fetch_page(url, auth_credentials, login_url)
