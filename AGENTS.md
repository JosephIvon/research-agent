# research-agent 项目约定

## 共享记忆

- 编码任务开始前读 `$QCHECK_HOME/memory.md`（跨工具共享的编程习惯与偏好），遵守其中条目。
  `QCHECK_HOME` 指向本机的 self-improving 权威源目录；未设置时按 `$HOME/self-improving`。
- 本项目特有的教训追加到 `.self-improving/lessons.md`（不存在则创建，只追加）；与全局层冲突时项目层优先。

## 质量规则

- 编码/评审前读 `$QCHECK_HOME/quality-rules.md`（QR 编号规则集，只放引用，不复抄内容）。
  仓内快照在 `.quality/rules.min.md`（离线/CI 用快照，判定强度同源，权威源改后需重导出）。
- 阻断位是 `git pre-commit` → `qcheck`（只跑确定性检查；LLM 意见永不进阻断位）。改了数字或口径后至少跑一次：
  `python "$QCHECK_HOME/tools/qcheck.py" --staged`。`QCHECK_HOME` 未设置时闸门会显式失败并给出修复命令，
  不要绕过它，也不要把钩子文件删掉。
