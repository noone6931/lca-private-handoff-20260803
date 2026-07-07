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

当前项目处于 P5 阶段：基础 Agent 能力、项目管理基线、长任务时间预算、todo、ask_user、per-tool approval 和最小版本地 context compaction 都已经具备；正在增强中断与恢复安全性。

已具备的核心能力：

- Python 标准项目结构：`pyproject.toml`、`src/`、`tests/`、`docs/`。
- CLI 入口：推荐 `./agent`，安装后可用 `local-agent`，源码入口仍是 `python3 -m local_agent.cli`。
- 支持 `bailian` provider，对接阿里云百炼 OpenAI-compatible API。
- Agent Runtime 已支持工具调用循环。
- 工具注册、schema 校验、审批模式已经可用。
- 文件读取、目录浏览、代码搜索、shell、测试、git 状态、git diff、anchored patch、Markdown memory 已经可用。
- `apply_patch` 已支持 `replace`、`insert_before`、`insert_after`，并兼容 Python 3.12。
- 非交互审批、LLM 非 JSON 响应、session 恢复坏尾部、search_code 绝对路径泄漏等问题已经修复。
- 已完成 Agent 自举测试：能够通过百炼模型调用工具读取、修改、测试和查看 diff。
- 测试基线：60 个测试在正常本地环境通过。

当前已具备：

- 仓库已有初始 git commit。
- 长任务已有初版 `--budget-seconds` 墙钟预算，默认 600 秒。
- `max_steps` 默认值为 0，表示不限步；只在用户显式设置时作为安全保险丝。
- 已有 Agent 可维护的 session 级 todo 工具。
- 已有 ask_user 工具，需求歧义时可以主动暂停提问。
- `ask` 模式已有 `--auto-approve-tools` 白名单，减少重复确认。
- deadline 到期和用户中断工具执行时，会补齐 synthetic tool result，避免 session 留下未配对 tool_calls。
- `apply_patch` 支持 `dry_run=true`，可在不写文件的情况下预览 diff。

真实缺口：

- 当前 compaction 是本地确定性摘要，不调用 LLM 做语义总结。
- 还没有基于模型 context window 的 token 级阈值。
- 模型输出 `length` 截断、provider 异常等更细分场景的 synthetic result 还未完整覆盖。

## 阶段路线图

| 阶段 | 名称 | 状态 | 目标 |
|---|---|---|---|
| P0 | OMP 分析与 MVP 设计 | 已完成 | 明确不照搬 OMP，采用本地优先、封闭 VM 友好的简化架构。 |
| P1 | 基础 Agent Loop | 已完成 | CLI、Provider、Agent Runtime、基础工具、patch、memory、session、测试基线。 |
| P2 | 项目管理与可见性 | 已完成 | 建立 Excel + Markdown 项目状态，让目标、进度、风险、Todo 一目了然。 |
| P3 | 长任务运行基础 | 已完成 | 引入 deadline / budget-seconds、提高 max_steps 兜底值、todo、ask_user、per-tool approval。 |
| P4 | 上下文治理 | 已完成 MVP 版 | 初版 summary / compaction、工具输出折叠、长需求文件工作流。 |
| P5 | 安全与恢复增强 | 进行中 | synthetic tool result、patch preview、回滚策略、非信任仓库提示。 |
| P6 | 高级工程能力 | 暂缓 | LSP、DAP、TUI、subagents、reviewer、AST edit 等能力后置评估。 |

## 已完成功能

| 能力 | 状态 | 依据 |
|---|---|---|
| 项目骨架 | 已完成 | `pyproject.toml`、`src/local_agent/`、`tests/`、`docs/` 已存在。 |
| CLI | 已完成 | `src/local_agent/cli.py` 提供命令行入口。 |
| 配置加载 | 已完成 | `src/local_agent/config.py` 支持 provider、cwd、approval mode、session、max steps 等参数。 |
| 一键启动 | 已完成 | 仓库根目录 `./agent` 会自动设置 `PYTHONPATH=src` 并启动 CLI。 |
| `.env` 加载 | 已完成 | 当前 workspace 的 `.env` 可提供 `DASHSCOPE_API_KEY` 等本地配置。 |
| 百炼 Provider | 已完成 | 支持 `bailian`，默认 OpenAI-compatible endpoint 和 `qwen-plus`。 |
| Agent Runtime | 已完成 | `src/local_agent/agent.py` 实现模型调用、工具分发和循环。 |
| Tool Registry | 已完成 | `src/local_agent/tools/base.py` 管理工具、审批、异常包装。 |
| 文件工具 | 已完成 | `read_file`、`list_files`、`write_file` 已可用，写文件为 create-only。 |
| 搜索工具 | 已完成 | `search_code` 使用 `rg`，输出 workspace 相对路径并做总结果截断。 |
| Shell / Test 工具 | 已完成 | `shell`、`run_tests` 可用，执行类工具需要审批。 |
| Git 工具 | 已完成 | `git_status`、`git_diff` 可用，空 diff 时提示 untracked 文件。 |
| Anchored Patch | 已完成 | `apply_patch` 使用 tag、line、old_text 校验，并返回 diff。 |
| Patch Preview | 已完成 | `apply_patch dry_run=true` 复用 anchored 校验，只返回 diff，不写文件。 |
| Markdown Memory | 已完成 | `memory_read`、`memory_write` 写入项目级 Markdown 记忆。 |
| Session | 已完成 | JSONL session 支持继续会话，并处理坏尾部。 |
| 兼容性修复 | 已完成 | patch 读写使用 bytes，避免 Python 3.12 的 `newline` 参数问题。 |
| 错误处理修复 | 已完成 | 非交互审批和 LLM 非 JSON 响应已有明确错误路径。 |
| 时间预算 | 已完成 | `--budget-seconds` / `AGENT_BUDGET_SECONDS` 控制单次任务墙钟预算。 |
| 预算细粒度检查 | 已完成 | LLM 请求和 shell/run_tests timeout 会按剩余预算夹紧，tool 调用后也会检查 deadline。 |
| 不限步主循环 | 已完成 | `max_steps=0` 表示不限步，任务主要靠 `budget_seconds` 控制。 |
| Todo 工具 | 已完成 | `todo_read`、`todo_add`、`todo_update` 维护 session 级任务清单。 |
| 用户澄清工具 | 已完成 | `ask_user` 可在交互式终端中向用户提问。 |
| Per-tool approval | 已完成 | `--auto-approve-tools` / `AGENT_AUTO_APPROVE_TOOLS` 支持 ask 模式工具白名单。 |
| OMP 核心架构笔记 | 已完成 | `docs/omp-core-architecture-notes.md` 固化 OMP 主循环、deadline、compaction、stepCounter 结论。 |
| 本地 Context Compaction | 已完成 | 超过 `context_char_budget` 时折叠早期历史，保留最近消息，并注入未完成 todo。 |
| Synthetic Tool Result | 部分完成 | deadline 到期和用户中断工具执行时会补齐剩余 tool_call 的 tool result。 |
| 测试基线 | 已完成 | 本地正常环境下测试通过。 |

## 下一步 Todo

| ID | 任务 | 状态 | 优先级 | 说明 |
|---|---|---|---|---|
| T-001 | 确认项目管理基线 | 已完成 | P0 | Excel 已被人工复核，结论可信。 |
| T-002 | 建立 `docs/project-status.md` | 已完成 | P0 | 已将 Excel 内容转成 Agent 可读 Markdown，作为后续开发基线。 |
| T-003 | 创建初始 git commit | 已完成 | P0 | 初始提交已创建，作为后续开发可回滚基线。 |
| T-004 | 增加 `--budget-seconds` / deadline | 已完成 | P1 | 已支持 CLI、环境变量和配置文件中的墙钟预算。 |
| T-005 | 将 `max_steps` 调整为不限步保险丝 | 已完成 | P1 | 默认值为 0，表示不限步；日常任务预算交给 `budget_seconds`。 |
| T-006 | 增加 todo 工具 | 已完成 | P1 | Agent 可维护 session 级待办、进行中、已完成、阻塞、跳过状态。 |
| T-007 | 增加 ask_user 工具 | 已完成 | P1 | 需求不清时允许 Agent 在交互式终端中暂停并向用户提问。 |
| T-008 | 增加 per-tool approval policy | 已完成 | P2 | 已支持 ask 模式下按工具名免确认。 |
| T-009 | 更新 README 安全工作流 | 已完成 | P2 | 已明确 shell 不是沙箱，并补充预算和审批白名单说明。 |
| T-010 | 初版 context summary / compaction | 已完成 | P3 | 已实现本地确定性 compaction；超过字符预算时折叠早期历史并注入未完成 todo。 |
| T-011 | synthetic tool result | 部分完成 | P3 | deadline 到期和用户中断已补齐 tool_call 配对；模型 `length` 截断等场景待评估。 |
| T-012 | patch preview / rollback | 部分完成 | P4 | 已完成 `dry_run` 预览；rollback 最小设计待评估。 |
| T-013 | 评估 LSP / TUI / subagents / AST edit | 暂缓 | P5 | 高级能力，不进入第一阶段 MVP。 |
| T-014 | 固化 OMP 核心架构笔记 | 已完成 | P1 | 已新增 `docs/omp-core-architecture-notes.md`，避免重复翻 OMP 源码。 |
| T-015 | 简化一键启动命令 | 已完成 | P1 | 已新增 `./agent`；支持 `.env` token；默认当前目录为 workspace。 |
| T-016 | 细化 budget deadline 执行检查 | 已完成 | P1 | LLM/tool timeout 使用剩余预算；到期时为未执行工具补 synthetic result。 |
| T-017 | 处理模型输出截断的 synthetic result | 未开始 | P5 | 需要先在 LLM 层暴露 `finish_reason`，再为 `length` 截断补齐可恢复提示。 |

## 风险清单

| ID | 风险 | 状态 | 影响 | 应对 |
|---|---|---|---|---|
| R-001 | 仓库没有初始 commit | 已关闭 | 后续修改缺少稳定回滚基线。 | 已创建初始 commit。 |
| R-002 | 长任务上下文持续膨胀 | 已缓解 | 多轮工具调用后 token 成本和失败率上升。 | 已增加本地 context compaction；后续再做 token 级阈值和更强摘要。 |
| R-003 | 没有 todo 工具 | 已关闭 | 长需求中不容易追踪完成项和遗漏项。 | 已增加 session 级 todo 工具。 |
| R-004 | 没有 ask_user 工具 | 已关闭 | 遇到歧义时模型只能猜。 | 已增加 ask_user 工具。 |
| R-005 | ask 模式确认次数多 | 已缓解 | 日用体验偏慢。 | 已增加 per-tool approval 白名单；默认仍保持谨慎。 |
| R-006 | shell 工具不是安全沙箱 | 开放 | 命令可以越过 workspace 访问系统。 | 文档明确风险；封闭 VM 作为真正边界。 |
| R-007 | 恶意仓库 prompt injection | 开放 | 文件内容可能诱导模型执行不安全操作。 | 不信任仓库禁用 `yolo`，保留人工审批。 |
| R-008 | 中断时 tool_call 配对仍可增强 | 已缓解 | 恢复会话时可能遇到兼容性问题。 | deadline 和用户中断已补齐；输出截断场景后续处理。 |
| R-009 | ask_user 会阻塞等待用户 | 开放 | 带预算的长任务如果触发 ask_user，会等待人工输入。 | 长任务需求尽量写清；P4/P5 再评估 ask_user 超时策略。 |

## 架构决策

| ID | 决策 | 依据 |
|---|---|---|
| ADR-001 | 不照搬 OMP，只借鉴能力边界和关键机制。 | OMP 功能很完整，但第一阶段个人本地使用不需要 LSP、DAP、多 Agent、Browser 等复杂能力。 |
| ADR-002 | `max_steps` 只作为安全保险丝，不作为主要预算。 | OMP 主循环不靠步数终止，而靠模型是否继续请求工具、时间预算和上下文预算。 |
| ADR-008 | 默认不限步，默认使用时间预算。 | 避免 `100` 这类硬上限卡住真实任务；默认 `budget_seconds=600`，`max_steps=0`。 |
| ADR-009 | 固化 OMP 核心架构笔记。 | OMP 的主循环、deadline、compaction、synthetic tool result 等结论写入 `docs/omp-core-architecture-notes.md`，后续不再重复扫描。 |
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
- `read`、`state`、`interaction` tier 工具默认不额外审批；当前 `state` 用于 session todo，`interaction` 用于 `ask_user`。
- `yolo` 只用于完全可信仓库和封闭 VM。
- shell / run_tests / apply_patch 都应保留可审计日志。

## P5 开发入口

用户确认本文件后，建议按以下顺序继续：

1. 评估 patch rollback 的最小实现。
2. 在 LLM 层暴露 `finish_reason`，处理 `length` 截断时的 synthetic tool result。
3. 评估是否需要 LLM summary 或 token 级 compaction 阈值。
