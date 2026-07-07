# Local Coding Agent 项目状态

更新时间：2026-07-07

本文档是 `local-coding-agent` 的 Agent 可读项目管理基线。`docs/local-coding-agent-project-management.xlsx` 继续作为人工查看的表格视图；本 Markdown 文件作为后续开发时优先读取的项目状态、路线、Todo 和决策来源。

## 最终目标

构建一个个人本地编程助手 Agent，第一阶段只面向个人本地使用，并且可以运行在封闭 VM 中。

目标能力：

- 读取本地代码和文档。
- 搜索本地代码。
- 通过安全、可审计的 patch 修改代码。
- 运行本地命令和测试。
- 生成并展示 git diff。
- 沉淀项目级记忆。
- 只访问指定的 OpenAI-compatible AI API，例如阿里云百炼。
- 不依赖公网搜索，不自动下载依赖，不做远程控制。

第一阶段暂不做：

- 多 Agent 并行。
- LSP / DAP。
- Browser 工具。
- 自动联网搜索。
- 远程仓库控制。
- AST 级复杂编辑。

## 当前进度

当前项目处于 P2 阶段：基础 Agent 能力已经可用，正在补齐项目管理、长任务可控性和日用体验。

已具备的核心能力：

- Python 标准项目结构：`pyproject.toml`、`src/`、`tests/`、`docs/`。
- CLI 入口：`PYTHONPATH=src python3 -m local_agent.cli`。
- 支持 `bailian` provider，对接阿里云百炼 OpenAI-compatible API。
- Agent Runtime 已支持工具调用循环。
- 工具注册、schema 校验、审批模式已经可用。
- 文件读取、目录浏览、代码搜索、shell、测试、git 状态、git diff、anchored patch、Markdown memory 已经可用。
- `apply_patch` 已支持 `replace`、`insert_before`、`insert_after`，并兼容 Python 3.12。
- 非交互审批、LLM 非 JSON 响应、session 恢复坏尾部、search_code 绝对路径泄漏等问题已经修复。
- 已完成 Agent 自举测试：能够通过百炼模型调用工具读取、修改、测试和查看 diff。
- 测试基线：38 个测试在正常本地环境通过。

当前主要缺口：

- 仓库还没有初始 git commit。
- 长任务仍缺少 deadline / budget-seconds。
- `max_steps` 目前仍偏向常规限制，后续应调整为较高的安全保险丝。
- 没有 Agent 可维护的 todo 工具。
- 没有 ask_user 工具，需求歧义时无法主动暂停提问。
- 上下文历史仍以全量消息为主，未做 summary / compaction。
- 中断、异常、输出截断时还需要进一步补齐 synthetic tool result 机制。

## 阶段路线图

| 阶段 | 名称 | 状态 | 目标 |
|---|---|---|---|
| P0 | OMP 分析与 MVP 设计 | 已完成 | 明确不照搬 OMP，采用本地优先、封闭 VM 友好的简化架构。 |
| P1 | 基础 Agent Loop | 已完成 | CLI、Provider、Agent Runtime、基础工具、patch、memory、session、测试基线。 |
| P2 | 项目管理与可见性 | 进行中 | 建立 Excel + Markdown 项目状态，让目标、进度、风险、Todo 一目了然。 |
| P3 | 长任务运行基础 | 未开始 | 引入 deadline / budget-seconds、提高 max_steps 兜底值、todo、ask_user、per-tool approval。 |
| P4 | 上下文治理 | 未开始 | 初版 summary / compaction、工具输出折叠、长需求文件工作流。 |
| P5 | 安全与恢复增强 | 未开始 | synthetic tool result、patch preview、回滚策略、非信任仓库提示。 |
| P6 | 高级工程能力 | 暂缓 | LSP、DAP、TUI、subagents、reviewer、AST edit 等能力后置评估。 |

## 已完成功能

| 能力 | 状态 | 依据 |
|---|---|---|
| 项目骨架 | 已完成 | `pyproject.toml`、`src/local_agent/`、`tests/`、`docs/` 已存在。 |
| CLI | 已完成 | `src/local_agent/cli.py` 提供命令行入口。 |
| 配置加载 | 已完成 | `src/local_agent/config.py` 支持 provider、cwd、approval mode、session、max steps 等参数。 |
| 百炼 Provider | 已完成 | 支持 `bailian`，默认 OpenAI-compatible endpoint 和 `qwen-plus`。 |
| Agent Runtime | 已完成 | `src/local_agent/agent.py` 实现模型调用、工具分发和循环。 |
| Tool Registry | 已完成 | `src/local_agent/tools/base.py` 管理工具、审批、异常包装。 |
| 文件工具 | 已完成 | `read_file`、`list_files`、`write_file` 已可用，写文件为 create-only。 |
| 搜索工具 | 已完成 | `search_code` 使用 `rg`，输出 workspace 相对路径并做总结果截断。 |
| Shell / Test 工具 | 已完成 | `shell`、`run_tests` 可用，执行类工具需要审批。 |
| Git 工具 | 已完成 | `git_status`、`git_diff` 可用，空 diff 时提示 untracked 文件。 |
| Anchored Patch | 已完成 | `apply_patch` 使用 tag、line、old_text 校验，并返回 diff。 |
| Markdown Memory | 已完成 | `memory_read`、`memory_write` 写入项目级 Markdown 记忆。 |
| Session | 已完成 | JSONL session 支持继续会话，并处理坏尾部。 |
| 兼容性修复 | 已完成 | patch 读写使用 bytes，避免 Python 3.12 的 `newline` 参数问题。 |
| 错误处理修复 | 已完成 | 非交互审批和 LLM 非 JSON 响应已有明确错误路径。 |
| 测试基线 | 已完成 | 本地正常环境下测试通过。 |

## 下一步 Todo

| ID | 任务 | 状态 | 优先级 | 说明 |
|---|---|---|---|---|
| T-001 | 确认项目管理基线 | 已完成 | P0 | Excel 已被人工复核，结论可信。 |
| T-002 | 建立 `docs/project-status.md` | 已完成 | P0 | 已将 Excel 内容转成 Agent 可读 Markdown，作为后续开发基线。 |
| T-003 | 创建初始 git commit | 待用户确认 | P0 | 当前仓库零提交；需要用户明确确认后再执行。 |
| T-004 | 增加 `--budget-seconds` / deadline | 未开始 | P1 | 学习 OMP 思路，用时间预算控制长任务。 |
| T-005 | 将 `max_steps` 调整为高兜底保险丝 | 未开始 | P1 | 不再把步数当日常终止条件，建议默认提高到 80 或 100。 |
| T-006 | 增加 todo 工具 | 未开始 | P1 | 让 Agent 在长任务中维护待办、进行中、已完成状态。 |
| T-007 | 增加 ask_user 工具 | 未开始 | P1 | 需求不清时允许 Agent 暂停并向用户提问。 |
| T-008 | 增加 per-tool approval policy | 未开始 | P2 | 减少 ask 模式下反复输入 `y` 的摩擦。 |
| T-009 | 更新 README 安全工作流 | 未开始 | P2 | 明确 shell 不是沙箱、`yolo` 只用于可信仓库。 |
| T-010 | 初版 context summary / compaction | 未开始 | P3 | 解决长任务中上下文不断膨胀的问题。 |
| T-011 | synthetic tool result | 未开始 | P3 | 中断、异常、输出截断时补齐 tool_call 配对。 |
| T-012 | patch preview / rollback | 暂缓 | P4 | 在 anchored patch 基础上增强可回退体验。 |
| T-013 | 评估 LSP / TUI / subagents / AST edit | 暂缓 | P5 | 高级能力，不进入第一阶段 MVP。 |

## 风险清单

| ID | 风险 | 状态 | 影响 | 应对 |
|---|---|---|---|---|
| R-001 | 仓库没有初始 commit | 开放 | 后续修改缺少稳定回滚基线。 | 用户确认后立即创建初始 commit。 |
| R-002 | 长任务上下文持续膨胀 | 开放 | 多轮工具调用后 token 成本和失败率上升。 | P4 增加 summary / compaction。 |
| R-003 | 没有 todo 工具 | 开放 | 长需求中不容易追踪完成项和遗漏项。 | P3 增加 todo 工具，并写入 session。 |
| R-004 | 没有 ask_user 工具 | 开放 | 遇到歧义时模型只能猜。 | P3 增加 ask_user，必要时暂停等待用户。 |
| R-005 | ask 模式确认次数多 | 开放 | 日用体验偏慢。 | 增加 per-tool approval policy。 |
| R-006 | shell 工具不是安全沙箱 | 开放 | 命令可以越过 workspace 访问系统。 | 文档明确风险；封闭 VM 作为真正边界。 |
| R-007 | 恶意仓库 prompt injection | 开放 | 文件内容可能诱导模型执行不安全操作。 | 不信任仓库禁用 `yolo`，保留人工审批。 |
| R-008 | 中断时 tool_call 配对仍可增强 | 开放 | 恢复会话时可能遇到兼容性问题。 | P5 增加 synthetic tool result。 |

## 架构决策

| ID | 决策 | 依据 |
|---|---|---|
| ADR-001 | 不照搬 OMP，只借鉴能力边界和关键机制。 | OMP 功能很完整，但第一阶段个人本地使用不需要 LSP、DAP、多 Agent、Browser 等复杂能力。 |
| ADR-002 | `max_steps` 只作为安全保险丝，不作为主要预算。 | OMP 主循环不靠步数终止，而靠模型是否继续请求工具、时间预算和上下文预算。 |
| ADR-003 | Excel 作为人工视图，Markdown 作为 Agent 可读事实源。 | 当前 Agent 的 `read_file` 不能读取 `.xlsx` 二进制文件。 |
| ADR-004 | 第一阶段 memory 使用 Markdown。 | Markdown 简单、可审计、封闭 VM 友好；暂不引入 SQLite 或向量库。 |
| ADR-005 | 第一阶段使用 anchored patch，不做 AST edit。 | hash + old_text + line 校验已经足够支撑 MVP 的可控修改。 |
| ADR-006 | 长需求应写入文件，让 Agent 读取。 | 直接把大段需求塞进 prompt 会挤占上下文，不利于长任务。 |
| ADR-007 | 封闭 VM 目标优先于联网能力。 | 第一阶段只允许访问指定 AI API，不引入公网搜索和自动下载依赖。 |

## 推荐工作流

处理普通代码任务：

1. 用户用自然语言描述目标。
2. Agent 先 `list_files` 和 `read_file` 理解项目。
3. 修改前必须读取目标文件。
4. 修改已有文件必须使用 `apply_patch`。
5. 修改后必须运行相关测试。
6. 最后调用 `git_diff` 展示变更。

处理复杂需求：

1. 将需求写入 `docs/requirements/*.md`。
2. Agent 读取需求文件，不把整篇长需求直接塞进 prompt。
3. Agent 使用 todo 工具拆解任务。
4. 每完成一小步运行测试或局部验证。
5. 最终输出已完成项、未完成项、测试结果和 diff 摘要。

审批建议：

- 默认使用 `ask`。
- 只读探索可以使用 `auto-read`。
- `yolo` 只用于完全可信仓库和封闭 VM。
- shell / run_tests / apply_patch 都应保留可审计日志。

## P3 开发入口

用户确认本文件后，建议按以下顺序继续：

1. 创建初始 git commit。
2. 实现 `--budget-seconds` / deadline。
3. 将 `max_steps` 默认值提高为安全兜底。
4. 实现 todo 工具。
5. 实现 ask_user 工具。
6. 实现 per-tool approval policy。

其中第 1 步需要用户明确确认提交动作。
