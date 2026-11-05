"""机器可读的错误码与运行终止契约。

两套词表刻意分开：

- `ToolErrorCode`：单次工具调用的失败分类，供模型与恢复决策表使用；
- `ReasonCode`：一次 Runner 运行的终止原因，必须先于字符串出现在代码里。

所有枚举都是 `str` 子类，序列化到 JSON / 事件 payload 时无需额外转换，
同时保留 `ReasonCode.HALT.value` 与旧字符串 `"halt"` 完全一致，向后兼容。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class ToolErrorCode(str, Enum):
    """工具调用失败分类。成功时用 `None`，不用 OK 值，避免两套语义。"""

    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNSAFE_URL = "unsafe_url"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    PRECONDITION_FAILED = "precondition_failed"
    NOT_FOUND = "not_found"


#: 未显式声明 error_codes 的工具默认使用这一组（覆盖当前全部失败路径）。
STANDARD_TOOL_ERROR_CODES = tuple(code.value for code in ToolErrorCode)

#: 这些失败允许模型原样重试；参数/安全类失败重试无意义。
RETRYABLE_TOOL_ERROR_CODES = frozenset({
    ToolErrorCode.TIMEOUT.value,
    ToolErrorCode.EXECUTION_ERROR.value,
})


class ToolContractError(Exception):
    """handler 显式声明的失败：错误码由业务代码指定，而不是兜底的 execution_error。

    与裸 ValueError 的区别：注册表捕获本异常后按声明的错误码归一返回，
    retryable 由 RETRYABLE_TOOL_ERROR_CODES 决定——「未先抓取就抽取」
    这类前置不满足是 precondition_failed（重试无意义），不该被标成
    可重试的 execution_error 误导恢复决策。
    """

    def __init__(self, code: ToolErrorCode, message: str = ""):
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value


class ReasonCode(str, Enum):
    """一次运行的终止原因。与 `trace.STOP_REASONS` 的字符串值一一对应。"""

    HALT = "halt"
    MAX_STEPS = "max_steps"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"
    EMPTY_OBSERVATIONS = "empty_observations"
    DUPLICATE_LOOP = "duplicate_loop"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunOutcome:
    """运行终止契约：一个枚举 + 一句人类可读说明，不携带裸错误字符串。"""

    reason_code: ReasonCode
    message: str = ""
    # 终态证据使用信号：被折叠但从未被 read_evidence 回读的条数，
    # 以及其中“终答里引用了来源 URL / key”的条数（更强的告警标记）。
    unread_folded_evidence: int = 0
    unread_cited_evidence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason_code": self.reason_code.value,
            "message": self.message,
            "unread_folded_evidence": self.unread_folded_evidence,
            "unread_cited_evidence": self.unread_cited_evidence,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["RunOutcome"]:
        if not data or not data.get("reason_code"):
            return None
        return cls(
            reason_code=ReasonCode(data["reason_code"]),
            message=data.get("message") or "",
            unread_folded_evidence=int(data.get("unread_folded_evidence") or 0),
            unread_cited_evidence=int(data.get("unread_cited_evidence") or 0),
        )
