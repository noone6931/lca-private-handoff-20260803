# Local Coding Agent 项目状态

更新时间：2026-07-08

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

当前项目处于 P7 轻量高级能力阶段：P5 的安全与恢复增强 MVP 已收口；P6 默认工作流 MVP 已落地，用户可以用自然语言描述任务，而不是每次手写 `list_files/read_file/dry_run/run_tests/git_diff` 工具顺序。本轮已补 OMP 风格 auto summary、多语言轻量 LSP、multi-root `--allow-dir`、workspace roots 注入、Markdown memory 启动注入、`learn` 工具、可选 session memory consolidation、authored skills discovery、重复工具调用熔断、duplicate-tool forced-final steering、tool result pruning、todo steering、跨项目 `--env-file` / launcher 安装目录 `.env` 加载、OMP 风格用户级 `--state-dir` runtime state 分层，以及 Evidence Ledger 证据账本。

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
- 测试基线：153 个测试在正常本地环境通过。

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
- 轻量 LSP 风格工具已落地：`lsp_symbols`、`lsp_workspace_symbols`、`lsp_document_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics`，覆盖 Python、Java、JavaScript、TypeScript、Vue，不启动外部语言服务器；workspace/document symbols 是兼容别名。
- Multi-root workspace 已落地：`--allow-dir` / `AGENT_ALLOWED_DIRS` 可显式授权额外目录给文件、搜索、LSP 和 patch 工具；system prompt、`list_files` 根目录输出、path-not-found 错误和带 allowed-dir 的空搜索会列出 primary `--cwd` 和 allowed dirs；需求/文档类任务会触发 OMP 风格 soft tool requirement，先要求 `read_file` allowed-dir 文档，再释放完整工具集；shell、git、显式项目 memory/skills 仍锚定 `--cwd`，session/todo/patch logs 和默认 consolidation memory 走 state dir。
- 跨项目 env-file 已落地：CLI 支持显式 `--env-file`，`./agent` 会自动把 LCA 安装目录 `.env` 作为 env-file 加载，使 token/provider 配置与目标 `--cwd` 解耦。优先级是：真实环境变量 > 显式 env-file > 目标 workspace `.env`。
- 用户级 / 项目级常驻上下文已落地：新 session 会读取用户级 `AGENTS.md` 和项目级 `.local-agent/AGENTS.md`，作为 advisory context 注入。
- Sticky rules 已落地：每次发送模型请求前会读取用户级 `RULES.md` 和项目级 `.local-agent/RULES.md`，用于短规则在长会话中保持可见。
- Markdown memory 启动注入已落地：新 session 会读取项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/{project,decisions,conventions,learned}.md` 并作为 advisory context 注入。
- `learn` 工具已落地：可把可复用经验写入 `.local-agent/memory/learned.md`，默认仍按写工具审批。
- Memory consolidation 已落地 MVP：默认 `off`；显式 `--memory-consolidation auto|llm` 后，一轮结束时从 session 中抽取长期 project/decisions/conventions/learned；默认 `--memory-scope state` 写 state dir `memory/*.md`，显式 `project` 才写 `.local-agent/memory/*.md`。
- 已完成 memory consolidation review：默认 `off` 不会额外调用 LLM，也不会写 memory；已补 runtime 级回归测试覆盖默认 off、默认 state scope 和显式 project scope。
- Authored skills discovery 已落地：新 session 会扫描 `.local-agent/skills/<name>/SKILL.md`，只注入 name、description 和 source path，正文按需读取。
- OMP memory / skills / autolearn 设计已核实并形成 LCA 裁剪方案：见 `docs/memory-skills-implementation-plan.md`。
- P7 综合压测记录已落地：见 `docs/pressure-test-2026-07-08.md`。
- 重复工具调用熔断和 forced-final steering 已落地：最近窗口内同名同参工具调用超过阈值会返回 tool error；重复命中后 runtime 会注入 steering，并让下一次 LLM 请求 `tools=[]`，强制模型基于已有证据输出最终回答；连续命中仍有硬停兜底。
- 同文件连续切片读取漂移 guard 已落地：只读/分析类任务中，近期同一路径 `read_file` 超阈值后会返回 tool error，并强制下一轮无工具最终回答，避免长任务偏成“只总结最后一个大文件”；编辑类任务不触发。
- OMP 风格 tool result pruning / todo steering 已落地：空搜索/LSP 结果会标记 useless；发送给模型的上下文会折叠 useless/superseded 工具结果并注入未完成 todo reminder，session 原文仍保留。
- Path escape roots hint 已落地：越界路径错误会返回 resolved path、primary workspace 和 allowed dirs；父目录误用时提示使用 `.` 或精确 `--cwd`。
- LSP symbol 空 query guard 已落地：连续一批 `lsp_symbols` / `lsp_workspace_symbols` / `lsp_document_symbols` 无结果后跳过并 forced-final；有命中则清空该批空探索计数。
- Current task contract 已落地：每次 provider request 注入当前原始用户请求、最终输出结构约束和 evidence-backed path 规则，防止长工具链后只总结最后一个文件或把猜测路径当证据。
- Evidence Ledger 已落地：runtime 从工具结果中央提炼本轮短证据账本，provider request 注入 `[Evidence ledger]`，并写入 session JSONL `evidence` 事件，帮助最终回答区分证据事实和推断。

真实缺口：

- Path-scoped rules 还未实现，作为下一步候选。
- Managed skills / autolearn 继续暂缓。
- 企业项目联网压测：当前 full-access + network enabled 环境已可由 Agent 代跑。单项目压测 session `20260708T083312934017` 已按 5 点结构收束；多项目压测连续暴露 path escape 父目录误用、LSP 空 query 扩散和最终回答结构漂移，已分别补 path escape roots hint、LSP 空 query guard、Current task contract；session `20260708T085927874078` 已按 6 点结构输出，并定位 `CrclLimitMainBySelectController.limitConstituteAllotImport`、`CrclLimitMainBySelectApplication.limitConstituteAllotImport`、`LimitConstituteAllotImportReq`、`BatchImportConstituteDto` 等真实证据。最新 LCA 自身小实现压测 session `20260708T092554037057Z` 已验证 Evidence Ledger 后小改闭环仍可跑通，并暴露 `path#tag` 易误填、脏工作区 diff 归因两个后续问题。
- 用户确认当前测试项目可能无法完全覆盖需求，尤其“拓展服务费结算”可能需要其他项目配合；后续跨服务需求应把相关项目也作为 `--allow-dir`，或让 Agent 明确输出需要补充的项目/服务。
- Runtime state 与 workspace 已解耦：`--state-dir` / `AGENT_STATE_DIR` 可指定用户级 state root；默认 `${XDG_STATE_HOME:-~/.local/state}/local-coding-agent/workspaces/<workspace-key>/`；sessions/todos/patch logs 已不再默认写入目标 `--cwd/.local-agent`。显式项目 memory/skills 仍保留在 workspace 中，自动 consolidation 默认写 state dir。
- 已对 `/Users/chengming/mycode/project/crcl-open/crcl-open` 做本地 state-dir 验证：默认 state dir 为 `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-crcl-open-966d4fe7a33b`，目标仓库当前未发现 `.local-agent`。
- 百炼真实只读压测会话 `20260707T093557800154Z` 已验证：在 `context_char_budget=2500` 的强压缩场景下，模型完成指定 5 个工具调用后停止探索，并按要求输出三句话总结。
- LCA 自身综合压测会话 `20260708T024203733199Z` 暴露重复工具调用循环，已用窗口式重复工具熔断缓解；修复后复测会话 `20260708T025519414693Z` 已按要求完成工具调用并输出总结。企业压测 session `20260708T062614211387Z` 又暴露“硬停但无最终回答”，因此新增 forced-final steering。
- 百炼真实小改复测会话 `20260707T094246132064Z` 已验证 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff 主链路可跑通；最终仅新增一个测试 docstring。
- 还没有基于模型 context window 的精确 token 预算；当前用字符窗口近似 OMP reserve 策略。
- 还没有完整 OMP ToolChoiceQueue；当前只为 allowed-dir 需求文档读取实现了轻量 soft tool requirement，其余场景仍用 system/tool 描述、runtime reminder、todo reminder、pruning、重复工具熔断和 forced-final steering 做本地版。
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
| P7 | 高级工程能力轻量版 | 进行中 | 已完成 OMP 风格 auto summary、多语言轻量 LSP、LSP 兼容别名、multi-root workspace roots 与工具观察提示、allowed-dir soft tool requirement、Markdown memory 启动注入、learn、可选 memory consolidation、authored skills discovery、综合压测记录、重复工具调用熔断、duplicate-tool forced-final steering、同文件切片读取漂移 guard、search_code 空搜索词跨路径 guard、path escape roots hint、LSP 空 query guard、Current task contract、Evidence Ledger、tool result pruning、todo steering、跨项目 env-file 和用户级 `--state-dir` runtime state 分层；path-scoped rules、DAP、TUI、subagents、reviewer、AST edit、managed skills 继续后置。 |

## 已完成功能

| 能力 | 状态 | 依据 |
|---|---|---|
| 项目骨架 | 已完成 | `pyproject.toml`、`src/local_agent/`、`tests/`、`docs/` 已存在。 |
| CLI | 已完成 | `src/local_agent/cli.py` 提供命令行入口。 |
| 配置加载 | 已完成 | `src/local_agent/config.py` 支持 provider、cwd、approval mode、session、max steps 等参数。 |
| 一键启动 | 已完成 | 仓库根目录 `./agent` 会自动设置 `PYTHONPATH=src` 并启动 CLI。 |
| `.env` / `--env-file` 加载 | 已完成 | 当前 workspace 的 `.env` 可提供 `DASHSCOPE_API_KEY` 等本地配置；`./agent` 会自动加载安装目录 `.env`，也可显式传 `--env-file`。 |
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
| 轻量 LSP 工具 | 已完成 MVP 版 | `lsp_symbols`、`lsp_workspace_symbols`、`lsp_document_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics` 支持 Python、Java、JavaScript、TypeScript、Vue；workspace/document symbols 是兼容别名。 |
| Multi-root Workspace | 已完成 MVP 版 | `--allow-dir` / `AGENT_ALLOWED_DIRS` 支持显式授权额外目录给文件、搜索、LSP、patch 工具；workspace roots 会进入模型上下文。 |
| Cross-project Env File | 已完成 MVP 版 | `src/local_agent/cli.py` 支持 `--env-file`；`./agent` 自动加载 LCA 安装目录 `.env`，使 provider 凭据与目标 `--cwd` 解耦。 |
| Runtime State Dir | 已完成 MVP 版 | `--state-dir` / `AGENT_STATE_DIR`；默认写入用户级 state root 下的 workspace-specific 目录。 |
| Startup Context / Sticky Rules | 已完成 MVP 版 | 用户级和项目级 `AGENTS.md` 启动注入；用户级和项目级 `RULES.md` 每次 provider request 前注入。 |
| Markdown Memory 启动注入 | 已完成 MVP 版 | 项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/*.md` 会作为 advisory context 注入 system prompt。 |
| Learn 工具 | 已完成 MVP 版 | `learn` 写入 `.local-agent/memory/learned.md`，用于显式沉淀可复用经验。 |
| Memory Consolidation | 已完成 MVP 版 | `--memory-consolidation auto|llm` 从 session 抽取长期经验；默认 `off`，开启后默认写 state dir，`--memory-scope project` 才写 `.local-agent/memory/*.md`。 |
| Authored Skills Discovery | 已完成 MVP 版 | `.local-agent/skills/<name>/SKILL.md` 启动时只注入 name、description、source path，正文按需读取。 |
| Memory / Skills 方案 | 已完成设计 | `docs/memory-skills-implementation-plan.md` 明确 Markdown memory 注入、`learn`、skills discovery、managed skills/autolearn 的分阶段方案。 |
| P7 综合压测记录 | 已完成 | `docs/pressure-test-2026-07-08.md` 记录压测证据、OMP 对应机制和 LCA 措施。 |
| 重复工具调用熔断 / forced-final steering | 已完成 MVP 版 | 最近窗口内同名同参工具调用超过阈值时跳过；重复命中后下一轮不给工具 schema，强制模型基于已有证据输出最终回答；连续命中仍有硬停兜底。 |
| Tool Result Pruning | 已完成 MVP 版 | `ToolResult.useless` 支持标记无信息结果；空搜索/LSP 结果标记 useless；发送给模型的上下文会把 useless 和 superseded 工具结果折叠成 notice，session 原文保留。 |
| Todo Steering | 已完成 MVP 版 | 未完成 todo 会作为 runtime reminder 注入发送给模型的 system context，即使未触发 compaction 也能帮助模型保持方向。 |
| Evidence Ledger | 已完成 MVP 版 | `src/local_agent/agent.py` 从工具结果提炼短证据记录，注入 provider-bound `[Evidence ledger]`，并写 session `evidence` 事件；测试覆盖 read_file 后账本注入。 |
| Synthetic Tool Result | 已完成 MVP 版 | deadline 到期、用户中断、`finish_reason=length` 时会补齐剩余 tool_call 的 tool result。 |
| 测试基线 | 已完成 | 本地正常环境下 153 个测试通过。 |

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
| T-039 | Markdown memory 启动注入 | 已完成 MVP 版 | P7 | 读取项目 `.local-agent/memory/*.md` 和 state dir `memory/*.md`，并以 advisory block 注入 system prompt，带 source path 和字符预算。 |
| T-040 | 实现 `learn` 工具 | 已完成 MVP 版 | P7 | 把可复用 lesson 写入 `.local-agent/memory/learned.md`，限制长度并清洗会进入 prompt 的字段。 |
| T-041 | Authored skills discovery | 已完成 MVP 版 | P7 | 先扫 `.local-agent/skills/<name>/SKILL.md`，system prompt 只列 name / description / source path，正文按需读取。 |
| T-042 | Managed skills / autolearn | 暂缓 | P7 | 默认关闭，后续按 OMP 风格加入 `manage_skill`，generated skills 与 authored skills 隔离且优先级最低。 |
| T-043 | P7 综合压测记录 | 已完成 | P7 | 新增 `docs/pressure-test-2026-07-08.md`，记录压测证据、OMP 对应机制和 LCA 措施。 |
| T-044 | 重复工具调用熔断 | 已完成 MVP 版 | P7 | 最近窗口内同名同参超过 3 次会返回 tool error；连续命中 8 次停止本轮，避免只靠 budget 截断。 |
| T-045 | 企业项目外发策略确认 | 用户已确认，full-access 已代跑 | P7 | 用户确认可把企业源码/需求发给百炼；早期受限 Codex 环境拒绝代跑，切换 full-access + network enabled 后已由 Agent 代跑 session `20260708T081827983347Z`。 |
| T-046 | 跨项目 env-file / launcher env 加载 | 已完成 MVP 版 | P7 | CLI 支持 `--env-file`；`./agent` 自动加载安装目录 `.env`，让 token 配置与目标 `--cwd` 解耦。 |
| T-047 | OMP 风格 tool result pruning / todo steering | 已完成 MVP 版 | P7 | 已新增 `ToolResult.useless`、空搜索/LSP useless 标记、provider-bound useless/superseded pruning 和 open todo runtime reminder；session 原文保留。 |
| T-048 | LSP workspace/document symbols 兼容别名 | 已完成 MVP 版 | P7 | `lsp_workspace_symbols` / `lsp_document_symbols` 已注册为 `lsp_symbols` 只读别名，减少 OMP/Codex 风格提示迁移摩擦。 |
| T-049 | OMP 风格 runtime state 与 workspace 解耦 | 已完成 MVP 版 | P7 | `--state-dir` / `AGENT_STATE_DIR` 已落地；默认 sessions/todos/patch logs 使用用户级状态目录，避免只读跨项目分析在目标仓库写 `.local-agent/sessions`。 |
| T-050 | 用户级/项目级 AGENTS 与 sticky RULES | 已完成 MVP 版 | P7 | 支持 `AGENT_CONFIG_DIR` 下的用户级 `AGENTS.md` / `RULES.md`，以及 workspace `.local-agent/AGENTS.md` / `RULES.md`；AGENTS 启动注入，RULES 每次 provider request 前注入。 |
| T-051 | Session memory consolidation | 已完成并 review | P7 | `memory_consolidation=off|auto|llm` 和 `memory_scope=state|project` 已落地；默认 off，开启后默认追加到 state dir `memory/*.md`，显式 project 才写 `.local-agent/memory/*.md`；测试覆盖默认 state、显式 project、坏 JSON 不写、默认 off 不额外调用 LLM/不写 memory。 |
| T-052 | 重复工具后强制最终回答 steering | 已完成 MVP 版 | P7 | 用户本机企业压测 session `20260708T062614211387Z` 暴露 `feePlan` 重复搜索后硬停且无最终分析；runtime 现在会在重复工具命中后注入 steering，并让下一次 LLM 请求 `tools=[]`。 |
| T-053 | allowed-dir workspace roots 注入 | 已完成 MVP 版 | P7 | 用户本机复跑 session `20260708T065705459243Z` 暴露模型不知道 `--allow-dir` 绝对路径；system prompt/provider-bound context 现在会列出 primary workspace 和 allowed dirs。 |
| T-054 | 跨项目需求覆盖边界记录 | 已完成记录 | P7 | 用户确认当前测试项目可能无法完全覆盖需求；压测记录已说明单仓库只能输出候选前置能力和缺口，后续需要把相关项目也作为 `--allow-dir`。 |
| T-067 | Evidence Ledger MVP | 已完成并小改压测通过 | P7 | provider-bound `[Evidence ledger]` 已落地；session `20260708T092554037057Z` 验证小改闭环仍可跑通。 |
| T-068 | apply_patch tag 参数易误填 `path#tag` | 待评估 | P7 | 小实现压测中模型先把 `README.md#3988a904` 传给 `tag`，dry_run 失败后才自我修正；后续考虑 read_file 显式输出 tag 或 apply_patch 兼容 `path#tag`。 |
| T-069 | git_diff 区分已有修改与本轮修改 | 待评估 | P7 | 脏工作区下 `git_diff` 会混合本轮 patch 和 pre-existing diff；后续可记录 run start baseline 或用 patch records 辅助归因。 |

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
| R-014 | 重复工具调用循环导致 budget 耗尽且无最终回答 | 已进一步缓解并复跑通过 | 模型可能在同一工具参数或同一无结果关键词上循环，用户只得到预算停止或重复工具硬停信息。 | 已补最近窗口重复工具调用熔断、`ToolResult.useless`、空结果标记、provider-bound useless/superseded pruning、open todo runtime reminder、duplicate-tool forced-final steering，以及 search_code 空搜索词级 guard；session `20260708T083312934017` 已验证能产出最终分析。 |
| R-015 | 企业项目源码和需求可能被发送到三方 AI API | 用户已确认，full-access 已代跑 | 联网 LCA 压测会把进入上下文的企业代码/需求发给百炼。 | 用户已确认可外发；早期受限 Codex 环境拒绝代跑，full-access + network enabled 后已由 Agent 代跑。LCA 自身不内置禁止外发，按 OMP 思路由用户、provider、permission 和运行环境策略决定。 |
| R-016 | 跨项目运行时 token 配置绑定目标 workspace `.env` | 已关闭 MVP 版 | `--cwd` 切到其他项目后，LCA 仓库 `.env` 不会自动加载。 | 已新增 `--env-file` 和 `./agent` 安装目录 `.env` 自动加载；凭据配置与 `--cwd` 解耦。 |
| R-017 | 只读任务仍在目标 workspace 写 runtime 状态 | 已关闭 MVP 版 | 目标仓库会出现 `.local-agent/sessions`，不利于企业项目零业务落盘压测。 | 已参考 OMP 将 sessions 放在用户 agent dir 的设计，实现 `--state-dir`；sessions/todos/patch logs 与 workspace 解耦。 |
| R-018 | AGENTS/RULES 长期注入可能与当前任务冲突 | 已缓解，持续关注 | 用户级或项目级规则如果过期，会跨 session 影响模型判断。 | 注入区明确 advisory；system prompt 明确当前用户指令和源码证据优先；RULES 适合短规则，长背景放 AGENTS 或 memory。 |
| R-019 | 自动 memory consolidation 可能隐式写入陈旧或敏感内容 | 已进一步缓解，持续关注 | session 中的企业信息、临时结论或模型误判如果自动写入 memory，会跨 session 放大。 | 默认 `off`；显式开启后默认写用户级 state dir 的 memory，只有 `memory_scope=project` 才写项目 `.local-agent/memory`；只接受严格 JSON 的四类短条目；坏 JSON、空结果、deadline 耗尽、本轮已显式写 memory 时不写；memory 仍是 advisory。 |
| R-020 | multi-root allowed dir 没有稳定进入模型操作路径 | 已复跑通过 | 模型会猜 `requirements` 等不存在目录，或看到 roots 后仍不读取真实需求文档；session `20260708T072404789287Z` 证明仅提示和工具观察不够。 | 参考 OMP ToolChoiceQueue / soft tool requirement：需求/文档类任务在 allowed-dir 文档读取前只暴露 `list_files` / `read_file`，并要求先读取候选需求文档；session `20260708T083312934017` 已验证先读真实需求目录中的两份需求 md。 |
| R-021 | 单仓库无法覆盖跨服务需求 | 已记录，持续关注 | 如果需求实际涉及 incentive/settlement/用户中心等其他项目，单仓库分析会误把“当前仓库未命中”当成完整结论。 | 参考 OMP 对 workspace/context 的依赖边界，后续把相关项目也作为 `--allow-dir`，或让 Agent 明确输出“需要补充哪个项目”。 |
| R-022 | 同文件连续切片读取导致任务漂移 | 已补并复跑通过 | session `20260708T073252231781Z` 中模型连续读取同一大文件多个相邻区间；session `20260708T074609696125Z` 中显式只读任务因“下一步实现”措辞误关 guard；session `20260708T083312934017` 中 repeated read-file guard 成功收束并按 5 点结构输出。 | 参考 OMP 病态子循环小上限、runtime steering 和 runtime context：显式只读/不要修改文件/不要写文件优先于编辑词；近期同一路径 `read_file` 超阈值后强制下一轮无工具最终回答，并在 steering 里列出已读文件路径、原始请求和已读一致性规则。 |
| R-023 | 同一空搜索词跨路径扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T082703005777Z` 中模型对同一无结果关键词反复切换 path 搜索，因参数不同绕过 exact duplicate guard。 | 参考 OMP useless tool result / pruning / soft escalation：按 pattern 而非完整参数统计无结果搜索，多次无结果后 forced-final。 |
| R-024 | path escape 纠偏不足会让模型漏读主项目 | 已补并复跑通过 | session `20260708T084322924403Z` 中模型误用父目录后没有恢复，最终只分析辅助项目。 | 参考 OMP cwd/project context 和工具观察：公共 path escape 错误已列出 primary workspace/allowed dirs；session `20260708T085927874078` 已验证可恢复。 |
| R-025 | LSP 空 query 扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T084714338485Z` 中模型猜测大量不存在符号名，参数不同绕过同参重复 guard。 | 参考 OMP useless result / pruning / soft escalation：新增 LSP symbol 空 query 小上限并 forced-final。 |
| R-026 | 最终回答结构和证据路径可能漂移 | 已补并复跑通过 | session `20260708T085426840146Z` 最终只总结最后一个需求文档；此前也出现把未验证路径当下一步建议路径的倾向。 | 参考 OMP 当前任务和 runtime evidence 持续注入：新增 Current task contract 和 evidence-backed path rule。 |
| R-027 | 模型可能把 `path#tag` 整串误当成 patch tag | 已记录，待评估修复 | session `20260708T092554037057Z` 中 `apply_patch dry_run` 因 `tag=README.md#3988a904` 连续失败。 | 参考 OMP 结构化工具观察/编辑参数提示：后续可让 read_file 显式给出 pure tag，或让 apply_patch 兼容 `path#tag` 并提示模型。 |
| R-028 | 脏工作区下最终 diff 摘要可能混入非本轮改动 | 已记录，待评估修复 | session `20260708T092554037057Z` 的 `git_diff` 同时包含 README 小改和正在开发的 Evidence Ledger 代码 diff。 | 参考 OMP task/worktree/session state：后续可记录 run start baseline，并按 pre-existing / this-run patch / runtime state 分组展示。 |

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
| ADR-014 | Runtime 问题优先采用 OMP 已验证设计。 | 对 deadline、compaction、permission、synthetic tool result、todo/tool-choice steering、pruning 这类 OMP 已经覆盖的机制，不再为了“自己造一套”而绕开；LCA 不内置“企业数据不能外发”禁令，但必须尊重当前执行宿主或企业环境的策略拦截。 |
| ADR-017 | 解决 runtime/工具/上下文问题时先查 OMP 做法。 | 用户明确要求后续解决问题都参考 OMP；本项目原则更新为先找 OMP 已验证设计，再按本地个人 Agent、封闭 VM、单 Agent 和无自动下载边界裁剪落地。 |
| ADR-018 | Evidence Ledger 是本轮 provider-bound runtime context，不是长期 memory。 | 工具证据服务于当前会话最终回答和审计，不能替代 session 原文，也不应默认写入项目长期 memory；参考 OMP runtime state / tool evidence / steering 持续入上下文的思路。 |
| ADR-015 | 人工上下文按 AGENTS/RULES 分层。 | 参照 Claude Code 与 OMP 的上下文文件/Sticky rules 分层：`AGENTS.md` 作为启动背景，`RULES.md` 作为短规则每轮注入；二者不同于长期 memory 和 session summary。 |
| ADR-016 | Session memory consolidation 默认关闭；开启后默认写 state memory。 | 这一步不同于只发给模型的 context compaction；默认 off 可以保护只读分析，开启后默认写用户级 state dir，只有显式 `memory_scope=project` 才写项目 `.local-agent/memory`。 |
| ADR-003 | Excel 作为人工视图，Markdown 作为开发协作 Agent 可读事实源。 | 这套文档服务于开发 LCA 的过程；`.xlsx` 是二进制展示产物，不适合作为协作 Agent 的事实源。 |
| ADR-004 | 第一阶段 memory 使用 Markdown。 | Markdown 简单、可审计、封闭 VM 友好；暂不引入 SQLite 或向量库。 |
| ADR-005 | 第一阶段使用 anchored patch，不做 AST edit。 | hash + old_text + line 校验已经足够支撑 MVP 的可控修改。 |
| ADR-006 | 长需求应写入文件，让 Agent 读取。 | 直接把大段需求塞进 prompt 会挤占上下文，不利于长任务。 |
| ADR-007 | 封闭 VM 目标优先于联网能力。 | 第一阶段只允许访问指定 AI API，不引入公网搜索和自动下载依赖。 |

## P5 收口结论

| 项目 | 结论 | 依据 |
|---|---|---|
| 主链路 | 通过 | 百炼真实小改复测已跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff。 |
| 测试 | 通过 | P5 收口时 90 个 unittest、compileall、xlsx 检查、diff check 均通过；P7 当前代码已跑通 153 个 unittest、compileall 和 diff check。 |
| 日用入口 | 通过 | README 已补只读分析和小改任务命令模板。 |
| 开放风险 | 可接受 | shell 仍非沙箱、prompt injection 仍需靠审批和封闭 VM；token budget / output reserve / managed skills 留到后续评估。 |
| 下一阶段 | P7 轻量高级能力真实压测后续 | 企业项目联网压测已获用户允许并由 Agent 代跑；跨项目 env-file、轻量 pruning / todo steering、memory consolidation、duplicate-tool forced-final steering、allowed-dir soft tool requirement、repeated read_file guard、空搜索词 guard、path escape roots hint、LSP 空 query guard 和 Current task contract 已完成。下一步继续评估回答准确性，尤其是跨项目缺失证据时的措辞和实现前二次验证。 |

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

1. 用用户本机相同企业压测命令复跑，验证 duplicate-tool forced-final steering 是否能把重复搜索收束成最终分析。
2. 用默认 `--summary-mode auto --context-char-budget` 跑一次长上下文压测，验证 OMP 风格 auto summary 的真实 provider 兼容性。
3. 用 `--allow-dir` 跑一次“需求文档目录 + 代码项目目录”的真实工作流。
4. 让 Agent 在 Python/Java/Vue/TS 项目里主动调用 `lsp_definition` / `lsp_references` / `lsp_diagnostics`，验证轻量多语言 LSP 工具是否能改善定位效率。
5. 验证 startup memory、`learn` 和 `--memory-consolidation auto` 是否能减少重复交代项目约定。
