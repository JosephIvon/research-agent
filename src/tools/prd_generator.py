"""PRD生成器模块 - 从竞品分析报告生成PRD文档"""
from typing import Optional
from src.llm import get_llm_client, LLMClient
from src.config.settings import LLM_MAX_CONTENT_CHARS


class PRDGenerator:
    """PRD生成器 - 从竞品分析生成PRD"""

    PRD_STRUCTURE = """
## 1. 背景与目标
- 业务背景
- 解决什么问题
- 成功指标（KPI）

## 2. 用户需求
- 用户画像
- User Story（作为[角色]，我想[功能]，以便[收益]）

## 3. 功能需求
- P0（必须有）
- P1（应该有）
- P2（可以有）

## 4. 验收标准（AC）
- Given/When/Then 格式

## 5. 非功能性需求
- 性能要求
- 安全要求
- 兼容性

## 6. 技术方案
- 系统架构
- 技术选型

## 7. 里程碑
- Alpha / Beta / GA
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self._llm = llm_client

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm

    def generate_from_competitive_report(self, report_content: str, query: str) -> str:
        """从竞品分析报告生成PRD"""
        prompt = [
            {
                "role": "system",
                "content": f"""你是一名资深产品经理，擅长将竞品分析转化为PRD文档。

【PRD结构】
{self.PRD_STRUCTURE}

【要求】
- 语言简洁专业
- User Story使用标准格式
- 验收标准可量化测试
- 控制在2000字以内
- 基于竞品分析结果，提出差异化竞争策略"""
            },
            {
                "role": "user",
                "content": f"""基于以下竞品分析报告，生成PRD：

{report_content[:LLM_MAX_CONTENT_CHARS]}

调研主题：{query}"""
            }
        ]

        return self.llm.chat_completion(prompt, temperature=0.2)

    def generate_feature_requirements(self, report_content: str, query: str) -> str:
        """生成功能需求清单"""
        prompt = [
            {
                "role": "system",
                "content": """你是一名产品经理，擅长从竞品分析中提取功能需求。

【输出格式】
请输出JSON格式的功能需求清单：
{
    "p0_features": ["功能描述1", "功能描述2", ...],
    "p1_features": ["功能描述1", "功能描述2", ...],
    "p2_features": ["功能描述1", "功能描述2", ...],
    "differentiation_points": ["差异化点1", "差异化点2", ...]
}

【原则】
- P0：核心竞争力，必须实现
- P1：重要功能，应该实现
- P2：可选功能，可以实现
- 差异化点：区别于竞品的独特价值"""
            },
            {
                "role": "user",
                "content": f"""基于以下竞品分析报告，提取功能需求：

{report_content[:LLM_MAX_CONTENT_CHARS]}

调研主题：{query}"""
            }
        ]

        return self.llm.chat_completion(prompt, temperature=0.2)


_prd_generator_instance = None

def get_prd_generator() -> PRDGenerator:
    global _prd_generator_instance
    if _prd_generator_instance is None:
        _prd_generator_instance = PRDGenerator()
    return _prd_generator_instance
