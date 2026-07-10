# T-127/T-129 自主小改压测（2026-07-11）

## 范围与结论

目标是在隔离 Git worktree 中验证“自主选择一个极小测试改进”的收束路径：

```text
source + test evidence -> dry_run preview -> write -> tests -> git diff
```

结论不是“百炼已能稳定完成自主小改”。本轮确认了 LCA 的边界能阻止探索/编辑失控，并将病态 anchored-patch 重试收束为可审计的停止；当前百炼模型仍会生成错误的 `todo_*` / `apply_patch` 参数和虚构锚点。因此，受控的明确目标小改仍是日用主路径；自主选点保留为受保护的实验能力。

## 最终复测证据

| 项 | 事实 |
|---|---|
| 隔离 worktree session | `20260710T232919852951Z` |
| 运行时长 | 67,018 ms |
| 工具调用 | 20 次；其中 `apply_patch` 3 次，均为错误 |
| 终止原因 | `tool_choice_queue` / `autonomous_small_change_patch_retry_exhausted` |
| 写入结果 | 临时 worktree 的 `git status --short` 为空，`git diff --check` 与 `git diff --stat` 均为空 |
| 最终消息 | 三次无效 preview 后，在修改文件前停止；建议以更具体目标重跑或人工检查候选 |

该结果证明：即使 provider 返回未开放工具或无效 patch，Registry/anchored patch 均不会让错误调用产生文件副作用。

## 问题、OMP 对照与措施

| ID | 压测事实 | OMP 源码事实 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-042 | “不要修改 README/docs”曾被 task contract 误判为整项只读，候选收束根本不会启动。 | OMP 将会话级 tool choice 与运行时上下文区分处理，不能仅用文本片段覆盖完整任务意图。 | `task_contract.py` 区分全局只读指令与局部文件排除；Queue 以已归类的 task kind 为权威。 | 已修复：`4663315`、`d302688`。 |
| PT-043 | 仅从 provider request 删除工具 schema 不足；百炼仍可能返回未暴露的 `read_file` / `search_code`，旧实现会执行。 | OMP 在 `packages/agent/src/agent-loop.ts:233` 的 `coerceToolResult()` 和执行路径集中归一工具结果，工具循环不是只依赖模型自觉。 | `ToolRegistry` 增加 runtime allowlist 最终执行边界；未开放工具返回可纠正 error，不调用 handler。 | 已修复：`167ce61`。 |
| PT-044 | 候选阶段完全禁止 `read_file` 会让模型无法回看已选文件的精确锚点，误报 blocked。 | OMP 的 soft requirement 是有界调度，不等于剥夺完成当前动作所需的局部上下文。 | Queue 携带 candidate paths；Registry 仅允许回读已经选中的源码/测试路径。 | 已修复：`e45c64f`。 |
| PT-045 | 只限制路径仍会让模型把同一候选文件切成很多区间读取，造成 20+ 步无效循环。 | OMP 的主 loop 不以总步数结束，但对病态子循环设置显式小上限，例如 `MAX_PAUSED_TURN_CONTINUATIONS=8`、`MAX_SOFT_TOOL_ESCALATIONS=3`（`packages/agent/src/agent-loop.ts:86-94`）。 | candidate read scope 设为最多 4 次补读，超限后只允许 preview/write/verification，并提示使用已有 tag/证据。 | 已修复：`3c57567`。 |
| PT-046 | 百炼连续虚构 `apply_patch` 锚点并重试；即使没有文件写入也会消耗大量 token。 | OMP 对同一 soft tool requirement 连续强制最多 3 次，超过即停止以避免无界循环（`packages/agent/src/agent-loop.ts:989-993`）。 | candidate 在成功 preview 前最多允许 3 次失败的 `apply_patch`；达到上限以 `tool_choice_queue` 结束 run 并写 `run_summary`。 | 已修复并真实复测：`0eac8c5`。 |

## 回归验证

本轮新增/更新的测试覆盖：

- 局部 README/docs 排除仍分类为 `code-implementation`。
- Queue 的 candidate 阶段严格顺序：preview/write 后才开放 tests，tests 后才开放 diff。
- provider 返回未开放工具时，Registry 不执行 handler。
- candidate 回读仅允许已选路径，且最多 4 次。
- 连续 3 次失败的 candidate preview 会生成 stop decision。

本地验证：`PYTHONPATH=src python3 -m unittest discover -s tests` 通过 311 项；`python3 -m compileall -q src tests` 与 `git diff --check` 通过。

## 日用建议

1. 生产需求优先给出明确目标文件、行为和验收命令，走已验证的 preview -> write -> tests -> diff 链路。
2. “自己找一个小改动”只用于 LCA 自身或隔离 worktree；它现在不会污染文件，但仍受 provider 规划质量影响。
3. 后续不要为了让模型强行完成而放松 anchored patch、preview 或 runtime allowlist；优先改善 provider tool-call 兼容提示和明确任务的端到端压测。

---

# T-128A 动态工作目录真实压测（2026-07-11）

## 范围与结论

目标是在隔离 Git worktree 的同一 chat session 中，验证动态追加目录不会扩大 primary workspace 的 shell/Git/memory 权限，并验证 session 重开后 root 状态可以恢复：

```text
primary backend workspace
  + /workspace add frontend
  + /add-dir requirements
  -> requirement + frontend + backend evidence
  -> remove/reset
  -> reopen same session and replay root state
```

结论：T-128A 通过。用户无需重启 session 即可追加、移除和重置只读/patch/LSP 可用根目录；动态 roots 会写入 session JSONL 并在同一 session 恢复。primary workspace 没有被切换，shell/Git 的执行根也没有随之扩大。`/move` 仍是后续 T-128B，不应误认为已实现。

## 真实压测证据

| 项 | 事实 |
|---|---|
| 隔离 worktree session | `20260710T234527128194Z` |
| primary root | `/private/tmp/lca-t128-pressure.ZPZ0JN/worktree` |
| 动态 roots | `/private/tmp/lca-t128-pressure.ZPZ0JN/frontend`、`/private/tmp/lca-t128-pressure.ZPZ0JN/requirements` |
| 前端/需求/后端证据 | 成功 `read_file` requirement Markdown、`frontend/src/SettlementView.vue`、primary 的 `src/local_agent/tools/files.py` |
| root 命令 | `/workspace add`、`/add-dir`、`/workspace list`、`/workspace remove`、`/workspace reset` 均成功，revision 从 1 递增到 4 |
| primary 边界 | 模型额外调用的 `git_status` 返回空；未产生 worktree diff，未发生 write/exec 副作用 |
| session replay | reset 后新增 requirements root（revision 5），退出并用同一 `--session` 重开，`/workspace list` 仍显示该 session root |
| session 持久化 | JSONL 同时记录 `workspace_roots_changed` 与 `event_v1` 的 `WorkspaceRootsChanged` |

## 压测观察、OMP 对照与措施

| ID | 压测事实 | OMP 源码事实 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-047 | 追加前端、需求目录后，不需要退出 session 就可把三份跨 root 证据交给模型。 | OMP 在 session 内维护 cwd/project context；目录变化不是让模型获得任意 filesystem 权限，而是运行时显式更新 project context。 | `WorkspaceContext` 保存 canonical primary/configured/session roots；仅前端命令可更新，ToolContext 与 provider workspace block 同步刷新。 | 已完成：`3ad198f`，真实复测通过。 |
| PT-048 | add/remove/reset 的变化必须可恢复，否则恢复会话会出现“模型以为能读、工具却拒绝”的断裂。 | OMP 的 session history 与 runtime context 一起维持可恢复的会话状态。 | 每次操作追加 `workspace_roots_changed`，恢复时仅重放同一 primary 的最近快照；丢失路径跳过并发出错误事件。 | 已完成：单元测试 + 同 session 重开复测。 |
| PT-049 | 只读任务的第一次最终回答被既有 CompletionAudit 引到“不能实现”的无关结论；二次追问时模型又多调用了 `git_status`。 | OMP 依靠 tool-choice/steering 约束病态流程，但不应把完成审计变成与原问题无关的模板回答。 | 记录为 P10 prompt/ToolChoiceQueue 收束质量问题；不在 T-128A 内新增主循环 guard，也不影响 dynamic-root 边界结论。 | 开放，等待后续真实任务样本归类。 |

## 回归验证

- T-128A 单元/Runtime/CLI/Protocol/Session 测试已覆盖：canonical roots、重复 add、remove/reset、busy rejection、ToolContext 刷新、provider root block 唯一、cross-root design evidence、session replay/missing root、前端命令与事件形状。
- 实现提交前本地验证：`PYTHONPATH=src python3 -m unittest discover -s tests` 通过 321 项；`python3 -m compileall -q src tests` 与 `git diff --check` 通过。
- 本次真实压测在独立 worktree 内完成，测试 fixture 与临时 state directory 将在压测结束后清理，不进入项目仓库。

## 日用建议

1. 后端 workspace 已开 chat 时，发现前端或需求目录可直接执行 `/workspace add "/path/to/project"` 或 `/add-dir "/path/to/requirements"`，再让 Agent 读取证据；不必重启。
2. 用 `/workspace list` 核对当前 primary/configured/session 边界；临时关联目录做完后用 `/workspace remove PATH` 或 `/workspace reset` 收回。
3. 需要让 shell、Git、startup rules、LSP primary 和项目 memory 一起切换时，等待 T-128B 的 `/move`；T-128A 故意不暗中改变这些根。
