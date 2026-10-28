"""站点类型判定与入选规则的回归测试。

依据：真实搜索引擎实测（2026-10-28，SearXNG 本机 8888，query「百度搭子 竞品」）。
当时只认"媒体/社区域名"一张表，候选池里混进 36kr / cnblogs / apps.apple.com /
watcha.cn，8 个入选里只有 3 家是真正的竞品官网——因为它们都有独立域名、
都拿到了"有独立站点"的分。
"""
from src.workflow import site_types as st
from src.workflow import search_stages as ss
from src.workflow import research_plan as rp


def test_app_store_is_not_a_product_site():
    assert st.classify_site("https://apps.apple.com/us/app/id6468766519", "卿卿我我") == st.SITE_APP_STORE


def test_ugc_platform_is_not_a_product_site():
    assert st.classify_site("https://www.cnblogs.com/GEO-AI/p/22883718", "对比") == st.SITE_UGC


def test_media_hosts_are_not_product_sites():
    for url in (
        "https://m.36kr.com/p/3182105979902084",
        "https://zhuanlan.zhihu.com/p/1",
        "https://baike.baidu.com/item/maoxiang",
        "https://www.sohu.com/a/1069365530",
        "https://www.toutiao.com/article/7683456812/",
    ):
        assert st.classify_site(url, "某个标题") != st.SITE_PRODUCT, url


def test_product_directory_is_not_a_product_site():
    assert st.classify_site("https://watcha.cn/products/bai-du-da-zi-dumate", "百度搭子 DuMate") == st.SITE_DIRECTORY


def test_real_product_site_is_product_site():
    assert st.classify_site("https://lumichat.ink/zh", "LumiChat云梦伴聊") == st.SITE_PRODUCT
    assert st.classify_site("https://miyu-ai.com.cn/", "miyuai聊天") == st.SITE_PRODUCT


def test_article_title_makes_a_product_site_a_media_page():
    """同一域名下，文章标题意味着这是文章页而不是产品首页。

    这是真实数据里最高产的信号：lumichat.ink 既是产品官网，也发博客文章；
    只看域名分不出来，看标题能分。
    """
    assert st.classify_site("https://www.lumichat.ink/zh/blog/best-free-ai-roleplay-chatbots",
                            "2026 年 10 款最佳免费 Ai 角色扮演聊天机器人：实测排行") == st.SITE_MEDIA
    assert st.classify_site("https://www.lumichat.ink/zh", "LumiChat云梦伴聊") == st.SITE_PRODUCT
    assert st.classify_site("https://www.lumichat.ink/zh/article/x",
                            "2026 年 10 款最佳免费 Ai 角色扮演聊天机器人：实测排行") == st.SITE_MEDIA


def test_empty_url_is_not_a_product_site():
    assert st.classify_site("", "某产品") != st.SITE_PRODUCT


def test_looks_like_aggregator_accepts_title():
    assert ss.looks_like_aggregator("https://miyu-ai.com.cn/", "miyuai聊天") is False
    assert ss.looks_like_aggregator("https://miyu-ai.com.cn/blog/a", "2026 年盘点：10 款最佳") is True


# ---- 入选规则 --------------------------------------------------------

def _plan():
    return rp.build_default_plan("百度搭子")


def test_media_site_candidate_is_not_admitted():
    """媒体站、应用商店、开发者博客、收录目录都不得入选。"""
    plan = _plan()
    candidates = [
        {"name": "2025出海盘点：社交出海大年", "url": "https://m.36kr.com/p/3637773206293641",
         "description": "盘点", "site_type": st.SITE_MEDIA},
        {"name": "卿卿我我", "url": "https://apps.apple.com/us/app/id6468766519",
         "description": "AI 角色聊天", "site_type": st.SITE_APP_STORE},
        {"name": "2026比较好的AI社交App 功能与体验实测对比", "url": "https://www.cnblogs.com/GEO-AI/p/22883718",
         "description": "对比", "site_type": st.SITE_UGC},
        {"name": "百度搭子 DuMate", "url": "https://watcha.cn/products/bai-du-da-zi-dumate",
         "description": "收录", "site_type": st.SITE_DIRECTORY},
        {"name": "LumiChat云梦伴聊", "url": "https://www.lumichat.ink/zh",
         "description": "AI 角色扮演", "site_type": st.SITE_PRODUCT},
    ]
    result = rp.admit_competitors(candidates, plan)
    names = [item["name"] for item in result["selected"]]
    assert names == ["LumiChat云梦伴聊"], names


def test_product_site_without_overlap_still_passes_the_floor():
    plan = _plan()
    result = rp.admit_competitors(
        [{"name": "LumiChat", "url": "https://lumichat.ink/", "description": "AI 角色扮演",
          "site_type": st.SITE_PRODUCT}],
        plan,
    )
    assert len(result["selected"]) == 1


def test_site_type_is_reasoned_in_the_score():
    plan = _plan()
    media = rp.score_candidate(
        {"name": "某盘点", "url": "https://m.36kr.com/p/1", "site_type": st.SITE_MEDIA}, plan)
    product = rp.score_candidate(
        {"name": "某产品", "url": "https://product.example/", "site_type": st.SITE_PRODUCT}, plan)
    assert product["score"] > media["score"]
    assert any("产品" in r and "官网" in r for r in product["reasons"])
    assert any("不是产品官网" in r for r in media["reasons"])


# ---- 主体自身识别 ----------------------------------------------------

def test_subject_alias_page_is_not_a_competitor():
    """主体"百度搭子"在候选里是"百度搭子 DuMate"——主体不是自己的竞品。"""
    assert ss._is_subject_itself("百度搭子 DuMate", "百度搭子") is True
    assert ss._is_subject_itself("百度搭子", "百度搭子") is True
    assert ss._is_subject_itself("猫箱", "百度搭子") is False


def test_subject_prefix_without_space_is_not_the_subject():
    """"美菜网"是另一家公司的产品，不是"美菜"自己——按包含判会排掉真竞品。"""
    assert ss._is_subject_itself("美菜网", "美菜") is False
