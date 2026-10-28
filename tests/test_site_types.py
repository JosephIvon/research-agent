"""站点类型判定与入选规则的回归测试。

依据：真实搜索引擎实测（2026-10-28，SearXNG 本机 8888，query「百度搭子 竞品」）。
当时只认"媒体/社区域名"一张表，候选池里混进 36kr / cnblogs / apps.apple.com /
watcha.cn，8 个入选里只有 3 家是真正的竞品官网——因为它们都有独立域名、
都拿到了"有独立站点"的分。
"""
from src.workflow import site_types as st
from src.workflow import search_stages as ss
from src.workflow import research_plan as rp

import pytest


class _StubSearch:
    """按查询词里的关键词返回固定结果（不联网）。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.queries = []

    def search(self, query, num_results=None, provider=None):
        self.queries.append(query)
        for key, results in self.mapping.items():
            if key in query:
                return list(results)
        return []


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


# ---- 真实轮次里出现过的"不是竞品官网"的页面（2026-10-28 实测）----------

@pytest.mark.parametrize("url,title", [
    ("https://pdf.dfcfw.com/pdf/H3_AP202605261822873336_1.pdf",
     "2026年4月中国头部AI 用户行为月报"),
    ("https://assets.kpmg.com/content/dam/kpmgsites/cn/pdf/zh/2025/06/x.pdf",
     "人工智能就绪度白皮书"),
    ("https://www.woshipm.com/ai/6333818.html",
     "老板说“分析一下竞品的Deep Research”，我交出了这份报告"),
    ("https://forum.gamer.com.tw/C.php?bsn=85831&snA=40", "【心得】BabeChat 使用心得"),
    ("https://ai.fly63.com/cate/31-1", "AI角色扮演"),
    ("https://www.aigc.cn/favorites/ai-role-play-chatbot", "AI角色扮演聊天机器人"),
    ("https://cloud.google.com/use-cases/free-ai-tools?hl=zh-CN", "2026 年必备的10 多款免费AI 工具"),
    ("https://ai-bot.cn/ai-virtual-companion-social-app/",
     "15个AI虚拟女友/男友社交软件，专属恋爱聊天和情感陪伴"),
    ("https://www.aitoollab.cn/tools/dumate/", "DuMate（百度搭子）"),
    ("https://www.aitop100.cn/tools/dumate-cn", "百度搭子DuMate：百度通用桌面AI智能体"),
])
def test_real_pages_that_are_not_product_sites(url, title):
    assert st.classify_site(url, title) != st.SITE_PRODUCT, url


@pytest.mark.parametrize("url,title", [
    # 实测第二轮：漏网的目录/厂商子页/视频页/搜索页
    ("https://usefulai.com/tools/ai-agent-builders", "16 Best AI Agent Builders for 2026"),
    ("https://aimultiple.com/ai-agent-builders", "20+ AI Agent Builders: Microsoft, CrewAI"),
    ("https://sourceforge.net/software/ai-agent-builders/", "Best AI Agent Builders of 2026"),
    ("https://www.mroldcat.top/detail/1853", "15人研发团队做出了AI情感陪伴类订阅榜第一的APP"),
    ("https://www.sensetime.com/cn/case-detail?categoryId=51134683", "IP角色定制"),
    ("https://top.aibase.com/tool/yollo-ai", "Yollo AI : 无限制的AI 聊天与角色扮演平台。"),
    ("https://top.aibase.com/tool/rubii", "Rubii : 发现AI角色社区，与虚拟角色互动"),
    ("https://www.legal500.com/intelligence/china/technology/x", "当AI成为“陪伴者”：合规边界与实务建议"),
    ("https://picsart.com/zh/persona/", "AI网红生成器"),
    ("https://www.xfyun.cn/services/AI_personality", "角色模拟"),
    ("https://www.flowpixai.com/ai-tools/ai-companion-app-comparison.html",
     "AI情感陪伴App横评：Character.AI/星野/筑梦岛/Talkie谁更"),
    ("https://www.douyin.com/video/7600756702586272499", "3000万!假扮千金!"),
    ("https://www.douyin.com/search/%E7%AD%91%E6%A2%A6%E5%B2%9B", "筑梦岛豆包猫箱星野哪个更好玩"),
    ("https://support.microsoft.com/zh-hk/viva/learning/learning-agent-role-play", "學習代理中的角色扮演"),
    ("https://foreverse.cn/zh/ai-roleplay-apps", "AI 角色扮演 App 横评 2026"),
])
def test_second_round_leak_pages_are_not_product_sites(url, title):
    assert st.classify_site(url, title) != st.SITE_PRODUCT, url


@pytest.mark.parametrize("url,title", [
    ("https://www.lumichat.ink/zh", "LumiChat云梦伴聊"),
    ("https://miyu-ai.com.cn/", "miyuai聊天"),
    ("https://www.aijuqingapp.com/", "AI剧情: AI 角色聊天与多角色互动剧本"),
    ("https://lianyu.chat/", "予念 YuNian"),
    ("https://www.sayloai.com/zh-Hans", "免费AI 角色故事聊天"),
    ("https://www.tofaipro.cn/", "Tofai"),
])
def test_real_product_sites_stay_product_sites(url, title):
    assert st.classify_site(url, title) == st.SITE_PRODUCT, url


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


def test_subject_official_domain_is_excluded_from_the_pool():
    """主体的产品名常与用户说法不同（"百度搭子"=dumate.cn）——按域名排才对。"""
    plan = rp.build_default_plan("百度搭子")
    plan.entity_name = "百度搭子"
    plan.official_domains = ["dumate.cn"]
    stub = _StubSearch({
        "百度搭子 竞品": [
            {"title": "DuMate", "url": "https://www.dumate.cn/", "content": "百度通用桌面AI智能体"},
            {"title": "LumiChat", "url": "https://www.lumichat.ink/zh", "content": "AI 角色扮演"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    urls = [c["url"] for c in result.selected]
    assert "https://www.dumate.cn/" not in urls, "主体自己的官网不能入选"
    assert any("dumate.cn" in c.url for c in result.candidates) is False


def test_entity_anchor_first_product_site_is_treated_as_the_subject_site():
    """锚定阶段排第一的产品官网 = 主体自己的官网（名字对不上也能排掉）。"""
    plan = rp.build_default_plan("百度搭子")
    stub = _StubSearch({
        "百度搭子 官网": [
            {"title": "DuMate - 百度通用桌面AI智能体", "url": "https://www.dumate.cn/", "content": "官方"},
            {"title": "某盘点文章", "url": "https://zhuanlan.zhihu.com/p/1", "content": "盘点"},
        ],
        "百度搭子 竞品": [
            {"title": "DuMate", "url": "https://www.dumate.cn/product", "content": "产品页"},
            {"title": "LumiChat", "url": "https://www.lumichat.ink/zh", "content": "AI 角色扮演"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    runner.run_entity_anchor(plan)
    result = runner.run_competitor_discovery(plan)
    urls = [c["url"] for c in result.selected]
    assert not any("dumate.cn" in u for u in urls), urls


def test_same_site_counts_once():
    """同一站点的两个页面只算一家——否则白占一个竞品名额。"""
    plan = rp.build_default_plan("猫箱")
    candidates = [
        {"name": "character.ai", "url": "https://character.ai/", "site_type": st.SITE_PRODUCT},
        {"name": "About Character.AI", "url": "https://character.ai/about", "site_type": st.SITE_PRODUCT},
        {"name": "LumiChat", "url": "https://www.lumichat.ink/zh", "site_type": st.SITE_PRODUCT},
    ]
    result = rp.admit_competitors(candidates, plan)
    urls = [item["url"] for item in result["selected"]]
    assert len(urls) == 2, urls
    # 同一站点里取**首页**（路径最浅）那一份
    assert "https://character.ai/" in urls


def test_blog_subdomain_is_not_a_product_site():
    assert st.classify_site("https://blog.character.ai/", "character.ai blog") != st.SITE_PRODUCT
    assert st.classify_site("https://character.ai/", "character.ai") == st.SITE_PRODUCT


def test_model_named_competitors_are_verified_by_search():
    """模型提名的竞品：代码去搜它的官网并判是不是产品官网。"""
    plan = rp.build_default_plan("猫箱")
    plan.competitor_candidates = ["星野", "筑梦岛"]
    stub = _StubSearch({
        "猫箱 竞品": [
            {"title": "某盘点文章", "url": "https://zhuanlan.zhihu.com/p/1", "content": "盘点"},
        ],
        "星野 官网": [
            {"title": "星野 - AI 角色扮演", "url": "https://www.xingyeai.com/", "content": "星野官网"},
        ],
        "筑梦岛 官网": [
            {"title": "筑梦岛 - 角色社区", "url": "https://www.zhumengdao.com/", "content": "筑梦岛官网"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    names = [c["name"] for c in result.selected]
    assert "星野" in names and "筑梦岛" in names, names


def test_model_named_candidate_must_still_pass_the_site_type_check():
    """模型提名不等于入选：不是产品官网的照样剔掉。"""
    plan = rp.build_default_plan("猫箱")
    plan.competitor_candidates = ["某榜单"]
    stub = _StubSearch({
        "猫箱 竞品": [],
        "某榜单 官网": [
            {"title": "某榜单 - 盘点", "url": "https://zhuanlan.zhihu.com/p/2", "content": "盘点"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    assert result.selected == []
