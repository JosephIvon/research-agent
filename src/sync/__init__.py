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
            return self._feishu_for_call().sync_markdown(markdown_content, title)
        except Exception as e:
            return {"platform": "feishu", "error": str(e)}

    def sync_to_tencent(self, markdown_content: str, title: Optional[str] = None) -> Dict[str, Any]:
        """同步到腾讯文档"""
        try:
            return self._tencent_for_call().sync_markdown(markdown_content, title)
        except Exception as e:
            return {"platform": "tencent", "error": str(e)}

    # ------------------------------------------------------------------
    # 凭据在**调用时**解析
    #
    # 此前 FeishuSync 在构造时读一次环境变量、又被进程级单例缓存住：管理员在
    # 界面上配好凭据后，不重启进程就用不了。这里每次调用现读，配置即时生效；
    # 没有可用凭据时回落到原来的进程级默认（环境变量），行为不变。
    # ------------------------------------------------------------------
    def _feishu_for_call(self) -> FeishuSync:
        from src.config.credentials import resolve_credentials

        app_id, app_secret = resolve_credentials("feishu")
        if app_id and app_secret:
            return FeishuSync(app_id, app_secret)
        return self.feishu

    def _tencent_for_call(self) -> TencentDocSync:
        from src.config.credentials import resolve_credentials

        app_id, app_secret = resolve_credentials("tencent")
        if app_id and app_secret:
            return TencentDocSync(app_id, app_secret)
        return self.tencent


doc_sync_manager = DocSyncManager()