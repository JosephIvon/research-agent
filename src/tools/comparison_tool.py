"""竞品对比分析工具"""
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse


class ComparisonAnalyzer:
    """横向对比分析器"""

    def __init__(self, competitors: List[Dict[str, Any]]):
        self.competitors = competitors

    def align_dimensions(self) -> List[Dict[str, Any]]:
        """将不同竞品的数据对齐到同一维度"""
        aligned = []
        for comp in self.competitors:
            item = {
                "name": comp["name"],
                "url": comp["url"],
                "status": comp["status"],
                "pricing": self._extract_field(comp["extracted_data"], "定价", "价格", "月费", "年费", "收费"),
                "features": self._extract_list(comp["extracted_data"], "功能", "特性", "优势"),
                "models": self._extract_list(comp["extracted_data"], "模型", "支持", "引擎"),
                "target_users": self._extract_field(comp["extracted_data"], "目标用户", "用户", "适用"),
                "limitations": self._extract_list(comp["extracted_data"], "限制", "劣势", "不足"),
                "integrations": self._extract_list(comp["extracted_data"], "集成", "对接", "生态"),
            }
            aligned.append(item)
        return aligned

    def _extract_field(self, data: List[Dict], *keywords) -> Optional[str]:
        """提取包含关键词的字段"""
        for item in data:
            key = str(item.get("key", "")).lower()
            if any(kw.lower() in key for kw in keywords):
                return str(item.get("value", ""))[:200]
        return None

    def _extract_list(self, data: List[Dict], *keywords) -> List[str]:
        """提取包含关键词的列表字段"""
        results = []
        for item in data:
            key = str(item.get("key", "")).lower()
            if any(kw.lower() in key for kw in keywords):
                value = item.get("value", "")
                if isinstance(value, list):
                    results.extend([str(v)[:100] for v in value[:10]])
                elif isinstance(value, str):
                    results.append(value[:200])
        return list(dict.fromkeys(results))[:10]

    def generate_comparison_table(self) -> str:
        """生成Markdown对比表格"""
        aligned = self.align_dimensions()
        if not aligned:
            return "数据不足，无法生成对比表"

        headers = ["竞品", "定价", "核心功能", "支持模型数", "目标用户"]
        rows = []
        for item in aligned:
            status_icon = "✅" if item["status"] == "success" else "⚠️"
            name = f"{status_icon} {item['name'][:20]}"
            pricing = item["pricing"] or "未知"
            features = (item["features"][0][:40] if item["features"] else "未知") + ("..." if len(item["features"]) > 1 else "")
            model_count = str(len(item["models"]))
            target_users = item["target_users"] or "未知"

            rows.append([name, pricing, features, model_count, target_users])

        table = "| " + " | ".join(headers) + " |\n"
        table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in rows:
            table += "| " + " | ".join(row) + " |\n"

        return table

    def generate_differential_analysis(self) -> str:
        """生成差异化分析"""
        aligned = self.align_dimensions()
        if len(aligned) < 2:
            return "竞品数量不足，无法进行差异化分析"

        analysis = []
        for item in aligned:
            if item["features"]:
                unique_feature = item["features"][0]
                analysis.append(f"**{item['name']}**独特优势：{unique_feature}")
            if item["limitations"]:
                limitation = item["limitations"][0]
                analysis.append(f"**{item['name']}**明显劣势：{limitation}")

        all_features = set()
        for item in aligned:
            all_features.update(item["features"])
        if all_features:
            common = list(all_features)[:5]
            analysis.append(f"\n**共同能力**：{', '.join(common)}")

        return "\n\n".join(analysis) if analysis else "数据不足"

    def generate_full_comparison_section(self) -> str:
        """生成完整的对比章节"""
        comparison_table = self.generate_comparison_table()
        differential_analysis = self.generate_differential_analysis()
        completeness = self._evaluate_completeness()

        section = f"""## 二、竞品横向对比

### 2.1 对比概览

{comparison_table}

### 2.2 差异化分析

{differential_analysis}

### 2.3 数据完整性评估

{completeness}
"""
        return section

    def _evaluate_completeness(self) -> str:
        """评估各竞品数据完整性"""
        aligned = self.align_dimensions()
        if not aligned:
            return "数据不足"

        results = []
        for item in aligned:
            data_points = sum([
                1 if item["pricing"] else 0,
                len(item["features"]),
                len(item["models"]),
                1 if item["target_users"] else 0,
            ])
            status = "完整" if data_points >= 4 else "部分缺失" if data_points >= 2 else "数据不足"
            results.append(f"- **{item['name']}**：{status}（{data_points}个维度有数据）")

        return "\n".join(results)


def infer_competitor_name(url: str, extracted_data: List[Dict] = None) -> str:
    """从提取数据或URL推断竞品名称"""
    if extracted_data:
        for item in extracted_data:
            key = str(item.get("key", "")).lower()
            if any(kw in key for kw in ["名称", "名字", "品牌", "产品名"]):
                return str(item.get("value", ""))[:50]

    parsed = urlparse(url)
    netloc = parsed.netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]

    name_map = {
        "shimo.im": "石墨文档",
        "wps.cn": "WPS文档",
        "docs.qq.com": "腾讯文档",
        "feishu.cn": "飞书文档",
        "notion.so": "Notion",
        "confluence": "Confluence",
        "slite": "Slite",
        "craft": "Craft",
    }

    for domain, name in name_map.items():
        if domain in netloc:
            return name

    return netloc.split(".")[0].capitalize()