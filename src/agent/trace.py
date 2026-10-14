"""Agent 运行轨迹数据结构。

trace 是 M4 评测与前端"思考面板"的共用数据源：每一步记录模型看到了什么、
调了什么工具、观察到了什么、花了多久。刻意不记录完整工具返回原文——
体积不可控，原文进 workspace，trace 只留摘要。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 全部终止原因。前四个来自实施草案 M3 的契约，后两个是收束兜底的细分。
STOP_REASONS = (
    "halt",               # 模型给出最终答案，正常结束
    "max_steps",          # 步数上界
    "budget_exceeded",    # token 或墙钟预算耗尽
    "error",              # LLM 连续空响应/不可恢复错误
    "empty_observations",  # 连续 2 轮工具结果为空，继续空转无意义
    "duplicate_loop",     # 同工具同参数重复 ≥3 次且提醒后仍重复
)


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
    elapsed_s: float = 0.0
    workspace: Dict[str, Any] = field(default_factory=dict)
