"""多角色审查模块 - 从开发/测试/运营视角审查报告"""
from typing import List, Dict, Any, Optional
from src.llm import get_llm_client, LLMClient


class MultiRoleReviewer:
    """多角色审查器 - 从不同专业视角审查报告质量"""

    ROLES = {
        "developer": {
            "name": "开发工程师视角",
            "focus": "技术实现难度、集成复杂度、可维护性",
            "prompt_addition": """【开发工程师视角审查要点】
1. 技术可行性：技术栈是否主流？实现难度如何？
2. 集成复杂度：与现有系统集成需要多少工作量？
3. 可维护性：后续迭代升级的复杂度如何？
4. 性能考量：大规模使用时的性能瓶颈在哪里？
5. 风险点：技术层面有哪些潜在风险？"""
        },
        "tester": {
            "name": "测试工程师视角",
            "focus": "验收标准、测试覆盖、风险点",
            "prompt_addition": """【测试工程师视角审查要点】
1. 验收标准：功能需求是否可量化测试？
2. 测试覆盖：哪些场景容易被忽略？
3. 边界条件：极端情况下的表现如何？
4. 回归风险：新增功能对现有功能的影响？
5. 自动化可行性：哪些测试可以自动化？"""
        },
        "operations": {
            "name": "运营视角",
            "focus": "成功指标、数据支撑、落地性",
            "prompt_addition": """【运营视角审查要点】
1. 成功指标：如何量化评估项目成功？
2. 数据支撑：决策是否有足够的数据支撑？
3. 落地性：建议是否可执行？
4. 资源需求：落地需要多少人力物力？
5. ROI评估：投入产出比如何？"""
        }
    }

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self._llm = llm_client

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm

    def review_from_role(self, report_content: str, role: str) -> str:
        """从特定角色视角审查报告"""
        if role not in self.ROLES:
            raise ValueError(f"不支持的角色: {role}，可选: {list(self.ROLES.keys())}")

        role_info = self.ROLES[role]

        prompt = [
            {
                "role": "system",
                "content": f"""你是一名专业的{role_info['name']}，擅长从专业视角审查竞品分析报告。

【任务】
从{role_info['name']}的角度，对以下报告进行审查，提供有价值的反馈。

{role_info['prompt_addition']}

【输出要求】
1. 先给出总体评价（1-10分）
2. 列出3-5个具体问题或建议
3. 最后给出改进建议
4. 保持专业、客观
5. 控制在400字以内"""
            },
            {
                "role": "user",
                "content": f"""请审查以下报告：

{report_content[:4000]}

请从{role_info['name']}的角度给出审查意见。"""
            }
        ]

        return self.llm.chat_completion(prompt, temperature=0.2)

    def review_all_roles(self, report_content: str) -> Dict[str, Any]:
        """从所有角色视角审查报告"""
        results = {}

        for role_key in self.ROLES:
            try:
                results[role_key] = {
                    "role_name": self.ROLES[role_key]["name"],
                    "review": self.review_from_role(report_content, role_key),
                    "status": "completed"
                }
            except Exception as e:
                results[role_key] = {
                    "role_name": self.ROLES[role_key]["name"],
                    "review": f"审查失败: {str(e)}",
                    "status": "failed"
                }

        return results

    def generate_review_section(self, report_content: str, roles: Optional[List[str]] = None) -> str:
        """生成多角色审查章节"""
        roles = roles or list(self.ROLES.keys())

        section = "\n\n---\n\n## 附录：多角色审查意见\n\n"

        results = self.review_all_roles(report_content)

        for role_key in roles:
            if role_key in results:
                result = results[role_key]
                section += f"### {result['role_name']}\n\n"
                section += f"{result['review']}\n\n"

        section += "---\n"
        return section

    def extract_action_items(self, report_content: str) -> Dict[str, List[str]]:
        """从多角色视角提取可落地的行动项"""
        all_reviews = self.review_all_roles(report_content)

        action_items = {
            "developer": [],
            "tester": [],
            "operations": [],
            "common": []
        }

        for role_key, result in all_reviews.items():
            if result["status"] == "completed":
                items = self._parse_action_items(result["review"], role_key)
                action_items[role_key] = items
                action_items["common"].extend(items[:2])

        for key in action_items:
            action_items[key] = list(set(action_items[key]))[:5]

        return action_items

    def _parse_action_items(self, review_text: str, role: str) -> List[str]:
        """从审查文本中解析行动项"""
        items = []

        import re
        patterns = [
            r"(?:建议|需要|应该|可以)(.{10,50}?)[\n。]",
            r"(\d+\..{10,50}?)[\n。]",
            r"[-•\*]\s*(.{10,50}?)$"
        ]

        for pattern in patterns:
            matches = re.findall(pattern, review_text, re.MULTILINE)
            for match in matches:
                if 10 < len(match) < 100:
                    items.append(match.strip())

        return items[:5]


_multi_role_reviewer_instance = None

def get_multi_role_reviewer() -> MultiRoleReviewer:
    global _multi_role_reviewer_instance
    if _multi_role_reviewer_instance is None:
        _multi_role_reviewer_instance = MultiRoleReviewer()
    return _multi_role_reviewer_instance
