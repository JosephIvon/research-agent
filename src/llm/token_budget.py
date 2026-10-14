"""任务级 token/成本计量与预算守卫（M6）。

设计要点：
- **真实 usage 优先**：provider 返回的 prompt/completion tokens 是唯一真值，
  字符估算只在没有 usage 时兜底（误差带 ±30%，只用于"宁紧勿松"的预算拦截，
  绝不对外当精确数字展示）。
- **估价不是账单**：PRICING 表是各 provider 公开价的粗略快照，用于预算守卫
  与成本量级感知；cost_report 里的 estimated_usd 必须带 estimated 标记。
- **线程安全**：LLM 调用在 asyncio.to_thread 线程池里执行，记账用 threading.Lock。
"""
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# USD / 1M tokens —— 预算守卫用的估算价（公开价快照，会过时，不是账单）
PRICING_PER_MTOK: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "abab": {"input": 0.10, "output": 0.10},      # minimax 系列
    "glm-4": {"input": 0.10, "output": 0.10},
    "doubao": {"input": 0.08, "output": 0.20},
}
DEFAULT_PRICE = {"input": 0.50, "output": 2.00}

_CHARS_PER_TOKEN_FALLBACK = 3  # 中英混合的折中；真值永远来自 usage

_encoding_cache: Dict[str, Any] = {}


def count_tokens(text: str) -> int:
    """离线 token 估算：装了 tiktoken 用真编码器，没装按字符折中。

    只用于预算拦截与量级展示；任何"精确 token 数"的口径都必须来自
    provider 返回的 usage。
    """
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return math.ceil(len(text) / _CHARS_PER_TOKEN_FALLBACK)


def _get_encoding():
    """tiktoken 可选依赖：装了就用，没装就降级——不强制引入 20MB 依赖。"""
    if "fallback" in _encoding_cache:
        return _encoding_cache.get("encoding")
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        enc = None
    _encoding_cache["encoding"] = enc
    _encoding_cache["fallback"] = enc is None
    return enc


def price_for(model: Optional[str]) -> Dict[str, float]:
    if not model:
        return DEFAULT_PRICE
    model_lower = model.lower()
    for prefix, price in PRICING_PER_MTOK.items():
        if prefix in model_lower:
            return price
    return DEFAULT_PRICE


class BudgetExceeded(Exception):
    """预算耗尽。携带原因（token/usd）与当前用量，调用方可转 PARTIAL。"""

    def __init__(self, reason: str, report: Dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.report = report


@dataclass
class TaskBudget:
    """单任务的 token/USD 预算与累计用量。线程安全（LLM 调用在线程池执行）。"""
    task_id: str
    max_tokens: int = 120_000
    max_usd: float = 0.50
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_calls: int = 0     # 有多少次调用没拿到 usage、只能靠估算
    estimated_tokens: int = 0    # 这些估算一共贡献了多少 token（真值缺口的量化）
    calls: int = 0
    by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        provider: str,
        model: Optional[str],
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        estimated: bool = False,
    ) -> None:
        """记账一次调用。usage 缺失时由调用方估算并显式打 estimated 标记。"""
        model = model or "unknown"
        with self._lock:
            self.calls += 1
            entry = self.by_model.setdefault(
                model, {"prompt": 0, "completion": 0, "calls": 0, "provider": provider}
            )
            if prompt_tokens is not None:
                self.prompt_tokens += prompt_tokens
                entry["prompt"] += prompt_tokens
            if completion_tokens is not None:
                self.completion_tokens += completion_tokens
                entry["completion"] += completion_tokens
            if estimated:
                # 次数与 token 数分开记：老字段名 estimated_tokens 实际累加的是
                # 调用次数，字段名与语义不符会让人把 3 次估算读成 3 个 token。
                self.estimated_calls += 1
                self.estimated_tokens += (
                    (prompt_tokens or 0) + (completion_tokens or 0)
                )

    def record_estimated(self, provider: str, model: Optional[str], prompt_text: str,
                         completion_text: str = "") -> None:
        """没有 usage 时的估算入口（估算量单独统计，不与真值混算）。"""
        self.record(
            provider, model,
            count_tokens(prompt_text), count_tokens(completion_text),
            estimated=True,
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def estimate_usd(self) -> float:
        """按各模型累计量与估价表折算，保留 6 位。"""
        total = 0.0
        for model, usage in self.by_model.items():
            price = price_for(model)
            total += usage["prompt"] / 1e6 * price["input"]
            total += usage["completion"] / 1e6 * price["output"]
        return round(total, 6)

    def exceeded_reason(self) -> Optional[str]:
        if self.total_tokens > self.max_tokens:
            return f"token_budget_exceeded: {self.total_tokens}/{self.max_tokens}"
        if self.estimate_usd() > self.max_usd:
            return f"usd_budget_exceeded: ${self.estimate_usd():.4f}/${self.max_usd}"
        return None

    def snapshot(self) -> Dict[str, Any]:
        """cost_report：进任务结果与前端展示的唯一口径。"""
        with self._lock:
            return {
                "task_id": self.task_id,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "calls": self.calls,
                "estimated_usd": self.estimate_usd(),
                # estimated 语义不变：**本任务里是否出现过估算**（有任一次
                # 估算记账就为 True），前端据此决定要不要给数字打"约"字。
                "estimated": self.estimated_calls > 0,
                "estimated_calls": self.estimated_calls,
                "estimated_tokens": self.estimated_tokens,
                "by_model": {k: dict(v) for k, v in self.by_model.items()},
                "limits": {"max_tokens": self.max_tokens, "max_usd": self.max_usd},
            }


class BudgetGuard:
    """按 task_id 维护预算实例（进程内注册表）。release 后移除，防泄漏。"""

    def __init__(self):
        self._budgets: Dict[str, TaskBudget] = {}
        self._lock = threading.Lock()

    def create(self, task_id: str, max_tokens: int = 120_000,
               max_usd: float = 0.50) -> TaskBudget:
        with self._lock:
            self._budgets[task_id] = TaskBudget(task_id, max_tokens=max_tokens, max_usd=max_usd)
            return self._budgets[task_id]

    def get(self, task_id: str) -> Optional[TaskBudget]:
        with self._lock:
            return self._budgets.get(task_id)

    def release(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            budget = self._budgets.pop(task_id, None)
        return budget.snapshot() if budget else None


budget_guard = BudgetGuard()
