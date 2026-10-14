"""LLM客户端抽象层 - 统一接口支持多种大模型提供商"""
import asyncio
import json
import os
import random
import time
import logging
import re
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any, Union, Type
from urllib.error import URLError, HTTPError
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
    MAX_RETRIES,
    LLM_CONNECT_TIMEOUT,
    LLM_READ_TIMEOUT,
    LLM_RETRY_BACKOFF_MAX,
    LLM_CIRCUIT_BREAKER_THRESHOLD,
    LLM_CIRCUIT_BREAKER_TIMEOUT,
    LLM_FALLBACK_PROVIDER,
)
from src.llm.circuit_breaker import (
    get_circuit_breaker,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
)

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用基础异常"""
    pass


class LLMTimeoutError(LLMError):
    """LLM 调用超时"""
    pass


class LLMRetryableError(LLMError):
    """可重试的 LLM 异常"""
    pass


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

    async def achat_completion(self, messages: List[Dict[str, str]], **kwargs):
        """异步调用包装，供 async 代码路径使用。

        chat_completion 是同步实现：内部既做阻塞式网络 I/O，也在重试退避时
        `time.sleep`（单次最长 LLM_RETRY_BACKOFF_MAX，乘以重试次数可达分钟级）。
        直接在 async 代码里调用会冻结整个事件循环——单 worker 部署下，SSE 心跳
        和其他并发请求会一起停摆。

        这里统一丢进线程池执行。熔断器用的是 threading.Lock，保护的是跨线程共享
        的状态机，在多线程下正是需要的。
        """
        return await asyncio.to_thread(self.chat_completion, messages, **kwargs)
    
    @abstractmethod
    def extract_structured_data(
        self,
        content: str,
        schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """提取结构化数据"""
        pass


class OpenAICompatibleClient(LLMClient):
    """OpenAI兼容API客户端基类 - 带超时、熔断和智能重试"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str,
        provider_name: str = "unknown"
    ):
        from openai import OpenAI
        from openai import APIError, RateLimitError, Timeout as OpenAI_Timeout

        self.api_key = api_key
        self.base_url = self._normalize_base_url(base_url)
        self.default_model = default_model
        self.provider_name = provider_name
        self.max_retries = MAX_RETRIES

        # 配置超时
        self._timeout = LLM_READ_TIMEOUT

        # 初始化 OpenAI 客户端（不设置默认超时，让我们在调用时控制）
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # 初始化熔断器
        self._circuit_breaker = get_circuit_breaker(
            name=provider_name,
            config=CircuitBreakerConfig(
                failure_threshold=LLM_CIRCUIT_BREAKER_THRESHOLD,
                recovery_timeout=LLM_CIRCUIT_BREAKER_TIMEOUT,
            )
        )

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """统一Base URL格式，已含 /vN 后缀的不再追加 /v1"""
        normalized = base_url.rstrip("/")
        if re.search(r"/v\d+$", normalized):
            return normalized
        return f"{normalized}/v1"

    def _is_retryable_error(self, error: Exception) -> bool:
        """判断错误是否可重试"""
        from openai import APIError, RateLimitError, Timeout as OpenAI_Timeout
        from httpx import ConnectError, ReadTimeout, ConnectTimeout

        error_type = type(error)

        # 超时错误 - 可重试
        if isinstance(error, (LLMTimeoutError, OpenAI_Timeout, ReadTimeout, ConnectTimeout)):
            logger.warning(f"[{self.provider_name}] Timeout error, will retry")
            return True

        # 速率限制 - 可重试
        if isinstance(error, (RateLimitError, APIError)):
            if hasattr(error, 'status_code') and error.status_code == 429:
                retry_after = getattr(error, 'retry_after', 1)
                logger.warning(f"[{self.provider_name}] Rate limited, retry after {retry_after}s")
                return True

        # 服务器错误 - 可重试 (5xx)
        if isinstance(error, APIError):
            status = getattr(error, 'status_code', 0)
            if 500 <= status < 600:
                logger.warning(f"[{self.provider_name}] Server error {status}, will retry")
                return True

        # 网络错误 - 可重试
        if isinstance(error, (URLError, ConnectError, ConnectionError)):
            logger.warning(f"[{self.provider_name}] Connection error, will retry")
            return True

        # 其他错误 - 不可重试
        logger.error(f"[{self.provider_name}] Non-retryable error: {type(error).__name__}: {error}")
        return False

    def _calculate_backoff(self, attempt: int, base_delay: float = 1.0) -> float:
        """计算带抖动的指数退避时间"""
        # 指数退避: 1, 2, 4, 8, 16... 秒
        delay = min(base_delay * (2 ** attempt), LLM_RETRY_BACKOFF_MAX)
        # 添加 ±25% 的随机抖动
        jitter = delay * 0.25 * (2 * random.random() - 1)
        return delay + jitter

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        timeout: Optional[int] = None
    ) -> Union[str, Dict[str, Any], None]:
        """
        对话补全 - 带超时控制、熔断和智能重试

        Args:
            messages: 对话消息列表
            model: 模型名称（可选）
            temperature: 温度参数
            max_tokens: 最大 token 数
            tools: 工具定义
            tool_choice: 工具选择策略
            timeout: 请求超时（秒），默认使用配置值

        Returns:
            对话补全结果

        Raises:
            LLMTimeoutError: 请求超时
            LLMRetryableError: 可重试错误且重试次数耗尽
            CircuitBreakerError: 熔断器开启
            LLMError: 其他错误
        """
        # 检查熔断器
        if not self._circuit_breaker.is_available:
            raise CircuitBreakerError(
                self.provider_name,
                self._circuit_breaker.state,
                f"Circuit breaker is {self._circuit_breaker.state.value}"
            )

        request_timeout = timeout or self._timeout

        def _do_request():
            last_error = None

            for attempt in range(self.max_retries):
                try:
                    params = {
                        "model": model or self.default_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "timeout": request_timeout,
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
                    last_error = e
                    if not self._is_retryable_error(e):
                        raise

                    if attempt < self.max_retries - 1:
                        backoff = self._calculate_backoff(attempt)
                        logger.info(
                            f"[{self.provider_name}] Retry {attempt + 1}/{self.max_retries} "
                            f"after {backoff:.2f}s: {type(e).__name__}"
                        )
                        time.sleep(backoff)
                        continue

            # 所有重试都失败
            raise LLMRetryableError(
                f"[{self.provider_name}] All {self.max_retries} retries failed: {last_error}"
            )

        # 通过熔断器执行
        try:
            result = self._circuit_breaker.call(_do_request)
            return result
        except CircuitBreakerError:
            # 熔断器拒绝，让调用者知道需要降级
            raise
        except LLMRetryableError:
            raise
        except Exception as e:
            # 记录最终错误
            logger.error(f"[{self.provider_name}] chat_completion failed: {e}")
            raise LLMError(f"[{self.provider_name}] chat_completion failed: {e}") from e

    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: Optional[int] = None
    ) -> str:
        """
        流式对话补全 - 带超时控制

        Note: 流式调用暂不支持熔断（因为 SSE 需要保持连接）
        """
        request_timeout = timeout or self._timeout

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=model or self.default_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    timeout=request_timeout
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
                last_error = e
                if not self._is_retryable_error(e):
                    raise LLMError(f"[{self.provider_name}] stream_completion failed: {e}") from e

                if attempt < self.max_retries - 1:
                    backoff = self._calculate_backoff(attempt)
                    logger.info(
                        f"[{self.provider_name}] Stream retry {attempt + 1}/{self.max_retries} "
                        f"after {backoff:.2f}s"
                    )
                    time.sleep(backoff)
                    continue

        raise LLMRetryableError(
            f"[{self.provider_name}] All stream retries failed: {last_error}"
        )

    def extract_structured_data(
        self,
        content: str,
        schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """提取结构化数据 - 复用 chat_completion"""
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

    def get_circuit_status(self) -> dict:
        """获取熔断器状态"""
        return self._circuit_breaker.get_status()

    def reset_circuit(self) -> None:
        """重置熔断器"""
        self._circuit_breaker.reset()


class MinimaxClient(OpenAICompatibleClient):
    """Minimax LLM客户端"""
    def __init__(self):
        if not MINIMAX_API_KEY:
            raise ValueError("Missing required environment variable: MINIMAX_API_KEY")
        super().__init__(MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL, provider_name="minimax")


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek LLM客户端"""
    def __init__(self):
        if not DEEPSEEK_API_KEY:
            raise ValueError("Missing required environment variable: DEEPSEEK_API_KEY")
        super().__init__(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, provider_name="deepseek")


class DoubaoClient(OpenAICompatibleClient):
    """豆包 LLM客户端"""
    def __init__(self):
        if not DOUBAO_API_KEY:
            raise ValueError("Missing required environment variable: DOUBAO_API_KEY")
        super().__init__(DOUBAO_API_KEY, DOUBAO_BASE_URL, DOUBAO_MODEL, provider_name="doubao")


class GLMClient(OpenAICompatibleClient):
    """GLM (智谱) LLM客户端"""
    def __init__(self):
        if not GLM_API_KEY:
            raise ValueError("Missing required environment variable: GLM_API_KEY")
        super().__init__(GLM_API_KEY, GLM_BASE_URL, GLM_MODEL, provider_name="glm")


class OpenAIClient(OpenAICompatibleClient):
    """OpenAI LLM客户端"""
    def __init__(self):
        if not OPENAI_API_KEY:
            raise ValueError("Missing required environment variable: OPENAI_API_KEY")
        super().__init__(OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, provider_name="openai")


# ============================================================
# ScriptedLLMClient：确定性 Fake Provider（评测与测试地基）
# ============================================================
# 使命：让完整工作流在 CI 里零网络、零密钥、逐字确定地跑起来，并为
# M3 工具循环与 M4 评测集提供可精确控制的"模型第 N 步返回什么"。
#
# 意图识别：优先匹配 system 提示词里的 <!--intent:xxx--> 标记（新代码用），
# 未标记时按 legacy 提示词的特征句兜底——历史提示词无需改动即可被脚本化。

INTENT_MARKER_RE = re.compile(r"<!--intent:([a-z0-9_]+)-->")

# legacy 提示词特征句 → 意图（顺序即优先级，首中即停）
LEGACY_INTENT_HINTS = (
    ("decompose", "任务分解专家"),
    ("extract", "信息提取专家"),
    ("report", "市场调研分析师"),
    ("prd", "资深产品经理"),
)

# 内置缺省脚本：响应都是可被现有解析路径消费的合法文本/JSON，
# 保证"没配脚本也能跑通不炸"。要让报告有意义（如带 [e:id] 引用），
# 用 LLM_FAKE_SCRIPT 文件或构造参数覆盖对应意图。
DEFAULT_FAKE_SCRIPT = {
    "decompose": ['[{"id": "1", "description": "分析目标网站内容", "priority": 1}]'],
    "extract": ['[{"key": "content", "value": "脚本化抽取占位内容", "confidence": 0.9}]'],
    "report": ["（脚本化报告占位。通过 LLM_FAKE_SCRIPT 或 ScriptedLLMClient(script=...) 提供正文。）"],
    "prd": ["（脚本化 PRD 占位。）"],
}


def detect_llm_intent(messages: List[Dict[str, str]]) -> Optional[str]:
    """从消息序列识别意图：显式标记优先，其次 legacy 特征句。"""
    for message in messages or []:
        if message.get("role") != "system":
            continue
        content = message.get("content") or ""
        marker = INTENT_MARKER_RE.search(content)
        if marker:
            return marker.group(1)
        for intent, hint in LEGACY_INTENT_HINTS:
            if hint in content:
                return intent
    return None


class ScriptedLLMClient(LLMClient):
    """按意图返回脚本化响应的确定性 LLM 客户端。

    - 脚本结构 `{intent: [响应, ...]}`，响应为 str（终答）或
      `{"type": "tool_call", "name": ..., "arguments": ...}`（供工具循环）。
      同一意图调用次数超出脚本长度时**重复最后一条**——extract 逐页调用、
      页数不定，尾重复是唯一确定语义。
    - `calls` 记录每次调用的 intent/step/messages/tools，是 prompt 快照测试的锚点。
    - 脚本来源优先级：构造参数 > `LLM_FAKE_SCRIPT`（JSON 文件，按键覆盖）> 内置缺省。
    - 与真实客户端同样遵守返回约定：str 或首个 tool_call 的 dict。
    """

    provider_name = "fake"

    def __init__(self, script: Optional[Dict[str, List[Any]]] = None):
        self.default_model = "scripted"
        self.api_key = "not-needed"
        self.base_url = None
        merged: Dict[str, List[Any]] = {k: list(v) for k, v in DEFAULT_FAKE_SCRIPT.items()}
        env_path = os.getenv("LLM_FAKE_SCRIPT")
        if env_path:
            merged.update(self._load_script_file(env_path))
        if script:
            merged.update({k: list(v) for k, v in script.items()})
        self._validate(merged)
        self._script = merged
        self.calls: List[Dict[str, Any]] = []

    @staticmethod
    def _load_script_file(path: str) -> Dict[str, List[Any]]:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"LLM_FAKE_SCRIPT 必须是 {{intent: [响应]}} 的 JSON 对象: {path}")
        return data

    @staticmethod
    def _validate(script: Dict[str, List[Any]]) -> None:
        """构造期就拒绝非法条目——让坏脚本在测试启动时炸，而不是跑到一半。"""
        for intent, entries in script.items():
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"fake 脚本意图 {intent!r} 必须是非空列表")
            for entry in entries:
                if isinstance(entry, str):
                    continue
                if isinstance(entry, dict) and entry.get("type") == "tool_call":
                    continue
                raise ValueError(
                    f"fake 脚本意图 {intent!r} 的条目必须是 str 或 "
                    f"{{'type': 'tool_call', ...}}，实际: {entry!r}"
                )

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        timeout=None,
    ) -> Union[str, Dict[str, Any], None]:
        intent = detect_llm_intent(messages)
        if intent is None or intent not in self._script:
            available = ", ".join(sorted(self._script))
            raise ValueError(
                f"[fake] 无法识别意图 {intent!r}（脚本可用意图: {available}）。"
                "请在 system 提示词加 <!--intent:名称-->，或在脚本中补该意图。"
            )
        step = sum(1 for call in self.calls if call["intent"] == intent)
        entry = self._script[intent][min(step, len(self._script[intent]) - 1)]
        self.calls.append(
            {"intent": intent, "step": step, "messages": messages, "tools": tools}
        )
        return entry

    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        # 脚本没有流式语义：直接返回全量文本
        result = self.chat_completion(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    def extract_structured_data(self, content: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        if "extract_structured" in self._script:
            result = self.chat_completion(
                [
                    {"role": "system", "content": "<!--intent:extract_structured-->"},
                    {"role": "user", "content": content},
                ]
            )
            if isinstance(result, dict):
                return result
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"raw": result}
        return {"raw": content[:500], "schema_keys": list(schema.get("properties", {}))}


# LLM提供商映射
_PROVIDERS = {
    "minimax": MinimaxClient,
    "deepseek": DeepSeekClient,
    "doubao": DoubaoClient,
    "glm": GLMClient,
    "openai": OpenAIClient,
    "fake": ScriptedLLMClient,
}


def get_llm_client(provider: Optional[str] = None) -> LLMClient:
    """
    获取LLM客户端实例

    如果主提供商的熔断器开启，会自动尝试降级到备用提供商。

    Args:
        provider: 提供商名称，可选值: minimax, deepseek, doubao, glm, openai
                  若不指定，使用环境变量 LLM_PROVIDER

    Returns:
        LLM客户端实例

    Raises:
        ValueError: 未知的提供商或缺少必要配置，且无备用提供商可用
    """
    selected_provider = provider or LLM_PROVIDER

    # 检查主提供商的熔断器状态
    primary_breaker = get_circuit_breaker(selected_provider.lower())

    if not primary_breaker.is_available and LLM_FALLBACK_PROVIDER:
        logger.warning(
            f"Primary provider {selected_provider} circuit breaker is OPEN. "
            f"Falling back to {LLM_FALLBACK_PROVIDER}"
        )
        selected_provider = LLM_FALLBACK_PROVIDER
    elif not primary_breaker.is_available:
        logger.warning(
            f"Primary provider {selected_provider} circuit breaker is OPEN. "
            f"No fallback provider configured."
        )

    client_class = _PROVIDERS.get(selected_provider.lower())
    if not client_class:
        raise ValueError(f"Unknown LLM provider: {selected_provider}. "
                       f"Available providers: {list(_PROVIDERS.keys())}")

    return client_class()


def get_llm_client_with_fallback(
    primary: Optional[str] = None,
    fallback: Optional[str] = None
) -> tuple[LLMClient, Optional[LLMClient]]:
    """
    获取主 LLM 客户端和备用客户端

    Args:
        primary: 主提供商名称
        fallback: 备用提供商名称（默认使用 LLM_FALLBACK_PROVIDER）

    Returns:
        tuple: (主客户端, 备用客户端或None)
    """
    fallback_provider = fallback or LLM_FALLBACK_PROVIDER

    primary_client = get_llm_client(primary or LLM_PROVIDER)
    fallback_client = None

    if fallback_provider and fallback_provider != (primary or LLM_PROVIDER).lower():
        try:
            fallback_class = _PROVIDERS.get(fallback_provider.lower())
            if fallback_class:
                fallback_client = fallback_class()
        except ValueError:
            logger.warning(f"Fallback provider {fallback_provider} is not configured")
            fallback_client = None

    return primary_client, fallback_client


def list_available_providers() -> List[str]:
    """获取可用的LLM提供商列表"""
    return list(_PROVIDERS.keys())


def get_provider_status() -> List[dict]:
    """获取所有提供商的熔断器状态"""
    providers = list(_PROVIDERS.keys())
    status = []
    for provider in providers:
        breaker = get_circuit_breaker(provider)
        status.append({
            "provider": provider,
            **breaker.get_status()
        })
    return status