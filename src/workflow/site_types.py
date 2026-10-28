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

# 应用商店 / 下载站
_APP_STORE_HOST_HINTS = (
    "apps.apple.com", "itunes.apple.com", "play.google.com", "appgallery.",
    "app.mi.com", "sj.qq.com", "myapp.com", "appstore.", "apps.apple",
    "apkpure.", "2265.", "9game.", "wandoujia.", "coolapk.", "apkmirror.",
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
    "similarweb.", "semrush.", "ahrefs.",
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
