"""网页爬虫模块 - 支持公开URL抓取和登录场景"""
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Optional, Dict
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

        page = None
        retry_delay = 1

        for attempt in range(MAX_RETRIES):
            try:
                page = await self.browser.new_page()
                await page.route("**/*", self._guard_outbound_request)
                page.set_default_timeout(TIMEOUT_SECONDS * 1000)

                if auth_credentials and login_url:
                    await page.goto(login_url)

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

                await page.goto(url)

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
                return text_content

            except Exception as e:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

                if attempt < MAX_RETRIES - 1:
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
