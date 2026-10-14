"""ReAct Runner 的预算与开关配置。

刻意不进 src/config/settings.py：该文件是多处并行改动的热点，Agent 预算
属自包含关注点，env 直读 + 此处集中定义即可，接线（settings 聚合）后移。

默认值依据实施草案 M3：
- AGENT_MAX_STEPS=8：≤3 会退化成"固定几步"，演示不出与 legacy 的区别
- AGENT_TOKEN_BUDGET：待 M4 基线给出任务均值后按 ×2 校准，当前给保守上限
- AGENT_WALL_CLOCK_BUDGET=180s
"""
import os

AGENT_MODE = os.getenv("AGENT_MODE", "legacy").lower()  # legacy | react

AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
AGENT_TOKEN_BUDGET = int(os.getenv("AGENT_TOKEN_BUDGET", "120000"))
AGENT_WALL_CLOCK_BUDGET = float(os.getenv("AGENT_WALL_CLOCK_BUDGET", "180"))
# USD 预算：估价口径（见 src/llm/token_budget.py），超额以 budget_exceeded 收束为 PARTIAL
AGENT_USD_BUDGET = float(os.getenv("AGENT_USD_BUDGET", "0.5"))

# 收束兜底阈值
TOOL_DUPLICATE_LIMIT = int(os.getenv("AGENT_DUPLICATE_LIMIT", "3"))  # 同工具同参数
EMPTY_OBSERVATION_LIMIT = int(os.getenv("AGENT_EMPTY_ROUNDS", "2"))  # 连续空结果轮数
MODEL_EMPTY_RESPONSE_LIMIT = int(os.getenv("AGENT_EMPTY_MODEL_ROUNDS", "2"))
