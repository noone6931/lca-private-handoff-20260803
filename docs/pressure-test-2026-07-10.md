# T-113/T-114 Patch Reviewer 压测记录（2026-07-10）

本次百炼压测均在临时 Git worktree 中执行，主仓库未被 LCA 写入；session、todo 和 patch log 使用 `/tmp` state dir。

## 结论

本轮没有把“百炼端到端完整通过”作为结论。压测证明了两个 runtime 漏口，并已修复：Reviewer 不应等模型准备最终回答才运行；实现任务也不能只因模型写了 `blocked` 就接受无改动收尾。

`qwen3-coder-next` 在紧凑编辑任务中频繁错传 `apply_patch` / `todo_*` 参数。这是独立的工具调用兼容性问题，记录为 T-115，不能混同为 Reviewer 失败。

## 压测证据

| Session | 场景 | 观察到的事实 | 判定 |
|---|---|---|---|
| `20260710T013027866183Z` | 修改运行中的 `patch_reviewer.py`，为“加一个测试”补 marker 和测试 | 源码 marker 成功写入，测试文件多次 anchored patch 失败；模型最终虚报测试已存在。Python 不会热重载刚修改的 Reviewer marker。 | 此自举场景不能验证新 marker 的即时触发；保留 marker 兼容与单测。 |
| `20260710T013610393204Z` | `task_contract.py` 的“仅分析”兼容小改 | 模型只读文件后输出原始 `<tool_call>` 文本，没有完成合法工具调用。 | Provider/tool-call 稳定性问题，未进入 Reviewer 路径。 |
| `20260710T013957052930Z` | 同一小改，要求首次只改源码再由 Reviewer 收束 | 模型初始未写入却以 `blocked/unexecuted` 收尾；后续终于对 `task_contract.py` 成功写入 `仅分析` 并执行 `git_diff`，随后为避免继续燃烧 token 主动中断。 | 暴露 T-114；post-diff Reviewer 以 runtime 单测锁定，未将这次中断会话标记为端到端通过。 |

## 问题与措施

| ID | 问题 | OMP 架构原则 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-033 | Reviewer 原先只在模型准备最终回答时运行；模型在 `git_diff` 后继续游走时，缺测试/调用方风险不能及时收束。 | 工具结果是 active loop 的观察点；soft requirement/reviewer 应在工具事件后推动下一步，而非只依赖最后自述。 | T-113 改为成功 `git_diff` 后立即评审；有 finding 时跳过同批剩余 tool call，写 runtime steering，仅开放修复/验证/回滚工具。 | 已修复，单元回归覆盖。 |
| PT-034 | 实现任务可以只说 blocked/no-edit 就绕过实际修改，即使已读到目标源码且没有任何工具层阻断证据。 | 终止/跳过必须有可审计 runtime observation，不能只信模型文本。 | T-114：CompletionAudit 只接受带工具观察的 no-edit stop，例如 search/LSP 未命中、文件缺失、relevance gate 或审批拒绝；无证据 blocked 会重新开放 read/search/apply_patch。 | 已修复，单元回归覆盖。 |
| PT-035 | 百炼模型常传旧式 `apply_patch` 参数（如 `file_hash`、`old_str`、`mode=edit`、字符串行号），也会给 `todo_add` 传不支持的状态值；导致大量无效工具调用。 | OMP 在 tool-call/结果边界做协议归一与可行动错误反馈，不能把 provider 方言直接留给每个工具。 | 下一步 T-115：在 ToolRegistry 边界设计严格、可审计的兼容归一，仅接收已知旧字段别名并保留原 schema 的安全约束。 | 开放，P0。 |

## 已验证的回归

- `tests/test_agent.py` 覆盖 post-diff Reviewer 在模型尝试最终回答前返回 `requested_test_missing`。
- `tests/test_completion_audit.py` 覆盖“无工具阻断证据的 blocked/no-edit 必须失败”和“搜索未命中可作为真实 no-edit 证据”。
- `tests/test_patch_reviewer.py` 覆盖“加测试/加一个测试”视为显式测试请求，且没有测试 diff 时必须产生 `requested_test_missing`。
- 本轮本地完整验证：`PYTHONPATH=src python3 -m unittest discover -s tests`，257 tests OK；`python3 -m compileall src tests` 与 `git diff --check` 通过。

## 下一步

先完成 T-115 的受限 tool-argument normalization，再用一个不修改 LCA 自身 runtime 的小项目重跑端到端 patch-review 压测。通过标准是：一次源码 patch 后的 `git_diff` 立刻触发 reviewer，模型能够用合法 anchored patch 补测试、运行定向测试并输出与实际 diff 一致的总结。
