# Approval Permission 改造设计

更新时间：2026-07-07

本文记录 `local-agent` 的 OMP 风格权限模型改造。目标是把粗粒度审批升级为配置级 `approvalMode` / per-tool policy，加会话内 `Allow once` / `Always allow` / `Reject` / `Always reject`。

## 当前状态

当前已完成 MVP 版实现：

- `src/local_agent/config.py` 支持 `tools.approvalMode`、`tools.approval`、`AGENT_TOOL_APPROVAL` 和旧配置兼容。
- `src/local_agent/cli.py` 支持 `--approval-mode always-ask|write|yolo`、`--tool-approval` 和 REPL `/approval` 命令。
- `src/local_agent/agent.py` 维护当前进程内存态的 session approval policy。
- `src/local_agent/tools/base.py` 按 config policy、session policy、approval mode 解析审批。
- `tests/` 已覆盖 config、tool registry 和 REPL 命令路径。
- 新配置 `tools.approvalMode` / `tools.approval` 优先于旧顶层 `approval_mode` / `tool_approval`，方便迁移时渐进替换旧字段。
- 配置级 `prompt` / `deny` 是硬护栏；session allow 不能绕过配置级 `prompt`，但 session reject 可以更保守地拒绝。
- REPL `/approval allow|prompt|deny|reset TOOL` 会校验工具名，避免输错工具名后“看似成功但实际不生效”。

后续增强优先级：

1. approval prompt 支持 deadline/abort，避免同步 `input()` 等待耗尽 `budget_seconds`。
2. 命令级 shell permission intent；第一阶段先按工具名做 session cache key。

## 目标模型

对齐 OMP 的三层：

```text
tools.approvalMode -> always-ask / write / yolo
tools.approval.<toolName> -> allow / prompt / deny
session decision -> allow_once / allow_always / reject_once / reject_always
```

`yolo` 必须保留为一等模式。它表示当前进程/会话默认自动批准所有工具 tier，适合完全可信仓库、封闭 VM、一次性批处理任务。它不是安全沙箱，不能替代 VM 隔离和危险命令硬拒绝。

兼容旧配置：

- `approval_mode=ask` 等价于 `tools.approvalMode=always-ask`。
- `approval_mode=auto-read` 作为旧别名保留，也映射到 `always-ask`。
- `approval_mode=yolo` 映射到 `tools.approvalMode=yolo`。
- `auto_approve_tools=run_tests,git_diff` 映射成对应工具的 `allow`，但不覆盖显式 `deny`。

## 用户配置形态

继续支持旧写法：

```json
{
  "approval_mode": "ask",
  "auto_approve_tools": ["run_tests", "git_diff"]
}
```

新增推荐写法：

```json
{
  "tools": {
    "approvalMode": "always-ask",
    "approval": {
      "run_tests": "allow",
      "git_diff": "allow",
      "shell": "prompt",
      "write_file": "deny"
    }
  }
}
```

CLI 建议：

```bash
./agent --approval-mode always-ask
./agent --approval-mode write
./agent --approval-mode yolo
./agent --tool-approval run_tests=allow,shell=prompt,write_file=deny
```

环境变量建议：

```bash
export AGENT_APPROVAL_MODE="always-ask"
export AGENT_TOOL_APPROVAL="run_tests=allow,shell=prompt,write_file=deny"
```

## 审批解析顺序

建议实现为一个明确函数，避免散在 if 里：

```text
1. 读取工具 tier：read / state / interaction / write / exec。
2. 读取 config policy：allow / prompt / deny。
3. 读取 session policy：allow_always / prompt / reject_always。
4. config deny 直接拒绝。
5. session reject_always 直接拒绝。
6. config prompt 强制询问，且不允许 session allow 绕过。
7. session prompt 强制询问。
8. session allow_always 直接允许。
9. config allow 直接允许。
10. approvalMode=yolo 直接允许所有未被显式拒绝或显式 prompt 的工具。
11. approvalMode=write 自动允许 read/state/interaction/write，exec 询问。
12. approvalMode=always-ask 自动允许 read/state/interaction，write/exec 询问。
```

危险 shell 命令继续在 `shell.py` 里硬拒绝。即使 approval 允许，也不能执行明显危险命令。

## Yolo 模式要求

`yolo` 模式要满足：

- 启动参数支持：`--approval-mode yolo`。
- 环境变量支持：`AGENT_APPROVAL_MODE=yolo`。
- 旧 config 支持：`"approval_mode": "yolo"`。
- 新 config 支持：`"tools": {"approvalMode": "yolo"}`。
- 在 `yolo` 下，`tools.approval.<tool>=prompt` 仍可强制某个工具询问。
- 在 `yolo` 下，`tools.approval.<tool>=deny` 仍可强制某个工具拒绝。
- 在 `yolo` 下，危险 shell 硬拒绝仍然生效。

推荐用户用法：

```bash
./agent --approval-mode yolo "在可信 VM 里跑完整验证"
```

推荐配置：

```json
{
  "tools": {
    "approvalMode": "yolo",
    "approval": {
      "shell": "prompt",
      "write_file": "deny"
    }
  }
}
```

## 会话内权限

在 `ToolContext` 增加一个运行时内存字段：

```python
session_tool_approval: dict[str, str] | None = None
```

`AgentRuntime.__init__()` 创建一个可变字典，并传给 `ToolContext`：

```python
self._session_tool_approval: dict[str, str] = {}
self._tool_context = ToolContext(
    ...,
    session_tool_approval=self._session_tool_approval,
)
```

交互提示从：

```text
Allow exec tool 'run_tests'? [y/N]
```

改成：

```text
Allow exec tool 'run_tests'?
[y] once / [s] always this session / [n] reject / [d] reject this session
```

行为：

- `y` / `yes`：只允许本次。
- `s` / `session`：写入 `session_tool_approval[tool.name] = "allow_always"`，并允许本次；如果该工具有配置级 `prompt`，则不提供 session allow，仍需每次确认。
- `n` / `no` / 空输入：拒绝本次。
- `d` / `deny`：写入 `session_tool_approval[tool.name] = "reject_always"`，并拒绝本次。

先按工具名做 session cache key 即可；后续如果要更像 OMP，再给 `shell` 加命令级 cache key。

## REPL 命令

建议补最小 slash command，便于用户在会话里主动改权限：

```text
/approval
/approval mode always-ask
/approval mode write
/approval mode yolo
/approval allow run_tests
/approval prompt shell
/approval deny write_file
/approval reset run_tests
```

实现位置：`src/local_agent/cli.py` 的 `_repl()`。

建议给 `AgentRuntime` 增加方法：

```python
def approval_summary(self) -> str: ...
def set_session_approval_mode(self, mode: str) -> None: ...
def set_session_tool_policy(self, tool: str, policy: str) -> None: ...
def reset_session_tool_policy(self, tool: str) -> None: ...
```

注意：这里是当前进程内存态，不写全局 config。

## 后续增强：approval prompt deadline / abort

背景：

- OMP 的 `deadline` 是 wall-clock absolute timestamp，等待 permission response 也在同一时间窗口内。
- OMP 的 ACP permission gate 会把 `requestPermission` 和 abort signal `Promise.race(...)`，deadline 到期可以取消等待。
- 我们当前 `_interactive_approval_denial_reason()` 使用同步 `input()`，不能被 deadline 主动打断；用户长时间不确认时，可能出现“确认后工具执行成功，但下一次 deadline 检查立刻停止”。

建议实现：

1. 在 `src/local_agent/tools/base.py` 增加 timed stdin helper，可参考 `src/local_agent/tools/interaction.py` 的 `_read_timed_answer()`。
2. `_interactive_approval_denial_reason()` 根据 `context.deadline_monotonic` 计算剩余秒数。
3. 如果剩余时间已经小于等于 0，直接返回 “approval cancelled because budget_seconds is exhausted” 之类的拒绝原因。
4. 如果有剩余时间，用 `select.select([sys.stdin], [], [], timeout)` 等待输入。
5. 超时无输入时，按取消/拒绝处理，不执行工具。
6. 保持现有 `y/s/n/d` 行为不变；`s` 写入 `allow_always`，`d` 写入 `reject_always`。
7. Agent loop 已会把 tool error 回灌；如果 deadline 已过，后续检查会停止并补齐剩余 tool_call。

建议测试：

- `deadline_monotonic` 已过时，write/exec approval 不调用 `input()`，直接拒绝。
- `select.select` 超时时，approval 返回 tool error。
- 有输入 `y` 时仍允许本次执行。
- 有输入 `s` 时写入 `session_tool_approval[tool] = "allow_always"`。
- 有输入 `d` 时写入 `session_tool_approval[tool] = "reject_always"`。
- `approval_mode=write` 自动允许 write 的行为不受影响；exec 仍会走 timed approval。

## 已补的测试

`tests/test_config.py`：

- `tools.approvalMode` 能从 config 读取。
- `tools.approval` 能从 config 读取。
- `approvalMode` 支持 `always-ask` / `write` / `yolo`。
- 旧 `approval_mode=ask` / `auto-read` 仍可用。
- 新 `tools.approvalMode` / `tools.approval` 优先于旧顶层字段。
- 旧 `auto_approve_tools` 映射为 `allow`。
- 显式 `tool_approval.write_file=deny` 不被 `auto_approve_tools` 覆盖。
- 非法 policy 报错。

`tests/test_tools.py`：

- `tool_approval={"sample_write": "allow"}` 在非交互下允许 write。
- `tool_approval={"sample_read": "prompt"}` 会让 read 工具也提示。
- `tool_approval={"sample_write": "deny"}` 直接拒绝。
- `approval_mode="write"` 自动允许 write，仍提示 exec。
- 输入 `s` 后当前 session 后续同工具不再提示。
- 配置级 `prompt` 不被 session allow 覆盖。
- 输入 `d` 后当前 session 后续同工具直接拒绝。

`tests/test_cli.py`：

- REPL `/approval ...` 命令能更新运行时内存态。
- REPL `/approval ... TOOL` 会拒绝未知工具名。

## 文档更新

已同步更新：

- `README.md`：用户如何设置 `approvalMode`、per-tool policy、会话内 always allow。
- `docs/project-management.md`：R-003 的 OMP 实现建议更新为“采用 OMP 三层模型”。
- `docs/project-status.md`：项目状态和测试数已更新。
- `docs/local-coding-agent-project-management.xlsx`：由 `docs/project-management.md` 重新生成。

## 验收标准

- 旧命令仍可用：

```bash
./agent --approval-mode ask --auto-approve-tools run_tests,git_diff "..."
```

- 新命令可用：

```bash
./agent --approval-mode write --tool-approval shell=prompt,write_file=deny "..."
```

- REPL 中可以临时允许当前 session 的工具调用。
- `python3 -m unittest discover -s tests` 通过。
- `python3 -m compileall src tests scripts` 通过。
