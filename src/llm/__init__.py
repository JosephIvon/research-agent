"""LLM客户端抽象层 - 统一接口支持多种大模型提供商"""
import asyncio
import json
import os
import random
import time
import logging
import re
from abc import ABC, abstractmethod
from typing import Callable, List, Dict, Optional, Any, Union, Type
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

# 推理模型的思考块。MiniMax M3 等模型把思考过程内联在 content 里返回：
#    <think>用户问的是定价，先找价格。</think>## 结论\n定价 49 美元
#
# 不剥离有两个后果：(1) 下游 json.loads 必然失败，结构化抽取静默降级；
# (2) 思考内容被当成正文交付，并进入引用校验当成模型的事实主张。
# 实测一次真实冒烟里思考占了输出的 66.6%（5894 字中 3927 字）。
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def strip_thinking(content: Optional[str]) -> str:
    """剥离 `<think>…</think>` 推理块，返回纯正文。空/None 输入返回 ""。

    未闭合的块按「截断」处理：`<think>` 之后全是推理，模型没来得及给答案，
    此时返回空串——宁可让调用方知道没内容，也不要把思考当正文交出去。

    已知取舍：正文里**字面讨论** `<think>` 标签（如讲解推理模型的文章）会被截断。
    真实模型不会在正文里这么写，而误判的代价远小于泄露推理。
    """
    if not content:
        return ""
    text = _THINK_BLOCK_RE.sub("", content)
    # 完整块移除后仍存在的 <think> 只可能是没闭合的（输出被 max_tokens 截断）
    text = _THINK_OPEN_RE.split(text)[0]
    text = _THINK_TAG_RE.sub("", text)
    return text.strip()


# --------------------------------------------------------------------------
# usage 真值：抽取与投递
#
# 背景：provider 返回的 prompt/completion tokens 是唯一真值，字符估算只在
# usage 缺失时兜底。但历史上只有 chat_completion_raw 读得到 usage——
# chat_completion（只回正文或首个 tool_call）与 stream_completion（回纯字符串）
# 都把它整个丢掉，于是除 ReAct 外的所有调用路径永远只有估算。
#
# 补法不改这两个方法的返回类型（它们被全仓按 str | dict 使用），改为**可选**
# usage_sink 回调：默认 None 时连 response.usage 都不碰，与改造前逐字一致。
# --------------------------------------------------------------------------
UsageSink = Callable[[Dict[str, Optional[int]]], None]


def _extract_usage(source: Any) -> Dict[str, Optional[int]]:
    """从响应对象或流式 chunk 抽 usage 真值。字段缺失一律 None，交调用方估算。"""
    usage = source.get("usage") if isinstance(source, dict) else getattr(source, "usage", None)
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None}

    def _int(value: Any) -> Optional[int]:
        # bool 是 int 的子类：True 不能被当成 1 个 token 记进去
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if isinstance(usage, dict):
        return {
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
        }
    return {
        "prompt_tokens": _int(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": _int(getattr(usage, "completion_tokens", None)),
    }


def _notify_usage(sink: Optional[UsageSink], usage: Dict[str, Optional[int]]) -> None:
    """投递 usage 真值。sink 自身的异常不许打断 LLM 调用——记账失败最多丢一笔账，
    抛出去会让已经付过费的这次调用整条链路失败。"""
    if sink is None:
        return
    try:
        sink(usage)
    except Exception:
        logger.debug("[llm] usage_sink 回调失败（不影响主流程）", exc_info=True)


def _stream_usage_default() -> bool:
    """流式 usage 的默认开关。

    默认**关闭**：并非所有 OpenAI 兼容后端都认 stream_options（部分自建网关、
    老版本 vLLM 会直接 400），默认打开等于把所有流式调用打成故障。确认后端
    支持后置 LLM_STREAM_INCLUDE_USAGE=true，或按调用点传 include_usage=True
    局部打开。
    """
    return os.getenv("LLM_STREAM_INCLUDE_USAGE", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


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
        tool_choice: str = "auto",
        usage_sink: Optional[UsageSink] = None,
    ) -> Union[str, Dict[str, Any], None]:
        """对话补全

        usage_sink：可选回调，命中时收到 provider 返回的 usage 真值
        （{"prompt_tokens": int|None, "completion_tokens": int|None}）。
        默认 None 时不读 usage、不回调，与加此参数前的行为逐字一致。
        """
        pass
    
    @abstractmethod
    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        usage_sink: Optional[UsageSink] = None,
        include_usage: Optional[bool] = None,
    ) -> str:
        """流式对话补全

        usage_sink/include_usage 语义同 chat_completion 与 OpenAICompatibleClient。
        """
        pass

    async def achat_completion(self, messages: List[Dict[str, str]], **kwargs):
        """异步调用包装，供 async 代码路径使用。

        chat_completion 是同步实现：内部既做阻塞式网络 I/O，也在重试退避时
        `time.sleep`（单次最长 LLM_RETRY_BACKOFF_MAX，乘以重试次数可达分钟级）。
        直接在 async 代码里调用会冻结整个事件循环——单 worker 部署下，SSE 心跳
        和其他并发请求会一起停摆。

        这里统一丢进线程池执行。熔断器用的是 threading.Lock，保护的是跨线程共享
        的状态机，在多线程下正是需要的。

        kwargs 原样透传给 chat_completion，`usage_sink=...` 照常可用。
        """
        return await asyncio.to_thread(self.chat_completion, messages, **kwargs)

    def chat_completion_raw(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """完整结构化返回：{content, tool_calls(全部,含 id), finish_reason, usage}。

        chat_completion 只回正文或**第一个** tool_call，拿不到 tool_call_id，
        无法回填合法的 tool 消息——ReAct 循环（src/agent/runner.py）必须用本方法。
        默认实现包装 chat_completion（无真实 usage，token 由调用方估算）；
        OpenAI 兼容客户端覆写为带 usage 的完整解析。
        """
        result = self.chat_completion(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools, tool_choice=tool_choice,
        )
        if isinstance(result, dict) and result.get("type") == "tool_call":
            call = dict(result)
            call.setdefault("id", "call_0")
            return {
                "content": None,
                "tool_calls": [call],
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": None, "completion_tokens": None},
            }
        return {
            "content": result if isinstance(result, str) else None,
            "tool_calls": [],
            "finish_reason": "stop" if result else None,
            "usage": {"prompt_tokens": None, "completion_tokens": None},
        }

    async def achat_completion_raw(self, messages: List[Dict[str, str]], **kwargs):
        """chat_completion_raw 的线程池异步包装（同 achat_completion 的理由）。"""
        return await asyncio.to_thread(self.chat_completion_raw, messages, **kwargs)
    
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
        timeout: Optional[int] = None,
        usage_sink: Optional[UsageSink] = None,
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
            usage_sink: 可选 usage 回调。**不传则完全不读 response.usage**
                （零开销，行为与加此参数前逐字一致）；传了则在拿到响应的
                那一刻回调一次，usage 由 TaskBudget.record / record_estimated
                之类的记账函数接管——provider 没给就是 None 值，由它决定是否估算。

        Returns:
            对话补全结果（正文，或首个 tool_call 的 dict）

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

                    # usage 只在有人要账时才读：回调放在 choices 判断之前，
                    # 因为"有响应但无 choices"同样计费，漏记就是真金白银的缺口。
                    if usage_sink is not None:
                        _notify_usage(usage_sink, _extract_usage(response))

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
                        # 推理块在客户端边界剥离：下游拿到的永远是纯正文。
                        # 全被剥掉（输出只有思考）时返回 None，让调用方走自己的
                        # 空响应分支，而不是把空串当成有效内容。
                        cleaned = strip_thinking(message.content)
                        return cleaned or None

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

    def chat_completion_raw(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        timeout=None,
    ) -> Dict[str, Any]:
        """完整返回：正文 + **全部** tool_calls（含 id）+ 真实 usage。

        与 chat_completion 共享重试/熔断/退避策略，但解析层保留完整
        assistant message——降维成"首个 tool_call"会丢 tool_call_id 与
        单轮多调用，ReAct 协议回填 tool 消息时两者都必须在。
        """
        if not self._circuit_breaker.is_available:
            raise CircuitBreakerError(
                f"Circuit breaker is {self._circuit_breaker.state.value}"
            )

        request_timeout = timeout or self._timeout

        def _do_request_raw():
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

                    def _usage():
                        return _extract_usage(response)

                    if not response.choices:
                        return {"content": None, "tool_calls": [], "finish_reason": None,
                                "usage": _usage()}

                    message = response.choices[0].message
                    tool_calls = [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in (message.tool_calls or [])
                    ]
                    return {
                        "content": strip_thinking(message.content) or None,
                        "tool_calls": tool_calls,
                        "finish_reason": getattr(response.choices[0], "finish_reason", None),
                        "usage": _usage(),
                    }
                except Exception as e:
                    last_error = e
                    if not self._is_retryable_error(e):
                        raise
                    if attempt < self.max_retries - 1:
                        backoff = self._calculate_backoff(attempt)
                        logger.info(
                            f"[{self.provider_name}] raw retry {attempt + 1}/{self.max_retries} "
                            f"after {backoff:.2f}s: {type(e).__name__}"
                        )
                        time.sleep(backoff)
                        continue
            raise LLMRetryableError(
                f"[{self.provider_name}] All {self.max_retries} retries failed: {last_error}"
            )

        try:
            return self._circuit_breaker.call(_do_request_raw)
        except CircuitBreakerError:
            raise
        except LLMRetryableError:
            raise
        except Exception as e:
            logger.error(f"[{self.provider_name}] chat_completion_raw failed: {e}")
            raise LLMError(f"[{self.provider_name}] chat_completion_raw failed: {e}") from e

    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: Optional[int] = None,
        usage_sink: Optional[UsageSink] = None,
        include_usage: Optional[bool] = None,
    ) -> str:
        """
        流式对话补全 - 带超时控制

        usage 真值只在流末尾的最后一批 chunk 里（choices 为空的收尾块），
        所以必须显式要：`stream_options={"include_usage": True}`。
        该参数并非所有 OpenAI 兼容后端都认（部分自建网关直接 400），因此做成
        双层开关且**默认关闭**：
        - include_usage 为 None → 读环境变量 LLM_STREAM_INCLUDE_USAGE（默认 false）
        - 显式传 True/False → 覆盖环境变量，供单点强制打开/关闭
        并且只有真的传了 usage_sink 才会带上这个参数——没人在账上就不发。

        Note: 流式调用暂不支持熔断（因为 SSE 需要保持连接）
        """
        request_timeout = timeout or self._timeout

        want_usage = usage_sink is not None and (
            _stream_usage_default() if include_usage is None else bool(include_usage)
        )

        last_error = None
        for attempt in range(self.max_retries):
            try:
                params = {
                    "model": model or self.default_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "timeout": request_timeout,
                }
                if want_usage:
                    params["stream_options"] = {"include_usage": True}
                response = self.client.chat.completions.create(**params)

                full_content = ""
                usage: Optional[Dict[str, Optional[int]]] = None
                for chunk in response:
                    # 收尾块的 choices 为空、只带 usage——必须先取 usage 再判 choices，
                    # 否则这段真值会被 `continue` 跳过，流式路径就永远只有估算。
                    if want_usage:
                        chunk_usage = _extract_usage(chunk)
                        if chunk_usage["prompt_tokens"] is not None or \
                                chunk_usage["completion_tokens"] is not None:
                            usage = chunk_usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        full_content += delta.content

                # 推理块跨多个 delta 到达，只能在拼接完之后整体剥离
                text = strip_thinking(full_content)
                # 后端不支持或未回传 usage 时回 None 值，让调用方照旧走估算
                _notify_usage(usage_sink, usage or _extract_usage(None))
                return text

            except Exception as e:
                last_error = e
                if not self._is_retryable_error(e):
                    if want_usage:
                        logger.warning(
                            f"[{self.provider_name}] 流式请求带了 stream_options 却失败，"
                            f"疑似后端不支持：该调用点传 include_usage=False 可绕过。"
                            f"{type(e).__name__}: {e}"
                        )
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
    - `usage` 构造参数（可选）：脚本客户端默认**没有** usage 真值（回 None 值，
      调用方据此走估算并打 estimated 标记）。传入后 chat_completion 的
      usage_sink 与 chat_completion_raw 的 usage 都按它上报——接线测试可以
      在零网络下验证"拿到真值就不估算"这条分支。
    """

    provider_name = "fake"

    def __init__(self, script: Optional[Dict[str, List[Any]]] = None,
                 usage: Optional[Dict[str, int]] = None):
        self.default_model = "scripted"
        self.api_key = "not-needed"
        self.base_url = None
        self.usage = dict(usage) if usage else None
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
        def _check_call(entry: Any, intent: str) -> None:
            if not (isinstance(entry, dict) and entry.get("type") == "tool_call" and entry.get("name")):
                raise ValueError(
                    f"fake 脚本意图 {intent!r} 的 tool_call 必须含 type='tool_call' 与 name，"
                    f"实际: {entry!r}"
                )

        for intent, entries in script.items():
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"fake 脚本意图 {intent!r} 必须是非空列表")
            for entry in entries:
                if isinstance(entry, str):
                    continue
                if isinstance(entry, dict) and entry.get("type") == "tool_call":
                    _check_call(entry, intent)
                    continue
                if isinstance(entry, dict) and entry.get("type") == "tool_calls":
                    calls = entry.get("calls")
                    if not isinstance(calls, list) or not calls:
                        raise ValueError(
                            f"fake 脚本意图 {intent!r} 的 tool_calls 条目需要非空 calls 列表"
                        )
                    for call in calls:
                        _check_call(call, intent)
                    continue
                raise ValueError(
                    f"fake 脚本意图 {intent!r} 的条目必须是 str、"
                    f"{{'type': 'tool_call', ...}} 或 {{'type': 'tool_calls', 'calls': [...]}}，"
                    f"实际: {entry!r}"
                )

    def _next_entry(self, messages, tools):
        """取当前意图的下一个脚本条目并记录调用。chat_completion 与 raw 共用。"""
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

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        timeout=None,
        usage_sink: Optional[UsageSink] = None,
    ) -> Union[str, Dict[str, Any], None]:
        entry = self._next_entry(messages, tools)
        _notify_usage(usage_sink, self._usage_value())
        # legacy 单返回路径降维取首个（raw 路径保留全部）
        if isinstance(entry, dict) and entry.get("type") == "tool_calls":
            return entry["calls"][0]
        return entry

    def _usage_value(self) -> Dict[str, Optional[int]]:
        """脚本客户端的 usage：没配就是 None 值（调用方据此决定是否估算）。"""
        if not self.usage:
            return {"prompt_tokens": None, "completion_tokens": None}
        return {
            "prompt_tokens": self.usage.get("prompt_tokens"),
            "completion_tokens": self.usage.get("completion_tokens"),
        }

    def chat_completion_raw(self, messages, model=None, temperature=0.2, max_tokens=4096,
                            tools=None, tool_choice="auto", timeout=None) -> Dict[str, Any]:
        """脚本版 raw：tool_call 自动补 id（call_<序号>），多调用条目原样展开。"""
        entry = self._next_entry(messages, tools)
        usage = self._usage_value()
        if isinstance(entry, dict) and entry.get("type") == "tool_call":
            call = dict(entry)
            call.setdefault("id", f"call_{len(self.calls)}")
            return {"content": None, "tool_calls": [call], "finish_reason": "tool_calls",
                    "usage": usage}
        if isinstance(entry, dict) and entry.get("type") == "tool_calls":
            calls = []
            for i, call in enumerate(entry["calls"]):
                call = dict(call)
                call.setdefault("id", f"call_{len(self.calls)}_{i}")
                calls.append(call)
            return {"content": None, "tool_calls": calls, "finish_reason": "tool_calls",
                    "usage": usage}
        return {"content": entry if isinstance(entry, str) else None, "tool_calls": [],
                "finish_reason": "stop" if entry else None, "usage": usage}

    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        usage_sink: Optional[UsageSink] = None,
        include_usage: Optional[bool] = None,
    ) -> str:
        # 脚本没有流式语义：直接返回全量文本
        # （include_usage 无意义——不发网络请求，没有 stream_options 可塞）
        result = self.chat_completion(
            messages, model=model, temperature=temperature, max_tokens=max_tokens,
            usage_sink=usage_sink,
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


class RecordingLLMClient(LLMClient):
    """包装真实客户端，把每次响应按「意图 + 调用序」录制为可回放脚本。

    用途：把 e2e 真跑（L3）的模型轨迹一键沉淀回离线回放（L2）——录出的
    JSON 与 ScriptedLLMClient 的脚本格式逐字兼容：直接
    ``LLM_PROVIDER=fake LLM_FAKE_SCRIPT=<path>`` 回放，或并入 evals case
    的 llm.json（站点 HTML 夹具另行拷贝，录制只负责模型侧）。

    为什么 key 是 intent+序号而不是 hash(messages)：
    - messages 含 system prompt，prompt 一改版全部 fixture 失效；
    - 真模型有温度，同请求不同响应，hash 锁死的是「这一次的随机结果」。
    intent 序列在 prompt 改版后通常稳定，是回放可维护的唯一锚点。

    归一化规则（对齐 ScriptedLLMClient._validate 接受的条目类型）：
    - 纯文本 → str
    - 单 tool_call → ``{"type": "tool_call", "name", "arguments"}``
    - 多 tool_call → ``{"type": "tool_calls", "calls": [...]}``
    - extract_structured_data 的 dict → json.dumps 成 str（回放侧
      ScriptedLLMClient.extract_structured_data 会 json.loads 还原；直接
      存 dict 会被脚本构造校验拒绝）

    识别不出意图的调用照常透传、不落盘（仅 warning）：录制是尽力而为，
    不能反过来阻断真跑。每次调用后原子落盘（.tmp + rename）——进程被
    kill 也不丢已录条目。
    """

    provider_name = "recording"

    def __init__(self, inner: LLMClient, path: Optional[str] = None):
        self._inner = inner
        self._path = path
        self.script: Dict[str, List[Any]] = {}

    def __getattr__(self, name: str) -> Any:
        # default_model / api_key / base_url 等属性被调用方直接读，代理到被包装客户端
        return getattr(self._inner, name)

    # ------------------------------------------------------------------
    # 归一化：真客户端的返回形态 → ScriptedLLMClient 脚本条目
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_call(tc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """单个 tool_call → 脚本条目。兼容内建形态与 OpenAI function 形态。"""
        if not isinstance(tc, dict):
            return None
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        if not name:
            return None
        arguments = tc.get("arguments")
        if arguments is None:
            arguments = (tc.get("function") or {}).get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        return {"type": "tool_call", "name": name, "arguments": arguments}

    @classmethod
    def _normalize_raw(cls, raw: Dict[str, Any]) -> Optional[Any]:
        """chat_completion_raw 的完整返回 → 脚本条目。"""
        calls = [c for c in (cls._normalize_call(tc) for tc in raw.get("tool_calls") or []) if c]
        if calls:
            return calls[0] if len(calls) == 1 else {"type": "tool_calls", "calls": calls}
        content = raw.get("content")
        return content if isinstance(content, str) and content.strip() else None

    def _record(self, entry: Optional[Any], messages: Optional[List[Dict[str, str]]] = None,
                intent: Optional[str] = None) -> None:
        if entry is None:
            return
        if intent is None:
            intent = detect_llm_intent(messages or [])
        if intent is None:
            logger.warning("[recording] 无法识别调用意图，本条不录制（调用照常透传）")
            return
        self.script.setdefault(intent, []).append(entry)
        if self._path:
            self.dump(self._path)

    def dump(self, path: str) -> None:
        """原子落盘：先写 .tmp 再 rename，读侧永远看到完整文件。"""
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.script, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # LLMClient 接口：透传 + 录制
    # ------------------------------------------------------------------

    def chat_completion(self, messages, model=None, temperature=0.2, max_tokens=4096,
                        tools=None, tool_choice="auto", timeout=None,
                        usage_sink=None) -> Union[str, Dict[str, Any], None]:
        result = self._inner.chat_completion(
            messages, model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice, timeout=timeout, usage_sink=usage_sink,
        )
        entry = result if isinstance(result, str) else self._normalize_call(result or {})
        self._record(entry, messages=messages)
        return result

    def chat_completion_raw(self, messages, model=None, temperature=0.2, max_tokens=4096,
                            tools=None, tool_choice="auto", timeout=None) -> Dict[str, Any]:
        result = self._inner.chat_completion_raw(
            messages, model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice, timeout=timeout,
        )
        self._record(self._normalize_raw(result), messages=messages)
        return result

    def stream_completion(self, messages, model=None, temperature=0.2, max_tokens=4096,
                          usage_sink=None, include_usage=None) -> str:
        result = self._inner.stream_completion(
            messages, model=model, temperature=temperature, max_tokens=max_tokens,
            usage_sink=usage_sink, include_usage=include_usage,
        )
        if isinstance(result, str) and result.strip():
            self._record(result, messages=messages)
        return result

    def extract_structured_data(self, content: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        result = self._inner.extract_structured_data(content, schema)
        # 回放侧按 "extract_structured" 意图消费（ScriptedLLMClient 的约定）
        self._record(json.dumps(result, ensure_ascii=False), intent="extract_structured")
        return result


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

    client = client_class()
    # L3→L2 轨迹沉淀：设置 LLM_RECORD_SCRIPT=<path> 后，所有调用按意图录制，
    # 录出的文件可直接被 ScriptedLLMClient（LLM_FAKE_SCRIPT）回放。不设则零开销。
    record_path = os.getenv("LLM_RECORD_SCRIPT")
    if record_path:
        return RecordingLLMClient(client, record_path)
    return client


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