"""追问优化模块 - 支持生成报告后追问补充特定维度分析"""
from typing import List, Dict, Any, Optional
from src.llm import get_llm_client, LLMClient


class FollowUpOptimizer:
    """报告追问优化器 - 参考墨刀AI的追问机制"""

    DIMENSION_PROMPTS = {
        "pricing": "定价策略",
        "features": "核心功能",
        "models": "支持模型",
        "integrations": "集成生态",
        "target_users": "目标用户",
        "limitations": "限制劣势",
        "comparison": "对比分析",
        "recommendations": "选型建议",
        "technical": "技术实现",
        "market": "市场定位"
    }

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self._llm = llm_client

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm

    def suggest_follow_up_questions(self, original_query: str, report_content: str, quality_report: Any = None) -> List[str]:
        """基于已有报告，推荐可能的追问方向"""
        prompt = [
            {
                "role": "system",
                "content": """你是一名专业的竞品分析顾问，擅长基于已有报告推荐追问方向。

【任务】
分析已有报告，找出信息不足或可以深入的方向，生成3-5个有价值的追问问题。

【输出格式】
请按以下JSON格式输出，不要包含其他内容：
{
    "suggested_questions": [
        {"dimension": "维度关键词", "question": "具体的追问问题"},
        ...
    ],
    "reasoning": "简要说明推荐这些问题的理由"
}

【追问维度库】
- pricing: 定价策略相关
- features: 核心功能深入
- models: 支持模型细节
- integrations: 集成生态
- target_users: 目标用户画像
- limitations: 限制与劣势
- comparison: 对比细节
- recommendations: 选型建议细化
- technical: 技术实现难度
- market: 市场定位分析

【原则】
1. 优先推荐用户实际关心的维度
2. 问题要具体可回答
3. 不要重复报告已有的内容
4. 考虑调研场景的实用性"""
            },
            {
                "role": "user",
                "content": f"""原始调研主题：{original_query}

已有报告内容：
{report_content[:3000]}

{'数据质量评估：' + str(quality_report) if quality_report else ''}

请推荐3-5个有价值的追问方向。"""
            }
        ]

        response = self.llm.chat_completion(prompt, temperature=0.3)

        try:
            import json
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data.get("suggested_questions", [])
        except Exception as e:
            pass  # 解析失败时使用默认建议

        return self._fallback_suggestions()

    def expand_dimension(
        self,
        original_query: str,
        report_content: str,
        dimension: str,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """针对特定维度进行深入扩展分析"""
        dimension_desc = self.DIMENSION_PROMPTS.get(dimension, dimension)

        prompt = [
            {
                "role": "system",
                "content": f"""你是一名专业的市场调研分析师，擅长对报告的特定维度进行深入扩展分析。

【任务】
基于已有报告，针对"{dimension_desc}"这一维度进行深入扩展分析。

【要求】
1. 不重复已有内容
2. 提供更深入的见解
3. 补充具体的例子或数据
4. 保持与原报告风格一致
5. 控制在500字以内

【输出格式】
请直接输出扩展内容，使用Markdown格式，包含标题和正文。"""
            },
            {
                "role": "user",
                "content": f"""原始调研主题：{original_query}

已有报告：
{report_content[:3000]}

请深入扩展"{dimension_desc}"这一维度。"""
            }
        ]

        return self.llm.chat_completion(prompt, temperature=0.3)

    def answer_follow_up(self, original_query: str, report_content: str, question: str) -> str:
        """回答用户的追问问题"""
        prompt = [
            {
                "role": "system",
                "content": """你是一名专业的竞品分析顾问，擅长基于调研报告回答追问。

【任务】
用户针对已有报告提出了一个追问，请基于报告内容给出专业、准确的回答。

【要求】
1. 基于已有报告内容回答
2. 如报告内容不足，明确说明并给出合理推断
3. 保持专业、客观的语气
4. 控制在300字以内
5. 如问题超出报告范围，请说明"根据现有数据无法准确回答"""
            },
            {
                "role": "user",
                "content": f"""原始调研主题：{original_query}

已有报告：
{report_content[:3000]}

追问问题：{question}

请回答追问。"""
            }
        ]

        return self.llm.chat_completion(prompt, temperature=0.3)

    def generate_follow_up_section(self, original_query: str, report_content: str, user_question: str) -> str:
        """生成包含追问回答的扩展章节"""
        answer = self.answer_follow_up(original_query, report_content, user_question)

        section = f"""

---

## 追问补充

**Q: {user_question}**

{answer}

"""
        return section

    def _fallback_suggestions(self) -> List[Dict[str, str]]:
        """当无法解析时的默认建议"""
        return [
            {"dimension": "pricing", "question": "这些产品的定价策略有什么共同点和差异？"},
            {"dimension": "features", "question": "在核心功能上，各产品的技术实现有什么特点？"},
            {"dimension": "recommendations", "question": "针对中小企业，应该如何选择？"}
        ]


_follow_up_optimizer_instance = None

def get_follow_up_optimizer() -> FollowUpOptimizer:
    global _follow_up_optimizer_instance
    if _follow_up_optimizer_instance is None:
        _follow_up_optimizer_instance = FollowUpOptimizer()
    return _follow_up_optimizer_instance
