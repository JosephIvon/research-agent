"""页面发布日期/更新时间抽取（确定性，不经 LLM）。

报告要能回答"这些结论依据的数据是什么时候的"，唯一的外部证据是**页面自己
声明的**发布/更新时间。本模块只从结构化标记（meta / JSON-LD / <time>）与少量
可见文本模式里取日期。

两条硬约束：
1. **抽出的值一律重写为 ISO**：页面内容是攻击者可控的，原样渲染等于把注入面
   带进报告；走 `date().isoformat()` 之后，落进报告的只剩"年-月-日"三段数字。
2. **只认"页面声明的日期"**：抓取时间（何时取回）不是数据时点，它由本地时钟
   决定、与页面内容无关，因此**不在这里抽**。

用法：
    from src.crawler.page_dates import extract_page_dates
    dates = extract_page_dates(page_html)   # ['2026-10-20', ...]
"""
from __future__ import annotations

import re
from datetime import date
from typing import List

# 结构化标记：属性名 -> meta 标签。属性名白名单化，避免把 description 里的
# 日期误当成发布日（页面上出现的日期不一定是"这条内容是什么时候的"）。
_META_NAMES = (
    "article:published_time",
    "article:modified_time",
    "og:updated_time",
    "datepublished",
    "datemodified",
    "publishdate",
    "publish_date",
    "lastmod",
    "last-modified",
    "dc.date",
    "dcterms.modified",
)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([\w:.-]+)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_CONTENT_RE = re.compile(r"^\s*(?:article:published_time|article:modified_time|"
                         r"og:updated_time|datepublished|datemodified|publishdate|"
                         r"publish_date|lastmod|last-modified|dc\.date|dcterms\.modified)"
                         r"\s*$", re.IGNORECASE)
_TIME_RE = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_JSONLD_RE = re.compile(r"\"date(?:Published|Modified|Posted)\"\s*:\s*\"([^\"]+)\"", re.IGNORECASE)

# 可见文本模式：关键词后面紧跟日期。关键词里的"更新/发布"限定词不可省——
# 页面上任何日期都可能是无关的（合同日期、评论日期）。
# 「数据口径日期」是本仓自有的口径日说法（见 competitor_matrix 的 as_of），
# 与上游 fixture 页面里的写法一致。
_TEXT_RE = re.compile(
    r"(?:发布(?:时间|日期|于)|更新时间|更新日期|最后更新|更新于|首次发布|"
    r"数据口径日期|口径日期|数据日期|Last\s+updated|Published(?:\s+on)?|"
    r"Updated(?:\s+on)?)[^\d<>\n]{0,8}"
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})",
    re.IGNORECASE)

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def _iso(raw: str) -> str:
    """把任意日期写法归一为 ISO；解析不出来就返回空串（调用方跳过）。"""
    match = _DATE_RE.search(raw or "")
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)),
                    int(match.group(3))).isoformat()
    except ValueError:
        return ""


def extract_page_dates(html: str) -> List[str]:
    """抽取页面声明的发布/更新日期（ISO 字符串，去重升序）。

    找不到就返回空列表——"页面没写日期"是事实，不该编一个。
    """
    text = html or ""
    found = set()

    for tag in _META_TAG_RE.findall(text):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        name = attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or ""
        if _CONTENT_RE.match(name.strip()) and attrs.get("content"):
            iso = _iso(attrs["content"])
            if iso:
                found.add(iso)

    for raw in _TIME_RE.findall(text):
        iso = _iso(raw)
        if iso:
            found.add(iso)

    for raw in _JSONLD_RE.findall(text):
        iso = _iso(raw)
        if iso:
            found.add(iso)

    for year, month, day in _TEXT_RE.findall(text):
        try:
            found.add(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            continue

    return sorted(found)
