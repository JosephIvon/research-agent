"""文档同步模块 - 统一入口"""
from .feishu_sync import FeishuSync, get_feishu_sync
from .tencent_sync import TencentDocSync, get_tencent_doc_sync
from typing import Optional, Dict, Any


class DocSyncManager:
    """文档同步管理器 - 统一处理多平台文档同步"""

    def __init__(self):
        self._feishu = None
        self._tencent = None

    @property
    def feishu(self) -> FeishuSync:
        if self._feishu is None:
            self._feishu = get_feishu_sync()
        return self._feishu

    @property
    def tencent(self) -> TencentDocSync:
        if self._tencent is None:
            self._tencent = get_tencent_doc_sync()
        return self._tencent

    def sync_to_feishu(self, markdown_content: str, title: Optional[str] = None) -> Dict[str, Any]:
        """同步到飞书文档"""
        try:
            return self.feishu.sync_markdown(markdown_content, title)
        except Exception as e:
            return {"platform": "feishu", "error": str(e)}

    def sync_to_tencent(self, markdown_content: str, title: Optional[str] = None) -> Dict[str, Any]:
        """同步到腾讯文档"""
        try:
            return self.tencent.sync_markdown(markdown_content, title)
        except Exception as e:
            return {"platform": "tencent", "error": str(e)}

    def sync_to_all(self, markdown_content: str, title: Optional[str] = None, platforms: Optional[list] = None) -> Dict[str, Any]:
        """同步到多个平台"""
        platforms = platforms or ["feishu", "tencent"]
        results = {}

        if "feishu" in platforms:
            results["feishu"] = self.sync_to_feishu(markdown_content, title)

        if "tencent" in platforms:
            results["tencent"] = self.sync_to_tencent(markdown_content, title)

        return results


doc_sync_manager = DocSyncManager()