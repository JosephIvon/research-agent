"""站点类型判定：这个 URL 是不是"某个产品自己的官网"。

这不是"哪些竞品"的清单，是**站点类型**——与行业无关的结构特征。

实测依据（真实搜索引擎，2026-10-28，SearXNG 本机 8888）：只认"媒体/社区域名"
一张表时，候选池里混进了 36kr（媒体）、cnblogs（开发者博客）、apps.apple.com
（应用商店）、watcha.cn（产品收录目录）——它们都有独立域名、都拿到"有独立站点"
的分，于是全部通过准入线成了"竞品"，入选名单 8 家里只有 3 家是真正的产品官网。

域名表永远列不全，但**中文搜索结果里文章页的标题特征**（盘点/对比/哪个好/榜单/
实测/合集）是决定性的，所以标题特征是这层判定的兜底，也是产出最高的信号。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# 候选名里的**修饰词**（实测第八轮，2026-10-31）：
# 同一个产品会以多个名字出现在结果里——"Character.AI" 与
# "Character AI中文版"（汉化站）、"Kindroid" 与 "Kindred Stitch"（镜像站）。
# 名字不同 → 合并键不同 → **同一个产品白占两个名额**，把真竞品挤出去
# （实测入选 8 个里有 3 个是同一产品的不同版本）。
_NAME_MODIFIERS = (
    "简体中文", "繁体中文", "繁体版", "中文版", "中文", "英文版", "国际版",
    "官方版", "官方正版", "官方", "正版", "免费版", "免费", "下载版", "下载",
    "安装版", "安装", "最新版", "新版", "完整版", "专业版", "客户端",
    "安卓版", "安卓", "苹果版", "ios版", "ios", "windows版", "windows",
    "pc版", "pc", "web版", "web", "app版", "app", "apps", "官网", "官方助手",
)


def normalized_brand(name: str) -> str:
    """把候选名规范化成"品牌键"：去修饰词 + 去标点空格。

    "Character.AI" → "characterai"；"Character AI中文版" → "characterai" ✓
    "星野" → "星野"（中文名不带修饰词，原样保留）

    放在本模块而不是 search_stages：research_plan 也要用它
    （给"模型提名的竞品"加权重），而 search_stages 反过来依赖 research_plan，
    放这里才没有循环导入。
    """
    text = (name or "").strip().casefold()
    if not text:
        return ""
    text = "".join(ch for ch in text if ch.isalnum())
    changed = True
    while changed:
        changed = False
        for mod in _NAME_MODIFIERS:
            mod = mod.casefold()
            if text.endswith(mod) and len(text) > len(mod):
                text = text[: -len(mod)]
                changed = True
                break
            if text.startswith(mod) and len(text) > len(mod):
                text = text[len(mod):]
                changed = True
                break
    return text

# 应用商店 / 下载站
_APP_STORE_HOST_HINTS = (
    "apps.apple.com", "itunes.apple.com", "play.google.com", "appgallery.",
    "app.mi.com", "sj.qq.com", "myapp.com", "appstore.", "apps.apple",
    "apkpure.", "2265.", "9game.", "wandoujia.", "coolapk.", "apkmirror.",
    # 实测第五轮：下载分发站（抓取它们的"版本列表"页全是噪音）
    "uptodown.", "appteka.", "apkgk.", "apkcombo.", "f-droid.",
    "91mobiles.", "androidrank.", "appbrain.", "apkpure.com",
    # 实测第六轮（2026-10-29）：**下载平台为每个产品开的子域**。
    # Softonic 自述"全球最受信任的软件分发平台之一"，它给每个应用开一个子域
    # （chai.softonic.cn / character-ai.softonic.cn / talkie-soulful-ai.softonic.cn），
    # 路径是平台自己的（/android、/windows、/mac）。这些页面是**下载页**，
    # 不是厂商的官网——实测它们占了 6 个入选里的 4 个。
    "softonic.", "pc6.", "downza.", "cr173.", "duote.", "skycn.", "xiazai.",
    "onlinedown.", "gimy.", "52pojie.", "rankcc.", "52mac.", "macappstore.",
    "windowsappstore.", "appgallery.huawei.", "f-droid.org",
    # 实测第七轮（2026-10-29）：9na.cn 这类"下载+攻略"门户的详情页
    # （/2271.html 是筑梦岛的下载页），路径有 id 一段但域名是下载站。
    "9na.", "gamersky.", "3dmgame.", "ali213.", "m.ddooo.", "ddooo.",
    "crsky.", "xiazai.zol.", "pc6.cn", "www.9na.cn",
    # 实测第八轮（2026-10-31）：微软商店页（apps.microsoft.com）被判成
    # "产品官网"——它的路径是 /product/<id>，域名单看就是商店，
    # 但商店清单里没有它，于是成了某产品的"官网"，白占一个名额。
    "apps.microsoft.com", "windowsphone.com", "galaxystore.",
    "market.xiaomi.com", "chromewebstore.google.com", "snapcraft.io",
)

# 开发者社区 / UGC 博客平台
_UGC_PLATFORM_HOST_HINTS = (
    "cnblogs.", "csdn.", "juejin.", "jianshu.", "segmentfault.", "51cto.",
    "oschina.", "gitee.", "v2ex.", "github.io", "gitbook.", "notion.site",
    "cloud.tencent.com/developer", "cloud.tencent.com/developer/",
)

# 媒体 / 资讯
_MEDIA_HOST_HINTS = (
    "zhihu.", "baijiahao.", "sohu.", "163.com", "qq.com", "weibo.",
    "wikipedia.", "baike.", "douban.", "xiaohongshu.", "bilibili.",
    "toutiao.", "cnki.", "medium.com", "youtube.",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "reddit.com",
    "36kr.", "tmtpost.", "huxiu.", "ifeng.", "sina.com", "thepaper.",
    "leiphone.", "eefocus.", "ithome.", "pconline.", "zol.", "cnbeta.",
    "geekpark.", "pingwest.", "cyzone.", "ebrun.", "iyiou.", "analysys.",
    "sspai.", "geekbang.", "infoq.", "donews.",
    # 实测（2026-10-28）补的：这些域名不带"媒体"字样，但全是内容站
    "woshipm.", "aitntnews.", "aigc.cn", "ai-bot.cn", "hub.baai.",
    "10100.com", "dfcfw.", "gamer.com.tw", "pdf.dfcfw.", "assets.kpmg.",
    "iopn.library.", "dfcfw.com", "eeo.", "nbd.", "stcn.", "yicai.",
    "jiemian.", "huxiu.", "tmtpost.", "chinaventure.", "iyiou.", "donews.",
    # 实测第六轮（2026-10-29）：百科/镜像站。moegirl.icu 是萌娘百科的镜像，
    # 它的条目页（/独奏）标题是"独奏 - 萌娘百科…"，路径只有一段——
    # 域名不认识就会当成产品官网（实测它占了整组唯一的 1 个入选）。
    "moegirl.", "zh.moegirl.", "moegirl.icu", "wiki.biligame.",
    "handwiki.", "wikiwand.", "everipedia.", "dbpedia.", "wikizero.",
)

# 论坛 / 社区（不是产品官网）
_FORUM_HOST_HINTS = (
    "forum.", "bbs.", "tieba.", "community.", "gamer.com.tw", "guokr.",
    "v2ex.", "zhihu.com/question", "xda.",
)

# 收录 / 目录 / 工具聚合
_DIRECTORY_HOST_HINTS = (
    "watcha.", "gwdang.", "manmanbuy.", "duitang.", "smzdm.", "coolbuy.",
    "taobao.", "jd.com", "amazon.", "wenxuecity.", "xueshu.", "docin.",
    "doc88.", "pan.baidu.", "aliyundrive.",
    # 实测补的：工具收录/榜单/评测目录
    "aitoollab.", "aitop100.", "boardmix.", "hub.baai.ac.cn", "similaritylabs.",
    "similarlabs.", "fly63.", "ai-bot.cn",
    # 实测第二轮补的：AI 工具榜单站（英文目录最容易被当成"竞品官网"）
    "top.aibase.", "aibase.", "usefulai.", "aimultiple.", "sourceforge.net",
    "flowpixai.", "mroldcat.", "foreverse.", "legal500.", "producthunt.",
    "g2.com", "capterra.", "trustradius.", "apptopia.", "sensortower.",
    "similarweb.", "semrush.", "ahrefs.", "aiproducthub.",
    # 实测第七轮（2026-10-29）：mg21.com（美股之家）是 AI 资源导航/收录站，
    # 它的"某产品开发商"详情页（/character.html）路径只有一段、搜索结果标题
    # 里也没有"百科"（那是页面完整标题才有的）→ 域名得认。
    "mg21.", "www.mg21.",
)

# 社交 / 视频 / 社区平台（不是竞品官网）
_SOCIAL_HOST_HINTS = (
    "douyin.", "kuaishou.", "tiktok.", "instagram.", "facebook.",
    "twitter.", "x.com", "linkedin.", "reddit.", "youtube.",
    "bilibili.", "xiaohongshu.", "weibo.", "zhihu.", "tieba.",
)

# 文件直链：不是网站（实测：pdf.dfcfw.com 的研报 PDF 被当成了竞品官网）
_FILE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar",
    ".csv", ".mp4", ".mp3", ".apk",
)

# 路径段：内容站/聚合页的常见路径
_MEDIA_PATH_HINTS = (
    "/article", "/news", "/blog", "/post", "/topic", "/wiki", "/comment",
    "/encyclopedia", "/zhuanti", "/rank", "/list", "/roundup", "/collection",
    "/tag/", "/category", "/review", "/guide", "/tutorial", "/how-to",
    "/product/", "/products/", "/posts/",
    # 论坛/社区路径（实测：forum.gamer.com.tw 的帖子页）
    "/forum", "/bbs/", "/tieba", "/community/", "/c.php", "/thread",
    # 分类/筛选聚合页（实测：ai.fly63.com/cate/31-1、aigc.cn/favorites/…）
    "/cate/", "/cate", "/favorites/", "/favorites", "/tag", "/label/",
    "/use-cases/", "/solutions/", "/case-studies/",
)

# 标题里的**文章味**：中文搜索结果里，盘点/对比/评测类标题几乎必然是文章页
_ARTICLE_TITLE_HINTS = (
    "盘点", "对比", "评测", "榜单", "哪个好", "怎么选", "推荐", "排行",
    "实测", "合集", "教程", "指南", "解析", "总结", "值得", "哪家强",
    "有哪些", "如何", "为什么", "深度", "全景", "全网", "最佳",
    "前十", "哪家", "区别", "选型", "体验", "测评", "分析报告",
    "还有哪些", "怎么样", "吗", "？", "?",
    "top", " vs ", "vs.",
    # 实测补的（2026-10-28 真实候选池）：月报/前瞻/格局/心得/竞品分析 等
    "报告", "分析", "月报", "年报", "季报", "白皮书", "前瞻", "格局",
    "心得", "分享", "记录", "复盘", "观察", "洞察", "综述", "趋势",
    "浅谈", "探索", "必备", "竞品", "同行", "演进", "闭环",
    "多款", "款免费", "入选", "亮相", "上新",
    # 实测第六轮（2026-10-29）：百科类页面。mg21.com/character.html 的标题是
    #「开放式对话应用程序开发商：Character.ai | 美股之家 - 港股美股开户投资百科全书」，
    # moegirl.icu/独奏 的标题是「独奏 - 萌娘百科_万物皆可萌的百科全书」——
    # 两个都是百科页，但域名/path 看不出来，只能靠标题里的"百科"。
    "百科", "百科全书", "词典", "词条",
    # 实测第七轮（2026-10-29）：门户站里"某产品的官网地址"这类文章——
    # tuijianvpn.com/8359 的标题是「星岛梦/星梦岛(StarDream)机场官网地址」，
    # 是 VPN 服务商的推广页，不是星岛梦的官网。
    "官网地址", "官方网址", "地址是多少", "在哪下载",
)

SITE_PRODUCT = "product_site"
SITE_MEDIA = "media"
SITE_UGC = "ugc_platform"
SITE_APP_STORE = "app_store"
SITE_DIRECTORY = "directory"
SITE_UNKNOWN = "unknown"

# 站点类型 → 中文说明（报告/日志里用）
SITE_TYPE_LABELS = {
    SITE_PRODUCT: "产品官网",
    SITE_MEDIA: "媒体/内容站",
    SITE_UGC: "开发者社区/UGC 博客",
    SITE_APP_STORE: "应用商店/下载站",
    SITE_DIRECTORY: "产品收录/比价目录",
    SITE_UNKNOWN: "未知",
}


def host_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except ValueError:
        return ""


def classify_site(url: str, title: str = "") -> str:
    """判定 URL 属于哪种站点类型，返回 SITE_* 之一。

    顺序有讲究：**结构特征优先**（域名/路径是硬事实），标题特征兜底
    （域名看不出来、但标题像文章）。
    """
    host = host_of(url)
    if not host:
        return SITE_MEDIA
    path = (urlparse(url).path or "").lower()
    # 文件直链不是网站（实测：pdf.dfcfw.com 的研报 PDF 被当成了"竞品官网"，
    # 还被真去抓了，抓回来是"Download is starting"）
    if path.endswith(_FILE_SUFFIXES):
        return SITE_MEDIA
    if any(hint in host for hint in _APP_STORE_HOST_HINTS):
        return SITE_APP_STORE
    if any(hint in host for hint in _UGC_PLATFORM_HOST_HINTS):
        return SITE_UGC
    if any(hint in host for hint in _FORUM_HOST_HINTS):
        return SITE_UGC
    if any(hint in host for hint in _SOCIAL_HOST_HINTS):
        return SITE_UGC
    if any(hint in host for hint in _MEDIA_HOST_HINTS):
        return SITE_MEDIA
    if any(hint in host for hint in _DIRECTORY_HOST_HINTS):
        return SITE_DIRECTORY
    # 博客/新闻子域：blog.character.ai 是 character.ai 的博客，不是产品首页
    if host.startswith(("blog.", "news.", "docs.", "help.")):
        return SITE_MEDIA
    if any(hint in path for hint in _MEDIA_PATH_HINTS):
        return SITE_MEDIA
    # **路径深度**：产品自己的官网，首页就是根路径或一层（/zh、/en、/zh-cn）。
    # 两层以上的是站内子页——榜单页、厂商案例页、功能介绍页、目录详情页、
    # 视频页（实测第二轮的漏网之鱼几乎全是这一类）。
    segments = [s for s in path.split("/") if s]
    if len(segments) > 1:
        return SITE_MEDIA
    text = (title or "").lower()
    if text and any(hint in text for hint in _ARTICLE_TITLE_HINTS):
        return SITE_MEDIA
    return SITE_PRODUCT


def looks_like_aggregator(url: str, title: str = "") -> bool:
    """不是竞品官网的站点（媒体/社区/百科/应用商店/开发者平台/收录目录/文章页）。"""
    return classify_site(url, title) != SITE_PRODUCT


def site_type_label(site_type: Optional[str]) -> str:
    return SITE_TYPE_LABELS.get(site_type or "", SITE_TYPE_LABELS[SITE_UNKNOWN])
