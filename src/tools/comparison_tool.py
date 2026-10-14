"""竞品对比分析工具"""
import re
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

# 报告中最多展示的共同能力/独特优势条数
_MAX_COMMON_FEATURES = 5
_MAX_UNIQUE_FEATURES = 3

# 功能归一化：把一切非"单词字符"（含中文）替换成空格
_FEATURE_NOISE = re.compile(r"\W+", re.UNICODE)

# 模型名分隔符：逗号、中文逗号、斜杠、顿号、分号、竖线、空白
_MODEL_SEPARATORS = re.compile(r"[,，/、;；|\s]+")


def _normalize_feature(text: Any) -> str:
    """归一化一条功能/模型名，用于跨竞品比较。

    启发式近似，不是语义判定：仅做"小写化 + 标点/空白归一 + 折叠连续空白"。
    这样 ``"SSO 登录"``、``"sso登录"``、``"SSO-登录。"`` 会被视为同一项功能。
    但中文动词前缀不会被剥离，"支持 OpenAI SDK 兼容" 与 "OpenAI SDK compatible"
    仍判为不同项——彻底解决它需要语义 embedding，本轮刻意不引入新依赖。
    """
    return " ".join(_FEATURE_NOISE.sub(" ", str(text).lower()).split())


def _normalize_features(features: List[str]) -> Dict[str, str]:
    """把功能列表归一化为 {归一化键: 原始文本}，保留首次出现的原文用于展示。"""
    normalized: Dict[str, str] = {}
    for feature in features or []:
        key = _normalize_feature(feature)
        if key:
            normalized.setdefault(key, str(feature))
    return normalized


def count_models(models: List[str]) -> int:
    """统计去重后的模型名数量。

    启发式近似，不是权威解析：``_extract_list`` 对 list 值逐项 extend、对字符串值
    整条 append，所以 ``"GPT-4, Claude, Gemini"`` 原本只会被算作 1 条。这里按常见
    分隔符（逗号、中文逗号、斜杠、顿号、分号、竖线、空白）拆分后去重再计数。
    已知代价：含空格的单名模型（如 ``"Claude 3 Opus"``）会被切开算成 3 个。
    """
    seen = set()
    for entry in models or []:
        for part in _MODEL_SEPARATORS.split(str(entry)):
            key = _normalize_feature(part)
            if key:
                seen.add(key)
    return len(seen)


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
            model_count = str(count_models(item["models"]))
            target_users = item["target_users"] or "未知"

            rows.append([name, pricing, features, model_count, target_users])

        table = "| " + " | ".join(headers) + " |\n"
        table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
        for row in rows:
            table += "| " + " | ".join(row) + " |\n"

        return table

    def common_features(self, aligned: List[Dict[str, Any]]) -> List[str]:
        """所有竞品**都**具备的功能（交集，而非并集）。

        比较前按 :func:`_normalize_feature` 归一化；输出取首个竞品中的原始文本，
        顺序跟随首个竞品，保证同一份输入每次渲染一致。只有一个竞品时，交集
        退化为它自己的功能集；没有任何功能被所有竞品共有时返回空列表。
        """
        if not aligned:
            return []
        normalized = [_normalize_features(item["features"]) for item in aligned]
        shared = set(normalized[0])
        for other in normalized[1:]:
            shared &= set(other)
        return [text for key, text in normalized[0].items() if key in shared]

    def unique_features(self, index: int, aligned: List[Dict[str, Any]]) -> List[str]:
        """第 index 个竞品具备、而其余竞品**都不**具备的功能（差集）。

        没有任何独有功能时返回空列表——调用方必须如实说明，不得回退成"取第一条"。
        """
        if not aligned or not (0 <= index < len(aligned)):
            return []
        others = set()
        for position, other in enumerate(aligned):
            if position != index:
                others |= set(_normalize_features(other["features"]))
        own = _normalize_features(aligned[index]["features"])
        return [text for key, text in own.items() if key not in others]

    def generate_differential_analysis(self) -> str:
        """生成差异化分析"""
        aligned = self.align_dimensions()
        if len(aligned) < 2:
            return "竞品数量不足，无法进行差异化分析"

        analysis = []
        for index, item in enumerate(aligned):
            # 独特优势必须是真差集：他人也有的功能不能算优势
            unique = self.unique_features(index, aligned)
            if unique:
                shown = "、".join(unique[:_MAX_UNIQUE_FEATURES])
                if len(unique) > _MAX_UNIQUE_FEATURES:
                    shown += " 等"
                analysis.append(f"**{item['name']}**独特优势：{shown}")
            elif item["features"]:
                analysis.append(
                    f"**{item['name']}**独特优势：未识别到独有功能"
                    "（其已抽取的功能均见于其他竞品，或功能描述缺失）"
                )
            if item["limitations"]:
                limitation = item["limitations"][0]
                analysis.append(f"**{item['name']}**明显劣势：{limitation}")

        # 共同能力必须是交集：只有所有竞品都具备的功能才算共同能力
        common = self.common_features(aligned)
        if common:
            analysis.append(f"\n**共同能力**：{', '.join(common[:_MAX_COMMON_FEATURES])}")
        else:
            # 如实说明未识别到，而不是回退到并集去编造
            analysis.append(
                "\n**共同能力**：未识别到所有竞品共有的功能"
                "（各竞品功能描述可能缺失，或描述粒度不足以比较）"
            )

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