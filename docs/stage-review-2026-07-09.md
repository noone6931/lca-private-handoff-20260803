# P7 阶段回顾：OMP 差距、已关闭风险与下一步决策

日期：2026-07-09

本文是 `local-coding-agent` 在完成 T-070 之后的小型阶段回顾。它服务于开发 LCA 的项目管理，不是 LCA 运行时 memory。

## 结论

LCA 已经达到进入真实需求实现压测的门槛。下一步建议不是先补完整 reviewer 或完整 ToolChoiceQueue，而是用一个低风险真实需求做实现压测，并把压测暴露的问题作为下一轮 P7/P8 的输入。

判断依据：

- P0/P1 主链路风险已经基本关闭：读、搜、改、测、diff、session、approval、budget、synthetic tool result、patch preview/rollback、memory、todo、ask_user 都已具备。
- T-069/T-070 已把 `git_diff` 的本轮归因和结构化 diff summary 补上，百炼复测 session `20260708T100128250335Z` 已验证模型能正确引用 summary 和 attribution。
- 企业只读压测已经暴露并推动修复了 path escape、LSP 空 query、最终结构漂移、证据路径漂移、重复工具循环等 runtime 问题。
- reviewer / ToolChoiceQueue 都是强能力，但目前还缺少真实实现压测中的具体失败样本；先实现它们容易把系统复杂度推高，却不一定解决当前最痛的问题。

## 当前与 OMP 的对齐程度

| 维度 | OMP 设计 | LCA 现状 | 差距判断 |
|---|---|---|---|
| 主循环与停止条件 | 不靠主 `max_steps`，由 tool_calls、deadline、abort、compaction 共同控制 | `max_steps=0` 默认不限步，`budget_seconds` 为主要预算，deadline 到期补 synthetic result | MVP 已对齐 |
| 上下文治理 | token 预算、reserve、summary/compaction | `summary_mode=auto`，字符预算近似 reserve，超过阈值可调用当前 provider 做 LLM summary | 可用；精确 token budget 仍缺 |
| 默认工作流 | system prompt、project prompt、tool descriptions、runtime reminders | system prompt、tool descriptions、runtime workflow reminder、Current task contract 已落地 | MVP 已对齐 |
| ToolChoiceQueue | 支持 hard/soft tool requirement，偏离后可升级强制工具选择 | 已实现 allowed-dir soft requirement、重复工具 forced-final、todo steering、pruning，但不是完整队列 | 部分对齐；候选后补 |
| Todo | eager todo、mid-run nudge、completion check | `todo_read/add/update`、open todo runtime reminder | MVP 可用；eager todo 未做 |
| Tool result pruning | useless/superseded 降噪 | 空搜索/LSP 标记 useless，provider-bound context 折叠 useless/superseded | MVP 已对齐 |
| Runtime state / evidence | 持续注入任务、工具证据、会话状态 | Current task contract、Evidence Ledger、git baseline、diff attribution、diff summary | MVP 已对齐 |
| Patch / edit | rich edit 工具、上下文校验、失败后重读 | anchored patch、hash tag、old_text、dry_run、rollback | MVP 可用；AST edit 后置 |
| Permission | approval mode、per-tool policy、可取消权限请求 | always-ask/write/yolo、per-tool allow/prompt/deny、session decision、deadline-aware approval | MVP 已对齐 |
| Memory / skills | memory guidance、local memory、skills discovery、managed skills | Markdown memory、startup memory、learn、memory consolidation off by default、authored skills discovery | 个人本地版已够用；managed/autolearn 后置 |
| LSP / DAP | 更完整工程工具链 | 多语言轻量静态 LSP，覆盖 Python/Java/JS/TS/Vue；无 DAP | 第一阶段接受 |
| Subagents / reviewer | 可拆分任务、reviewer 辅助质量控制 | 暂无 subagents/reviewer | 高级能力，触发式后补 |
| Browser / TUI | OMP 有更丰富交互和外部能力 | 暂不做 browser/TUI | 与封闭 VM / MVP 边界一致 |
| Worktree / task isolation | 更丰富 task/worktree/session 状态 | git baseline、state dir、diff attribution | 单人本地够用；真实实现压测后再评估 |

## 已关闭或显著缓解的主要风险

| 风险 | 当前状态 | 依据 |
|---|---|---|
| 修改已有文件在 Python 3.12 崩溃 | 已关闭 | anchored patch 使用 bytes 读写，测试覆盖 |
| 非交互审批崩溃 | 已关闭 | approval 路径捕获非 TTY/EOF，并有 deadline-aware stdin |
| LLM 非 JSON 响应崩溃 | 已关闭 | LLM 层包装为 `LlmError` |
| session orphan tool_calls | 已关闭 MVP 版 | deadline、interrupt、length 均补 synthetic tool result |
| `max_steps` 卡住日常任务 | 已关闭 | 默认 `max_steps=0`，预算交给 `budget_seconds` |
| ask 模式确认过多 | 已缓解 | per-tool approval、session allow/reject、REPL `/approval` |
| 长上下文膨胀 | 已缓解 | auto summary、local fallback、tool output truncation、todo reminder |
| 跨项目 token 与 `--cwd` 绑定 | 已关闭 | `--env-file` 与 `./agent` 安装目录 `.env` |
| 只读压测污染目标仓库 `.local-agent/sessions` | 已关闭 MVP 版 | runtime state dir 默认写用户级 state root |
| allowed-dir 需求文档不被读取 | 已复跑通过 | workspace roots 注入 + allowed-dir soft requirement |
| 重复工具循环无最终回答 | 已缓解并复跑通过 | duplicate guard、useless pruning、forced-final steering |
| 最终回答结构和证据漂移 | 已缓解并复跑通过 | Current task contract、Evidence Ledger |
| 脏工作区 diff 混入非本轮改动 | 已关闭 MVP 版 | git baseline + diff attribution |
| diff 细节被模型说错 | 已关闭并复测通过 | `git_diff` structured summary |

仍然开放但可接受的风险：

- shell 不是沙箱，真正边界仍是 VM / OS / 人工审批。
- prompt injection 仍是 Agent 通病，不信任仓库不能开 `yolo`。
- 企业数据是否能发给 provider 由用户、provider、组织策略和运行宿主决定，LCA 不内置“一刀切禁止外发”。
- token 预算仍是字符近似，不是模型 tokenizer 级精确预算。

## 剩余 P7 候选项

| 候选项 | OMP 对应机制 | 收益 | 成本 | 建议 |
|---|---|---:|---:|---|
| 完整 ToolChoiceQueue | hard/soft tool requirement、soft escalation | 高 | 中高 | 暂缓；若真实实现压测出现“关键工具长期不用/乱用”，再做 |
| reviewer / self-review | reviewer/subagent/verification | 中高 | 中 | 暂缓；若真实实现压测出现 patch 质量或最终总结质量问题，再做轻量 post-diff reviewer |
| path-scoped rules | 项目/目录级 context rules | 中 | 中 | 可作为后续 P7 小项，特别适合企业多模块项目 |
| 精确 token budget | OMP token reserve / context window | 中 | 中 | 等长上下文真实失败后做；当前字符预算够用 |
| 更完整 LSP server | 真实 language server、rename、diagnostics | 高 | 高 | 后置；当前轻量 LSP 先服务定位 |
| DAP/debugger | 调试器集成 | 中 | 高 | 后置 |
| subagents | 分工探索/实现/review | 高 | 高 | 后置；当前个人本地第一阶段不做 |
| browser | 外部网页/应用观察 | 中 | 高 | 与封闭 VM 默认边界冲突，后置 |
| TUI | 交互体验 | 中 | 高 | 先 CLI，后置 |
| AST edit | 结构化修改 | 中 | 高 | anchored patch 已足够支撑 MVP |
| managed skills / autolearn | 自动生成长期技能 | 中 | 中高 | 后置；需要更强 prompt-injection 边界 |
| worktree isolation | task branch/worktree 状态隔离 | 中高 | 中 | 若真实实现压测频繁碰到脏工作区混淆，再评估 |

## 决策

决策：下一步进入真实需求实现压测，先不补完整 reviewer / ToolChoiceQueue。

理由：

1. 当前差距主要集中在高级工程化和多人/多任务能力，不再阻塞一个低风险真实需求的实现压测。
2. OMP 的 reviewer / ToolChoiceQueue 很有价值，但应根据 LCA 实战失败形态裁剪，而不是先完整搬入。
3. 真实实现压测可以同时检验默认工作流、allowed-dir、多语言 LSP、Evidence Ledger、git diff attribution、patch dry_run、run_tests 这几条主链路。
4. 只要保持 `apply_patch` prompt、禁止 `yolo`、先建 git baseline、写入前 dry_run、写入后 tests + git_diff，风险可控。

## 真实需求实现压测协议

建议选择一个低风险、可回滚、可测试的小需求。压测时遵守：

- 压测前确认目标项目 `git status --short`，最好在业务仓库创建临时分支或保证有干净 commit。
- 使用 `--approval-mode always-ask` 或 `write`，不要用 `yolo`。
- `apply_patch`、`run_tests`、`rollback_patch` 保持 `prompt`。
- 需求文档用 `--allow-dir` 授权，不把长需求整篇塞进 prompt。
- 写入前要求 `apply_patch dry_run=true`。
- 修改后必须 `run_tests` 或说明为什么当前项目无法运行测试。
- 最终必须调用 `git_diff`，并基于 `[diff summary]` 和 `[diff attribution]` 输出总结。
- 若证据不足，应该输出“需要补充哪个项目/服务”，不要猜成事实。

建议命令模板：

```bash
./agent --provider bailian \
  --approval-mode always-ask \
  --tool-approval shell=deny,write_file=deny,memory_write=deny,rollback_patch=prompt,run_tests=prompt,apply_patch=prompt \
  --budget-seconds 900 \
  --summary-mode auto \
  --cwd /path/to/code-project \
  --allow-dir /path/to/requirements \
  "读取需求文档，先理解现有实现，再做一个低风险小改；写入前 dry_run，写入后测试并总结 diff。"
```

## 下一步任务

| ID | 任务 | 建议状态 | 说明 |
|---|---|---|---|
| T-071 | P7 阶段回顾与 OMP 差距决策 | 已完成 | 本文档即输出物 |
| T-072 | 真实需求实现压测 | 下一步 | 用一个低风险真实需求验证 LCA 是否能从需求到 patch 到测试闭环 |
| T-073 | reviewer / ToolChoiceQueue 条件触发评估 | 候选 | 只有 T-072 暴露具体失败后才开做 |

## 成功标准

T-072 成功不要求一次把复杂企业需求完整实现，而要求验证 LCA 的“真实实现链路”：

- 能先读取需求和相关代码，而不是靠猜。
- 能维护 todo 并跟随任务目标。
- 能用 LSP/search/read 定位改动点。
- 能在写入前给出 dry_run patch。
- 能执行测试或明确说明测试阻塞原因。
- 能用 git diff summary 和 attribution 准确总结本轮改动。
- 不把缺失证据当事实，不把历史脏改动当成本轮成果。
