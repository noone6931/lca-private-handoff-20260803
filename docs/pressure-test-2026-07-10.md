# T-113/T-114 Patch Reviewer 压测记录（2026-07-10）

本次百炼压测均在临时 Git worktree 中执行，主仓库未被 LCA 写入；session、todo 和 patch log 使用 `/tmp` state dir。

## 结论

本轮没有把“百炼端到端完整通过”作为结论。压测证明了两个 runtime 漏口，并已修复：Reviewer 不应等模型准备最终回答才运行；实现任务也不能只因模型写了 `blocked` 就接受无改动收尾。

`qwen3-coder-next` 在紧凑编辑任务中频繁错传 `apply_patch` / `todo_*` 参数。T-115 已在 ToolRegistry 收敛可安全归一的 scalar 参数方言；raw diff 和 bulk todo 等不等价结构仍明确拒绝，不能混同为 Reviewer 失败。

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
| PT-035 | 百炼模型常传旧式 `apply_patch` 参数（如 `file_hash`、`old_str`、`mode=edit`、字符串行号），也会给 `todo_add` 传不支持的状态值；导致大量无效工具调用。 | OMP 在 tool-call/结果边界做协议归一与可行动错误反馈，不能把 provider 方言直接留给每个工具。 | T-115 已在 ToolRegistry 边界严格归一已观测 scalar alias 并保留原 schema 的安全约束；不等价 raw diff/bulk todo 转入 T-116。 | 已缓解，继续观察。 |

## 已验证的回归

- `tests/test_agent.py` 覆盖 post-diff Reviewer 在模型尝试最终回答前返回 `requested_test_missing`。
- `tests/test_completion_audit.py` 覆盖“无工具阻断证据的 blocked/no-edit 必须失败”和“搜索未命中可作为真实 no-edit 证据”。
- `tests/test_patch_reviewer.py` 覆盖“加测试/加一个测试”视为显式测试请求，且没有测试 diff 时必须产生 `requested_test_missing`。
- 本轮本地完整验证：`PYTHONPATH=src python3 -m unittest discover -s tests`，273 tests OK；`python3 -m compileall src tests` 与 `git diff --check` 通过。

## 下一步

T-120 已完成“真实需求文档跨 compaction 固定”MVP，并用服务费结算需求复测。下一步转向 T-121：设计任务的 evidence matrix / ToolChoiceQueue 收束，避免模型在没有覆盖前后端最小证据集时继续扩散探索。

## T-115 复测（20260710）

两次复测都使用临时 Git worktree；主仓库未被 LCA 写入。

| Session | 结果 | 可确认事实 |
|---|---|---|
| `20260710T020350759634Z` | 部分通过，暴露更多参数方言 | 成功修改源码和测试并运行全量测试、输出 diff；同时出现 `file_hash_tag` / `source_hash_tag` 与 `run_tests.cmd`。写前 `read_file` 快照被最终 evidence guard 错当成写后快照，已在 runtime 中失效化对应 path 的旧 source evidence。 |
| `20260710T020730075094Z` | 交付链路通过，但效率不合格 | 临时项目实际修改 `src/local_agent/task_contract.py` 与 `tests/test_task_contract.py`；`git_diff` 为 2 文件、`+8 -0`、2 hunks；定向 `tests.test_task_contract` 运行 `6` 项并通过。运行共 57 次工具调用、25 次错误；没有成功完成要求的 dry-run preview。 |

| ID | 问题 | 措施 | 状态 |
|---|---|---|---|
| PT-036 | 百炼会传 `file_hash` / `file_hash_tag` / `source_hash_tag` / `hash_tag`、`old_str` / `new_str`、`mode=edit`、字符串行号、`dry_run="True"`、`run_tests.cmd`、todo `key/content/pending`。 | T-115 在 `ToolRegistry` 校验前的单一边界精确归一这些已观测标量/别名；canonical 与 legacy 值冲突时拒绝。归一后仍走原 schema、审批、path/hash 与 anchored patch 校验。 | 已完成。 |
| PT-037 | 模型还会把完整 unified diff 放入 `patch_content`，或把 todo 数组传给单条 `todo_add`；这不是字段别名，自动拆解会绕过 anchored edit 与 session state 的语义。 | 不做隐式兼容；保留明确错误。T-116 将以 ToolChoiceQueue/preview contract 提醒和限制“先 preview 后 real patch”，但不会把 raw diff 直接执行。 | 开放，P0。 |
| PT-038 | source-grounded numeric steerer 会把 diff hunk/增删统计/测试计数当成源码枚举数字，导致最终回答额外重写。 | T-117 将区分“源码状态/枚举/接口数字事实”和“git/test 工具观测数字”；后者应由 tool evidence 支持，不进入 source numeric 比对。 | 开放，P1。 |

T-115 的结论不是“百炼已经完全高效”，而是：安全可逆的 provider 方言已在单一协议边界收敛；不能安全解释的结构化误调用仍明确失败并进入下一轮调度改进。

## T-116/T-117/T-118 复测（20260710）

| Session / 测试 | 事实 | 结论 |
|---|---|---|
| `20260710T021752034260Z` | `task_contract.py` 的完整 `dry_run="True"` anchored preview 成功，随后同锚点真实 patch 成功；测试文件锚点多次不匹配，任务未完成。14 次工具调用、8 次错误。 | T-116 preview contract 能允许“预览成功后同锚点写入”，但模型的锚点纠错仍不稳定。 |
| `20260710T022049107585Z` | 未带 `dry_run` 的 real patch 被 Preview contract 拒绝，模型重复同一调用后被 duplicate guard 收束；7 次调用、5 次错误，无 workspace write。 | T-116 安全边界有效：模型不能越过 preview 直接写入。但它收到错误后尚未稳定改用 canonical preview 调用。 |
| 本地回归 | `source_grounded_numeric` 已不再把 `apply_patch`、tag、diff/test 统计当作源码数字事实；真实 session `20260710T022049107585Z` 的 steering counts 未包含该 guard。 | T-117 MVP 修复有效。 |
| 本地回归 | 实现任务即使正文含待写入的 `‘只读核实’` 和断言文本 `read-only`，仍会归为 `code-implementation`。 | T-118 修复 quoted/read-only literal 误分类。 |

| ID | 问题 | 措施 | 状态 |
|---|---|---|---|
| PT-039 | preview contract 错误能阻止直接写入，但当前百炼模型常重复原调用而非改为 dry-run。 | 保持 hard pre-tool gate；下轮只基于新的失败样本改善 schema error / active-loop feedback，不降低 gate。 | 已缓解，继续观察。 |
| PT-040 | 实现任务中的待写入文本或测试断言包含 `只读` / `read-only` 时，deterministic classifier 曾被误导为 read-only。 | T-118 让明确实现意图优先；只有“不要修改/do not edit/no changes”等真正禁止修改的指令才覆盖实现意图。 | 已修复，单元回归覆盖。 |
| PT-041 | source-backed final guard 曾可能在未完成实现任务中抢占修复空间。 | T-119 将源码数字/证据核验类 final guard 限定为 `read-only` contract；实现任务保留 CompletionAudit、Patch Reviewer 和 ToolChoiceQueue 的受控修复链路。 | 已关闭 MVP，真实复测通过。 |

## T-119 真实写入复测（20260710）

临时 Git worktree 会话 `20260710T022516812575Z` 使用明确的实现任务：在两个文件中新增 `只读核实` 标记及其测试；每个文件都要求先 successful `apply_patch dry_run=true`，再以相同 anchored 参数真实写入，最后运行定向测试和 `git_diff`。

| 观察 | 事实 | 结论 |
|---|---|---|
| 实现任务分类 | prompt 同时出现待写入的 `只读核实`、断言 `read-only` 和 “只读核实”业务句子，但 runtime 归类为 `code-implementation`。 | T-118 在真实 provider 调用下成立。 |
| 修复控制流 | 两个文件的初始 patch 参数均有失败；模型仍继续读/修正锚点，完成每个文件的 preview 和同锚点写入，随后运行 `tests.test_task_contract`（6 tests OK）和 `git_diff`（2 files, `+8/-0`）。 | T-119 的 guard scope 修正有效：实现任务没有被 source-backed final guard 过早转入无工具最终回答。 |
| 运行成本 | 14 次工具调用中有 6 次参数/锚点错误，`apply_patch` 调用 10 次；run summary 的 `steering_counts` 为空。 | 安全与完成性已验证，但百炼对 anchored patch schema 的一次命中率仍不理想；不应为此自动执行 raw diff 或放松 preview gate。 |

| ID | 问题 | 措施 | 状态 |
|---|---|---|---|
| PT-041 | `SourceEvidenceFalseNegative` 与源码数字/证据类 final guard 的适用边界需要明确。 | `SourceEvidenceFalseNegative` 仍只负责“无写入时”的 todo/git 收尾卫生；源码数字/证据核验类 final guard 仅用于 `read-only` contract。 | 已关闭 MVP，真实复测通过。 |
| PT-042 | 百炼在 anchored patch 上仍会尝试不完整参数或整块式 patch，导致明显试错成本。 | 保持 ToolRegistry 的有限 scalar alias 归一、明确 schema 错误和 hard preview gate；不自动拆解 raw diff/bulk todo。后续仅在出现安全等价的新样本时扩展兼容。 | 开放，P1。 |

## T-120 真实需求设计复测（20260710）

| Session | 事实 | 结论 |
|---|---|---|
| `20260710T024503106586Z` | 读取 V1.3 需求、前端和后端后，运行 51 次工具调用、28 次 compaction（其中 12 次 LLM summary）。最终稿把后期规划的复核/签章/自动发送误写为本期双审/支付流程，并编造账单确认、外部对账等范围。 | 不可作为设计交付。需求正文在多次摘要后失去权威性，且 final guard 只检查“有无证据/数字”，不能检查“需求事实是否来自需求正文”。 |
| T-120 修复 | 新增 `requirement_evidence.py`：成功读取的 requirement/spec Markdown 作为 pinned runtime evidence，每次 provider request 均以高于 compaction summary 的优先级注入；只读设计最终稿若陈述需求事实，必须给真实 requirement 文件路径和行号，否则触发 final rewrite。 | 对齐 OMP “关键上下文不可被普通压缩替代”的原则；摘要继续用于历史导航，不再充当需求事实来源。 |
| `20260710T025606504484Z` | 极小 `context_char_budget=12000` 下运行 57 次工具调用、39 次 compaction（20 次 LLM summary）。最终需求事实已恢复为制单/已制单、Word、下载、回退和筛选条件，未再把双审/支付写成本期范围。 | T-120 对“需求事实漂移”有效，但仍未达到可交付设计质量：模型只写 `V1.3.md 第…`，未稳定输出完整 requirement path；未读取前端候选项目且后端复用结论过窄；探索成本仍过高。 |

| ID | 问题 | OMP 架构原则 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-043 | LLM compaction 可把需求正文压缩为错误业务流程，导致最终设计把后期规划或推测说成当前范围。 | 关键 current-task/context 需要独立于普通历史压缩保存，并拥有更高权威级别。 | T-120 pinned requirement evidence + 需求路径/行号 final gate；session `20260710T025606504484Z` 已不再编造双审/支付当前范围。 | 已缓解，继续观察。 |
| PT-044 | 真实设计任务在后端候选目录反复 list/search/read，未覆盖前端最小证据集，最终只能给出弱复用结论。 | OMP ToolChoiceQueue 用 requirement/active-loop observation 把缺失关键工具升级为 soft/hard requirement，并对病态探索设小上限。 | T-121 设计 evidence matrix：需求已读后，按用户声明的前后端 roots 要求至少各有一个命中后的 `read_file` 证据；满足最小覆盖后限制低价值继续探索并收束回答。 | 开放，P0。 |
