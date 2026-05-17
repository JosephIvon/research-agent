"""飞书文档同步模块"""
import os
import re
import time
import requests
from typing import Optional, Dict, Any


class FeishuSync:
    """飞书文档同步器"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: Optional[str] = None, app_secret: Optional[str] = None):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET")
        self.tenant_access_token = None
        self.token_expire_time = 0

    def _get_token(self) -> str:
        """获取 tenant_access_token"""
        if self.tenant_access_token and time.time() < self.token_expire_time:
            return self.tenant_access_token

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"获取飞书Token失败: {data.get('msg')}")

        self.tenant_access_token = data["tenant_access_token"]
        self.token_expire_time = time.time() + data.get("expire", 7200) - 300

        return self.tenant_access_token

    def create_document(self, title: str, folder_token: Optional[str] = None) -> Dict[str, Any]:
        """创建飞书文档"""
        token = self._get_token()

        url = f"{self.BASE_URL}/docx/v1/documents"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        payload = {"title": title}
        if folder_token:
            payload["folder_token"] = folder_token

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建飞书文档失败: {data.get('msg')}")

        document = data["data"]["document"]
        return {
            "document_id": document["document_id"],
            "title": document.get("title", title),
            "url": f"https://feishu.cn/docx/{document['document_id']}"
        }

    def create_text_block(self, document_id: str, text: str, block_id: Optional[str] = None) -> str:
        """在文档中创建文本块"""
        token = self._get_token()

        url = f"{self.BASE_URL}/docx/v1/documents/{document_id}/blocks/{block_id or document_id}/children"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        payload = {
            "children": [{
                "block_type": 2,
                "text": {
                    "elements": [{
                        "text_run": {
                            "content": text,
                            "text_element_style": {}
                        }
                    }],
                    "style": {}
                }
            }],
            "index": -1
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建文本块失败: {data.get('msg')}")

        return data["data"]["children"][0]["block_id"]

    def create_heading_block(self, document_id: str, text: str, level: int = 1, block_id: Optional[str] = None) -> str:
        """在文档中创建标题块"""
        token = self._get_token()

        url = f"{self.BASE_URL}/docx/v1/documents/{document_id}/blocks/{block_id or document_id}/children"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        block_type_map = {1: 3, 2: 4, 3: 5}

        payload = {
            "children": [{
                "block_type": block_type_map.get(level, 3),
                "heading1": {
                    "elements": [{
                        "text_run": {
                            "content": text,
                            "text_element_style": {}
                        }
                    }],
                    "style": {}
                }
            }],
            "index": -1
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建标题块失败: {data.get('msg')}")

        return data["data"]["children"][0]["block_id"]

    def create_table_block(self, document_id: str, headers: list, rows: list, block_id: Optional[str] = None) -> str:
        """在文档中创建表格块"""
        token = self._get_token()

        url = f"{self.BASE_URL}/docx/v1/documents/{document_id}/blocks/{block_id or document_id}/children"
        headers_req = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        def make_text_element(content):
            return {
                "text_run": {
                    "content": content,
                    "text_element_style": {}
                }
            }

        def make_cell(text):
            return {
                "table_cell": {
                    "elements": [make_text_element(text)],
                    "style": {}
                }
            }

        table_rows = [make_cell(h) for h in headers]
        for row in rows:
            table_rows.append(make_cell(row))

        payload = {
            "children": [{
                "block_type": 31,
                "table": {
                    "cells": table_rows,
                    "property": {
                        "row_size": len(table_rows),
                        "column_size": len(headers),
                        "column_width": [150] * len(headers),
                        "header_row": True,
                        "merge_info": []
                    }
                }
            }],
            "index": -1
        }

        response = requests.post(url, headers=headers_req, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建表格块失败: {data.get('msg')}")

        return data["data"]["children"][0]["block_id"]

    def create_blocks_from_markdown(self, document_id: str, markdown: str, block_id: Optional[str] = None) -> bool:
        """将Markdown内容解析并创建为飞书文档块"""
        token = self._get_token()
        base_block_id = block_id or document_id

        lines = markdown.split("\n")
        i = 0
        blocks_created = 0

        while i < len(lines):
            line = lines[i].strip()

            if not line:
                i += 1
                continue

            if line.startswith("## "):
                heading_text = line[3:].strip()
                self.create_heading_block(document_id, heading_text, level=1, block_id=base_block_id)
                blocks_created += 1

            elif line.startswith("### "):
                heading_text = line[4:].strip()
                self.create_heading_block(document_id, heading_text, level=2, block_id=base_block_id)
                blocks_created += 1

            elif line.startswith("- ") or line.startswith("* "):
                list_items = []
                while i < len(lines) and (lines[i].strip().startswith("- ") or lines[i].strip().startswith("* ")):
                    item_text = lines[i].strip()[2:].strip()
                    list_items.append(item_text)
                    i += 1
                for item in list_items:
                    self._create_bullet_block(document_id, item, base_block_id)
                    blocks_created += 1
                continue

            elif line.startswith("|") and line.endswith("|"):
                table_lines = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    table_lines.append(lines[i].strip())
                    i += 1

                if len(table_lines) >= 2:
                    headers = [h.strip() for h in table_lines[0].split("|")[1:-1]]
                    rows = []
                    for row_line in table_lines[2:]:
                        cells = [c.strip() for c in row_line.split("|")[1:-1]]
                        if cells:
                            rows.append(cells)

                    if headers and rows:
                        try:
                            self.create_table_block(document_id, headers, rows, base_block_id)
                            blocks_created += 1
                        except Exception:
                            for row in rows:
                                for cell in row:
                                    self.create_text_block(document_id, cell, base_block_id)
                                    blocks_created += 1
                    continue

            else:
                clean_text = line
                if line.startswith("# "):
                    clean_text = line[2:].strip()
                    self.create_heading_block(document_id, clean_text, level=1, block_id=base_block_id)
                else:
                    clean_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
                    clean_text = clean_text.replace("**", "").replace("*", "").replace("`", "")
                    if clean_text:
                        self.create_text_block(document_id, clean_text, base_block_id)
                blocks_created += 1

            i += 1

        return True

    def _create_bullet_block(self, document_id: str, text: str, block_id: str) -> str:
        """创建无序列表块"""
        token = self._get_token()

        url = f"{self.BASE_URL}/docx/v1/documents/{document_id}/blocks/{block_id}/children"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        payload = {
            "children": [{
                "block_type": 12,
                "bullet": {
                    "elements": [{
                        "text_run": {
                            "content": text,
                            "text_element_style": {}
                        }
                    }],
                    "style": {}
                }
            }],
            "index": -1
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"创建列表块失败: {data.get('msg')}")

        return data["data"]["children"][0]["block_id"]

    def sync_markdown(self, markdown_content: str, title: Optional[str] = None, folder_token: Optional[str] = None) -> Dict[str, Any]:
        """同步Markdown内容到飞书文档（完整流程）"""
        if not self.app_id or not self.app_secret:
            raise Exception("飞书APP配置缺失，请检查环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

        doc_title = title or "智能调研报告"
        doc_info = self.create_document(doc_title, folder_token)
        document_id = doc_info["document_id"]

        self.create_blocks_from_markdown(document_id, markdown_content)

        return {
            "platform": "feishu",
            "document_id": document_id,
            "title": doc_info["title"],
            "url": doc_info["url"]
        }


_feishu_sync_instance = None

def get_feishu_sync() -> FeishuSync:
    global _feishu_sync_instance
    if _feishu_sync_instance is None:
        _feishu_sync_instance = FeishuSync()
    return _feishu_sync_instance