"""报告生成模块 - 生成结构化Markdown竞品分析报告"""
from typing import List, Dict, Optional
from datetime import datetime
from src.llm import get_llm_client, LLMClient


class ReportGenerator:
    """报告生成器 - 将调研数据转换为结构化Markdown报告"""
    
    def __init__(self, llm_client: LLMClient = None):
        self.llm_client = llm_client or get_llm_client()
    
    def generate_report(
        self,
        user_query: str,
        extracted_data: List[Dict],
        verification_score: Optional[float] = None,
        verification_warning: bool = False
    ) -> str:
        """
        生成结构化Markdown报告
        
        Args:
            user_query: 用户原始查询
            extracted_data: 提取的结构化数据列表
            verification_score: 可信度评分
            verification_warning: 是否有核查警告
            
        Returns:
            Markdown格式的报告
        """
        prompt = self._build_report_prompt(user_query, extracted_data)
        response = self.llm_client.chat_completion(prompt, temperature=0.2)
        
        # 添加可信度标注
        if verification_warning and verification_score:
            warning_note = f"""
> ⚠️ **可信度警告**：本次调研的综合可信度评分为 {verification_score}/10，部分信息可能存在不确定性，请谨慎参考。
"""
            response = warning_note + "\n" + response
        
        return response
    
    def _build_report_prompt(self, user_query: str, extracted_data: List[Dict]) -> List[Dict[str, str]]:
        """构建报告生成提示词"""
        data_str = "\n".join([f"- {item['key']}: {item['value']} (来源: {item['source_url']})" 
                           for item in extracted_data])
        
        return [
            {
                "role": "system",
                "content": """你是一名专业的市场调研分析师，擅长撰写结构化的竞品分析报告。
                
                报告结构要求：
                1. **调研概述**：简要说明调研目的和范围
                2. **核心维度对比**：使用表格呈现各竞品在关键维度的对比
                3. **调研结论**：总结分析结果，提出建议和潜在机会
                
                写作要求：
                - 语言简洁专业，符合职场报告规范
                - 数据来源要注明
                - 分析要客观中立，突出核心差异
                - 输出格式为标准Markdown
                """
            },
            {
                "role": "user",
                "content": f"""请根据以下调研数据，生成一份专业的竞品分析报告。
                
                调研主题：{user_query}
                
                调研数据：
                {data_str}
                
                请输出结构化Markdown报告。
                """
            }
        ]
    
    def save_report(self, content: str, filename: Optional[str] = None) -> str:
        """
        保存报告到文件
        
        Args:
            content: 报告内容
            filename: 文件名（可选，默认为时间戳命名）
            
        Returns:
            保存的文件路径
        """
        from src.config.settings import OUTPUT_DIR
        import os
        
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"research_report_{timestamp}.md"
        
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        
        return filepath