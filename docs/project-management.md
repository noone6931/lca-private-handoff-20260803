# Local Coding Agent 项目管理数据源

更新时间：2026-07-07

本文件是项目管理 Excel 的唯一数据源。更新项目进度时优先修改本 Markdown，然后运行：

```bash
python3 scripts/sync_project_excel.py
```

脚本会读取本文件中的二级标题和 Markdown 表格，生成 `docs/local-coding-agent-project-management.xlsx`。不要手工编辑 Excel 后再把状态当作事实源。

## 总览

| 字段 | 当前值 | 说明 |
|---|---|---|
| 最终目标 | 个人本地编程助手 Agent | 本地优先、封闭 VM 可用、只访问指定 AI API，能读代码、搜代码、改代码、跑测试、生成 diff、沉淀项目记忆。 |
| 当前阶段 | P5：安全与恢复增强进行中 | 最小版本地 context compaction、patch preview 和 rollback 已完成；正在继续增强 synthetic tool result 和恢复能力。 |
| 推荐入口 | `./agent "阅读当前项目"` | 自动设置 `PYTHONPATH=src`，默认当前目录为 workspace。 |
| Token 配置 | `.env` 或环境变量 | `.env` 可写 `DASHSCOPE_API_KEY=...`，该文件已被 `.gitignore` 忽略。 |
| 测试数 | 62 | 完整 unittest 通过；compileall 通过。 |
| 默认 budget_seconds | 600 | 单次任务默认 10 分钟墙钟预算；`--budget-seconds 0` 可关闭。 |
| 默认 max_steps | 0 | 表示不限步；仅在用户显式设置时作为防失控保险丝。 |
| 预算执行 | 细粒度 | LLM 请求和 shell/run_tests timeout 会按剩余预算夹紧；deadline 到期会补齐未执行工具结果。 |
| Context compaction | 本地确定性 | 超过约 60000 字符时折叠早期历史，保留最近消息，并注入未完成 todo。 |
| Synthetic tool result | 部分完成 | deadline 到期和用户中断工具执行时会补齐剩余 tool_call 的 tool result。 |
| Patch preview | 已完成 | `apply_patch dry_run=true` 只校验并返回 diff，不写文件。 |
| Patch rollback | 已完成 MVP 版 | `rollback_patch` 只回滚本 session 的 patch 记录，且要求当前文件仍匹配 after tag。 |
| OMP 核心判断 | 已固化 | 见 `docs/omp-core-architecture-notes.md`。 |

## 阶段路线图

| 阶段 | 名称 | 目标 | 状态 | 完成度 | 下一步判定 |
|---|---|---|---|---:|---|
| P0 | OMP 分析与方案设计 | 看懂 OMP，确定我们自己的 MVP 路线 | 已完成 | 100% | 已形成不照搬、做简化版的原则。 |
| P1 | 基础 Agent 闭环 | 接 AI API，完成读、搜、改、测、diff、session、memory | 已完成 | 100% | 已创建初始 git commit，基础闭环可回滚。 |
| P2 | 项目管理与可见性 | 项目状态、路线图、todo、决策记录一目了然 | 已完成 | 100% | Excel + Markdown 项目状态已建立。 |
| P3 | 长任务运行基础 | budget_seconds、max_steps 不限步、todo、ask_user、per-tool approval、一键启动 | 已完成 | 100% | 已具备真实需求的基础运行体验。 |
| P4 | 上下文治理 | 简单 summary/compaction，工具输出折叠，支持长需求文件 | 已完成 MVP 版 | 100% | 后续可评估 LLM summary 和 token 级阈值。 |
| P5 | 安全与恢复增强 | synthetic tool result、patch preview、rollback | 进行中 | 65% | 已覆盖 deadline、用户中断、patch preview 和 rollback；下一步处理 length 截断。 |
| P6 | 高级工程能力 | LSP、TUI、subagents、reviewer、AST edit、DAP | 暂缓 | 0% | 日用闭环稳定后再评估。 |

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
| ask_user | 已完成 | 交互式终端可中途提问 | 避免需求歧义时硬猜 | 保持非交互错误提示 |
| Per-tool approval | 已完成 | `--auto-approve-tools` / `AGENT_AUTO_APPROVE_TOOLS` | ask 模式工具白名单 | 按使用反馈微调 |
| 一键启动 | 已完成 | `./agent` | 自动设置 `PYTHONPATH` 并以当前目录为 workspace | 日常入口 |
| `.env` 加载 | 已完成 | workspace `.env` | 可放 `DASHSCOPE_API_KEY`，被 gitignore | 避免重复 export |
| OMP 核心架构笔记 | 已完成 | `docs/omp-core-architecture-notes.md` | 固化主循环、deadline、compaction 结论 | 后续设计依据 |
| 本地 Context Compaction | 已完成 | `context_char_budget` / `context_recent_messages` | 折叠早期历史，保留最近消息，注入未完成 todo | 后续评估 LLM summary |
| Synthetic tool result | 部分完成 | deadline 到期和用户中断工具执行时补齐 tool result | 避免 session 留下未配对 tool_calls | 后续处理 `finish_reason=length` |
| Patch preview | 已完成 | `apply_patch dry_run=true` | 复用 anchored 校验并返回 diff，不写文件 | 后续评估 rollback |
| Patch rollback | 已完成 MVP 版 | `rollback_patch` | 校验当前文件 hash 后恢复 patch 前内容 | 继续真实任务验证 |
| 测试覆盖 | 已完成 | 当前 62 个测试通过 | unittest + compileall 通过 | 继续补 P5 边界 |

## 下一步 Todo

| ID | 优先级 | 阶段 | 任务 | 状态 | 负责人 | 为什么重要 | 完成标准 |
|---|---|---|---|---|---|---|---|
| T-001 | P0 | P2 | 确认项目管理基线 | 已完成 | User + Agent | 统一目标、阶段和下一步 | Excel 已复核；Markdown 看板已建立 |
| T-002 | P0 | P2 | 建立 `docs/project-status.md` | 已完成 | Agent | 让 Agent 可读项目路线 | 文档已存在 |
| T-003 | P0 | P2 | 做第一次 git commit | 已完成 | User + Agent | 建立干净回滚基线 | 提交 `2c4348b` 已创建 |
| T-004 | P0 | P3 | 加入 budget-seconds/deadline | 已完成 | Agent | 用时间预算控制长任务 | CLI/env/config 已支持；默认 600 秒 |
| T-005 | P0 | P3 | max_steps 改为不限步保险丝 | 已完成 | Agent | 步数不限制日常任务 | 默认值 0；显式设置才作为保险丝 |
| T-006 | P0 | P3 | 实现 todo 工具 | 已完成 | Agent | 长任务需要显式状态 | `todo_read/add/update` 可用 |
| T-007 | P0 | P3 | 实现 ask_user 工具 | 已完成 | Agent | 需求歧义时不硬猜 | 交互式终端可提问 |
| T-008 | P0 | P3 | 增加 per-tool approval policy | 已完成 | Agent | 减少重复敲 y | `--auto-approve-tools` 可用 |
| T-009 | P1 | P2 | 更新 README 安全工作流 | 已完成 | Agent | 说明预算、审批和 shell 边界 | README 已更新 |
| T-010 | P1 | P4 | 简单上下文 summary | 已完成 | Agent | 长任务会被全量历史拖垮 | 已实现本地 deterministic compaction，并注入未完成 todo |
| T-011 | P1 | P5 | 补 synthetic tool result | 部分完成 | Agent | 中断/异常时避免 orphan tool_calls | deadline 到期和用户中断已补齐；模型输出截断待评估 |
| T-012 | P1 | P5 | Patch preview/rollback 设计 | 已完成 MVP 版 | Agent | 进一步降低改错风险 | 已完成 dry_run 预览和 session 级 hash 校验 rollback |
| T-013 | P2 | P6 | 评估 LSP/TUI/subagents/AST edit | 暂缓 | User + Agent | 高级能力强但复杂 | P4/P5 稳定后再取舍 |
| T-014 | P0 | P3 | 提交 P3 变更 | 已完成 | User + Agent | 把本轮 P3 工作固化为第二个 commit | 提交 `304fbdf` 已创建 |
| T-015 | P0 | P2 | Markdown 模板同步 Excel | 已完成 | Agent | 避免手工同步 Excel 出错 | `scripts/sync_project_excel.py` 可从本文件生成 Excel |
| T-016 | P0 | P3 | 细化 budget deadline 执行检查 | 已完成 | Agent | 让时间预算从软闸变成实际主控 | LLM/tool timeout 按剩余预算夹紧；未执行工具有 synthetic result |
| T-017 | P0 | P4 | 提交 P4 compaction 变更 | 已完成 | Agent | 把上下文治理节点固化为 commit | 提交 `4beb487` 已创建 |
| T-018 | P1 | P5 | 处理模型输出截断 synthetic result | 未开始 | Agent | `finish_reason=length` 可能产生不完整工具参数 | LLM 层暴露 finish_reason 并补可恢复提示 |
| T-019 | P1 | P5 | 实现 patch dry-run preview | 已完成 | Agent | 写入前先看 diff，减少误改风险 | `apply_patch dry_run=true` 不写文件并返回 diff |
| T-020 | P1 | P5 | 实现 session 级 patch rollback | 已完成 | Agent | 写错后可以在安全条件下恢复 | `rollback_patch` 校验 after tag 后恢复 before_text |

## 风险与决策

| 类型 | ID | 严重度/日期 | 事项 | 状态 | 应对/后续 | OMP 是怎么实现的（建议实现方式） |
|---|---|---|---|---|---|---|
| 风险 | R-001 | 高 | 长任务上下文膨胀 | 已缓解 | 已做本地 compaction；后续评估 token 级阈值和 LLM summary | OMP 用 token 估算触发 compaction，保留 recent 和输出 reserve；我们短期用字符阈值，后续补 token 估算和 LLM summary。 |
| 风险 | R-002 | 高 | 没有 todo 工具 | 已关闭 | 已增加 session 级 todo 工具 | OMP 把 todo 作为会话状态在 UI、session 和 reminder 中同步；我们保留轻量 `todo_read/add/update`，先满足长任务追踪。 |
| 风险 | R-003 | 中 | ask 模式确认过多 | 已缓解 | 已增加 per-tool approval 白名单 | OMP 用 tool approval tier、approvalMode 和 per-tool policy 控制确认；我们保留白名单，危险 shell 仍强制确认。 |
| 风险 | R-004 | 中 | 中断时 tool_calls 配对仍可增强 | 已缓解 | deadline 和用户中断已补齐；输出截断场景后续处理 | OMP 在 abort、error、skipped、截断时补 synthetic tool result；我们按 call_id 补齐未执行工具，`finish_reason=length` 单独处理。 |
| 风险 | R-005 | 中 | 没有初始 git commit | 已关闭 | 已创建初始 commit | OMP 依赖 session、diff 和工作区状态追踪修改，但不替代 VCS 基线；我们继续用 git commit 作为回滚锚点。 |
| 风险 | R-006 | 低 | 高级能力过早引入 | 受控 | P6 暂缓，先稳定日用闭环 | OMP 将 LSP、subagents、AST edit、TUI 等做成可组合高级能力；我们 P6 后置，先稳定单 Agent 闭环。 |
| 风险 | R-007 | 中 | Prompt injection | 开放 | 文档提示；不信任仓库禁用 yolo | OMP 将仓库 context 视为 advisory，并靠 approval/yolo 策略限制工具权限；我们默认不信任仓库内容，危险工具需确认。 |
| 风险 | R-008 | 中 | P3/P4 变更尚未提交 | 已关闭 | P3 提交 `304fbdf`，P4 提交 `4beb487` | OMP 持久化 session 和 compaction 以支持恢复，但代码里程碑仍要靠 VCS；我们继续阶段性 commit 固化节点。 |
| 风险 | R-009 | 中 | ask_user 会阻塞等待用户 | 开放 | 长任务需求尽量写清；后续评估 ask_user 超时策略 | OMP 的 approval/elicitation 可以被拒绝或取消并回灌结果；我们后续给 `ask_user` 加 timeout/default，支持无人值守。 |
| ADR | ADR-001 | 2026-07-07 | 不照搬 OMP，只借鉴能力类型和边界 | 已接受 | 每个能力做简化版 | OMP 是平台型 Agent，能力面很宽；我们只借主循环、deadline、compaction、approval、memory 等边界，逐个做简化版。 |
| ADR | ADR-002 | 2026-07-07 | max_steps 只作为防失控保险丝 | 已落地 | 默认值已改为 0，不限步 | OMP 的 stepCounter 主要用于 telemetry，终止靠无 tool_calls、deadline、abort；我们把 `max_steps` 仅作为显式保险丝。 |
| ADR | ADR-003 | 2026-07-07 | todo、ask_user、per-tool approval 是主功能 | 已落地 | P3 已实现 | OMP 将 todo、approval、elicitation 做成可观测会话能力；我们 P3 先做终端轻量版，后续再补 UI 化。 |
| ADR | ADR-004 | 2026-07-07 | 第一阶段 memory 用 Markdown | 已接受 | 后续看需求升级 | OMP 有本地 memory 后台抽取，并在启动时注入 Memory Guidance；我们先用项目 Markdown，后续再做自动整理。 |
| ADR | ADR-005 | 2026-07-07 | Patch 先用 anchored patch，不上 AST edit | 已接受 | P5 再评估 preview/rollback | OMP 的 edit/apply_patch 结合审批、渲染和更丰富编辑链路；我们先做 anchored patch 与 dry_run，AST edit 后置。 |
| ADR | ADR-006 | 2026-07-07 | 长需求建议放文件让 Agent read_file | 已接受 | README 已写推荐工作流 | OMP 会自动发现 context files，也支持按需读取 Markdown；我们让复杂需求落 md，再用 `read_file` 分段注入。 |
| ADR | ADR-007 | 2026-07-07 | 封闭 VM 下不做公网搜索/自动下载 | 已接受 | 依赖提前准备 | OMP 可接 web、MCP、插件等外部能力，并由配置和 approval 管控；我们封闭 VM 默认离线，依赖提前准备。 |
| ADR | ADR-008 | 2026-07-07 | Excel 给人看，Markdown 给 Agent 读 | 已接受 | 持续同步本文件和 `project-status.md` | OMP 的事实上下文来自 session、memory 和 Markdown 规则，UI 只是视图；我们以 Markdown 为事实源，Excel 只生成展示。 |
| ADR | ADR-009 | 2026-07-07 | OMP 核心架构笔记单独固化 | 已接受 | 见 `docs/omp-core-architecture-notes.md` | OMP 的关键判断来自源码和 docs，需要沉淀成项目 context；我们单独维护笔记，避免每次重复扫源码。 |

## 推荐工作流

| 步骤 | 操作 | 建议命令/做法 | 原因 | 适用阶段 | 备注 |
|---:|---|---|---|---|---|
| 1 | 确认需求 | 需求复杂时先写到 `docs/requirements/*.md` | 避免长 prompt 挤占上下文 | P2+ | 让 Agent 分段 `read_file` |
| 2 | 启动 Agent | `./agent "描述当前项目"` | 一键启动，默认当前目录为 workspace | P3+ | token 可来自环境变量或 `.env` |
| 3 | 权限模式 | 默认 ask；只读可 auto-read；可信重复工具可 auto-approve；信任仓库才 yolo | 平衡安全和效率 | P3+ | `--auto-approve-tools run_tests,git_diff` 可用 |
| 4 | 修改前 | 要求 Agent `read_file/list_files/search_code` | 减少凭空猜测 | P1+ | 已在 prompt 中约束 |
| 5 | 修改时 | 必须 `apply_patch`，修改已有文件不使用 `write_file` | 保留 hash/old_text 校验 | P1+ | 已支持插入模式 |
| 6 | 修改后 | 必须 `run_tests + git_diff` | 形成可验证闭环 | P1+ | 初始 commit 后 diff 更好用 |
| 7 | 长任务 | 先 todo，再做实现，再验证 | 防止漏任务 | P3+ | `todo_read/add/update` 已可用 |
| 8 | 有歧义 | 使用 `ask_user` 中途问用户 | 避免模型瞎猜 | P3+ | 非交互会返回明确错误 |
| 9 | 同步 Excel | `python3 scripts/sync_project_excel.py` | Excel 是产物，Markdown 是事实源 | P2+ | 无第三方依赖 |
