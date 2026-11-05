"""Agent 运行轨迹数据结构。

trace 是 M4 评测与前端"思考面板"的共用数据源：每一步记录模型看到了什么、
调了什么工具、观察到了什么、花了多久。刻意不记录完整工具返回原文——
体积不可控，原文进 workspace，trace 只留摘要。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.error_codes import ReasonCode, RunOutcome

# 全部终止原因。前四个来自实施草案 M3 的契约，后两个是收束兜底的细分。
# 契约 v2 起以 ReasonCode 为唯一来源，本元组仅作为向后兼容的只读视图。
STOP_REASONS = tuple(code.value for code in ReasonCode if code is not ReasonCode.CANCELLED)


@dataclass
class AgentStep:
    """一轮 LLM 交互（一轮可含多个工具调用）。"""
    index: int
    thought: Optional[str] = None              # 模型本轮的文本（无则为 None）
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)  # {ok, summary, error, elapsed}
    elapsed: float = 0.0
    stop_reason_if_any: Optional[str] = None   # 本轮结束时若触发终止，记录原因


@dataclass
class AgentResult:
    """Runner 的最终产物。answer 为 None 表示未得到终答（看 stop_reason）。"""
    answer: Optional[str]
    stop_reason: str
    steps: List[AgentStep] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)  # prompt/completion/total tokens
    usage_estimated: bool = True
    cost_report: Dict[str, Any] = field(default_factory=dict)  # TaskBudget.snapshot() 口径
    elapsed_s: float = 0.0
    workspace: Dict[str, Any] = field(default_factory=dict)
    # 契约 v2：终止契约的枚举形态。stop_reason 保留字符串以向后兼容。
    reason_code: Optional[ReasonCode] = None
    outcome: Optional[RunOutcome] = None
    # 证据使用信号：被折叠但从未被 read_evidence 回读 / 其中被终答引用的条数。
    unread_folded_evidence: int = 0
    unread_cited_evidence: int = 0
