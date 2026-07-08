# Local Coding Agent 开发项目管理数据源

更新时间：2026-07-08

本文件是开发 `local-coding-agent` 过程中的项目管理数据源，给参与开发本项目的人和协作 Agent 读取。它不是 LCA 运行时自己的 memory 或用户项目记忆。

本文件也是项目管理 Excel 的唯一数据源。更新项目进度时优先修改本 Markdown，然后运行：

```bash
python3 scripts/sync_project_excel.py
```

脚本会读取本文件中的二级标题和 Markdown 表格，生成 `docs/local-coding-agent-project-management.xlsx`。不要手工编辑 Excel 后再把状态当作事实源。

## 总览

| 字段 | 当前值 | 说明 |
|---|---|---|
| 最终目标 | 个人本地编程助手 Agent | 本地优先、封闭 VM 可用、只访问指定 AI API，能读代码、搜代码、改代码、跑测试、生成 diff、沉淀项目记忆。 |
| 当前阶段 | P7：轻量高级能力与真实压测 | P6 默认工作流 MVP 已落地；P7 已补 OMP 风格 auto summary、多语言轻量 LSP、multi-root、startup context/rules、startup memory、learn、可选 memory consolidation、authored skills discovery，并通过综合压测发现和修复重复工具调用循环，新增 OMP 风格 tool result pruning / todo steering；2026-07-08 已按 OMP 思路完成 runtime state 与 cwd 分层，自动 memory consolidation 默认写 state dir；full-access 后 Agent 已代跑单项目和多项目企业压测，最新 session `20260708T085927874078` 已验证 path escape roots hint、LSP 空 query guard、Current task contract 和证据路径规则，最终按 6 点结构输出。 |
| 推荐入口 | `./agent "阅读当前项目"` | 自动设置 `PYTHONPATH=src`，默认当前目录为 workspace。 |
| Token 配置 | 环境变量 / `--env-file` / `.env` | `./agent` 会自动加载安装目录 `.env`，也可显式传 `--env-file`；真实环境变量优先。 |
| 测试数 | 152 | 完整 unittest、compileall、diff check、xlsx 检查通过。 |
| 默认 budget_seconds | 600 | 单次任务默认 10 分钟墙钟预算；`--budget-seconds 0` 可关闭。 |
| 默认 max_steps | 0 | 表示不限步；仅在用户显式设置时作为防失控保险丝。 |
| 预算执行 | 细粒度 | LLM 请求和 shell/run_tests timeout 会按剩余预算夹紧；deadline 到期会补齐未执行工具结果。 |
| Context compaction | OMP 风格 auto 默认 | `context_char_budget` 近似上下文窗口，runtime 至少预留 15%；超过 reserve 阈值后 `--summary-mode auto` 调用当前 provider 生成语义摘要，失败回退 local summary。 |
| Synthetic tool result | 已完成 MVP 版 | deadline 到期、用户中断和 `finish_reason=length` 时会补齐剩余 tool_call 的 tool result。 |
| Patch preview | 已完成 | `apply_patch dry_run=true` 只校验并返回 diff，不写文件。 |
| Patch rollback | 已完成 MVP 版 | `rollback_patch` 只回滚本 session 的 patch 记录，且要求当前文件仍匹配 after tag。 |
| ask_user timeout | 已完成 | `ask_user` 支持 `timeout_seconds` / `default_answer`，显式 timeout 也会被当前 budget 剩余时间夹紧。 |
| Per-tool approval | 已完成 | 支持 `always-ask` / `write` / `yolo`、per-tool `allow` / `prompt` / `deny`、session always allow/reject 和 REPL `/approval`。 |
| OMP 核心判断 | 已固化 | 见 `docs/omp-core-architecture-notes.md`，已补 OMP 如何通过系统提示、工具描述和 runtime 纠偏让用户不用指定工具顺序。 |
| 默认工作流落地 | 已完成 MVP 版 | system prompt + tool descriptions + runtime workflow reminder 已落地，用户不需要每次手写工具顺序。 |
| 轻量 LSP | 已完成 MVP 版 | `lsp_symbols` / `lsp_workspace_symbols` / `lsp_document_symbols` / `lsp_definition` / `lsp_references` / `lsp_diagnostics`，覆盖 Python、Java、JavaScript、TypeScript、Vue。 |
| Multi-root workspace | 已完成 MVP 版 | `--allow-dir` / `AGENT_ALLOWED_DIRS` 支持显式授权额外目录给文件、搜索、LSP、patch 工具；system prompt 和 `list_files`/path-not-found 等工具观察会列出 primary workspace 和 allowed dirs；需求/文档类任务会先用 soft tool requirement 要求读取 allowed-dir 文档；shell/git/显式项目 memory/skills 仍锚定 `--cwd`，session/todo/patch logs 和默认 consolidation memory 走 state dir。 |
| Startup context / sticky rules | 已完成 MVP 版 | 用户级和项目级 `AGENTS.md` 启动注入；用户级和项目级 `RULES.md` 每次 provider request 前注入。 |
| Startup memory | 已完成 MVP 版 | 新 session 自动注入项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/*.md`，作为 advisory context。 |
| Learn 工具 | 已完成 MVP 版 | `learn` 写入 `.local-agent/memory/learned.md`，用于显式沉淀可复用经验。 |
| Memory consolidation | 已完成 MVP 版 | 默认 `off`；显式 `--memory-consolidation auto|llm` 后从 session 抽取长期经验；默认写 state dir，`--memory-scope project` 才写 `.local-agent/memory/*.md`。 |
| Authored skills discovery | 已完成 MVP 版 | 新 session 扫描 `.local-agent/skills/<name>/SKILL.md`，只注入 name、description、source path，正文按需读取。 |
| Runtime state dir | 已完成 MVP 版 | `--state-dir` / `AGENT_STATE_DIR`；sessions/todos/patch logs 默认写入用户级 state root 下的 workspace-specific 目录。 |
| Memory / Skills 设计 | 已完成 | 见 `docs/memory-skills-implementation-plan.md`；Markdown memory 注入、`learn`、memory consolidation 和 authored skills discovery 已完成，path-scoped rules / managed skills 后置。 |

## 阶段路线图

| 阶段 | 名称 | 目标 | 状态 | 完成度 | 下一步判定 |
|---|---|---|---|---:|---|
| P0 | OMP 分析与方案设计 | 看懂 OMP，确定我们自己的 MVP 路线 | 已完成 | 100% | 已形成“优先吸收成熟设计，按本地目标裁剪”的原则。 |
| P1 | 基础 Agent 闭环 | 接 AI API，完成读、搜、改、测、diff、session、memory | 已完成 | 100% | 已创建初始 git commit，基础闭环可回滚。 |
| P2 | 项目管理与可见性 | 项目状态、路线图、todo、决策记录一目了然 | 已完成 | 100% | Excel + Markdown 项目状态已建立。 |
| P3 | 长任务运行基础 | budget_seconds、max_steps 不限步、todo、ask_user、per-tool approval、一键启动 | 已完成 | 100% | 已具备真实需求的基础运行体验。 |
| P4 | 上下文治理 | 简单 summary/compaction，工具输出折叠，支持长需求文件 | 已完成 MVP 版 | 100% | 首轮百炼只读 compaction 压测已通过；当前已补可选 LLM summary，后续再评估 token 预算、输出 reserve 和 recent 保留。 |
| P5 | 安全与恢复增强 | synthetic tool result、patch preview、rollback、ask_user timeout、per-tool approval | 已完成并收口 | 100% | 主链路已通过真实百炼复测；后续只修日用反馈中的 P0/P1 问题。 |
| P6 | 日用体验与默认工作流固化 | OMP 默认工作流本地化：system prompt、工具描述、轻量 runtime nudge | 已完成 MVP 版 | 100% | 进入真实任务压测。 |
| P7 | 高级工程能力轻量版 | OMP 风格 auto summary、轻量 LSP、LSP 兼容别名、multi-root workspace roots、allowed-dir soft tool requirement、startup context/rules、startup memory、learn、memory consolidation、authored skills discovery、重复工具调用熔断、duplicate-tool forced-final steering、同文件切片读取漂移 guard、空搜索词跨路径 guard、path escape roots hint、LSP 空 query guard、Current task contract、tool result pruning、todo steering、跨项目 env-file、runtime state dir、真实项目压测记录 | 进行中 | 99% | full-access 后 Agent 已代跑单项目和多项目企业项目联网压测；session `20260708T085927874078` 已先读需求、纠正父目录 path escape、定位真实 `limitConstituteAllotImport` 证据，并按 6 点结构输出。下一步继续评估回答准确性和跨项目覆盖边界。 |

## 已完成功能

| 模块/能力 | 状态 | 依据 | 备注 | 下一步 |
|---|---|---|---|---|
| 项目骨架 | 已完成 | `pyproject.toml`、`src/`、`tests/`、`docs/` | Python 标准布局 | 保持 |
| CLI | 已完成 | `src/local_agent/cli.py` + `./agent` | 一键启动，支持高级参数 | 保持 |
| Bailian Provider | 已完成 | `--provider bailian` 已实测 | OpenAI-compatible API | 保留 base_url/model 可配置 |
| Agent Runtime | 已完成 | 模型调用、tool_calls、tool_result 回灌闭环 | `max_steps=0` 不限步，budget 控制时间 | P4 做上下文治理 |
| Tool Registry | 已完成 | 统一 Tool/ToolResult/ToolContext | schema 校验、审批、state/interaction 工具类型 | 保持 |
| read_file | 已完成 | hash tag + 行号输出 | 限制大小和行数 | 保持 |
| list_files | 已完成 | 跳过 `.git`、`.local-agent`、cache | 相对路径输出 | 保持 |
| search_code | 已完成 | `rg` 搜索，相对路径输出，总结果截断 | 已修绝对路径泄漏 | 保持 |
| shell/run_tests | 已完成 | 本地命令和测试执行 | 带审批和超时 | per-tool auto allow 可选 |
| git_status/git_diff | 已完成 | untracked 时解释空 diff 原因 | 初始 commit 后 diff 更清晰 | 保持 |
| apply_patch | 已完成 | replace/insert_before/insert_after/dry_run | hash + old_text 校验；dry_run 只预览不写入 | 保持 |
| rollback_patch | 已完成 MVP 版 | session patch log + after tag 校验 | 只回滚本 session 中 apply_patch 写过且未被后续修改的文件 | 继续真实任务验证 |
| write_file | 已完成 | 只创建新文件，拒绝覆盖 | 安全保守 | 保持 |
| Markdown memory | 已完成 | project/decisions/conventions 基础版 | 轻量封闭 VM 友好 | 后续看需求升级 |
| Session JSONL | 已完成 | session/continue 基础恢复 | 已修缺 role 和坏尾部问题 | 加 synthetic result |
| 时间预算 | 已完成 | `--budget-seconds` / `AGENT_BUDGET_SECONDS` | 默认 600 秒，0 可关闭 | 保持 |
| 预算细粒度检查 | 已完成 | LLM/tool 使用剩余预算 | LLM timeout、shell/run_tests timeout 会被夹紧 | 保持测试 |
| 不限步主循环 | 已完成 | `max_steps=0` | 默认不限步，显式设置才作为保险丝 | 保持 |
| Todo 工具 | 已完成 | `todo_read` / `todo_add` / `todo_update` | session 级状态追踪 | P4 长任务中继续验证 |
| ask_user | 已完成 | 交互式终端可中途提问，支持 timeout/default | 避免需求歧义时硬猜，也避免长任务无限等待 | 继续真实任务验证 |
| Per-tool approval | 已完成 | `--auto-approve-tools` + `--tool-approval` + `/approval` | 兼容旧白名单，并支持每个工具 allow / prompt / deny、session always allow/reject | 继续真实任务验证 |
| 一键启动 | 已完成 | `./agent` | 自动设置 `PYTHONPATH` 并以当前目录为 workspace | 日常入口 |
| `.env` / `--env-file` 加载 | 已完成 | `src/local_agent/config.py` + `./agent` | `./agent` 自动加载安装目录 `.env`；CLI 支持显式 `--env-file`；目标 workspace `.env` 仍可用 | 跨项目 token 与 `--cwd` 解耦 |
| Runtime state dir | 已完成 MVP 版 | `src/local_agent/state.py` + `--state-dir` | sessions/todos/patch logs 默认写入用户级 state root；显式项目 memory/skills 保留在 workspace，自动 consolidation 默认写 state memory | 真实企业项目只读复测 |
| Startup context / sticky rules | 已完成 MVP 版 | `AgentRuntime` system prompt / provider-bound context | 用户级和项目级 `AGENTS.md` 启动注入；用户级和项目级 `RULES.md` 每次 provider request 前注入 | 真实 session 验证 |
| OMP 核心架构笔记 | 已完成 | `docs/omp-core-architecture-notes.md` | 固化主循环、deadline、compaction、tool approval、默认工作流分层结论 | 后续设计依据 |
| OMP 默认工作流源码依据 | 已完成 | `docs/omp-core-architecture-notes.md` | 已记录 system prompt、project prompt、tool registry、tool descriptions、todo reminders、ToolChoiceQueue、agent-loop 的具体文件依据 | P6 实现依据 |
| 本地 Context Compaction | 已完成 MVP 版 | `context_char_budget` / `context_recent_messages` | 折叠早期历史，保留最近消息和当前用户请求，注入未完成 todo，截断发送给模型的超大 tool 输出，并保持单 system 消息 | 下一步补 token 预算、输出 reserve；字符阈值保留兜底 |
| OMP 风格 Auto Summary | 已完成 MVP 版 | `--summary-mode auto` / `AGENT_SUMMARY_MODE=auto` | 小历史不摘要；超过 reserve 阈值后调用当前 provider 总结早期历史；失败回退 local summary | 需要百炼长上下文实测 |
| 默认工作流 | 已完成 MVP 版 | `SYSTEM_PROMPT` + runtime workflow reminder | 自然语言代码任务默认探索、todo、patch preview、验证、diff | 需要真实任务实测 |
| 轻量 LSP 工具 | 已完成 MVP 版 | `lsp_symbols` / `lsp_workspace_symbols` / `lsp_document_symbols` / `lsp_definition` / `lsp_references` / `lsp_diagnostics` | Python、Java、JavaScript、TypeScript、Vue 静态导航，不启动外部 server；workspace/document symbols 是兼容别名 | 后续看是否升级完整 LSP |
| Multi-root workspace | 已完成 MVP 版 | `--allow-dir` / `AGENT_ALLOWED_DIRS` | 支持需求文档目录 + 代码项目目录；system prompt 和工具观察会列出 allowed dirs；需求/文档类任务先 soft-require `read_file` allowed-dir 文档；文件/search/LSP/patch 可访问 allowed dirs，shell/git/显式项目 memory/skills 仍锚定 `--cwd`，默认 consolidation memory 走 state dir | 真实需求压测 |
| Startup memory | 已完成 MVP 版 | `AgentRuntime` system prompt 构造 | 项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/*.md` 作为 advisory context 注入 | 真实 session 验证 |
| Learn 工具 | 已完成 MVP 版 | `learn` | 写入 `.local-agent/memory/learned.md`，默认走写工具审批 | 真实 session 验证 |
| Memory consolidation | 已完成 MVP 版 | `memory_consolidation=off|auto|llm` + `memory_scope=state|project` | 默认 off；显式开启后从 session 抽取长期 project/decisions/conventions/learned；默认追加到 state dir `memory/*.md`，显式 project 才写 `.local-agent/memory/*.md` | 真实 session 验证 |
| Authored skills discovery | 已完成 MVP 版 | `AgentRuntime` system prompt 构造 | `.local-agent/skills/<name>/SKILL.md` 只注入 name/description/source，正文按需读取 | 真实 session 验证 |
| Memory / Skills 方案 | 已完成设计 | `docs/memory-skills-implementation-plan.md` | 分阶段对齐 OMP：memory 注入、`learn`、memory consolidation、authored skills、managed skills/autolearn | 下一步真实压测 |
| Synthetic tool result | 已完成 MVP 版 | deadline 到期、用户中断和 `finish_reason=length` 时补齐 tool result | 避免 session 留下未配对 tool_calls | 继续真实任务验证 |
| Patch preview | 已完成 | `apply_patch dry_run=true` | 复用 anchored 校验并返回 diff，不写文件 | 后续评估 rollback |
| Patch rollback | 已完成 MVP 版 | `rollback_patch` | 校验当前文件 hash 后恢复 patch 前内容 | 继续真实任务验证 |
| ask_user timeout | 已完成 | `timeout_seconds` / `default_answer` / budget 剩余时间 | 长任务无人响应时可以继续或明确失败；显式 timeout 也受 budget 夹紧 | 继续真实任务验证 |
| Tool approval policy | 已完成 MVP 版 | `tool_approval` + `session_tool_approval` | config deny/prompt 是硬护栏，session allow/reject 可记住当前会话，REPL 会校验工具名，approval prompt 受 deadline 约束 | 日用反馈 |
| 重复工具调用熔断 / forced-final steering | 已完成 MVP 版 | `AgentRuntime._execute_tool_with_repeat_guard()` / `_steer_after_duplicate_tool_call()` | 最近窗口内同名同参工具调用超过阈值会返回 tool error；重复命中后注入 runtime steering，下一轮不给工具 schema，强制模型从已有证据输出最终回答；连续命中仍有硬停兜底 | 用户本机复跑企业需求压测；后续视结果评估完整 ToolChoiceQueue / soft tool requirement |
| Tool result pruning | 已完成 MVP 版 | `ToolResult.useless` + provider-bound context pruning | `search_code` / LSP 空结果标记 useless；重复等价 read/search/LSP 旧结果在发给模型的上下文中替换为 notice，session 原文保留 | 继续真实长任务观察 |
| Todo steering | 已完成 MVP 版 | provider-bound runtime todo reminder | 未完成 todo 会注入发送给模型的 system context，即使未触发 compaction 也能提醒模型保持任务方向 | 后续评估 OMP 风格 eager todo / mid-run nudge |
| 测试覆盖 | 已完成 | 当前 152 个测试通过 | unittest、compileall、diff check、xlsx 检查通过 | 日用反馈补测 |

## 下一步 Todo

| ID | 优先级 | 阶段 | 任务 | 状态 | 负责人 | 为什么重要 | 完成标准 |
|---|---|---|---|---|---|---|---|
| T-001 | P0 | P2 | 确认项目管理基线 | 已完成 | User + Agent | 统一目标、阶段和下一步 | Excel 已复核；Markdown 看板已建立 |
| T-002 | P0 | P2 | 建立 `docs/project-status.md` | 已完成 | Agent | 让开发协作 Agent 可读项目路线 | 文档已存在 |
| T-003 | P0 | P2 | 做第一次 git commit | 已完成 | User + Agent | 建立干净回滚基线 | 提交 `2c4348b` 已创建 |
| T-004 | P0 | P3 | 加入 budget-seconds/deadline | 已完成 | Agent | 用时间预算控制长任务 | CLI/env/config 已支持；默认 600 秒 |
| T-005 | P0 | P3 | max_steps 改为不限步保险丝 | 已完成 | Agent | 步数不限制日常任务 | 默认值 0；显式设置才作为保险丝 |
| T-006 | P0 | P3 | 实现 todo 工具 | 已完成 | Agent | 长任务需要显式状态 | `todo_read/add/update` 可用 |
| T-007 | P0 | P3 | 实现 ask_user 工具 | 已完成 | Agent | 需求歧义时不硬猜 | 交互式终端可提问 |
| T-008 | P0 | P3 | 增加 per-tool approval policy | 已完成 | Agent | 减少重复敲 y | `--auto-approve-tools` 可用 |
| T-009 | P1 | P2 | 更新 README 安全工作流 | 已完成 | Agent | 说明预算、审批和 shell 边界 | README 已更新 |
| T-010 | P1 | P4 | 简单上下文 summary | 已完成 MVP 版 | Agent | 长任务会被全量历史拖垮 | 已实现字符阈值 deterministic compaction；LLM summary 已补，下一步升级 token budget / reserve |
| T-011 | P1 | P5 | 补 synthetic tool result | 已完成 MVP 版 | Agent | 中断/异常时避免 orphan tool_calls | deadline 到期、用户中断和 length 截断已补齐 |
| T-012 | P1 | P5 | Patch preview/rollback 设计 | 已完成 MVP 版 | Agent | 进一步降低改错风险 | 已完成 dry_run 预览和 session 级 hash 校验 rollback |
| T-013 | P2 | P6 | 评估 LSP/TUI/subagents/AST edit | 已部分完成 | User + Agent | 高级能力强但复杂 | 轻量 LSP 已做；TUI/subagents/AST edit/DAP 继续后置 |
| T-014 | P0 | P3 | 提交 P3 变更 | 已完成 | User + Agent | 把本轮 P3 工作固化为第二个 commit | 提交 `304fbdf` 已创建 |
| T-015 | P0 | P2 | Markdown 模板同步 Excel | 已完成 | Agent | 避免手工同步 Excel 出错 | `scripts/sync_project_excel.py` 可从本文件生成 Excel |
| T-016 | P0 | P3 | 细化 budget deadline 执行检查 | 已完成 | Agent | 让时间预算从软闸变成实际主控 | LLM/tool timeout 按剩余预算夹紧；未执行工具有 synthetic result |
| T-017 | P0 | P4 | 提交 P4 compaction 变更 | 已完成 | Agent | 把上下文治理节点固化为 commit | 提交 `4beb487` 已创建 |
| T-018 | P1 | P5 | 处理模型输出截断 synthetic result | 已完成 | Agent | `finish_reason=length` 可能产生不完整工具参数 | LLM 层已暴露 finish_reason，并补可恢复提示 |
| T-019 | P1 | P5 | 实现 patch dry-run preview | 已完成 | Agent | 写入前先看 diff，减少误改风险 | `apply_patch dry_run=true` 不写文件并返回 diff |
| T-020 | P1 | P5 | 实现 session 级 patch rollback | 已完成 | Agent | 写错后可以在安全条件下恢复 | `rollback_patch` 校验 after tag 后恢复 before_text |
| T-021 | P1 | P5 | 实现 ask_user timeout/default | 已完成 | Agent | 防止长任务等待用户输入时无限阻塞 | `ask_user` 支持 timeout/default，并受 budget 剩余时间约束 |
| T-022 | P1 | P5 | 实现 tool_approval allow/prompt/deny | 已完成 | Agent | 白名单不够表达显式拒绝和强制询问 | `--tool-approval` / `AGENT_TOOL_APPROVAL` 支持每工具策略 |
| T-023 | P1 | P5 | 实现 approvalMode / session decision / REPL 命令 | 已完成 | Agent | 对齐 OMP 三层审批模型的本地 MVP | 支持 `always-ask` / `write` / `yolo`、`s/d` 会话记忆、`/approval` 命令 |
| T-024 | P1 | P5 | approval prompt 支持 deadline/abort | 已完成 MVP 版 | Agent | 人工确认等待也消耗 wall-clock budget，不能让确认等待绕过 `budget_seconds` | approval prompt 使用 deadline-aware timed stdin；deadline 到期自动取消/拒绝，保留 `y/s/n/d` 和 session allow/reject |
| T-025 | P1 | P5 | 修复 approval 优先级和工具名校验 | 已完成 | Agent | 避免新 `tools.*` 被旧顶层字段静默覆盖、config prompt 被 session allow 绕过、REPL 工具名输错后假成功 | 新配置优先于旧字段；config prompt/deny 是硬护栏；REPL 校验未知工具名 |
| T-026 | P1 | P5 | 夹紧 ask_user timeout 并截断 compaction tool 输出 | 已完成 | Agent | 消除 ask_user 文档与代码不一致，降低长任务 compaction 软预算超标风险 | 显式 `timeout_seconds` 会被剩余 budget 夹紧；recent tool 输出只在发送模型副本中截断，session 原文保留 |
| T-027 | P1 | P5 | compaction 保持单 system 消息 | 已完成 | Agent | 降低 OpenAI-compatible provider 对多 system 消息的兼容风险 | 压缩摘要合并进首个 system prompt；测试锁定发送给模型时只有一条 system |
| T-028 | P1 | P5 | 百炼只读压测后的目标漂移修复 | 已完成 | Agent | 极小上下文预算下，模型会被续读提示和最近代码片段带偏 | compaction 摘要强保留当前用户请求；read_file 截断提示改为“任务需要才继续” |
| T-029 | P1 | P5 | 复测百炼只读 compaction 压测 | 已完成 | User + Agent | 验证 T-028 是否真正修复目标漂移 | 会话 `20260707T093557800154Z` 严格完成 5 个指定工具调用后输出三句话总结，未继续额外读文件 |
| T-030 | P1 | P5 | 真实小改任务压测 | 已完成 | User + Agent | 验证 compaction、approval、patch preview/rollback、run_tests、git_diff 在真实修改任务中的协同 | 复测会话 `20260707T094246132064Z` 跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff；最终仅新增一个测试 docstring |
| T-031 | P0 | P5 | 修正 `write_file` schema 描述误导 | 已完成 | Agent | 工具描述会进入模型上下文，错误描述会直接导致错误修改 | `write_file` 描述已改为 create-only，并加测试确保不再出现 `fully overwrite` |
| T-032 | P1 | P5 | P5 收口检查 | 已完成 | Agent | 收口前确认文档、测试、工作树和已知风险一致 | README 已补日用模板；项目状态和 Excel 已同步；90 个测试、compileall、xlsx、diff check 通过 |
| T-033 | P1 | P6 | P6 取舍评估 | 已完成首轮 | User + Agent | 决定下一阶段是继续日用打磨，还是引入 token budget / LLM summary / LSP / TUI 等高级能力 | 已决定优先做 OMP 默认工作流本地化；随后按用户要求补 LLM summary 和轻量 LSP |
| T-034 | P0 | P6 | 固化 OMP 默认工作流源码依据 | 已完成 | Agent | 避免后续只凭“大概”实现；让设计有源码依据 | `docs/omp-core-architecture-notes.md` 已新增“OMP 如何让用户不用指定工具顺序” |
| T-035 | P0 | P6 | 固化 LCA 默认工作流 system prompt | 已完成 MVP 版 | Agent | 让用户不用每次手写 `list_files/read_file/dry_run/run_tests/git_diff` | 默认 prompt 覆盖理解、修改、验证、todo、ask_user、patch preview、diff |
| T-036 | P0 | P6 | 增强工具描述与真实能力一致性 | 已完成 MVP 版 | Agent | 模型会相信 tool schema；描述不准会直接导致错误动作 | 既有 create-only/patch dry_run 描述保持测试；新增 LSP 工具描述 |
| T-037 | P1 | P6 | 实现轻量 runtime workflow nudge | 已完成 MVP 版 | Agent | 复杂任务中提醒 todo 和验证，降低模型跑偏 | 非平凡代码任务会注入 reminder；短 prompt 不注入 |
| T-038 | P1 | P7 | 评估 multi-root workspace allow-dir | 已完成 MVP 版 | User + Agent | 支持“需求文档目录 + 代码项目目录”的真实工作流 | `--allow-dir` / `AGENT_ALLOWED_DIRS` 已覆盖 read/list/search/LSP/apply_patch/write_file/rollback_patch |
| T-039 | P1 | P7 | 实现 OMP 风格 auto summary | 已完成 MVP 版 | Agent | 长上下文需要语义摘要能力，但不应小历史也额外调用模型 | 默认 `--summary-mode auto`；超过 reserve 阈值才调用 LLM summary，失败回退 local summary |
| T-040 | P1 | P7 | 实现轻量 LSP 工具 | 已完成 MVP 版 | Agent | 提升主流项目定位效率 | Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics 已可用 |
| T-041 | P0 | P7 | 固化 Memory / Skills 实现方案 | 已完成 | Agent | OMP memory/autolearn/skills 机制复杂，需先裁剪成 LCA 可执行方案 | 已新增 `docs/memory-skills-implementation-plan.md`，并补充 `docs/omp-core-architecture-notes.md` |
| T-042 | P1 | P7 | Markdown memory 启动注入 | 已完成 MVP 版 | Agent | 当前 memory 只能手动读写，不能跨 session 自动影响 agent | 读取项目 `.local-agent/memory/*.md` 和 state dir `memory/*.md`，以 advisory block 注入 system prompt，带 source path 和预算 |
| T-043 | P1 | P7 | 实现 `learn` 工具 | 已完成 MVP 版 | Agent | 让 agent 用显式工具沉淀可复用 lesson，而不是混写 project memory | 写入 `.local-agent/memory/learned.md`，限制长度并清洗会进入 prompt 的字段 |
| T-044 | P2 | P7 | Authored skills discovery | 已完成 MVP 版 | Agent | 支持项目内可复用工作流，降低重复提示成本 | 先扫 `.local-agent/skills/<name>/SKILL.md`，system prompt 只列 name/description/source，正文按需读取 |
| T-045 | P2 | P7 | Managed skills / autolearn | 暂缓 | User + Agent | 自动生成 skills 有长期污染和 prompt injection 风险 | 默认关闭；后续按 OMP `manage_skill` 思路隔离 generated skills，authored skills 优先 |
| T-046 | P0 | P7 | P7 综合压测记录 | 已完成 | Agent | 真实压测发现的问题必须沉淀成可追踪事实源 | 新增 `docs/pressure-test-2026-07-08.md`，记录压测证据、OMP 对应机制和 LCA 措施 |
| T-047 | P0 | P7 | 重复工具调用熔断 | 已完成 MVP 版 | Agent | 防止模型在同一工具参数上循环到 budget 耗尽而无最终回答 | 最近 12 次工具调用内同名同参超过 3 次会跳过；连续命中 8 次停止；测试覆盖 JSON 参数顺序归一化 |
| T-048 | P1 | P7 | 企业项目外发策略确认 | 用户已确认，full-access 已代跑 | User + Agent | 企业源码和需求发给百炼属于三方 API 外发，需要明确边界 | LCA 产品设计不内置禁止外发；按 OMP 思路由用户、provider、permission 和运行环境策略决定。早期受限 Codex 环境曾阻断代跑；切换 full-access + network enabled 后已由 Agent 代跑 session `20260708T081827983347Z`。 |
| T-049 | P1 | P7 | 跨项目 `--env-file` / launcher env 加载 | 已完成 MVP 版 | Agent | 让 token 配置与目标 `--cwd` 解耦，跨项目一键启动更顺手 | CLI 支持显式 `--env-file`；`./agent` 自动把安装目录 `.env` 注入为 env-file；测试覆盖优先级和缺失文件报错 |
| T-050 | P1 | P7 | OMP 风格 tool result pruning / todo steering | 已完成 MVP 版 | Agent | compaction 只是装得下上下文，还需要降低无效工具结果和旧结果对模型的污染 | 已新增 `ToolResult.useless`；空搜索/LSP 结果会标记 useless；provider-bound context 会折叠 useless/superseded 工具输出并注入 open todo reminder；session 原文保留 |
| T-051 | P1 | P7 | LSP workspace/document symbols 兼容别名 | 已完成 MVP 版 | Agent | 减少模型和用户从 OMP/Codex 概念迁移时的工具名摩擦 | `lsp_workspace_symbols` / `lsp_document_symbols` 已注册为 `lsp_symbols` 只读别名；测试覆盖 registry 和执行结果 |
| T-052 | P1 | P7 | OMP 风格 runtime state 与 workspace 解耦 | 已完成 MVP 版 | Agent | 只读跨项目分析不应在目标仓库写 `.local-agent/sessions`；企业项目压测需要更干净的零业务落盘体验 | `--state-dir` / `AGENT_STATE_DIR` 已落地；默认 sessions/todos/patch logs 使用用户级状态目录；项目 memory/skills 保留在 workspace；测试覆盖跨 `--cwd` 不写目标 `.local-agent/sessions` |
| T-053 | P1 | P7 | 用户级/项目级 AGENTS 与 sticky RULES | 已完成 MVP 版 | Agent | 对齐 Claude Code / OMP 的人工上下文层级，减少重复提示并让短规则跨长会话可见 | `AGENT_CONFIG_DIR` 下的用户级 `AGENTS.md` / `RULES.md` 和 workspace `.local-agent/AGENTS.md` / `RULES.md` 已支持；测试覆盖启动注入和 provider-bound 注入 |
| T-054 | P1 | P7 | 企业项目真实联网只读压测复跑 | 已由 Agent 代跑并复跑收束 | User + Agent | 验证 `--cwd` 企业项目 + `--allow-dir` 需求目录 + 百炼 provider + 用户级 state-dir 的真实链路 | session `20260708T083312934017` 已验证真实需求文档前置读取、代码搜索、guard 收束和 5 点结构输出；目标仓库未写入 `.local-agent`。下一步转向回答准确性和跨项目覆盖评估。 |
| T-055 | P1 | P7 | Session memory consolidation | 已完成并 review | Agent | 把 session 中的可复用经验定期整理进长期 memory，减少后续重复交代，同时避免默认写项目文件 | `--memory-consolidation auto|llm` 和 `--memory-scope state|project` 已支持；默认 off，开启后默认写 state dir，显式 project 才写 `.local-agent/memory`；测试覆盖默认 state、显式 project、坏 JSON 不写、默认 off 不额外调用 LLM/不写 memory |
| T-056 | P0 | P7 | 重复工具后强制最终回答 steering | 已完成 MVP 版 | Agent | 用户本机企业压测已证明“重复工具硬停”仍会让真实需求分析没有最终答案 | 重复同参工具超过阈值后，runtime 追加 steering 并让下一次 LLM 请求 `tools=[]`；回归测试确认模型会返回 `final answer from collected evidence` |
| T-057 | P0 | P7 | allowed-dir workspace roots 注入 | 已完成并复跑通过 | Agent | session `20260708T065705459243Z` / `20260708T070722601499Z` / `20260708T072404789287Z` 暴露模型仍不稳定读取 allowed-dir 需求文档，仅提示和工具观察不够 | system prompt/provider-bound context 增加 `[Workspace roots]`；`list_files {}` 根目录输出、path-not-found 和带 allowed-dir 的空搜索会提示 exact allowed dirs；需求/文档类任务创建 soft tool requirement；session `20260708T083312934017` 已先读两份 allowed-dir 需求 md |
| T-058 | P1 | P7 | 跨项目需求覆盖边界记录 | 已完成记录 | User + Agent | 用户确认当前测试项目可能无法完全覆盖需求，尤其结算需求可能需要其他项目协同 | 压测记录已说明单仓库只能输出候选前置能力和缺口；后续把相关项目也作为 `--allow-dir`，或让 Agent 先列“需要补充的项目清单” |
| T-059 | P0 | P7 | 同文件连续切片读取漂移 guard | 已完成并复跑通过 | Agent | session `20260708T073252231781Z` 已读需求文档但连续读大文件漂移；session `20260708T074609696125Z` 又暴露“只读压测 + 下一步实现”误关 guard | 参考 OMP 病态子循环小上限：显式只读/不要修改文件/不要写文件优先于编辑词；只读/分析任务中近期同一路径 `read_file` 超阈值后返回 tool error 并 forced-final；session `20260708T083312934017` 已验证触发后按 5 点结构输出 |
| T-060 | P0 | P7 | forced-final 已读文件证据摘要 | 已完成并复跑收束 | Agent | session `20260708T074609696125Z` 明明已读 V1.1 需求文档，最终回答却称未读 | 参考 OMP runtime context/steering：重复工具 forced-final 消息会列出本轮已成功 `read_file` 的路径，要求模型不要声称这些文件未读，并回到用户原始输出结构；最终由 T-061 一起收束 |
| T-061 | P0 | P7 | forced-final 原始请求和已读一致性约束 | 已完成并复跑通过 | Agent | session `20260708T081827983347Z` 中 `QueryFeePlanInfoReq.java` 已读，但最终回答仍写成 not yet read，且未完全按用户 5 点结构输出 | 参考 OMP runtime context/steering：forced-final 消息注入原始用户请求摘要，并明确已读文件不得称未读；session `20260708T083312934017` 最终按 5 点结构输出，未再出现已读文件称未读 |
| T-062 | P0 | P7 | search_code 空搜索词跨路径 guard | 已完成并复跑通过 | Agent | session `20260708T082703005777Z` 中同一无结果关键词 `exceptionCoreEnterprise` / `ExceptionCoreEnterprise` 在多级目录扩散搜索，绕过同参重复 guard | 参考 OMP useless tool result / pruning / soft escalation：同一搜索词忽略大小写后多次无结果会跳过后续搜索并 forced-final；session `20260708T083312934017` 已按 5 点结构收束 |
| T-063 | P0 | P7 | path escape roots hint | 已完成并复跑通过 | Agent | session `20260708T084322924403Z` 中模型把主 `--cwd` 误写成父目录，工具错误没有给可行动纠偏，导致主项目未被检查 | 参考 OMP runtime context/tool observation：公共 path resolver 越界错误返回 resolved path、primary workspace、allowed dirs，并提示父目录误用时使用 `.` 或精确 `--cwd`；session `20260708T085927874078` 已纠正回主项目 |
| T-064 | P0 | P7 | LSP symbol 空 query guard | 已完成并复跑通过 | Agent | session `20260708T084714338485Z` 中模型连续猜不存在的 `CoreEnterpriseBatchImport*` 符号，参数不同但全是低价值空 LSP 查询 | 参考 OMP useless result / pruning / soft escalation：连续一批 LSP symbol query 无结果后跳过并 forced-final；有命中则清空空探索计数 |
| T-065 | P0 | P7 | Current task contract / evidence-backed path rule | 已完成并复跑通过 | Agent | session `20260708T085426840146Z` 最终只总结最后一份需求文档，没有按 6 点结构输出；回答也有把猜测路径当证据路径的风险 | 参考 OMP runtime context/steering：每次 provider request 注入当前原始用户请求、最终输出结构和证据路径规则；session `20260708T085927874078` 已按 6 点结构输出 |
| T-066 | P1 | P7 | 多项目企业只读压测 | 已完成首轮 | Agent | 验证 `--cwd crcl-open` + `--allow-dir 需求目录` + `--allow-dir zqyl-user-center-service` 的跨项目链路 | session `20260708T085927874078` 通过：定位主项目批量导入真实链路，并把辅助项目结算行/黑名单导入线索区分为支撑或相似模式；拓展服务费结算仍需补项目或确认新建 |

## 风险与决策

| 类型 | ID | 严重度/日期 | 事项 | 状态 | 应对/后续 | OMP 是怎么实现的（建议实现方式） |
|---|---|---|---|---|---|---|
| 风险 | R-001 | 高 | 长任务上下文膨胀 | 已进一步缓解，继续增强 | 已做 OMP 风格 reserve 阈值、auto LLM summary、当前用户请求保留、超大 tool 输出截断和单 system 摘要合并；后续再评估精确 token 预算、输出 reserve、recent 保留 | OMP 按上下文 token 预算触发压缩，给下一轮 prompt/输出预留 reserve，并把早期历史压成 summary；我们当前用字符窗口近似，下一步可升级为 token 估算。 |
| 风险 | R-011 | 高 | 工具 schema 描述与实现不一致会误导模型 | 已关闭首例，持续关注 | 首轮真实小改压测发现 `write_file` 描述宣称可覆盖文件，但实现拒绝覆盖，导致模型把 README 改错 | 工具 schema 是模型的操作说明，应与实现和测试保持一致；已修正 `write_file` 描述并新增测试。 |
| 风险 | R-002 | 高 | 没有 todo 工具 | 已关闭 | 已增加 session 级 todo 工具 | OMP 把 todo 作为会话状态在 UI、session 和 reminder 中同步；我们保留轻量 `todo_read/add/update`，先满足长任务追踪。 |
| 风险 | R-003 | 中 | ask 模式确认过多 | 已关闭 MVP 版 | 已增加 approvalMode、per-tool allow/prompt/deny、session allow/reject | OMP 用 tool approval tier、approvalMode 和 per-tool policy 控制确认；我们保留旧白名单并补 `tool_approval` 和 session decision，危险 shell 仍可显式 deny。 |
| 风险 | R-004 | 中 | 中断时 tool_calls 配对仍可增强 | 已关闭 MVP 版 | deadline、用户中断和输出截断已补齐 | OMP 在 abort、error、skipped、截断时补 synthetic tool result；我们按 call_id 补齐未执行工具，并已处理 `finish_reason=length`。 |
| 风险 | R-005 | 中 | 没有初始 git commit | 已关闭 | 已创建初始 commit | OMP 依赖 session、diff 和工作区状态追踪修改，但不替代 VCS 基线；我们继续用 git commit 作为回滚锚点。 |
| 风险 | R-006 | 低 | 高级能力过早引入 | 受控 | 已只引入多语言轻量 LSP、auto summary、multi-root、startup memory 和 learn，完整 DAP/TUI/subagents 继续后置 | OMP 将 LSP、subagents、AST edit、TUI 等做成可组合高级能力；我们先做无外部依赖的本地 MVP。 |
| 风险 | R-007 | 中 | Prompt injection | 开放 | 文档提示；不信任仓库禁用 yolo | OMP 将仓库 context 视为 advisory，并靠 approval/yolo 策略限制工具权限；我们默认不信任仓库内容，危险工具需确认。 |
| 风险 | R-008 | 中 | P3/P4 变更尚未提交 | 已关闭 | P3 提交 `304fbdf`，P4 提交 `4beb487` | OMP 持久化 session 和 compaction 以支持恢复，但代码里程碑仍要靠 VCS；我们继续阶段性 commit 固化节点。 |
| 风险 | R-009 | 中 | ask_user 会阻塞等待用户 | 已缓解 | 已支持 timeout/default，并自动受剩余 budget 约束；显式 timeout 也会被剩余 budget 夹紧 | OMP 的 approval/elicitation 可以被拒绝或取消并回灌结果；我们给 `ask_user` 加 timeout/default，支持无人值守场景。 |
| 风险 | R-010 | 中 | approval prompt 等待耗尽预算 | 已关闭 MVP 版 | approval prompt 已使用 deadline-aware timed stdin；deadline 已过或等待超时会取消工具调用 | OMP 的 deadline 是 wall-clock absolute timestamp；ACP permission gate 会把 `requestPermission` 和 abort signal 竞争。我们本地版用 `select.select` 按剩余 deadline 等 stdin，超时即取消。 |
| 风险 | R-012 | 中 | 日用命令仍依赖用户手写工具流程 | 已关闭 MVP 版 | 已把默认工作流沉到 system prompt 和 runtime nudge；后续靠真实任务验证效果 | OMP 把默认工作流拆到 system prompt、tool descriptions 和 runtime nudge；我们先做本地 MVP 版，不急着引入完整 ToolChoiceQueue。 |
| 风险 | R-013 | 中 | Memory / skills 注入长期 prompt injection 或陈旧事实 | 已缓解，managed skills 仍暂缓 | memory 和 generated skills 会跨 session 影响模型，错误或恶意内容可能持续放大 | OMP 将 memory 标成 heuristic/advisory，managed skills 隔离且 authored skills 优先；我们已做 advisory 注入、预算限制、learned 字段和 skill description 清洗，managed skills 默认关闭。 |
| 风险 | R-014 | 高 | 重复工具调用循环导致 budget 耗尽且无最终回答 | 已进一步缓解 | 已补最近窗口同名同参工具调用熔断、`ToolResult.useless`、空结果标记、provider-bound useless/superseded pruning、open todo runtime reminder，以及 duplicate-tool forced-final steering；下一步用户本机复跑企业压测验证 | OMP 不靠主步数限制日常任务，而靠 deadline/abort、synthetic tool result、soft tool escalation 小上限、todo/tool-choice steering 和 compaction pruning 共同收敛；我们已落地本地轻量 pruning/steering，并在重复工具后强制下一轮无工具最终回答。 |
| 风险 | R-015 | 高 | 企业项目源码和需求可能被发送到三方 AI API | 用户已确认，full-access 已代跑 | 用户已确认可外发给百炼；早期受限 Codex 环境拒绝代跑，full-access + network enabled 后已由 Agent 代跑 session `20260708T081827983347Z` | OMP 由用户配置 provider 和 permission，但进入模型上下文的内容会发送给 provider；这不是自动隐私隔离，也不是 LCA 内置禁令，需要由用户、provider、permission 和运行环境策略控制。 |
| 风险 | R-016 | 中 | 跨项目运行时 token 配置绑定目标 workspace `.env` | 已关闭 MVP 版 | 已新增 `--env-file` 和 launcher 安装目录 `.env` 自动加载；凭据与目标 `--cwd` 解耦 | OMP 的 provider/model/apiKey 是 runtime 配置，cwd 是项目上下文；我们采用同一分层。 |
| 风险 | R-017 | 中 | 只读任务仍在目标 workspace 写 runtime 状态 | 已关闭 MVP 版 | 当前 `JsonlSessionStore`、todo、patch log 曾默认写入 `--cwd/.local-agent`；2026-07-08 企业项目只读代跑创建了 `.local-agent/sessions/*.jsonl` | 已参考 OMP 默认 session 目录在用户 agent dir 的设计，实现 `--state-dir`；sessions/todos/patch logs 与源码目录解耦，项目 memory/skills 仍保留在 workspace。 |
| 风险 | R-018 | 中 | AGENTS/RULES 长期注入可能与当前任务冲突 | 已缓解，持续关注 | 注入区明确 advisory；system prompt 明确当前用户指令和源码证据优先；RULES 适合短规则，长背景放 AGENTS 或 memory | Claude Code 和 OMP 都把这类上下文作为指导而非硬约束；真正硬限制应靠 permission/hooks。我们先做 advisory 注入，危险动作仍靠 approval。 |
| 风险 | R-019 | 中 | memory consolidation 可能隐式写入陈旧或敏感内容 | 已进一步缓解，持续关注 | 默认 off；显式开启后默认写用户级 state dir 的 memory，只有 `memory_scope=project` 才写项目 `.local-agent/memory`；只接受严格 JSON 的短条目；坏 JSON、空结果、deadline 耗尽、本轮已显式写 memory 时不写 | OMP local memory 位于用户 agent dir，项目 `.omp/` 主要承载人工 context/rules/skills；我们按同一边界把自动 consolidation 默认放入 state dir，项目 memory 仍需显式写入。 |
| 风险 | R-020 | 高 | multi-root allowed dir 没有稳定进入模型操作路径 | 已复跑通过 | session `20260708T065705459243Z` 和 `20260708T070722601499Z` 中模型尝试 `requirements` 并失败；session `20260708T072404789287Z` 已看到 roots 提示但仍未读需求文档，导致代码侧反推 | OMP 会显式提供 cwd/project context/rules，并通过 ToolChoiceQueue / soft tool requirement 做小上限纠偏；我们新增 `[Workspace roots]`、工具观察 roots 提示，并对需求/文档类任务前置 soft tool requirement；session `20260708T083312934017` 已验证先读真实需求文档。 |
| 风险 | R-021 | 中 | 单仓库无法覆盖跨服务需求 | 已记录，持续关注 | 用户确认当前测试项目可能不覆盖完整需求；`拓展服务费结算` 可能在 incentive/settlement 等其他项目 | OMP 依赖用户提供完整 workspace/context；我们记录为压测边界，后续用多个 `--allow-dir` 接入相关项目，或者要求 Agent 明确输出“还需要哪个项目”。 |
| 风险 | R-022 | 高 | 同文件连续切片读取导致任务漂移 | 已复跑通过 | session `20260708T073252231781Z` 中模型连续读取同一大文件多个相邻区间；session `20260708T074609696125Z` 中显式只读任务因“下一步实现”措辞误关 guard；session `20260708T081827983347Z` 中 guard 成功收束但证据一致性待补 | OMP 对病态子循环设置命名小上限，并通过 steering/pruning/deadline/runtime context 收束；我们新增显式只读优先级、repeated read_file guard、forced-final 已读文件证据摘要、原始请求摘要和已读一致性规则；session `20260708T083312934017` 已按 5 点结构输出。 |
| 风险 | R-023 | 高 | 同一空搜索词跨路径扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T082703005777Z` 中模型对同一无结果关键词反复切换 path 搜索，因参数不同绕过 exact duplicate guard | OMP 会把 no-op/useless tool result 降权、prune，并对 soft tool escalation 设置小上限；我们新增 search pattern 级 guard，按 pattern 而非完整参数统计无结果搜索，并在阈值后 forced-final。 |
| 风险 | R-024 | 高 | path escape 纠偏不足会让模型漏读主项目 | 已补并复跑通过 | session `20260708T084322924403Z` 中模型误用父目录后没有恢复，最终只分析辅助项目 | OMP 会把 cwd/project context 和可行动工具观察持续放回上下文；我们把 roots hint 放进公共 path escape 错误，session `20260708T085927874078` 已验证可恢复。 |
| 风险 | R-025 | 高 | LSP 空 query 扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T084714338485Z` 中模型猜测大量不存在符号名，参数不同绕过同参重复 guard | OMP 会把 useless result/pruning/soft escalation 结合使用；我们新增 LSP symbol 空 query 小上限并 forced-final。 |
| 风险 | R-026 | 高 | 最终回答结构和证据路径可能漂移 | 已补并复跑通过 | session `20260708T085426840146Z` 最终只总结最后一个需求文档；此前也出现把未验证路径当下一步建议路径的倾向 | OMP 将当前任务、runtime state 和 tool evidence 持续放进 provider context；我们新增 Current task contract 和 evidence-backed path rule。 |
| ADR | ADR-001 | 2026-07-07 | 优先采纳 OMP 成熟设计，按本地目标裁剪 | 已接受 | 好设计可直接采用，复杂度按需收敛 | OMP 是重要参考实现；我们不为了“避免复制”而绕开好设计。采用标准是收益是否大于复杂度，并且不破坏个人本地使用、封闭 VM、无公网依赖和第一阶段 MVP 边界。 |
| ADR | ADR-002 | 2026-07-07 | max_steps 只作为防失控保险丝 | 已落地 | 默认值已改为 0，不限步 | OMP 的 stepCounter 主要用于 telemetry，终止靠无 tool_calls、deadline、abort；我们把 `max_steps` 仅作为显式保险丝。 |
| ADR | ADR-003 | 2026-07-07 | todo、ask_user、per-tool approval 是主功能 | 已落地 | P3 已实现 | OMP 将 todo、approval、elicitation 做成可观测会话能力；我们 P3 先做终端轻量版，后续再补 UI 化。 |
| ADR | ADR-004 | 2026-07-07 | 第一阶段 memory 用 Markdown | 已接受 | 后续看需求升级 | OMP 有本地 memory 后台抽取，并在启动时注入 Memory Guidance；我们先用项目 Markdown，后续再做自动整理。 |
| ADR | ADR-005 | 2026-07-07 | Patch 先用 anchored patch，不上 AST edit | 已接受 | P5 再评估 preview/rollback | OMP 的 edit/apply_patch 结合审批、渲染和更丰富编辑链路；我们先做 anchored patch 与 dry_run，AST edit 后置。 |
| ADR | ADR-006 | 2026-07-07 | 长需求建议放文件让 Agent read_file | 已接受 | README 已写推荐工作流 | OMP 会自动发现 context files，也支持按需读取 Markdown；我们让复杂需求落 md，再用 `read_file` 分段注入。 |
| ADR | ADR-007 | 2026-07-07 | 封闭 VM 下不做公网搜索/自动下载 | 已接受 | 依赖提前准备 | OMP 可接 web、MCP、插件等外部能力，并由配置和 approval 管控；我们封闭 VM 默认离线，依赖提前准备。 |
| ADR | ADR-008 | 2026-07-07 | Excel 给人看，Markdown 给开发协作 Agent 读 | 已接受 | 持续同步本文件和 `project-status.md` | 这套项目管理文档服务于开发 LCA 的过程，不是 LCA 运行时 memory。我们以 Markdown 作为开发项目事实源，Excel 只生成展示视图。 |
| ADR | ADR-009 | 2026-07-07 | OMP 核心架构笔记单独固化 | 已接受 | 见 `docs/omp-core-architecture-notes.md` | OMP 的关键判断来自源码和 docs，需要沉淀成项目 context；我们单独维护笔记，避免每次重复扫源码。 |
| ADR | ADR-010 | 2026-07-07 | P6 优先实现 OMP 默认工作流的本地 MVP 版 | 已接受并落地 | 已做 system prompt、tool descriptions、轻量 runtime nudge | OMP 的体验来自系统上下文、工具规范和 runtime 纠偏共同作用；我们直接采纳分层设计，但暂不搬入完整 ToolChoiceQueue、subagents 等复杂能力。 |
| ADR | ADR-011 | 2026-07-07 | 默认采用 OMP 风格 auto summary | 已接受并落地 | `summary_mode=auto` 默认，`local` / `llm` 可选 | 小历史不摘要；超过 reserve 阈值才调用已配置 AI API 做 LLM summary；失败回退 local summary。 |
| ADR | ADR-012 | 2026-07-07 | LSP 第一版做轻量多语言静态工具 | 已接受并落地 | 不启动外部 language server | 满足 Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics，不引入 npm/pip 依赖或后台进程；完整 LSP/DAP 后置。 |
| ADR | ADR-013 | 2026-07-07 | Memory / skills 按 OMP 思路分阶段本地化 | 已接受并部分落地 | Markdown memory 启动注入、显式 `learn` 和 authored skills discovery 已完成 | 后续最后才评估 managed skills/autolearn；不引入 Hindsight、Mnemopi、向量库或插件市场。 |
| ADR | ADR-014 | 2026-07-08 | Runtime 问题优先采用 OMP 已验证设计 | 已接受 | 直接采纳 OMP 的成熟机制，再按本地/封闭 VM/MVP 边界裁剪 | 对 deadline、compaction、permission、synthetic tool result、todo/tool-choice steering、pruning 这类 OMP 已覆盖的问题，不再为了“自己造一套”而绕开；LCA 不内置“企业数据不能外发”禁令，但必须尊重当前执行宿主或企业环境的策略拦截。 |
| ADR | ADR-015 | 2026-07-08 | 人工上下文按 AGENTS/RULES 分层 | 已接受并落地 | `AGENTS.md` 适合启动背景，`RULES.md` 适合短 sticky rules；二者不同于长期 memory、skills 和 session summary | 参照 Claude Code 的 CLAUDE.md/rules 与 OMP 的 AGENTS.md/RULES.md 思路；LCA 使用用户级配置目录和项目 `.local-agent` 目录做本地 MVP。 |
| ADR | ADR-016 | 2026-07-08 | Session memory consolidation 默认关闭，开启后默认写 state memory | 已接受并落地 | 这一步不同于只发给模型的 context compaction；默认 off 保护只读分析，开启后默认写用户级 state dir；只有显式 `memory_scope=project` 才写项目 `.local-agent/memory` | 显式 `auto/llm` 后才用当前 provider 抽取长期记忆；memory 仍是 advisory，当前用户指令和源码证据优先。 |
| ADR | ADR-017 | 2026-07-08 | 解决 runtime/工具/上下文问题时先查 OMP 做法 | 已接受 | 先找 OMP 已验证设计，再按 LCA 本地个人 Agent、封闭 VM、单 Agent 和无自动下载边界裁剪 | 用户明确要求后续解决问题都参考 OMP；allowed-dir 问题按 OMP 显式提供 cwd/project context 的思路，落地为 `[Workspace roots]` 注入。 |

## P7 综合压测问题

| ID | 优先级 | 状态 | 现象 | OMP 对应方式 | LCA 措施 |
|---|---|---|---|---|---|
| PT-001 | P0 | 已进一步缓解并复测 | LCA 自身只读压测中重复 `search_code` / `todo_read`，最终由 `budget_seconds=240` 截断；修复后 session `20260708T025519414693Z` 按要求收尾。用户本机企业压测 session `20260708T062614211387Z` 又在 `feePlan` 搜索上重复，触发重复工具硬停且没有最终分析。 | OMP 组合使用 deadline/abort、synthetic tool result、soft tool escalation 小上限、todo/tool-choice steering、useless/superseded pruning，而不是主循环步数。 | 已实现最近窗口重复工具调用熔断、`ToolResult.useless`、空搜索/LSP useless 标记、provider-bound pruning、open todo reminder；并新增 duplicate-tool forced-final steering，重复工具后下一轮不给工具 schema，强制模型基于已有证据回答。 |
| PT-002 | P0 | 已澄清并通过 full-access 代跑 | 用户已确认可外发给百炼；早期 Codex 受限环境拒绝代跑企业私有代码/需求到三方 API，full-access + network enabled 后已由 Agent 代跑 session `20260708T081827983347Z`。 | OMP 由用户配置 provider 和 permission；进入模型上下文的内容会发送给 provider，不是自动隐私隔离，也不是默认禁止外发。 | LCA 不内置“企业数据不能外发”禁令；是否可跑由用户授权、provider、permission 和当前宿主环境共同决定。 |
| PT-003 | P1 | 已关闭 MVP 版 | `--cwd` 指向企业项目后，LCA 仓库 `.env` 不会自动加载，需要手动 source。 | OMP 的 provider/model/apiKey 属于 runtime 配置，cwd 是项目上下文。 | 已新增 `--env-file`；`./agent` 自动加载安装目录 `.env`，再加载目标 workspace `.env`。 |
| PT-004 | P1 | 已关闭 MVP 版 | prompt 中出现 `lsp_workspace_symbols` / `lsp_document_symbols` 时，模型会搜索这些旧概念而非直接用 `lsp_symbols`。 | OMP 通过准确工具 schema、tool discovery 和 tool-choice steering 降低工具名漂移。 | 已增加 `lsp_workspace_symbols` / `lsp_document_symbols` 只读兼容别名，均复用 `lsp_symbols` handler 和 schema；system prompt 已说明它们是 alias。 |
| PT-005 | P1 | 已进一步缓解 | compaction 能让上下文装下，但不自动保证任务收敛。 | OMP 将 compaction 与 todo reminder、tool choice、queued steering、deadline/abort、pruning 组合。 | 已补重复工具熔断、provider-bound useless/superseded pruning、open todo runtime reminder 和 duplicate-tool forced-final steering；当前先不上完整 ToolChoiceQueue，等同一企业命令复跑后再判断。 |
| PT-006 | P2 | 本地只读扫描完成，联网 LCA 未由 Codex 代跑 | `zqyl-user-center-service` 为 Maven 多模块项目，约 5309 个 Java/XML/YAML 文件；`crcl-open` 约 2584 个 Java/XML/YAML 文件。本地扫描确认需求关键词可定位到投资方案、Charge、TradeBg、结算单枚举等方向，但百炼联网 LCA 在当前 Codex 执行环境被阻断。 | OMP 可组合更完整工程工具、subagents/reviewer 和 compaction/pruning。 | 用户可在自己的允许环境中用 LCA 跑真实联网只读分析；LCA 已补轻量 pruning / steering，后续再评估更完整工程工具。 |
| PT-007 | P1 | 已关闭并本地验证 | 企业项目只读代跑曾在目标仓库创建 `.local-agent/sessions/20260708T053955405637Z.jsonl`，导致 `git status` 新增 `?? .local-agent/`。提交 `cb7400d` 后，本地配置解析确认 `crcl-open` 默认 state dir 为用户级 `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-crcl-open-966d4fe7a33b`，目标仓库未发现 `.local-agent`。 | OMP 默认 session 目录在用户 agent dir 的 `sessions/<cwd-encoded>` 下，而不是直接写进目标 repo。 | 已实现 `--state-dir` / `AGENT_STATE_DIR`，默认把 sessions/todos/patch logs 放到用户级状态目录；项目 memory/skills 仍保留在 workspace。 |
| PT-008 | P2 | 已记录 | `crcl-open/crcl-open` 本身已有大量 modified 文件，压测前后需要快照才能证明 LCA 是否污染业务文件。当前版本已把 runtime state 移出目标仓库，但真实联网压测仍要记录前后 `git status`。 | OMP 的更完整 task/worktree 能力可隔离 WIP；普通 CLI 也需要清楚记录 cwd/worktree 状态。 | 后续企业压测前先记录 `git status --short` 到 LCA 压测记录，压测后对比，不在目标 repo 写快照。 |
| PT-009 | P1 | 已关闭并补测试 | memory consolidation 可能让只读压测产生隐式项目 memory 写入。 | OMP/Claude Code 的自动记忆是启发式长期上下文，必须与普通 context compaction 区分；OMP local memory 位于用户 agent dir，默认策略要避免只读任务隐式写项目文件。 | 默认 `off`；开启后默认 `--memory-scope state` 写 state dir；只有显式 `project` 才写 `.local-agent/memory`；新增 runtime 回归测试覆盖默认 off、默认 state 写入、显式 project 写入。 |
| PT-010 | P0 | 已由 full-access 复跑验证进一步缓解 | 企业需求只读压测中，模型围绕 `feePlan` 在相近目录重复 `search_code`，最终硬停且无最终需求落点分析。 | OMP 把重复/软强制工具看作 runtime steering 问题：小上限后切回回答或换策略，而不是无限工具探索。 | session `20260708T081827983347Z` 未再因 `feePlan` 重复搜索硬停，并输出最终回答；后续问题转为 forced-final 证据一致性。 |
| PT-011 | P0 | 已由 full-access 复跑验证通过 | `--allow-dir` 具体路径进入了提示和工具观察，但模型仍未稳定 `read_file` 真实需求文档。 | OMP 会把 cwd/project context 放进运行上下文，并用 ToolChoiceQueue / soft tool requirement 在模型偏航时限制工具和持续纠偏。 | session `20260708T081827983347Z` 前两步即读取两份 allowed-dir 需求 md，说明 soft requirement 已稳定生效。 |
| PT-012 | P1 | 已记录 | 用户确认当前测试项目可能无法完全覆盖需求，结算需求可能需要其他项目配合。 | OMP 需要完整 workspace/context 才能做跨项目判断；证据缺失时应输出不确定点和需要补充的项目。 | 后续压测把相关项目也作为 `--allow-dir`，或让 Agent 先列出需要补充的项目/服务。 |
| PT-013 | P0 | 已由 full-access 复跑验证通过收束 | allowed-dir 需求文档已前置读取，但模型后续连续读取同一个大文件相邻区间，最终偏离原始 5 点输出。 | OMP 对病态工具子循环用小上限和 runtime steering 收束，不靠主步数。 | session `20260708T081827983347Z` 再次连续读取 `HandleCrclServiceApplication.java`，repeated read-file guard 触发并成功 forced-final，没有卡死。 |
| PT-014 | P0 | 已由 full-access 复跑验证部分通过 | session `20260708T074609696125Z` 已先读两份需求 md，但 prompt 中“如果下一步要实现”误触编辑词排除，导致只读 drift guard 未开启；最终回答还错误声称 V1.1 未读。 | OMP 将当前用户硬约束和 runtime state 持续放入上下文，并用 steering/tool-choice 小上限让模型回到原始任务；显式 readonly/permission 语义不应被后续普通业务词覆盖。 | session `20260708T081827983347Z` 证明显式只读优先生效，guard 触发成功；但仍暴露 PT-015：forced-final 后可能把已读文件称未读。 |
| PT-015 | P0 | 已由复跑验证收束 | session `20260708T081827983347Z` 已由 Agent 在 full-access 环境代跑；allowed-dir 前置读和 repeated read-file guard 均生效，但最终回答仍把已读 `QueryFeePlanInfoReq.java` 写成 not yet read，且未完全按用户 5 点结构输出。 | OMP 将当前任务、runtime state、tool evidence 和 steering 一起放回上下文，并用 tool-choice/forced-final 明确模型下一步必须回答什么，而不是只提醒“不要继续读”。 | forced-final steering 已注入原始用户请求摘要和已读一致性规则；session `20260708T083312934017` 最终按 5 点结构输出，未再出现已读文件称未读。 |
| PT-016 | P0 | 已补并复跑通过 | session `20260708T082703005777Z` 中模型反复搜索 `exceptionCoreEnterprise` / `ExceptionCoreEnterprise`，路径从全仓到多个子目录变化，绕过同参重复 guard。 | OMP 将 useless tool result 进入 pruning/steering，并对低价值工具循环设置小上限。 | 已新增 search_code 空搜索词级 guard；同一 pattern 忽略大小写多次无结果后跳过后续搜索并 forced-final。session `20260708T083312934017` 未再出现该循环。 |
| PT-017 | P0 | 已补并复跑通过 | session `20260708T084322924403Z` 中模型误用父目录 `/Users/chengming/mycode/project/crcl-open`，工具错误未提示正确 primary workspace。 | OMP 通过 cwd/project context 和工具观察提示模型可行动路径。 | 公共 path resolver 已在 path escape 错误中列出 primary workspace/allowed dirs；session `20260708T085927874078` 已恢复到 `.` 主项目。 |
| PT-018 | P0 | 已补并复跑通过 | session `20260708T084714338485Z` 中模型连续猜不存在的 LSP symbol query。 | OMP 对 useless result 和 soft tool escalation 设小上限。 | 已新增 LSP symbol 空 query guard；session `20260708T085927874078` 未再卡死。 |
| PT-019 | P0 | 已补并复跑通过 | session `20260708T085426840146Z` 最终回答只总结最后一个需求文档，没有按 6 点结构输出。 | OMP 将当前任务和 runtime evidence 持续注入 provider context。 | 已新增 Current task contract 和 evidence-backed path rule；session `20260708T085927874078` 按 6 点结构输出。 |

## P5 收口结论

| 项目 | 结论 | 依据 |
|---|---|---|
| 主链路 | 通过 | 百炼真实小改复测已跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff。 |
| 测试 | 通过 | P5 收口时 90 个 unittest、compileall、xlsx 检查、diff check 均通过；P7 当前代码已跑通 152 个 unittest、compileall 和 diff check。 |
| 日用入口 | 通过 | README 已补只读分析和小改任务命令模板。 |
| 开放风险 | 可接受 | shell 仍非沙箱、prompt injection 仍需靠审批和封闭 VM；token budget / output reserve / managed skills 继续后置评估。 |
| 下一阶段 | P7 轻量高级能力真实压测后续 | 已验证默认工作流、auto summary、多语言轻量 LSP、multi-root、startup memory、learn、authored skills、runtime state dir 和多项目只读压测主链路；下一步继续做回答准确性评估，尤其是跨项目缺失证据时的措辞和实现前二次验证。 |

## 推荐工作流

| 步骤 | 操作 | 建议命令/做法 | 原因 | 适用阶段 | 备注 |
|---:|---|---|---|---|---|
| 1 | 确认需求 | 需求复杂时先写到 `docs/requirements/*.md` | 避免长 prompt 挤占上下文 | P2+ | 让 Agent 分段 `read_file` |
| 2 | 启动 Agent | `./agent "描述当前项目"`；跨项目用 `./agent --cwd /path/to/project ...` | 一键启动，默认当前目录为 workspace；跨项目时安装目录 `.env` 会自动作为 `--env-file` 加载 | P3+ | token 可来自环境变量、显式 `--env-file` 或 `.env` |
| 3 | 权限模式 | 默认 always-ask；写操作可用 write；可信重复工具可 tool-approval allow；信任仓库才 yolo | 平衡安全和效率 | P5+ | `--tool-approval run_tests=allow,shell=prompt,write_file=deny` 可用 |
| 4 | 修改前 | Agent 默认会按需 `list_files/read_file/search_code/lsp_*` | 减少凭空猜测 | P6+ | 用户不必手写工具顺序 |
| 5 | 修改时 | 修改已有文件使用 `apply_patch`，写入前优先 `dry_run=true` | 保留 hash/old_text 校验 | P1+ | 已写入默认 prompt 和工具描述 |
| 6 | 修改后 | 默认应 `run_tests + git_diff` | 形成可验证闭环 | P6+ | 自然语言任务默认走验证闭环 |
| 7 | 长任务 | 默认维护 todo，再做实现，再验证；默认 `--summary-mode auto` 按阈值触发 LLM summary | 防止漏任务并治理长上下文 | P7+ | Auto summary 需真实 provider 压测 |
| 8 | 多目录任务 | `./agent --cwd /path/to/code --allow-dir /path/to/requirements "读取需求并修改代码"` | 支持需求文档目录 + 代码项目目录 | P7+ | allowed dir 只扩展文件/search/LSP/patch 工具 |
| 9 | 常驻上下文 | 用户级 `AGENTS.md` 放个人偏好，项目 `.local-agent/AGENTS.md` 放项目背景；短规则放对应 `RULES.md` | 减少重复提示，并让短规则跨长会话可见 | P7+ | 可用 `AGENT_CONFIG_DIR` 改用户级目录；都是 advisory |
| 10 | 项目记忆 | 把长期约定写入 `.local-agent/memory/*.md`，或让 Agent 在明确要求时调用 `learn`；需要自动整理时开启 `--memory-consolidation auto`，默认写 state memory，团队共享才加 `--memory-scope project` | 减少重复交代项目约定 | P7+ | memory 是 advisory，当前用户指令优先；默认不隐式写项目文件 |
| 11 | 本地 skills | 将可复用工作流写到 `.local-agent/skills/<name>/SKILL.md`，带 `name` / `description` frontmatter | 降低重复提示成本 | P7+ | 启动只注入 metadata，正文按需 read_file |
| 12 | 有歧义 | 使用 `ask_user` 中途问用户 | 避免模型瞎猜 | P3+ | 非交互会返回明确错误 |
| 13 | 同步 Excel | `python3 scripts/sync_project_excel.py` | Excel 是开发展示产物，Markdown 是开发事实源 | P2+ | 无第三方依赖 |
