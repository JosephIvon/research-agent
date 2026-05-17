"""LLM客户端抽象层 - 统一接口支持多种大模型提供商"""
import re
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any, Union
from src.config.settings import (
    LLM_PROVIDER,
    MINIMAX_API_KEY,
    MINIMAX_BASE_URL,
    MINIMAX_MODEL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DOUBAO_API_KEY,
    DOUBAO_BASE_URL,
    DOUBAO_MODEL,
    GLM_API_KEY,
    GLM_BASE_URL,
    GLM_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    MAX_RETRIES
)


class LLMClient(ABC):
    """LLM客户端抽象接口"""
    
    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> Union[str, Dict[str, Any], None]:
        """对话补全"""
        pass
    
    @abstractmethod
    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096
    ) -> str:
        """流式对话补全"""
        pass
    
    @abstractmethod
    def extract_structured_data(
        self,
        content: str,
        schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """提取结构化数据"""
        pass


class OpenAICompatibleClient(LLMClient):
    """OpenAI兼容API客户端基类"""
    
    def __init__(self, api_key: str, base_url: str, default_model: str):
        from openai import OpenAI
        self.api_key = api_key
        self.base_url = self._normalize_base_url(base_url)
        self.default_model = default_model
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.max_retries = MAX_RETRIES
    
    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """统一Base URL格式，已含 /vN 后缀的不再追加 /v1"""
        normalized = base_url.rstrip("/")
        if re.search(r"/v\d+$", normalized):
            return normalized
        return f"{normalized}/v1"
    
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto"
    ) -> Union[str, Dict[str, Any], None]:
        retry_delay = 1
        
        for attempt in range(self.max_retries):
            try:
                params = {
                    "model": model or self.default_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
                
                if tools:
                    params["tools"] = tools
                    params["tool_choice"] = tool_choice
                
                response = self.client.chat.completions.create(**params)
                
                if not response.choices:
                    return None
                message = response.choices[0].message
                
                if message.tool_calls is not None and len(message.tool_calls) > 0:
                    tool_call = message.tool_calls[0]
                    return {
                        "type": "tool_call",
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments
                    }
                
                if message.content is not None:
                    return message.content
                
                return None
                
            except Exception as e:
                if attempt < self.max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                raise e
    
    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096
    ) -> str:
        retry_delay = 1
        
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=model or self.default_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True
                )
                
                full_content = ""
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        full_content += delta.content
                
                return full_content
                
            except Exception as e:
                if attempt < self.max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                raise e
    
    def extract_structured_data(
        self,
        content: str,
        schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": f"""你是一个数据提取专家。请根据提供的schema从文本中提取结构化数据。
                输出格式必须是有效的JSON，严格按照schema定义的字段输出。
                schema: {schema}"""
            },
            {
                "role": "user",
                "content": content
            }
        ]
        
        response = self.chat_completion(messages, temperature=0.1)
        return response


class MinimaxClient(OpenAICompatibleClient):
    """Minimax LLM客户端"""
    def __init__(self):
        if not MINIMAX_API_KEY:
            raise ValueError("Missing required environment variable: MINIMAX_API_KEY")
        super().__init__(MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL)


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek LLM客户端"""
    def __init__(self):
        if not DEEPSEEK_API_KEY:
            raise ValueError("Missing required environment variable: DEEPSEEK_API_KEY")
        super().__init__(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL)


class DoubaoClient(OpenAICompatibleClient):
    """豆包 LLM客户端"""
    def __init__(self):
        if not DOUBAO_API_KEY:
            raise ValueError("Missing required environment variable: DOUBAO_API_KEY")
        super().__init__(DOUBAO_API_KEY, DOUBAO_BASE_URL, DOUBAO_MODEL)


class GLMClient(OpenAICompatibleClient):
    """GLM (智谱) LLM客户端"""
    def __init__(self):
        if not GLM_API_KEY:
            raise ValueError("Missing required environment variable: GLM_API_KEY")
        super().__init__(GLM_API_KEY, GLM_BASE_URL, GLM_MODEL)


class OpenAIClient(OpenAICompatibleClient):
    """OpenAI LLM客户端"""
    def __init__(self):
        if not OPENAI_API_KEY:
            raise ValueError("Missing required environment variable: OPENAI_API_KEY")
        super().__init__(OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL)


# LLM提供商映射
_PROVIDERS = {
    "minimax": MinimaxClient,
    "deepseek": DeepSeekClient,
    "doubao": DoubaoClient,
    "glm": GLMClient,
    "openai": OpenAIClient,
}


def get_llm_client(provider: Optional[str] = None) -> LLMClient:
    """
    获取LLM客户端实例
    
    Args:
        provider: 提供商名称，可选值: minimax, deepseek, doubao, glm, openai
                  若不指定，使用环境变量 LLM_PROVIDER
    
    Returns:
        LLM客户端实例
    
    Raises:
        ValueError: 未知的提供商或缺少必要配置
    """
    selected_provider = provider or LLM_PROVIDER
    
    client_class = _PROVIDERS.get(selected_provider.lower())
    if not client_class:
        raise ValueError(f"Unknown LLM provider: {selected_provider}. "
                       f"Available providers: {list(_PROVIDERS.keys())}")
    
    return client_class()


def list_available_providers() -> List[str]:
    """获取可用的LLM提供商列表"""
    return list(_PROVIDERS.keys())