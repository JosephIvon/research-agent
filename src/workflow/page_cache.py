"""页缓存：缓存键、正文哈希与读写。

从 mvp_workflow 抽出的纯叶子职责：只依赖 task_id、抓取作用域与存储层，
不读工作流的其他 state，也不依赖工作流实例。
"""
import hashlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def page_cache_key(url: str, max_pages: Optional[int], page_tiers: Optional[tuple]) -> str:
    """缓存键：站点 URL + 抓取作用域。content_hash 在读时另行校验。"""
    return f"{url}|max_pages={max_pages}|tiers={page_tiers}"


def profile_content_hash(profile: Dict[str, Any]) -> str:
    """对站内全部页的 URL+正文求哈希：内容变了哈希必变，脏缓存按未命中处理。"""
    digest = hashlib.sha256()
    for page in profile.get("pages") or []:
        digest.update(str(page.get("url") or "").encode())
        digest.update(b"\x00")
        digest.update(str(page.get("content") or "").encode())
        digest.update(b"\x01")
    return digest.hexdigest()


def load_cached_profile(
    state: Dict[str, Any], cache_key: str, *, enabled: bool
) -> Optional[Dict[str, Any]]:
    """读页缓存；未启用、无任务号或内容哈希不一致时都按未命中处理。"""
    task_id = state.get("task_id")
    # 未走 __init__ 的实例（单测直接构造个别方法）不启用缓存：
    # 没有生命周期开关就没有副作用持久化，避免测试间任务号复用导致脏命中。
    if not enabled:
        return None
    try:
        from src.storage import get_report_db

        row = get_report_db().get_page_cache(task_id, cache_key)
        if not row:
            return None
        profile = row.get("payload") or {}
        if profile_content_hash(profile) != row.get("content_hash"):
            return None
        return profile if profile.get("pages") else None
    except Exception as exc:
        logger.warning("读取页缓存失败: %s", exc)
        return None


def save_cached_profile(
    state: Dict[str, Any],
    cache_key: str,
    profile: Dict[str, Any],
    *,
    enabled: bool,
) -> None:
    """写页缓存；无任务号、未启用或空 profile 时静默跳过。"""
    task_id = state.get("task_id")
    if not task_id or not enabled:
        return
    if not isinstance(profile, dict) or not profile.get("pages"):
        return
    try:
        from src.storage import get_report_db

        get_report_db().save_page_cache(
            task_id, cache_key, profile_content_hash(profile), profile,
        )
    except Exception as exc:
        logger.warning("保存页缓存失败: %s", exc)
