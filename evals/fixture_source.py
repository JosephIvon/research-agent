"""夹具站点源：用本地 HTML 文件替换 WebCrawler，同一 profile 契约。

网络边界（真实爬取/搜索）不在评测被测范围——由契约测试与 e2e-contract
覆盖。注入防护不在此层：sanitize / canaries / 整页丢弃都在工作流的
`_fetch_single_competitor` 里，替换 WebCrawler 恰好把防护留在被测路径上。
"""
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CASES_DIR = Path(__file__).parent / "cases"


class FixtureSiteSource:
    """WebCrawler 替身：按 URL 主机映射到夹具目录，返回真实形状的 profile。

    同时记录 fetch 计数与页面数——react 对照 case 的"少抓多少页"从这里取数。
    """

    def __init__(self, sites: List[Dict[str, Any]], case_dir: Path):
        self._by_host = {}
        for site in sites:
            host = site["url"].split("/")[2]
            self._by_host[host] = {
                "url": site["url"],
                "key": site["key"],
                "dir": case_dir / "sites" / site["key"],
                "pages": site.get("pages", ["index.html"]),
            }
        self.fetch_calls = 0
        self.pages_served = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _read_page(self, site: Dict[str, Any], filename: str) -> Dict[str, str]:
        raw = (site["dir"] / filename).read_text(encoding="utf-8")
        page_url = site["url"].rstrip("/") + "/" + filename
        return {"url": page_url, "content": raw, "html": raw}

    async def fetch_site_profile(self, url: str, max_pages: int = 3, **kwargs) -> Dict[str, Any]:
        host = url.split("/")[2]
        site = self._by_host.get(host)
        if site is None:
            raise FileNotFoundError(f"夹具未登记该主机: {host}")
        self.fetch_calls += 1
        # 与真实爬虫同语义：max_pages 截断站内页数（react 对照 case 依赖此差异）
        pages = [self._read_page(site, name) for name in site["pages"][:max(1, max_pages)]]
        self.pages_served += len(pages)
        return {
            "url": url,
            "content": "\n\n".join(
                f"### Source URL: {p['url']}\n{p['content']}" for p in pages
            ),
            "pages": pages,
            "discovered_urls": [],
        }


class FixtureSearch:
    """SearchTool 替身：返回 case 预置的候选结果（react 对照 case 用）。"""

    def __init__(self, results: List[Dict[str, str]]):
        self.results = results
        self.queries: List[str] = []

    def search(self, query: str, num_results=None, provider=None) -> List[Dict[str, str]]:
        self.queries.append(query)
        return self.results[: num_results or len(self.results)]


def _fixture_bytes(path: Path) -> bytes:
    """归一化后的夹具字节：去 UTF-8 BOM + CRLF→LF。

    摘要必须跨平台一致：Windows 工作区常带 BOM/CRLF（PowerShell 写文件、
    git autocrlf），Linux CI checkout 是 LF——直接哈希原始字节会让防篡改
    校验在不同平台互相误伤（首轮 CI 实测踩坑：本地全绿、CI 全红）。
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw  # 二进制夹具原样参与哈希
    return text.replace("\r\n", "\n").encode("utf-8")


def load_manifest(verify_digests: bool = True) -> Dict[str, Any]:
    manifest_path = CASES_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if verify_digests:
        mismatches = verify_fixture_digests(manifest)
        if mismatches:
            raise RuntimeError(
                "夹具 sha256 校验失败（改动夹具后必须显式运行 "
                "--update-digests 并重算基线）: " + "; ".join(mismatches[:5])
            )
    return manifest


def verify_fixture_digests(manifest: Dict[str, Any]) -> List[str]:
    digests = manifest.get("fixture_digests", {})
    problems = []
    for rel_path, expected in digests.items():
        path = CASES_DIR / rel_path
        if not path.exists():
            problems.append(f"{rel_path}: 文件缺失")
            continue
        actual = hashlib.sha256(_fixture_bytes(path)).hexdigest()
        if actual != expected:
            problems.append(f"{rel_path}: 摘要不匹配")
    return problems


def update_fixture_digests() -> Dict[str, str]:
    """重算全部夹具的 sha256 清单并写回 manifest。显式维护动作，不自动执行。"""
    manifest_path = CASES_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digests = {}
    for path in sorted(CASES_DIR.rglob("*")):
        # llm.json 一并纳入防篡改：改脚本能直接操纵指标（如删掉冲突口径），
        # 它和 HTML 夹具同属"评测输入"，摘要必须覆盖
        if path.is_file() and path.name != "manifest.json":
            digests[str(path.relative_to(CASES_DIR)).replace("\\", "/")] = \
                hashlib.sha256(_fixture_bytes(path)).hexdigest()
    manifest["fixture_digests"] = digests
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return digests


def resolve_citations(text: str, case: Dict[str, Any], llm: Dict[str, Any],
                      url_overrides: Optional[Dict[str, str]] = None) -> str:
    """把报告模板里的 {{cite:<site_key>:<item_index>}} 解析为真实证据 id。

    证据 id = make_evidence_id(url, key, value)，与工作流从同一份抽取脚本
    构建的 EvidenceStore 完全一致——id 对得上，引用校验才测得出真问题。
    extract_sequence 按 sites 声明顺序一一对应（被 blocked 的站点不占用条目，
    其 llm.json 里本来就不该有抽取脚本）。

    url_overrides：legacy 抽取的 source_url 是站点 URL；react 的
    extract_facts 走逐页 map-reduce，source_url 是**页面** URL——react 模板
    解析必须传页面级 URL 覆盖，否则 id 对不上会全军覆没。
    """
    import re as _re
    from src.agent.citations import make_evidence_id

    sites_by_key = {s["key"]: s for s in case["sites"]}
    extract_seq = llm.get("extract_sequence", [])
    order = [s["key"] for s in case["sites"]]

    def repl(match):
        site_key, idx = match.group(1), int(match.group(2))
        site = sites_by_key[site_key]
        items = extract_seq[order.index(site_key)]
        item = items[idx]
        url = (url_overrides or {}).get(site_key, site["url"])
        eid = make_evidence_id(
            url, str(item.get("key", "")), str(item.get("value", "")).strip()
        )
        return f"[e:{eid}]"

    return _re.sub(r"\{\{cite:([A-Za-z0-9_]+):(\d+)\}\}", repl, text)
