"""站内链接发现的策略测试（`WebCrawler.discover_research_links`）。

这套用例存在的理由：站内发现**从未被 13 条离线评测覆盖过**——
`evals/fixture_source.py` 的夹具爬虫整体覆写了 `fetch_site_profile`，
返回写死的 `discovered_urls: []`。所以这里改坏了基线不会红，
只能靠本文件钉住。

## 三条硬要求

1. **不同行业都不能失效。** 默认权重是 SaaS 风格的先验；医疗、金融、工具类
   站点的关键页全是 0 分。0 分可以**排在后面**，但不能"不存在"。
2. **过滤与排序分工明确。** 排序由词表决定（不可穷举，所以只影响顺序）；
   过滤由噪音段决定（可穷举，所以可以硬排除）。不能让"没匹配到关键词"
   兼职"不值得抓"。
3. **打分不能依赖词表顺序。** 同时命中多个档位时，首命中即 break 会让
   词表顺序变成隐式业务规则。
"""
from src.crawler.web_crawler import (
    DEFAULT_EXCLUDED_SEGMENTS,
    WebCrawler,
    _term_hit,
)


def _discover(html, max_links=8, **kwargs):
    return WebCrawler.discover_research_links("https://example.com", html,
                                              max_links=max_links, **kwargs)


def _nav(*pairs):
    """构造首页导航：pairs = (href, 锚文本)"""
    links = "\n".join(f'<a href="{h}">{t}</a>' for h, t in pairs)
    return f"<html><body><nav>{links}</nav></body></html>"


# --------------------------------------------------------------------------
# 1. 跨行业：默认权重之外的行业不能"失效"
# --------------------------------------------------------------------------

def test_medical_site_pages_are_not_invisible():
    """医疗站的关键页默认全是 0 分——旧实现里它们根本不会被考虑。

    这是本次审计最主要的修复：`if score <= 0: continue` 把"我不知道"
    伪装成"不存在"。现在 0 分只是排在后面。
    """
    html = _nav(
        ("/pricing", "Pricing"),
        ("/indications", "适应症"),
        ("/clinical-trials", "临床试验"),
        ("/insurance-directory", "医保目录"),
    )

    links = _discover(html)

    assert "https://example.com/indications" in links, "医疗站关键页不应消失"
    assert "https://example.com/clinical-trials" in links
    assert "https://example.com/insurance-directory" in links
    # 且仍然排在 pricing 之后——默认先验没有被推翻
    assert links.index("https://example.com/pricing") < \
        links.index("https://example.com/indications")


def test_financial_site_pages_are_reachable():
    """金融站：利率、理财、账户类路径同样默认 0 分。"""
    html = _nav(
        ("/pricing", "Pricing"),
        ("/rates", "利率表"),
        ("/wealth-products", "理财产品"),
    )
    links = _discover(html)
    assert "https://example.com/rates" in links
    assert "https://example.com/wealth-products" in links


def test_tool_vendor_pages_are_reachable():
    """工具类：规格、下载、许可类路径默认 0 分。"""
    html = _nav(
        ("/pricing", "Pricing"),
        ("/specs", "技术规格"),
        ("/downloads", "下载中心"),
        ("/licensing", "许可协议"),
    )
    links = _discover(html)
    for path in ("specs", "downloads", "licensing"):
        assert f"https://example.com/{path}" in links, f"/{path} 不应消失"


def test_industry_tiers_can_override_the_default_prior():
    """换行业应能整表覆盖默认先验，而不是改源码。"""
    html = _nav(
        ("/pricing", "Pricing"),
        ("/indications", "适应症"),
    )

    medical_tiers = ((200, ("适应症", "indications", "clinical")),
                     (50, ("pricing",)))

    links = _discover(html, priority_tiers=medical_tiers)

    assert links[0] == "https://example.com/indications", \
        "覆盖后的词表应让行业关键页排到最前"


# --------------------------------------------------------------------------
# 2. 子串误伤：ASCII 词必须按词边界匹配
# --------------------------------------------------------------------------

def test_ascii_terms_match_on_word_boundaries():
    """裸子串会让 api 命中 capital、model 命中 remodeling，把无关页顶进高分区。"""
    html = _nav(
        ("/capital-markets", "Capital Markets"),
        ("/remodeling", "Remodeling"),
        ("/therapist-portal", "Therapist Portal"),
        ("/api", "API"),
    )

    links = _discover(html)

    assert "/api" in links[-1] or links.index("https://example.com/api") < min(
        links.index("https://example.com/capital-markets"),
        links.index("https://example.com/remodeling"),
        links.index("https://example.com/therapist-portal"),
    ), f"真 API 页应排在被误伤的页面前，实得 {links}"


def test_keyword_in_a_real_segment_still_matches():
    """反向校验：词边界不是"什么都匹配不到"。

    `/remodeling-guide` 里的 `guide` 是合法的文档页信号，
    `/api/v2` 里的 `api` 也是——不能因为修了误伤就连真命中一起丢掉。
    """
    html = _nav(("/api/v2", "API reference"), ("/integration-guide", "Guide"))
    links = _discover(html)
    assert "https://example.com/api/v2" in links
    assert "https://example.com/integration-guide" in links


def test_cjk_terms_still_match_as_substring():
    """中文没有词边界，只能子串匹配——不能因为修了英文就误伤中文。"""
    assert _term_hit("定价", "我们的定价方案") is True
    assert _term_hit("定价", "定价") is True
    assert _term_hit("定价", "订制") is False
    assert _term_hit("pricing", "our pricing page") is True
    assert _term_hit("api", "/api/v2/docs") is True
    assert _term_hit("api", "/rapid-growth") is False


def test_multi_tier_pages_outrank_single_tier_pages():
    """同时命中多档的页面应排在单档页面前，且与词表顺序无关。

    原实现是首命中即 break，于是"同时命中 pricing 和 api"的页只拿 100 分，
    与"只命中 pricing"的页同分，最终由**文档出现顺序**决定——词表顺序
    变成了隐式业务规则。
    """
    html = _nav(
        ("/plans", "Plans"),                 # 只命中 pricing
        ("/pricing-and-api", "Pricing API"),  # 同时命中 pricing + api
    )

    links = _discover(html)

    assert links[0] == "https://example.com/pricing-and-api", \
        "多档命中应加分并排到前面；文档顺序不应决定结果"


# --------------------------------------------------------------------------
# 3. 过滤只由噪音段决定，不由"没匹配到关键词"决定
# --------------------------------------------------------------------------

def test_noise_paths_are_excluded_even_when_they_match_keywords():
    """登录/购物车/法务/招聘页即便撞上关键词也不该入选。"""
    html = _nav(
        ("/pricing", "Pricing"),
        ("/pricing/login", "Pricing login"),
        ("/cart", "Cart"),
        ("/privacy-policy", "Privacy Policy"),
        ("/careers", "Careers"),
    )

    links = _discover(html)

    for noise in ("login", "cart", "privacy-policy", "careers"):
        assert f"https://example.com/{noise}" not in links, f"/{noise} 应被排除"
    assert "https://example.com/pricing" in links


def test_exclusion_does_not_substring_match():
    """排除必须是 token 精确比对，不是子串。

    子串匹配会把 `accountability-report` 里的 `account` 命中排除集，
    于是合规报告页、透明度报告这类页面被静默丢掉——**丢的恰恰是竞品调研
    最该看的那种内容**。
    """
    html = _nav(
        ("/pricing", "Pricing"),
        ("/accountability-report", "Accountability"),
        ("/terms-of-service", "Terms of Service"),
        ("/sign-in", "Sign in"),
    )

    links = _discover(html)

    assert "https://example.com/accountability-report" in links, \
        "accountability-report 是竞品调研的合规证据，不该被 account 误伤"
    assert "https://example.com/terms-of-service" not in links
    assert "https://example.com/sign-in" not in links


def test_exclusion_is_reversible_by_passing_empty_set():
    html = _nav(("/pricing", "Pricing"), ("/careers", "Careers"))

    assert "https://example.com/careers" not in _discover(html)
    assert "https://example.com/careers" in _discover(html, excluded_segments=frozenset())


def test_min_score_can_restore_strict_behaviour_explicitly():
    """想恢复"零分即丢"必须显式传 min_score，且这是可见的、可审计的选择。"""
    html = _nav(("/pricing", "Pricing"), ("/indications", "适应症"))

    assert "https://example.com/indications" in _discover(html)
    assert "https://example.com/indications" not in _discover(html, min_score=1)


# --------------------------------------------------------------------------
# 4. 安全与确定性边界（本次改动不得削弱原有约束）
# --------------------------------------------------------------------------

def test_cross_origin_and_unsafe_schemes_are_still_rejected():
    """跨域链接必须被**同源检查**拦下，不能靠 DNS 碰巧拦。

    早先这条用例用的假域名 `external.example.org` 根本解析不了，于是拒绝来自
    `validate_fetch_url` 的 DNS 失败而非同源检查——把同源校验删掉它照样绿，
    属于"因为错误的原因通过"。必须用**能解析**的另一域（`example.org` 与
    `example.com` 同属 RFC 2606 保留域，netloc 不同），才测得到真正的约束。
    """
    html = _nav(
        ("https://example.org/pricing", "External pricing"),
        ("javascript:void(0)", "JS"),
        ("mailto:sales@example.com", "Mail"),
        ("/pricing", "Pricing"),
    )
    links = _discover(html)
    assert links == ["https://example.com/pricing"], \
        f"跨域页必须被同源检查排除，实得 {links}"


def test_result_is_deterministic_for_same_input():
    html = _nav(("/a-page", "A"), ("/b-page", "B"), ("/c-page", "C"))
    first = _discover(html)
    for _ in range(3):
        assert _discover(html) == first, "同一输入必须给出同一输出"


def test_base_url_is_never_returned():
    html = _nav(("/pricing", "Pricing"), ("/", "Home"))
    links = _discover(html)
    assert "https://example.com/" not in links


def test_excluded_segments_cover_the_obvious_noise():
    """排除集合本身要有内容，否则"过滤交给排除集"就是空话。"""
    for segment in ("login", "cart", "privacy", "careers", "terms",
                    "newsletter", "checkout", "register"):
        assert segment in DEFAULT_EXCLUDED_SEGMENTS, f"{segment} 应在排除集中"
