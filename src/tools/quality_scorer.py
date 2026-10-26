"""报告质量评分模块 - 评估数据完整度，提示缺失维度"""
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from src.agent.citations import EvidenceStore


@dataclass
class DimensionScore:
    """单个维度的评分"""
    name: str
    key: str
    keywords: List[str]
    score: float
    has_data: bool
    data_count: int
    sample_data: List[str]


@dataclass
class QualityReport:
    """质量评估报告"""
    overall_score: float
    grade: str
    dimensions: List[DimensionScore]
    missing_dimensions: List[str]
    recommendations: List[str]
    data_sources: List[str]
    is_usable: bool
    abandonment_reasons: List[str]


class QualityScorer:
    """报告质量评分器 - 评估数据完整度，参考PMAI风险评估 + BuildBetter放弃机制"""

    DIMENSIONS = [
        {"name": "定价信息", "key": "pricing", "keywords": ["定价", "价格", "月费", "年费", "收费", "免费", "套餐", "subscription", "price"]},
        {"name": "核心功能", "key": "features", "keywords": ["功能", "特性", "特点", "优势", "能力", "feature", "function"]},
        {"name": "支持模型", "key": "models", "keywords": ["模型", "支持", "引擎", "接入", "model", "engine"]},
        {"name": "目标用户", "key": "target_users", "keywords": ["目标用户", "用户", "适用", "人群", "客户", "user", "customer"]},
        {"name": "集成生态", "key": "integrations", "keywords": ["集成", "对接", "生态", "插件", "API", "integration", "plugin"]},
        {"name": "限制劣势", "key": "limitations", "keywords": ["限制", "劣势", "不足", "缺点", "limitation", "con"]},
        {"name": "使用评价", "key": "reviews", "keywords": ["评价", "口碑", "用户说", "review", "feedback"]},
    ]

    SCORE_THRESHOLDS = {
        "excellent": 8.0,
        "good": 6.0,
        "fair": 4.0,
        "poor": 2.0
    }

    def __init__(self, competitors: List[Dict[str, Any]]):
        self.competitors = competitors

    def evaluate(self) -> QualityReport:
        """评估整体数据质量"""
        dimension_scores = []
        missing_dims = []
        all_sources = []
        recommendations = []
        abandonment_reasons = []

        for dim in self.DIMENSIONS:
            dim_score = self._evaluate_dimension(dim)
            dimension_scores.append(dim_score)

            if not dim_score.has_data:
                missing_dims.append(dim["name"])
            elif dim_score.score < 3.0:
                recommendations.append(f"⚠️ {dim['name']}数据不足，建议补充")

            all_sources.extend([s for s in dim_score.sample_data if s])

        successful_competitors = [c for c in self.competitors if c.get("status") == "success"]
        overall_score = self._calculate_overall_score(dimension_scores, len(successful_competitors))

        if len(successful_competitors) == 0:
            abandonment_reasons.append("所有竞品抓取失败，无法生成报告")
        elif overall_score < 3.0:
            abandonment_reasons.append(f"数据质量过低（{overall_score:.1f}/10），建议补充更多数据源")
        elif len(successful_competitors) < 2 and len(missing_dims) > 4:
            abandonment_reasons.append("竞品数量不足且数据缺失较多，建议增加调研范围")

        grade = self._score_to_grade(overall_score)
        is_usable = overall_score >= 4.0 and len(abandonment_reasons) == 0

        if missing_dims:
            recommendations.insert(0, f"📋 缺失维度：{'、'.join(missing_dims)}")

        return QualityReport(
            overall_score=overall_score,
            grade=grade,
            dimensions=dimension_scores,
            missing_dimensions=missing_dims,
            recommendations=recommendations,
            data_sources=list(set(all_sources)),
            is_usable=is_usable,
            abandonment_reasons=abandonment_reasons
        )

    def _evaluate_dimension(self, dim: Dict) -> DimensionScore:
        """评估单个维度"""
        keywords = dim["keywords"]
        extracted_data = []

        for comp in self.competitors:
            for item in comp.get("extracted_data", []):
                key = str(item.get("key", "")).lower()
                value = str(item.get("value", ""))
                value_lower = value.lower()

                # 维度自带规范数据 key：key 命中即属于该维度，不靠关键词子串撞。
                # 关键词表写 "price"、数据 key 是 "pricing" —— "price" 不是 "pricing"
                # 的子串，值（"标准版 $10/月"）也不含关键词 → 定价被误判成缺失
                # （实测 16/20 个 case 受影响，附录自评表批量造假）。
                if dim["key"].lower() in key or any(
                        kw.lower() in key or kw.lower() in value_lower for kw in keywords):
                    if value and len(value) > 2:
                        extracted_data.append(value[:100])

        has_data = len(extracted_data) > 0
        data_count = len(extracted_data)

        if data_count == 0:
            score = 0.0
        elif data_count == 1:
            score = 3.0
        elif data_count == 2:
            score = 5.0
        elif data_count <= 5:
            score = 7.0
        else:
            score = min(10.0, 7.0 + (data_count - 5) * 0.5)

        return DimensionScore(
            name=dim["name"],
            key=dim["key"],
            keywords=keywords,
            score=score,
            has_data=has_data,
            data_count=data_count,
            sample_data=extracted_data[:3]
        )

    def _calculate_overall_score(self, dimension_scores: List[DimensionScore], competitor_count: int) -> float:
        """计算整体评分"""
        if not dimension_scores:
            return 0.0

        base_score = sum(d.score for d in dimension_scores) / len(dimension_scores)

        coverage_bonus = min(2.0, len([d for d in dimension_scores if d.has_data]) * 0.3)

        competitor_bonus = 0.0
        if competitor_count >= 3:
            competitor_bonus = 1.5
        elif competitor_count == 2:
            competitor_bonus = 1.0

        total_score = base_score + coverage_bonus + competitor_bonus
        return min(10.0, max(0.0, total_score))

    def _score_to_grade(self, score: float) -> str:
        """评分转等级"""
        if score >= self.SCORE_THRESHOLDS["excellent"]:
            return "A"
        elif score >= self.SCORE_THRESHOLDS["good"]:
            return "B"
        elif score >= self.SCORE_THRESHOLDS["fair"]:
            return "C"
        elif score >= self.SCORE_THRESHOLDS["poor"]:
            return "D"
        else:
            return "F"

    def generate_quality_section(self, report: Optional[QualityReport] = None) -> str:
        """生成数据完整度评估章节。

        刻意不叫「质量评分」或「可信度」：本评估只回答"抓到了多少个维度、每维有多少条数据"，
        回答不了"这些结论对不对"。可信度由引用校验（`agent.citations`）负责，
        两件事混为一谈就是用数据多少冒充事实正确。
        """
        if report is None:
            report = self.evaluate()

        section = f"""## 附录：数据完整度评估

> 本节只衡量**抓取到的数据量与维度覆盖**，不衡量结论正确性。结论的可追溯性见各事实句的 `[e:xxxx]` 证据标记。

### 数据完整度：{report.overall_score:.1f}/10 （覆盖等级：{report.grade}）

#### 维度覆盖情况

| 维度 | 评分 | 数据条数 | 状态 |
|------|------|----------|------|
"""

        for dim in report.dimensions:
            status = "✅ 有数据" if dim.has_data else "❌ 缺失"
            score_bar = "▓" * int(dim.score) + "░" * (10 - int(dim.score))
            section += f"| {dim.name} | {score_bar} {dim.score:.1f} | {dim.data_count} | {status} |\n"

        if report.missing_dimensions:
            section += f"\n#### ⚠️ 缺失维度\n\n以下重要维度数据不足，建议补充调研：\n\n"
            for dim_name in report.missing_dimensions:
                section += f"- **{dim_name}**：信息缺失\n"

        if report.recommendations:
            section += f"\n#### 💡 改进建议\n\n"
            for rec in report.recommendations:
                section += f"- {rec}\n"

        if report.abandonment_reasons:
            section += f"\n#### 🚫 放弃生成原因\n\n"
            for reason in report.abandonment_reasons:
                section += f"- {reason}\n"

        # 证据对照表：正文每个 [e:标记] 都能在这里查到 id → 原文（key/url/value），
        # judge（或人）据此核对「数字在不在它自己引用的那条证据里」，而不是只能查
        # 标记格式 —— 这是 citation 校验从「格式合规」升级为「扎根可核」的前提
        # （llm-judge-kit 校准裁决 2026-10-26：选项 A）。全量列出、不截断：截断会让
        # 部分标记查不到，反而被误读成「无来源」。
        store = EvidenceStore.from_extracted_data(
            item for comp in self.competitors for item in comp.get("extracted_data", []))
        section += (f"\n#### 📚 数据来源（[e:标记] → 原文）\n\n"
                    f"共 {len(store.evidences)} 条证据，与正文 [e:xxxx] 标记一一对应：\n\n")
        for ev in store.evidences:
            origin = f" @ {ev.url}" if ev.url else ""
            section += f"- [e:{ev.evidence_id}] {ev.key}{origin}：{ev.value.replace(chr(10), ' ')}\n"

        # 机器附录标记：本节由确定性代码生成，引用校验（agent.citations）会
        # 跳过标记内内容——这里的数字来自程序计算，不是模型的事实主张。
        return f"<!--machine-appendix-->\n{section}<!--/machine-appendix-->"
