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
