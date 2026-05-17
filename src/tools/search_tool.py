"""搜索工具模块 - 封装搜索引擎API"""
import requests
from typing import List, Dict, Optional
from src.config.settings import (
    SERPAPI_KEY,
    SEARXNG_URL,
    SEARCH_PROVIDER,
    SEARCH_TIMEOUT,
    SEARCH_MAX_RESULTS
)
from src.security.url_validation import validate_fetch_url, UnsafeURL


class SearchTool:
    """搜索工具封装 - 支持SearXNG、SerpAPI和Bing Search"""

    def __init__(self, api_key: Optional[str] = None):
        self.serpapi_key = api_key or SERPAPI_KEY
        self.searxng_url = SEARXNG_URL.rstrip("/")
        self.default_provider = SEARCH_PROVIDER
        self.timeout = SEARCH_TIMEOUT
        self.max_results = SEARCH_MAX_RESULTS

    def search_with_searxng(self, query: str, num_results: int = None) -> List[Dict[str, str]]:
        """
        使用 SearXNG 进行搜索（默认推荐）

        Args:
            query: 搜索关键词
            num_results: 返回结果数量

        Returns:
            搜索结果列表
        """
        url = f"{self.searxng_url}/search"
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
            "language": "zh-CN",
            "limit": num_results or self.max_results
        }

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            results = []
            for result in data.get("results", []):
                if result.get("url"):
                    results.append({
                        "url": result["url"],
                        "title": result.get("title", ""),
                        "content": result.get("content", ""),
                        "source_type": "searxng"
                    })

            return results[:num_results or self.max_results]

        except Exception as exc:
            raise RuntimeError("SearXNG search failed") from exc

    def search_with_serpapi(self, query: str, num_results: int = None) -> List[Dict[str, str]]:
        """
        使用SerpAPI进行搜索

        Args:
            query: 搜索关键词
            num_results: 返回结果数量

        Returns:
            搜索结果列表
        """
        if not self.serpapi_key:
            raise ValueError("SerpAPI key is not configured")

        url = "https://serpapi.com/search"
        params = {
            "q": query,
            "api_key": self.serpapi_key,
            "num": num_results or self.max_results,
            "engine": "google"
        }

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            results = []
            for result in data.get("organic_results", []):
                results.append({
                    "url": result.get("link", ""),
                    "title": result.get("title", ""),
                    "content": result.get("snippet", ""),
                    "source_type": "search"
                })

            return results

        except Exception as exc:
            raise RuntimeError("SerpAPI search failed") from exc

    def search_with_bing(self, query: str, num_results: int = None) -> List[Dict[str, str]]:
        """
        使用Bing Search API进行搜索

        Args:
            query: 搜索关键词
            num_results: 返回结果数量

        Returns:
            搜索结果列表
        """
        import urllib.parse

        url = "https://www.bing.com/search"
        params = {
            "q": query,
            "count": num_results or self.max_results
        }

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")

            results = []
            for item in soup.find_all("li", class_="b_algo"):
                title_tag = item.find("h2")
                link_tag = item.find("a")
                snippet_tag = item.find("p")

                if title_tag and link_tag:
                    results.append({
                        "url": link_tag.get("href", ""),
                        "title": title_tag.get_text(strip=True),
                        "content": snippet_tag.get_text(strip=True) if snippet_tag else "",
                        "source_type": "search"
                    })

            return results[:num_results or self.max_results]

        except Exception as exc:
            raise RuntimeError("Bing search failed") from exc

    def search(self, query: str, num_results: int = None, provider: str = None) -> List[Dict[str, str]]:
        """
        统一搜索接口（主入口）

        Args:
            query: 搜索关键词
            num_results: 返回结果数量
            provider: 搜索提供商 ("searxng", "serpapi", "bing")，默认使用配置值

        Returns:
            经过安全校验的搜索结果列表
        """
        selected_provider = (provider or self.default_provider).strip().lower()

        providers = {
            "searxng": self.search_with_searxng,
            "serpapi": self.search_with_serpapi,
            "bing": self.search_with_bing,
        }

        handler = providers.get(selected_provider)
        if not handler:
            raise ValueError(f"Unknown search provider: {selected_provider}")

        raw_results = handler(query, num_results)

        safe_results = []
        for r in raw_results:
            try:
                if not r.get("url"):
                    continue
                validate_fetch_url(r["url"])
                safe_results.append(r)
            except (UnsafeURL, ValueError):
                continue

        return safe_results


# 懒加载工厂函数
_search_tool_instance = None

def get_search_tool() -> SearchTool:
    """获取搜索工具实例（懒加载）"""
    global _search_tool_instance
    if _search_tool_instance is None:
        _search_tool_instance = SearchTool()
    return _search_tool_instance
