"""M4 离线评测集：确定性指标 + 版本化基线 + 阶段一门禁。

被测对象是**流水线**而非模型：抽取脚本由人按夹具内容手工撰写（fake LLM
逐字回放），评测衡量的是流水线是否正确地保真、校验、呈现这些事实——
引用三率、维度覆盖、冲突保留、缺失承认、注入抵抗。真实模型的端到端
质量由 CI 的 e2e-real-services job 负责，两者不混。

运行：
    uv run python -m evals.runner                 # 跑全部 case，出报告
    uv run python -m evals.runner --gate          # 门禁模式（注入=1.00 硬门槛）
    uv run python -m evals.runner --update-digests  # 夹具变更后显式刷新 sha256 清单
"""
