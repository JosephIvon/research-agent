"""M4 离线评测集：确定性指标 + 版本化基线 + 阶段一门禁。

被测对象是**流水线**而非模型：抽取脚本由人按夹具内容手工撰写（fake LLM
逐字回放），评测衡量的是流水线是否正确地保真、校验、呈现这些事实——
引用三率、维度覆盖、冲突保留、缺失承认、注入抵抗。真实模型的端到端
质量由 CI 的 e2e-real-services job 负责，两者不混。

运行：
    uv run python -m evals.runner                 # 跑全部 case，出报告
    uv run python -m evals.runner --gate          # 门禁模式（注入=1.00 硬门槛）
    uv run python -m evals.runner --update-digests  # 夹具变更后显式刷新 sha256 清单

真跑轨迹沉淀（L3 → L2）：
    给后端设置 LLM_RECORD_SCRIPT=<path> 后，所有真实 LLM 调用会按
    「意图+调用序」录制到该文件（RecordingLLMClient，格式与本评测的
    llm.json 脚本逐字兼容）。e2e-real-services CI job 已默认开启并上传
    artifact；本地等价用法：

        LLM_RECORD_SCRIPT=recordings/run.json python server.py        # 真跑即录
        LLM_PROVIDER=fake LLM_FAKE_SCRIPT=recordings/run.json ...     # 离线回放

    失败真跑的录制文件拷进 evals/cases/<id>/llm.json、站点 HTML 拷进
    sites/，即成一个可复现的离线回归 case。
"""
