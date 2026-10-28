"""搜索工具模块 - 封装搜索引擎API"""
import re

import requests
from typing import List, Dict, Optional

# 长中文整句的相关性下限：命中的二元组长度占比
_LONG_QUERY_COVERAGE_MIN = 0.25
from src.config.settings import (
    SERPAPI_KEY,
    SEARXNG_URL,
    SEARCH_PROVIDER,
    SEARCH_TIMEOUT,
    SEARCH_MAX_RESULTS,
    SEARCH_FALLBACK_PROVIDERS,
)
from src.security.url_validation import validate_fetch_url, UnsafeURL


class SearchTool:
    """搜索工具封装 - 支持SearXNG、SerpAPI和Bing Search"""

    _CJK_RE = re.compile(r"[\u4e00-\u9fff]")

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
        """统一搜索接口（主入口）。

        主 provider 失败或结果**全部与查询不相关**时，按
        `SEARCH_FALLBACK_PROVIDERS` 顺序重试。相关性过滤不是锦上添花：
        Bing 网页抓取实测会被污染（查“美菜”返回 CVE/秘塔），
        不过滤就会把无关页面当成竞品喂给下游。
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

        tried = []
        safe_results: List[Dict[str, str]] = []
        for name in [selected_provider] + [
            p for p in SEARCH_FALLBACK_PROVIDERS if p != selected_provider
        ]:
            fn = providers.get(name)
            if fn is None:
                continue
            tried.append(name)
            try:
                raw_results = fn(query, num_results)
            except Exception:
                continue
            safe_results = self._filter_results(raw_results, query)
            if safe_results:
                break

        if tried and not safe_results:
            # 明确返回空（而不是把污染结果硬塞下去）：调用方会走
            # “搜索无结果”的分支，不会拿无关页面生成假报告。
            return []
        return safe_results

    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        """检索单元：空格/标点切分的主 token + 长中文整句的二元组。

        中文整句没有空格，直接拿整串去匹配会把**所有**结果都过滤掉
        （整串几乎不可能出现在标题/摘要里）——那是比污染更糟的静默失效。
        长句用二元组当检索单元，区分度够，又不至于把整串当成唯一条件。
        """
        pieces = [t for t in re.split(r"[\s,，、;；|/]+", query or "") if len(t) >= 2]
        tokens: List[str] = []
        for piece in pieces:
            if piece not in tokens:
                tokens.append(piece)
            if len(piece) > 4 and SearchTool._CJK_RE.search(piece):
                for index in range(len(piece) - 1):
                    bigram = piece[index:index + 2]
                    if bigram not in tokens:
                        tokens.append(bigram)
        return tokens

    @staticmethod
    def _primary_tokens(query: str) -> List[str]:
        """主 token：切分后的词，以及长度 ≤6 的整句（短查询按整句判定）。"""
        pieces = [t for t in re.split(r"[\s,，、;；|/]+", query or "") if len(t) >= 2]
        whole = (query or "").strip()
        if 2 <= len(whole) <= 6 and whole not in pieces:
            pieces.append(whole)
        return pieces

    def _is_relevant(self, haystack: str, query: str) -> bool:
        """相关性判定：主 token 命中即算相关；长整句按二元组覆盖度判定。"""
        haystack = (haystack or "").lower()
        primary = self._primary_tokens(query)
        if any(token.lower() in haystack for token in primary):
            return True
        pieces = set(primary)
        bigrams = [
            token for token in self._query_tokens(query)
            if len(token) == 2 and token not in pieces
        ]
        if not bigrams:
            return False
        hit = sum(len(token) for token in bigrams if token.lower() in haystack)
        total = sum(len(token) for token in bigrams)
        return bool(total) and (hit / total) >= _LONG_QUERY_COVERAGE_MIN

    def _filter_results(self, raw_results: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
        safe_results = []
        for r in raw_results or []:
            try:
                if not r.get("url"):
                    continue
                validate_fetch_url(r["url"])
                if self._query_tokens(query):
                    haystack = " ".join([
                        str(r.get("title", "")),
                        str(r.get("content", "")),
                        str(r.get("url", "")),
                    ])
                    if not self._is_relevant(haystack, query):
                        continue
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
