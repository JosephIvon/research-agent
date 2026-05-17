"""腾讯文档同步模块"""
import os
import time
import requests
from typing import Optional, Dict, Any


class TencentDocSync:
    """腾讯文档同步器"""

    BASE_URL = "https://docs.qq.com"

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.getenv("TENCENT_CLIENT_ID") or os.getenv("TENCENT_APP_ID")
        self.client_secret = client_secret or os.getenv("TENCENT_APP_SECRET")
        self.access_token = None
        self.token_expire_time = 0
        self.open_id = None

    def _get_token(self) -> tuple:
        """获取 Access Token 和 Open ID"""
        if self.access_token and time.time() < self.token_expire_time:
            return self.access_token, self.open_id

        if not self.client_id or not self.client_secret:
            raise Exception("腾讯文档配置缺失，请检查环境变量 TENCENT_CLIENT_ID 和 TENCENT_APP_SECRET")

        url = f"{self.BASE_URL}/oauth/v2/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": "all"
        }

        response = requests.post(url, data=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if "access_token" not in data:
            error_message = data.get("msg") or data.get("error") or "unknown_error"
            raise Exception(f"获取腾讯文档Token失败: {error_message}")

        self.access_token = data["access_token"]
        self.token_expire_time = time.time() + data.get("expires_in", 2592000) - 300
        self.open_id = data.get("user_id", "")

        return self.access_token, self.open_id

    def create_document(self, title: str, doc_type: str = "DOC") -> Dict[str, Any]:
        """创建腾讯文档

        Args:
            title: 文档标题
            doc_type: 文档类型 - DOC(在线文档), SHEET(在线表格), SLIDE(在线幻灯片)
        """
        access_token, open_id = self._get_token()

        url = f"{self.BASE_URL}/openapi/drive/v3/files"
        headers = {
            "Access-Token": access_token,
            "Client-Id": self.client_id
        }
        payload = {
            "name": title,
            "type": doc_type,
            "folderId": ""
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建腾讯文档失败: {data.get('msg')}")

        file_info = data.get("fileInfo", {})
        return {
            "file_id": file_info.get("file_id", ""),
            "title": title,
            "url": file_info.get("url", ""),
            "type": doc_type
        }

    def update_document_content(self, file_id: str, content: str) -> bool:
        """更新文档内容（仅支持SHEET类型）

        Args:
            file_id: 文档ID
            content: 要写入的内容（纯文本）
        """
        access_token, _ = self._get_token()

        url = f"{self.BASE_URL}/openapi/sheet/v2/spreads/{file_id}/values"
        headers = {
            "Access-Token": access_token,
            "Client-Id": self.client_id
        }

        lines = content.split("\n")
        values = [[line] for line in lines if line.strip()]

        payload = {
            "data": {
                "values": values
            }
        }

        response = requests.put(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        return data.get("code") == 0

    def sync_markdown(self, markdown_content: str, title: Optional[str] = None) -> Dict[str, Any]:
        """同步Markdown内容到腾讯文档（完整流程）

        注意：腾讯文档API创建的是SHEET类型，支持写入纯文本内容
        """
        doc_title = title or "智能调研报告"

        doc_info = self.create_document(doc_title, "SHEET")

        if doc_info.get("file_id"):
            content_lines = self._convert_markdown_to_text(markdown_content)
            self.update_document_content(doc_info["file_id"], content_lines)

        return {
            "platform": "tencent",
            "document_id": doc_info.get("file_id", ""),
            "title": doc_info["title"],
            "url": doc_info.get("url", "")
        }

    def _convert_markdown_to_text(self, markdown: str) -> str:
        """将Markdown转换为纯文本"""
        lines = []
        for line in markdown.split("\n"):
            line = line.strip()

            if not line:
                lines.append("")
                continue

            if line.startswith("# "):
                lines.append(line[2:])
                lines.append("=" * len(line[2:]))
            elif line.startswith("## "):
                lines.append(line[3:])
                lines.append("-" * len(line[3:]))
            elif line.startswith("### "):
                lines.append("  " + line[4:])
            elif line.startswith("- ") or line.startswith("* "):
                lines.append("  * " + line[2:])
            elif line.startswith("|") and line.endswith("|"):
                continue
            else:
                line = line.replace("**", "").replace("*", "").replace("`", "")
                line = line.replace("[", "").replace("](", " - ").replace(")", "")
                lines.append(line)

        return "\n".join(lines)


_tencent_doc_sync_instance = None

def get_tencent_doc_sync() -> TencentDocSync:
    global _tencent_doc_sync_instance
    if _tencent_doc_sync_instance is None:
        _tencent_doc_sync_instance = TencentDocSync()
    return _tencent_doc_sync_instance
