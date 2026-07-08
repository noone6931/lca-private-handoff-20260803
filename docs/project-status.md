# Local Coding Agent 项目状态

更新时间：2026-07-07

本文档是开发 `local-coding-agent` 时给参与开发的人和协作 Agent 读取的项目管理基线。`docs/local-coding-agent-project-management.xlsx` 继续作为人工查看的表格视图；本 Markdown 文件作为后续开发时优先读取的项目状态、路线、Todo 和决策来源。它不是 LCA 运行时自己的 memory 或用户项目记忆。

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

第一阶段仍暂不做：

- 多 Agent 并行。
- 完整外部 LSP server / DAP。
- Browser 工具。
- 自动联网搜索。
- 远程仓库控制。
- AST 级复杂编辑。

## 当前进度

当前项目处于 P7 轻量高级能力阶段：P5 的安全与恢复增强 MVP 已收口；P6 默认工作流 MVP 已落地，用户可以用自然语言描述任务，而不是每次手写 `list_files/read_file/dry_run/run_tests/git_diff` 工具顺序。本轮已补 OMP 风格 auto summary、多语言轻量 LSP、multi-root `--allow-dir`、Markdown memory 启动注入、`learn` 工具和 authored skills discovery。

已具备的核心能力：

- Python 标准项目结构：`pyproject.toml`、`src/`、`tests/`、`docs/`。
- CLI 入口：推荐 `./agent`，安装后可用 `local-agent`，源码入口仍是 `python3 -m local_agent.cli`。
- 支持 `bailian` provider，对接阿里云百炼 OpenAI-compatible API。
- Agent Runtime 已支持工具调用循环。
- 工具注册、schema 校验、审批模式已经可用。
- 文件读取、目录浏览、代码搜索、shell、测试、git 状态、git diff、anchored patch、patch rollback、Markdown memory、learn 已经可用。
- `apply_patch` 已支持 `replace`、`insert_before`、`insert_after`，并兼容 Python 3.12。
- 非交互审批、LLM 非 JSON 响应、session 恢复坏尾部、search_code 绝对路径泄漏等问题已经修复。
- 已完成 Agent 自举测试：能够通过百炼模型调用工具读取、修改、测试和查看 diff。
- 测试基线：118 个测试在正常本地环境通过。

当前已具备：

- 仓库已有初始 git commit。
- 长任务已有初版 `--budget-seconds` 墙钟预算，默认 600 秒。
- `max_steps` 默认值为 0，表示不限步；只在用户显式设置时作为安全保险丝。
- 已有 Agent 可维护的 session 级 todo 工具。
- 已有 ask_user 工具，需求歧义时可以主动暂停提问，并支持 `timeout_seconds` / `default_answer`；显式 timeout 会被当前 budget 剩余时间夹紧。
- 审批模型已支持 `always-ask` / `write` / `yolo`，并支持每工具 `allow` / `prompt` / `deny` 和 session 内 always allow / always reject。
- approval prompt 会按 `budget_seconds` 剩余时间等待输入；deadline 到期会取消工具调用并返回 tool error。
- deadline 到期、用户中断工具执行、模型输出 `length` 截断时，会补齐 synthetic tool result，避免 session 留下未配对 tool_calls。
- `apply_patch` 支持 `dry_run=true`，可在不写文件的情况下预览 diff。
- `rollback_patch` 可回滚当前 session 中由 `apply_patch` 写入的补丁，并在回滚前校验当前文件 hash。
- OMP 默认工作流源码依据已固化：system prompt、project prompt、tool registry、tool descriptions、todo reminders、ToolChoiceQueue、agent-loop 的具体实现已写入 `docs/omp-core-architecture-notes.md`。
- LCA 默认工作流已沉到 system prompt 和 runtime workflow reminder：自然语言代码任务会默认先理解、必要时 todo、修改前读取、patch 写入、修改后测试和 diff。
- OMP 风格 auto summary 已落地：默认 `--summary-mode auto`，小历史不摘要，超过 reserve 阈值后调用当前 provider 生成语义摘要，失败回退本地摘要；`local` / `llm` 仍可显式指定。
- 轻量 LSP 风格工具已落地：`lsp_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics`，覆盖 Python、Java、JavaScript、TypeScript、Vue，不启动外部语言服务器。
- Multi-root workspace 已落地：`--allow-dir` / `AGENT_ALLOWED_DIRS` 可显式授权额外目录给文件、搜索、LSP 和 patch 工具；shell、git、session、todo、memory 仍锚定 `--cwd`。
- Markdown memory 启动注入已落地：新 session 会读取 `.local-agent/memory/{project,decisions,conventions,learned}.md` 并作为 advisory context 注入。
- `learn` 工具已落地：可把可复用经验写入 `.local-agent/memory/learned.md`，默认仍按写工具审批。
- Authored skills discovery 已落地：新 session 会扫描 `.local-agent/skills/<name>/SKILL.md`，只注入 name、description 和 source path，正文按需读取。
- OMP memory / skills / autolearn 设计已核实并形成 LCA 裁剪方案：见 `docs/memory-skills-implementation-plan.md`。

真实缺口：

- Managed skills / autolearn 继续暂缓。
- 百炼真实只读压测会话 `20260707T093557800154Z` 已验证：在 `context_char_budget=2500` 的强压缩场景下，模型完成指定 5 个工具调用后停止探索，并按要求输出三句话总结。
- 百炼真实小改复测会话 `20260707T094246132064Z` 已验证 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff 主链路可跑通；最终仅新增一个测试 docstring。
- 还没有基于模型 context window 的精确 token 预算；当前用字符窗口近似 OMP reserve 策略。
- LSP 目前是多语言轻量静态工具，不是完整 LSP server，不支持 rename / code action / DAP。
- provider 请求失败发生在 assistant tool_call 之前，当前会以 `LlmError` 停止；后续可继续优化用户提示。

## 阶段路线图

| 阶段 | 名称 | 状态 | 目标 |
|---|---|---|---|
| P0 | OMP 分析与 MVP 设计 | 已完成 | 明确优先吸收 OMP 成熟设计，并按本地优先、封闭 VM 友好和 MVP 边界做裁剪。 |
| P1 | 基础 Agent Loop | 已完成 | CLI、Provider、Agent Runtime、基础工具、patch、memory、session、测试基线。 |
| P2 | 项目管理与可见性 | 已完成 | 建立 Excel + Markdown 项目状态，让目标、进度、风险、Todo 一目了然。 |
| P3 | 长任务运行基础 | 已完成 | 引入 deadline / budget-seconds、提高 max_steps 兜底值、todo、ask_user、per-tool approval。 |
| P4 | 上下文治理 | 已完成 MVP 版 | 初版 summary / compaction、工具输出折叠、长需求文件工作流。 |
| P5 | 安全与恢复增强 | 已完成并收口 | synthetic tool result、patch preview、回滚策略、非信任仓库提示、OMP 风格 approval model、approval prompt deadline cancel；真实小改复测通过。 |
| P6 | 日用体验与默认工作流固化 | 已完成 MVP 版 | OMP 默认工作流本地化：system prompt、工具描述、轻量 runtime nudge。 |
| P7 | 高级工程能力轻量版 | 进行中 | 已完成 OMP 风格 auto summary、多语言轻量 LSP、multi-root、Markdown memory 启动注入、learn 和 authored skills discovery；DAP、TUI、subagents、reviewer、AST edit、managed skills 继续后置。 |

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
| Patch Rollback | 已完成 MVP 版 | `rollback_patch` 只回滚本 session 的 patch 记录，且要求当前文件仍匹配 after tag。 |
| Markdown Memory | 已完成 | `memory_read`、`memory_write` 写入项目级 Markdown 记忆。 |
| Session | 已完成 | JSONL session 支持继续会话，并处理坏尾部。 |
| 兼容性修复 | 已完成 | patch 读写使用 bytes，避免 Python 3.12 的 `newline` 参数问题。 |
| 错误处理修复 | 已完成 | 非交互审批和 LLM 非 JSON 响应已有明确错误路径。 |
| 时间预算 | 已完成 | `--budget-seconds` / `AGENT_BUDGET_SECONDS` 控制单次任务墙钟预算。 |
| 预算细粒度检查 | 已完成 | LLM 请求和 shell/run_tests timeout 会按剩余预算夹紧，tool 调用后也会检查 deadline。 |
| 不限步主循环 | 已完成 | `max_steps=0` 表示不限步，任务主要靠 `budget_seconds` 控制。 |
| Todo 工具 | 已完成 | `todo_read`、`todo_add`、`todo_update` 维护 session 级任务清单。 |
| 用户澄清工具 | 已完成 | `ask_user` 可在交互式终端中向用户提问，支持超时、默认答案和 budget 上限；显式 timeout 也会被剩余 budget 夹紧。 |
| Per-tool approval | 已完成 | 支持 `always-ask` / `write` / `yolo`、`--tool-approval`、旧白名单兼容映射、config prompt/deny 硬护栏、REPL 工具名校验和 approval deadline cancel。 |
| OMP 核心架构笔记 | 已完成 | `docs/omp-core-architecture-notes.md` 固化 OMP 主循环、deadline、compaction、stepCounter、tool approval、默认工作流分层结论。 |
| OMP 默认工作流源码依据 | 已完成 | 已记录 system prompt、project prompt、tool registry、tool descriptions、todo reminders、ToolChoiceQueue、agent-loop 如何让用户不用指定工具顺序。 |
| 本地 Context Compaction | 已完成 | 超过 `context_char_budget` 时折叠早期历史，保留最近消息和当前用户请求，注入未完成 todo，截断发送给模型的超大 tool 输出，并保持单 system 消息。 |
| OMP 风格 Auto Summary | 已完成 MVP 版 | 默认 `--summary-mode auto`；小历史不摘要，超过 reserve 阈值后调用当前 provider 总结早期历史；失败回退 local summary。 |
| 默认工作流 | 已完成 MVP 版 | system prompt 固化探索、todo、ask_user、patch preview、验证和 diff；runtime workflow reminder 会注入非平凡代码任务。 |
| 轻量 LSP 工具 | 已完成 MVP 版 | `lsp_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics` 支持 Python、Java、JavaScript、TypeScript、Vue。 |
| Multi-root Workspace | 已完成 MVP 版 | `--allow-dir` / `AGENT_ALLOWED_DIRS` 支持显式授权额外目录给文件、搜索、LSP、patch 工具。 |
| Markdown Memory 启动注入 | 已完成 MVP 版 | `.local-agent/memory/{project,decisions,conventions,learned}.md` 会作为 advisory context 注入 system prompt。 |
| Learn 工具 | 已完成 MVP 版 | `learn` 写入 `.local-agent/memory/learned.md`，用于显式沉淀可复用经验。 |
| Authored Skills Discovery | 已完成 MVP 版 | `.local-agent/skills/<name>/SKILL.md` 启动时只注入 name、description、source path，正文按需读取。 |
| Memory / Skills 方案 | 已完成设计 | `docs/memory-skills-implementation-plan.md` 明确 Markdown memory 注入、`learn`、skills discovery、managed skills/autolearn 的分阶段方案。 |
| Synthetic Tool Result | 已完成 MVP 版 | deadline 到期、用户中断、`finish_reason=length` 时会补齐剩余 tool_call 的 tool result。 |
| 测试基线 | 已完成 | 本地正常环境下 118 个测试通过。 |

## 下一步 Todo

| ID | 任务 | 状态 | 优先级 | 说明 |
|---|---|---|---|---|
| T-001 | 确认项目管理基线 | 已完成 | P0 | Excel 已被人工复核，结论可信。 |
| T-002 | 建立 `docs/project-status.md` | 已完成 | P0 | 已将 Excel 内容转成开发协作 Agent 可读 Markdown，作为后续开发基线。 |
| T-003 | 创建初始 git commit | 已完成 | P0 | 初始提交已创建，作为后续开发可回滚基线。 |
| T-004 | 增加 `--budget-seconds` / deadline | 已完成 | P1 | 已支持 CLI、环境变量和配置文件中的墙钟预算。 |
| T-005 | 将 `max_steps` 调整为不限步保险丝 | 已完成 | P1 | 默认值为 0，表示不限步；日常任务预算交给 `budget_seconds`。 |
| T-006 | 增加 todo 工具 | 已完成 | P1 | Agent 可维护 session 级待办、进行中、已完成、阻塞、跳过状态。 |
| T-007 | 增加 ask_user 工具 | 已完成 | P1 | 需求不清时允许 Agent 在交互式终端中暂停并向用户提问。 |
| T-008 | 增加 per-tool approval policy | 已完成 | P2 | 已支持 ask 模式下按工具名免确认，并支持 allow / prompt / deny。 |
| T-009 | 更新 README 安全工作流 | 已完成 | P2 | 已明确 shell 不是沙箱，并补充预算和审批白名单说明。 |
| T-010 | 初版 context summary / compaction | 已完成 | P3 | 已实现本地确定性 compaction；超过字符预算时折叠早期历史并注入未完成 todo。 |
| T-011 | synthetic tool result | 已完成 MVP 版 | P3 | deadline 到期、用户中断和模型 `length` 截断已补齐 tool_call 配对。 |
| T-012 | patch preview / rollback | 已完成 MVP 版 | P4 | 已完成 `dry_run` 预览和 session 级 hash 校验 rollback。 |
| T-013 | 评估 LSP / TUI / subagents / AST edit | 已部分完成 | P5/P7 | 轻量 LSP 已做；TUI、subagents、AST edit、DAP 继续后置。 |
| T-014 | 固化 OMP 核心架构笔记 | 已完成 | P1 | 已新增 `docs/omp-core-architecture-notes.md`，避免重复翻 OMP 源码。 |
| T-015 | 简化一键启动命令 | 已完成 | P1 | 已新增 `./agent`；支持 `.env` token；默认当前目录为 workspace。 |
| T-016 | 细化 budget deadline 执行检查 | 已完成 | P1 | LLM/tool timeout 使用剩余预算；到期时为未执行工具补 synthetic result。 |
| T-017 | 处理模型输出截断的 synthetic result | 已完成 | P5 | LLM 层已暴露 `finish_reason`，`length` 截断会补齐 synthetic tool result 并停止。 |
| T-018 | ask_user timeout / default | 已完成 | P5 | `ask_user` 支持 `timeout_seconds`、`default_answer`，显式 timeout 也受当前 budget 剩余时间约束。 |
| T-019 | tool_approval allow / prompt / deny | 已完成 | P5 | 支持配置每个工具 allow、prompt、deny；旧 auto approve 白名单兼容映射为 allow。 |
| T-020 | approvalMode / session decision / REPL commands | 已完成 | P5 | 支持 `always-ask` / `write` / `yolo`、session allow/reject always、REPL `/approval` 命令。 |
| T-021 | approval prompt deadline / abort | 已完成 MVP 版 | P5 | approval prompt 使用 deadline-aware timed stdin；deadline 已过或等待超时会取消工具调用，保留 `y/s/n/d` 和 session allow/reject。 |
| T-022 | approval 优先级和工具名校验修复 | 已完成 | P5 | 新 `tools.*` 配置优先于旧顶层字段；config prompt/deny 不被 session allow 绕过；REPL 未知工具名会报错。 |
| T-023 | ask_user timeout clamp / compaction tool truncation | 已完成 | P5 | 显式 `timeout_seconds` 会被剩余 budget 夹紧；recent tool 输出只在发送模型副本中截断，session 原文保留。 |
| T-024 | compaction 保持单 system 消息 | 已完成 | P5 | 压缩摘要合并进首个 system prompt，降低 OpenAI-compatible provider 对多 system 消息的兼容风险。 |
| T-025 | 百炼只读压测后的目标漂移修复 | 已完成 | P5 | 真实百炼压测确认 provider 接受 compaction，但极小上下文预算下模型会被续读提示带偏；已强保留当前用户请求并弱化 read_file 续读提示。 |
| T-026 | 复测百炼只读 compaction 压测 | 已完成 | P5 | 会话 `20260707T093557800154Z` 严格完成 5 个指定工具调用后输出三句话总结，未继续额外读文件。 |
| T-027 | 真实小改任务压测 | 已完成 | P5 | 复测会话 `20260707T094246132064Z` 跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff；最终仅新增一个测试 docstring。 |
| T-028 | 修正 `write_file` schema 描述误导 | 已完成 | P5 | `write_file` 描述已改为 create-only，并新增测试确保描述不再宣称 `fully overwrite`。 |
| T-029 | P5 收口检查 | 已完成 | P5 | README 已补日用模板；项目状态和 Excel 已同步；90 个测试、compileall、xlsx、diff check 通过。 |
| T-030 | P6 取舍评估 | 已完成首轮 | P6 | 已决定优先做 OMP 默认工作流本地化；随后按用户要求补 LLM summary 和轻量 LSP。 |
| T-031 | 固化 OMP 默认工作流源码依据 | 已完成 | P6 | `docs/omp-core-architecture-notes.md` 已新增“OMP 如何让用户不用指定工具顺序”，引用具体源码文件。 |
| T-032 | 固化 LCA 默认工作流 system prompt | 已完成 MVP 版 | P6 | 已把理解、修改、验证、todo、ask_user、patch preview、diff 的默认规则写入系统提示，并用测试覆盖 runtime reminder。 |
| T-033 | 增强工具描述与真实能力一致性 | 已完成 MVP 版 | P6 | 新增 LSP 工具描述；既有 create-only `write_file`、patch dry_run 等描述与实现保持一致并有测试。 |
| T-034 | 实现轻量 runtime workflow nudge | 已完成 MVP 版 | P6 | 非平凡代码任务会注入 runtime workflow reminder；短 prompt 如“只回答 OK”不会注入。 |
| T-035 | 评估 multi-root workspace allow-dir | 已完成 MVP 版 | P6 | 支持读取需求文档目录并修改另一个代码 workspace；`--allow-dir` / `AGENT_ALLOWED_DIRS` 已落地。 |
| T-036 | 实现 OMP 风格 auto summary | 已完成 MVP 版 | P7 | 默认 `summary_mode=auto`，按 reserve 阈值触发 LLM 摘要，空结果或 LLM 错误会回退本地摘要。 |
| T-037 | 实现轻量 LSP 工具 | 已完成 MVP 版 | P7 | 不启动外部 server，使用 AST/静态扫描提供 Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics。 |
| T-038 | 固化 Memory / Skills 方案 | 已完成 | P7 | 已新增 `docs/memory-skills-implementation-plan.md`，并在 OMP 架构笔记补充 memory backend、learn、managed skills、skills discovery。 |
| T-039 | Markdown memory 启动注入 | 已完成 MVP 版 | P7 | 读取 `.local-agent/memory/*.md` 并以 advisory block 注入 system prompt，带 source path 和字符预算。 |
| T-040 | 实现 `learn` 工具 | 已完成 MVP 版 | P7 | 把可复用 lesson 写入 `.local-agent/memory/learned.md`，限制长度并清洗会进入 prompt 的字段。 |
| T-041 | Authored skills discovery | 已完成 MVP 版 | P7 | 先扫 `.local-agent/skills/<name>/SKILL.md`，system prompt 只列 name / description / source path，正文按需读取。 |
| T-042 | Managed skills / autolearn | 暂缓 | P7 | 默认关闭，后续按 OMP 风格加入 `manage_skill`，generated skills 与 authored skills 隔离且优先级最低。 |

## 风险清单

| ID | 风险 | 状态 | 影响 | 应对 |
|---|---|---|---|---|
| R-001 | 仓库没有初始 commit | 已关闭 | 后续修改缺少稳定回滚基线。 | 已创建初始 commit。 |
| R-002 | 长任务上下文持续膨胀 | 已进一步缓解，继续增强 | 多轮工具调用后 token 成本和失败率上升。 | 已增加 OMP 风格 reserve 阈值、auto LLM summary、当前用户请求保留、超大 tool 输出截断和单 system 摘要合并；后续再评估精确 token 预算。 |
| R-011 | 工具 schema 描述与实现不一致 | 已关闭首例，持续关注 | 模型会相信工具描述并据此修改文档或代码，错误 schema 会直接造成错误结果。 | 已修正 `write_file` 描述并新增测试；后续压测继续关注 schema/实现一致性。 |
| R-003 | 没有 todo 工具 | 已关闭 | 长需求中不容易追踪完成项和遗漏项。 | 已增加 session 级 todo 工具。 |
| R-004 | 没有 ask_user 工具 | 已关闭 | 遇到歧义时模型只能猜。 | 已增加 ask_user 工具。 |
| R-005 | ask 模式确认次数多 | 已缓解 | 日用体验偏慢。 | 已增加 per-tool approval 白名单和 allow / prompt / deny 策略；默认仍保持谨慎。 |
| R-006 | shell 工具不是安全沙箱 | 开放 | 命令可以越过 workspace 访问系统。 | 文档明确风险；封闭 VM 作为真正边界。 |
| R-007 | 恶意仓库 prompt injection | 开放 | 文件内容可能诱导模型执行不安全操作。 | 不信任仓库禁用 `yolo`，保留人工审批。 |
| R-008 | 中断时 tool_call 配对仍可增强 | 已关闭 MVP 版 | 恢复会话时可能遇到兼容性问题。 | deadline、用户中断和输出截断已补齐。 |
| R-009 | ask_user 会阻塞等待用户 | 已缓解 | 带预算的长任务如果触发 ask_user，会等待人工输入。 | 已支持 `timeout_seconds` / `default_answer`，并自动受剩余 budget 约束；显式 timeout 也会被剩余 budget 夹紧。 |
| R-010 | approval prompt 等待耗尽预算 | 已关闭 MVP 版 | 用户长时间不确认工具调用时，确认后工具可能执行成功，但下一次 deadline 检查立刻停止。 | approval prompt 已按剩余 deadline 等待 stdin；deadline 到期直接取消并返回 tool error。 |
| R-012 | 日用命令仍依赖用户手写工具流程 | 已关闭 MVP 版 | 用户不应每次提示“先 list/read，再 dry_run，再 test/diff”；否则 LCA 更像压测脚本而不是本地编程助手。 | 已采纳 OMP 分层设计：system prompt 固化默认流程，tool descriptions 说明工具规范，runtime nudge 做轻量纠偏。 |
| R-013 | Memory / skills 注入长期 prompt injection 或陈旧事实 | 已缓解，managed skills 仍暂缓 | memory 和 generated skills 会跨 session 影响模型，错误或恶意内容可能持续放大。 | memory 和 authored skills 注入区已标注 advisory；已设置注入预算并清洗 learned / skill description 字段；managed skills 默认关闭且 authored skills 优先。 |

## 架构决策

| ID | 决策 | 依据 |
|---|---|---|
| ADR-001 | 优先采纳 OMP 成熟设计，按本地目标裁剪。 | 不为了“避免复制”而绕开好设计；判断标准是收益是否大于复杂度，并且不破坏个人本地使用、封闭 VM、无公网依赖和第一阶段 MVP 边界。 |
| ADR-002 | `max_steps` 只作为安全保险丝，不作为主要预算。 | OMP 主循环不靠步数终止，而靠模型是否继续请求工具、时间预算和上下文预算。 |
| ADR-008 | 默认不限步，默认使用时间预算。 | 避免 `100` 这类硬上限卡住真实任务；默认 `budget_seconds=600`，`max_steps=0`。 |
| ADR-009 | 固化 OMP 核心架构笔记。 | OMP 的主循环、deadline、compaction、synthetic tool result 等结论写入 `docs/omp-core-architecture-notes.md`，后续不再重复扫描。 |
| ADR-010 | P6 优先实现 OMP 默认工作流的本地 MVP 版。 | 已直接采纳 OMP 的分层设计：系统上下文、工具描述、runtime 纠偏共同让用户不用指定工具顺序；完整 ToolChoiceQueue、subagents 等复杂能力继续后置。 |
| ADR-011 | 默认采用 OMP 风格 auto summary。 | 小历史不摘要；超过 reserve 阈值才调用已配置 AI API 做 LLM summary；失败回退 local summary；`local` / `llm` 仍可显式指定。 |
| ADR-012 | LSP 第一版做轻量多语言静态工具。 | 满足 Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics，不引入外部 language server、npm/pip 依赖或后台进程；完整 LSP/DAP 后置。 |
| ADR-013 | Memory / skills 按 OMP 思路分阶段本地化。 | Markdown memory 启动注入、显式 `learn` 和 authored skills discovery 已落地；最后才评估 managed skills/autolearn；不引入 Hindsight、Mnemopi、向量库或插件市场。 |
| ADR-003 | Excel 作为人工视图，Markdown 作为开发协作 Agent 可读事实源。 | 这套文档服务于开发 LCA 的过程；`.xlsx` 是二进制展示产物，不适合作为协作 Agent 的事实源。 |
| ADR-004 | 第一阶段 memory 使用 Markdown。 | Markdown 简单、可审计、封闭 VM 友好；暂不引入 SQLite 或向量库。 |
| ADR-005 | 第一阶段使用 anchored patch，不做 AST edit。 | hash + old_text + line 校验已经足够支撑 MVP 的可控修改。 |
| ADR-006 | 长需求应写入文件，让 Agent 读取。 | 直接把大段需求塞进 prompt 会挤占上下文，不利于长任务。 |
| ADR-007 | 封闭 VM 目标优先于联网能力。 | 第一阶段只允许访问指定 AI API，不引入公网搜索和自动下载依赖。 |

## P5 收口结论

| 项目 | 结论 | 依据 |
|---|---|---|
| 主链路 | 通过 | 百炼真实小改复测已跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff。 |
| 测试 | 通过 | P5 收口时 90 个 unittest、compileall、xlsx 检查、diff check 均通过；P7 当前代码已跑通 118 个 unittest 和 compileall。 |
| 日用入口 | 通过 | README 已补只读分析和小改任务命令模板。 |
| 开放风险 | 可接受 | shell 仍非沙箱、prompt injection 仍需靠审批和封闭 VM；token budget / output reserve / managed skills 留到后续评估。 |
| 下一阶段 | P7 轻量高级能力真实压测 | 用真实需求验证默认工作流、auto summary、轻量 LSP、multi-root、startup memory、learn 和 authored skills 是否足够日用。 |

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

- 默认使用 `always-ask`。旧的 `ask` / `auto-read` 会兼容映射为 `always-ask`。
- 需要允许写文件但继续管住命令执行时，可以使用 `write`。
- `read`、`state`、`interaction` tier 工具默认不额外审批；当前 `state` 用于 session todo，`interaction` 用于 `ask_user`。
- `yolo` 只用于完全可信仓库和封闭 VM。
- `--tool-approval tool=allow|prompt|deny` 可覆盖单个工具；`prompt` / `deny` 是配置级护栏，不被 session allow 绕过；REPL 中可用 `/approval` 临时调整当前会话策略。
- approval prompt 会按剩余 `budget_seconds` 等待输入；超时会取消工具调用并回传 tool error。
- shell / run_tests / apply_patch 都应保留可审计日志。

## 下一步开发入口

用户确认本文件后，建议按以下顺序继续：

1. 用百炼跑一次真实任务，验证默认工作流是否不再需要用户手写工具顺序。
2. 用默认 `--summary-mode auto --context-char-budget` 跑一次长上下文压测，验证 OMP 风格 auto summary 的真实 provider 兼容性。
3. 用 `--allow-dir` 跑一次“需求文档目录 + 代码项目目录”的真实工作流。
4. 让 Agent 在 Python/Java/Vue/TS 项目里主动调用 `lsp_definition` / `lsp_references` / `lsp_diagnostics`，验证轻量多语言 LSP 工具是否能改善定位效率。
5. 验证 startup memory 和 `learn` 是否能减少重复交代项目约定。
