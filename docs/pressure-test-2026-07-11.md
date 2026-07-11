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
3. T-128A 本身不暗中改变 shell、Git、startup rules、LSP primary 或项目 memory；现在需要整体切换时可使用已完成的 T-128B `/move`。

---

# T-128B `/move` 主工作目录真实压测（2026-07-11）

## 范围与结论

目标是在同一 chat session 中，从独立 backend Git workspace 移动到独立 frontend Git workspace，验证 primary cwd、session artifacts、startup context、Git/shell/LSP root 和会话恢复不会分裂：

```text
backend primary
  -> /move frontend
  -> frontend becomes primary
  -> backend remains session root
  -> read frontend + read backend + git_status
  -> exit and reopen with frontend --cwd + same session id
```

结论：T-128B 通过。`/move` 不创建新 session；它把当前 session JSONL、todo、patch log 作为一个可回滚的 artifact 集迁移到新 primary 的 state partition，重建新项目 startup context，并清空 external LSP clients。旧 primary 自动作为 session root 保留，可继续用 file/search/LSP/patch 访问；Git 和 shell 则只锚定新 primary。

## 真实压测证据

| 项 | 事实 |
|---|---|
| 隔离会话 | `20260711T005836257738Z` |
| 初始/目标项目 | 两个独立、干净的临时 Git repo：`backend` -> `frontend` |
| session 命令 | `/status`、`/move frontend`、`/workspace list`、再次 `/status` 均成功；revision 为 1 |
| primary/state 切换 | 状态输出从 backend 及其 workspace-state partition 切到 frontend 及其对应 partition |
| startup context | backend/frontend 分别放置不同 `AGENTS.md`；Runtime 单测确认 move 后 system message 只保留 frontend context，不残留 backend block |
| 工具边界 | 百炼在 move 后相对 `read_file README.md` 读到 `frontend view`；绝对路径读取旧 backend 得到 `backend service`；`git_status` 在 frontend primary 中返回 clean |
| 持久化 | 新 state partition 的 JSONL 同时写入 `workspace_moved` 与 `WorkspaceMoved`，含 previous/next primary、session roots、revision、state dir |
| 重开恢复 | 使用 `--cwd frontend --session 20260711T005836257738Z` 重开后，primary 仍为 frontend，backend 仍是 session root，state dir 仍为 frontend partition |
| move 后小改 | 同一 session 仅修改 frontend `README.md`：`frontend view` -> `frontend view verified`；按 read -> dry-run -> apply -> `git diff --check` -> `git_diff` 完成，backend 仍无 diff |
| 副作用 | backend 和 LCA worktree 均无 diff；frontend 仅保留压测预期的一行 diff，临时项目和 state 均在压测结束后清理 |

## 压测观察、OMP 对照与措施

| ID | 压测事实 | OMP 源码事实 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-050 | 若只改 Runtime cwd 而不迁移 session artifacts，重开时会在旧/新 workspace state 间断裂。 | OMP 的 `/move` 会更新 session cwd、迁移 session/artifacts，再重载 cwd 相关 context。 | `workspace_migration.py` 只搬当前 session 的 JSONL/todo/patch artifacts；目标冲突或中途 `os.replace` 失败均拒绝或回滚。 | 已完成：`d3548b5`，单元 + 真实重开复测通过。 |
| PT-051 | move 后旧项目仍需保留为已授权证据源，但 Git/shell 不应偷偷继续指向旧项目。 | OMP 将 active cwd 与 session history 分开处理；cwd 切换后 runtime project context 要重新绑定。 | `WorkspaceContext.moved_primary()` 将旧 primary 转为 session root；ToolContext.workspace、Git baseline、shell cwd、LSP client cache 和 provider workspace block 同步切换。 | 已完成：真实 `read_file` 双 root + `git_status` frontend 验证。 |
| PT-052 | 百炼最终回复没有严格遵守“三句话”，且对 clean `git_status` 的仓库归属措辞过度保守。 | OMP 的 runtime 负责上下文/工具边界，最终表达仍受 provider 和 final steering 影响。 | 作为 P10 final-format / ToolChoiceQueue 质量样本记录；不为了格式强行放松 evidence hygiene，也不影响 move 的 runtime 正确性。 | 开放，等待同类真实任务累计后统一收束。 |

## 回归验证

- 新增 WorkspaceContext move、artifact 成功迁移/目标冲突/中途失败回滚、SessionStore move snapshot、Runtime move/reopen/旧 root 可读/LSP close、CLI `/move` 回归；真实链路额外覆盖 move 后 primary 内的 preview -> write -> diff check -> diff。
- 本地验证：`PYTHONPATH=src python3 -m unittest discover -s tests` 通过 329 项；`python3 -m compileall -q src tests` 与 `git diff --check` 通过。
- 代码提交：`d3548b5 Add session workspace move`。

## 日用方式

在 `./agent --chat` 中输入：

```text
/move "/path/to/new-primary-project"
/workspace list
/status
```

之后当前 session 的 Git、shell、startup rules、project memory/skills 和默认 LSP root 都以新项目为准；旧项目会显示为 session root。下次继续该会话时，应使用新 primary 的 `--cwd` 和原 session id。
