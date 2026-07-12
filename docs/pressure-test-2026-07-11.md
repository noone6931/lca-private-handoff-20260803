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

---

# T-130 真实跨项目需求只读压测（2026-07-11）

## 范围与结论

目标是在真实需求、后端 `zqylpaymentmaster9d423763` 和前端 `mpspaymasterce6ca65` 三个明确 roots 上，验证 LCA 是否能在不写入业务项目的前提下收束出可实施的影响范围。

结论：**只读证据链部分通过，尚不允许进入业务写入。** LCA 成功读取需求、后端平台费用/预制单邻近代码和前端预制单/接口模块；但没有取得订单“拓展服务费”字段、制单状态、结算单表、模板配置和真正数据归属的源码/DDL 证据。最终回答中的 `SettlementBillApplication`、`MonthlySettlementJob`、`SettlementList.vue` 只是实现候选，不能被当成既有代码事实或直接开始创建。

## 真实压测证据

| 项 | 事实 |
|---|---|
| session | `20260711T013052126987Z` |
| roots | primary=`zqylpaymentmaster9d423763`；allowed requirement Markdown；allowed frontend=`mpspaymasterce6ca65` |
| 需求证据 | 成功读取 `需求文档-拓展服务费结算V1.3.md`：待制单筛选、JS 编号、制单/回退事务、Word 变量、导出规则均有明确描述 |
| 后端邻近证据 | 成功读取 `PrepareOrderApplication.java`、`PrepareOrderBO.java`、`PlatformOrderInfoEntity.java`、`HistoryPlatformOrderBO.java`；搜索定位 `PlatformOrderController.java` 和“平台费用-预制单管理-待制单”下载模块 |
| 前端邻近证据 | 成功读取 `src/views/preOrderManagement/list.vue`、`src/store/modules/platformPayment.js`、`src/assets/interface/pay/platformPayment.js`、路由模块；当前已有预制单管理和缴费单管理入口 |
| run summary | 323,168 ms；70 次 LLM 请求；68 次工具调用；9 次 tool error；11 次 useless result；66 次 compaction |
| 安全副作用 | 配置 deny 的 shell/run_tests/write/memory 未产生业务写入；没有 `apply_patch` 或 Git diff |

## 问题、OMP 对照与措施

| ID | 压测事实 | OMP 源码事实 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-053 | `tool_approval=deny` 的 `run_tests` 曾仍出现在模型可见工具中，模型把 `find` 误当测试命令调用，浪费一步。 | OMP `AgentSession.#applyActiveToolsByName()` 只把 active tool 集交给 Agent；`agent.ts:74-89` 还会在 active tools 中校验 forced `toolChoice`。 | `_tools_for_model()` 与 Queue 的 available tool names 均过滤 config/session deny；Registry deny 保留为最终执行边界。 | 已修复并真实复测：session `20260711T014124870469Z` 仅三次指定 `read_file`，20.8 秒、0 tool error、无 shell/run_tests。 |
| PT-054 | 百炼返回 `todo_text`、`lsp_symbols.pattern`、`__invalid_tool_call`、错误前端路径等 provider 方言/规划错误。 | OMP 以 schema、active tools 和 `coerceToolResult()` 收敛无效工具结果，但不把未知 payload 静默解释成任意副作用。 | 保持严格 schema；只对已验证且语义无歧义的 scalar alias 兼容。`todo_text` 的批量语义不明确，暂不接收。 | 开放，归为 provider compatibility。 |
| PT-055 | 跨项目只读设计在已读取 requirement+backend+frontend 后仍继续 68 次工具调用，目录枚举和大搜索过多。 | OMP 对软工具要求/病态探索设置显式小上限，并用 ToolChoiceQueue 在满足或失败后收束；主循环不靠总步数截断。 | T-131 把“不得修改/写入”归为全局只读，沿用跨 root evidence coverage 的有限补证预算，并为无工具 final resample 增加 OMP 同类的 8 次上限、deadline reserve 与本地摘要优先。 | 已完成并真实复测：`20260711T115451962229Z` 仅 3 次精确 read、0 error、22.3 秒完成。 |
| PT-056 | 最终回答把未证实类名/字段名写进分阶段方案，虽标 blocked，仍可能被误当实现定位。 | OMP 的 project context/tool result 只能提供证据，不能把生成候选升级为源码事实。 | T-131 固定“已证实事实 / 建议新增候选 / 缺失 owner 证据”三段；绝对路径不再作为源码术语，数值核验仅在用户明确索要状态码/字段数值时才启动。 | 已完成并真实复测：候选均明确标为非现有源码，最终无额外工具调用。 |

## 进入业务实现前的最小前置条件

1. 确认订单“拓展服务费”和“制单状态”所在服务/表/DTO，以及该服务目录的授权路径。
2. 提供或定位结算单主表/明细表的 DDL、模板管理接口/模板编号和下载中心契约。
3. 以这些真实 owner roots 重跑只读设计；结论稳定后，才在同一 session `/move` 到被确认的 primary，进行一个明确目标的小改验证。

---

# T-131 跨 root 只读设计收束与最终答复韧性（2026-07-11）

## 问题与根因

首轮 T-130 的任务含“不得修改文件”，但 `TaskContract` 未把这类全局禁令识别为 read-only；它因此绕过了既有 cross-root design evidence coverage。修复分类后，第一次复测 `20260711T014453755095Z` 已正确读取 requirement/backend/frontend 并在 7 次工具调用后收束，却被多个最终答复 hygiene resample 与 LLM compaction 占用剩余时间，最终请求超时。

## OMP 对照与实现

OMP 的 `packages/agent/src/agent-loop.ts` 不用总步数结束主循环，但对无工具的非终止 continuation 使用 `MAX_PAUSED_TURN_CONTINUATIONS = 8`，并在 deadline 前停止继续。LCA 采用同一原则而不复制 OMP 的整套 UI/runtime：

1. `不得修改`、`禁止修改`、`不得写入`、`禁止写入` 归为全局只读指令。
2. `RunContext` 记录 forced-final continuation；连续无工具的最终答复重写最多 8 次，任一工具调用后重置。
3. 进入 deadline reserve 后不再追加可选 final hygiene 重写；forced-final compaction 强制使用本地摘要，避免在最终答复前再发一次 summary LLM 请求。
4. 若 forced-final 请求仍超时，返回本 run 最近一份无工具草稿，并明确标注该次最终重写超时。
5. absolute path 的 `path`/`line` 不再被 source-evidence false-negative gate 当成源码标识符；数值/状态核验只在原始用户请求明确要求时运行。

## 真实复测

| session | 结果 | 关键指标 | 结论 |
|---|---|---|---|
| `20260711T115216464654Z` | 初次复测 | 3 reads，0 error，5 LLM requests，4 次本地 compaction；仍有 `path:line` 与日期/状态文本引发的 3 次无工具重写 | 收束与 deadline 问题已消失，但发现两个过度 hygiene gate。 |
| `20260711T115451962229Z` | 最终复测 | 22,314 ms；4 LLM requests；3 `read_file`；0 error/useless/synthetic；1 次本地 compaction；无 steering hit | 通过。精确读取 requirement、Java、Vue 后直接输出三段证据化设计，无 shell/test/write/memory 副作用。 |

## 仍然不进入业务写入

这次只证明 LCA 可以稳定收束跨项目只读设计，**不证明业务 owner 已定位**。生成的候选和缺失项仍需用真正的订单字段/制单状态 owner、结算单 DDL、模板管理与下载中心契约进一步确认；在此之前不对 `zqylpayment` 或 `mpspay` 执行 patch。

---

# T-132 文件发现与负向证据可靠性压测（2026-07-11）

## 目标

用原 `/add-dir /Users/chengming/mycode/project` 失败样本复测：Agent 在 primary 仍是 LCA、`project` 仅为 authorized additional root 时，能否只靠只读工具判断 Maven/Java 代码存在，而不把内容搜索 no-match、截断目录列表或 primary Git 状态误写成 negative conclusion。

## 首轮问题

| session | 工具调用 / error | 观察 | 根因 | 处理 |
|---|---:|---|---|---|
| `20260711T124530411799Z` | 约 27 / 3 | 百炼两次对 `glob_files` 同时传 `path` 和 `paths`，被严格 schema 拒绝；随后一次 Java glob 输出约 198KB，虽然最终判断 Maven/Java 存在，但模型在目录枚举、重复读取和大结果中浪费了上下文。 | schema 只暴露 canonical `paths`，但 provider 有已观测的无歧义 scope 方言；glob 返回未设模型输出上限。 | 在 Registry 单点把安全的 `path` + `paths` scope 方言归一为 canonical `paths`，不增加第二个模型可见 schema；glob JSON 输出在 30KB 截断时显式标记 `complete=false` / `incomplete`。 |

这不是对任意参数的宽松兼容：冲突 path、未授权路径和含义不明确的字段仍由 schema/path resolver 拒绝。

## 复测结果

| session | 工具调用 / error | 关键证据 | 结论 |
|---|---:|---|---|
| `20260711T125014148285Z` | 5 / 0 | 先浏览 `project`，再用 `glob_files` 找到 36 个 `pom.xml`；对 `**/*.java` 的结果被限制为 29,386 字符且明确 incomplete；随后读取两个 Maven `pom.xml`。 | 通过。最终回答确认目标包含 Maven 项目和 Java 源码，并明确 Java 清单被截断，未把未展示模块或文件说成不存在。 |

完整调用序列为：`list_files`、`glob_files(**/pom.xml)`、`glob_files(**/*.java)`、两次 `read_file(pom.xml)`。没有调用 shell、Git、测试或写工具。

## OMP 对照与后续

OMP 的 `glob` 与 `grep` 是两类工具：前者发现路径/文件名，后者匹配文件内容。LCA 已对齐这一职责边界，并把完整性 metadata 接入 Evidence Ledger、CompletionAudit 和 FinalAnswerSteerer；通用“负向结论必须匹配证据类型”的硬门是 LCA 在现有审计框架上的增强。

| ID | 状态 | 后续 |
|---|---|---|
| PT-057 | 已关闭 | `glob_files.path` + `paths` 的安全方言归一和 30KB incomplete cap 已通过真实百炼复测。 |
| PT-058 | 已关闭 | 内容 no-match、截断 `list_files`、primary 非 Git 均不能再支撑“没有 Java/src/源码/additional root Git”类结论；定向单测锁定。 |
| PT-059 | 已关闭（T-132C） | workspace inventory contract、exposed-only unknown-tool suggestion 和 RunSummary discovery/misuse 指标均已落地；真实百炼 session `20260711T131716175598Z` 已验证有界收束。 |

## T-132C 真实 inventory 复测

### 命令与边界

在 primary 为 LCA、`/Users/chengming/mycode/project` 为 authorized additional root 的情况下，使用百炼执行只读 inventory。提示明确要求：filename/extension/directory discovery 只能用 `glob_files`；只允许对少量 README、`pom.xml` 或 manifest 执行 `read_file`；禁止 `search_code`、LSP、shell、Git、测试和写入。

ToolChoiceQueue 已按每个 authorized root 最多两次、总计最多八次 discovery 建立 contract；如有 root 未覆盖或结果不完整，最终回答必须报告范围，不能作缺失结论。

### 结果

| session | LLM / 工具 | RunSummary | 观察 |
|---|---:|---|---|
| `20260711T131716175598Z` | 4 / 5（2 error） | `file_discovery_calls=4`、`file_discovery_incomplete_results=1`、`file_discovery_no_match_results=0`、`unknown_tool_calls=1`、`unknown_tool_suggestions=0`、`filename_search_misuse_calls=0` | 模型对 primary 和 additional root 都发起 `glob_files`；additional root 识别到 Maven/Java 证据。Java 输出达到 30KB cap 后明确 `incomplete`；Queue 在 discovery budget 后强制无工具最终回答。没有 shell、Git、测试或写工具。 |

两次 error 均被安全收敛：一次为 provider 的 malformed tool name，另一次为其畸形 arguments 进入 `_invalid_arguments`。二者都以可审计 tool error 回传，没有污染下一轮 provider request、没有暴露 hidden/deny tool，也没有触发 unsafe alias。最终文字中对 `file_count`/`observed_match_count` 的数值叙述仍不够严谨，记录为 provider presentation quality，暂不为单次描述偏差新增 generic guard。

### 结论与 OMP 对齐

T-132C 关闭 PT-059：LCA 现在像 OMP 的 `glob`/`grep` 职责拆分一样，将 filename/path discovery 与内容搜索分开，并以有界队列限制 inventory 扩散。LCA 的逐 root coverage、负向证据审计和 exposed-only suggestion 是针对 multi-root 本地 Agent 的增强。

additional root 不自动获得 Git/shell 权限；本次也不据 primary Git 状态判断 additional root。若要给某仓库作 Git 结论，仍需 `/move <repo>` 切换 primary。

| ID | 状态 | 后续 |
|---|---|---|
| PT-059 | 已关闭 | workspace inventory contract、unknown-tool containment、discovery/misuse metrics 已完成并由真实百炼复测。 |
| PT-060 | 已缓解，继续观察 | provider 可继续生成 malformed tool name/arguments；R-055 的 provider-safe sanitize 将其保持为 tool error，不为畸形 payload 扩张工具 schema。 |
| PT-061 | 开放，低优先级 | provider final narration 可能误解 bounded discovery metadata；后续仅在重复真实样本证明泛化风险时，考虑在 result wording/summary template 层改善。 |

---

# T-136 真实服务费结算 owner 定位与 evidence 收束（2026-07-11）

## 范围与边界

使用需求目录 `需求文档-拓展服务费结算V1.3.md` 作为 primary workspace，授权后端 `zqylpaymentmaster9d423763` 与前端 `mpspaymasterce6ca65` 为 additional roots。任务只允许读取、filename/path discovery 与内容搜索；没有执行 shell、Git、测试、memory 或写入。

目标是让 Agent 输出四段式 owner 定位：订单服务费/状态、结算单实体/DDL/Mapper、模板与下载中心契约、前端页面/API；未验证内容必须明确留在“合理候选”或“仍缺证据”。

## 连续真实样本

| session | 结果 | 发现 | 处理/结论 |
|---|---|---|---|
| `20260711T140113779257Z` | 未正常收束 | requirement 已在 primary，但 additional code roots 被旧 soft requirement 当作“还必须读取的外部需求目录”，导致 runtime 长时间只暴露 `list_files/read_file`，后续路径发现和搜索被错误拦截。 | 修正 soft requirement：primary 直接包含命名需求文档时，不把 code-only additional roots 变成第二份必读需求；仍保留“workspace 是代码、allowed root 是真实需求文档”的原有 gate。 |
| `20260711T140907290753Z` | 安全失败 | 跨 root matrix 在 owner 定位任务中原本不触发；修复后 24 tools 即收束，但 final guard 把“具体对象未找到”误解为“没有源码证据”。 | 将 owner/impact/call-chain 纳入既有 cross-root evidence matrix；收紧 source-evidence false-negative，只处理“源码整体未读/不完整”声明，具体字段/表/类未找到交给 negative-evidence 审计。 |
| `20260711T141332812456Z` | 安全失败 | 27 tools、0 error；final numeric guard 把 `V1.3:95` 等需求引用行号以及同名 `list.vue` 的不同证据混成源码数值冲突。 | 数值审计改为路径优先、同名文件多候选保留、忽略 `path:line`/`Vx.y:line` 引用数字，并仅在没有任何匹配证据支持业务数值时拒绝。 |
| `20260711T141702815603Z` | 通过 | 134,077 ms；28 LLM requests；27 tools；0 error；最终正常生成“已验证证据 / 合理候选 / 仍缺证据 / 建议下一步”。 | 首个端到端通过样本。仍只证明 runtime 的证据化收束，不证明结算单 DDL、模板服务或下载中心的真实 owner 已由当前两仓确认。 |

## OMP 对照与 LCA 采取的措施

| ID | 问题 | OMP 对照 | LCA 措施 | 状态 |
|---|---|---|---|---|
| PT-062 | provider 使用 `glob_files(path + pattern)`、`search_code(maxResults="50")` 等已观测方言。 | OMP 由工具 schema/结果归一边界吸收 provider 表达差异，不扩张多个模型可见工具。 | 在 `ToolRegistry` 的 compatibility normalization 单点接受无歧义 scalar alias；冲突的 `paths/pattern` 仍拒绝，模型 schema 只暴露 canonical `glob_files`。 | 已关闭，定向测试覆盖。 |
| PT-063 | primary requirement + additional code roots 被 soft requirement 误判为“必须再读 allowed-dir 需求”。 | OMP soft tool requirement 是具体 pending action 的 reminder/escalation，不应把无关 active root 永久锁成同一工作流。 | 仅当 additional root 真实包含命名 requirement/spec 文档时创建该 gate；primary 直接包含需求文档时跳过该 gate。 | 已关闭，定向测试覆盖。 |
| PT-064 | owner/impact/call-chain 任务绕过 cross-root evidence budget，根因只是它没有出现“design”字样。 | OMP 的 ToolChoiceQueue 按当前阶段给 directive，并对偏离/完成后的 follow-up 有界处理，而非用主循环总步数结束。 | 复用现有 `DesignEvidenceCoverageSteerer`，把 owner/impact/call-chain 识别为同类 multi-root evidence task；每 root 成功 source read 后只给六次补证据，再 force final。 | 已关闭，session `20260711T141702815603Z` 验证。 |
| PT-065 | final guards 把 scoped missing、需求引用行号和同名路径误当成“源码证据不存在/数值冲突”。 | OMP 的 tool result/context 边界应保留来源与生命周期，不能仅按文本片段推断事实。 | `SourceEvidenceFalseNegativeSteerer` 仅拦截整体源码缺失声明；numeric evidence 以完整路径优先、保留同名文件所有候选并忽略 location citation。 | 已关闭，定向回归 + 180 秒真实复测通过。 |
| PT-066 | prompt 写“禁止 LSP”但未设置 tool policy，provider 仍调用 `lsp_workspace_symbols`。 | OMP 的实际权限来自 active tools/mode/permission，不把自然语言当安全边界。 | 记录为开放操作规则：用 `--tool-approval` 显式 deny 各 LSP 工具；不为自然语言关键词增加隐式权限解析。 | 开放，低优先级。 |

## 业务结论与下一步

本轮不进入业务写入。最终报告只能确认 `PlatformOrderEntity`、`PlatformOrderController`、`PlatformOrderApplication`、`platformPayment.js` 等为证据化邻近 owner/candidate；不能确认“拓展服务费”实际字段、结算单主表/明细 DDL、模板管理 API 或下载中心预约下载契约已经在这两个仓库中实现。

下一步需要先获取至少一项真实来源：结算单 DDL/Mapper 所在仓库、模板管理服务接口、下载中心契约，或订单字段的确定 owner。之后在同一 session `/move` 到被确认的 primary，选择验收边界明确的小切片走 `read -> preview -> patch -> test -> diff -> reviewer`，而不是对当前候选路径直接开始写入。

---

# T-140 live fixture 基准复测与 runtime 加固（2026-07-11）

## 边界

本轮 `--live --provider bailian` 只向百炼发送了 benchmark 临时 fixture，未读取或发送企业源码、需求文档或用户项目。deterministic 基准仍完全离线。

## 问题、证据与修复

| 样本 | 现象 | 根因 | 措施 | 结果 |
|---|---|---|---|---|
| 原 live `multi-root-code-inventory` | 模型回答已识别 Java/Maven，但原验收只接受固定短语 `Maven Java 项目`，会产生假阴性。 | live provider 文案不能按 deterministic fixture 的精确字符串判定。 | live acceptance 改为 normalized regex、每 root `glob_files` coverage 和禁止全局负向外推；deterministic 仍保留精确文案。 | 语义验收可解释，不再因同义表述显示 0/2。 |
| `20260711T160120792100Z` | 8 LLM、11 tools、3 errors；模型调用 Git，虽最终结论合理但违反只读 inventory 合约。 | 中文“盘点当前 primary/additional root 项目代码”未命中 workspace inventory intent，Queue 首次暴露了过宽工具集。 | inventory intent 增加“盘点/项目代码”；每次 `LlmRequest` 写入实际 `tool_schema_names`，方便审计 active-tool 投影。 | Git 工具不再进入后续复测的 active schema。 |
| `20260711T160317681068` | Git 调用已消失，但 7 LLM、7 tools、2 errors；`read_file` 被错误告知 candidate patch read budget 耗尽。 | inventory 复用了仅属于自主小改的 scoped candidate read guard。 | inventory 保持 glob/list/read allowlist，但不再设置 candidate read scope/budget。 | 正确区分“有界 inventory”与“自主小改交付”。 |
| `20260711T160446291065Z` | live multi-root 复测。 | 上述两项修复后。 | primary 和 additional root 各有结构化 `glob_files` 覆盖；工具 schema 与 Queue active allowlist 一致。 | **通过：6 LLM、7 tools、0 error、无 shell/Git/测试/写入。** |
| `20260711T160157187152Z` | scoped negative Java evidence。 | 验证语义化 live acceptance 与范围负向结论。 | 要求完整 primary glob，不允许外推 additional root。 | **通过：10 LLM、6 tools、0 error。** 最终明确“当前 primary workspace 不存在 Java 源码”，且不否定 additional root。 |

## 其他本轮防护

- `ToolUsageEvidenceSteerer` 对最终回答中“根据某工具证据”“某工具无结果”等陈述核对本 run 的真实 tool results。未调用的工具不能被伪造成证据；单元回归覆盖虚构 `lsp_symbols` / `lsp_workspace_symbols` 结果时的强制改写。
- benchmark 报告新增 session/run id、最多 8 条脱敏 tool error 摘要，以及 compaction 的 estimated token reduction、zero-gain 和连续 zero-gain 指标。`--preserve-failed-sessions` 才复制失败的 fixture JSONL，默认报告不保留内容副本。
- `run_tests` schema/错误提示明确 `command` 必须是完整可执行命令；单独的 `tests.test_math` 返回如何写成 `python3 -m unittest tests.test_math` 的安全提示，不做任意字符串猜测。

## 结论

T-140 的离线 6 个 fixture 继续全部通过。live provider 不再用固定文案误判两项只读任务；multi-root inventory 的真实错误已分别修在 ToolChoiceQueue intent 和 read-scope 所属边界，不向 `agent.py` 新增关键词 guard。仍需持续观察百炼在长只读最终回答中的工具数量与 presentation quality，但这不阻塞明确目标的小改链路。

---

# T-141 Xiaoya black-box multi-root finalization reliability（2026-07-12）

## 边界

本轮仍只使用临时 fixture 和百炼 live benchmark，不读取或发送企业源码。目标是按 OMP 的 queue / turn owner 原则修掉三类黑盒问题：finalization ping-pong、provider schema violation 缺少单独可观测性，以及 primary root 文档证据误导 sibling code root。

## 修复前样本

| session | 现象 | 观察 |
|---|---|---|
| `20260711T234948946966Z` | 正常完成 | 17.7 秒，5 LLM、12 tools、0 error，正确覆盖 primary / service-a / service-b。 |
| `20260711T235356936983Z` | 慢收敛 | 31.3 秒，12 LLM、11 tools、2 errors；`requirement_evidence`、`forced_final`、`negative_existence`、`workspace_inventory_budget` 交替触发，最终虽完成但重写过多。 |
| `20260711T235014928853Z` | 未正常终止 | 没有 `run_summary`；停在 `llm_request step 12`，4 分钟后被手工清理。说明 terminal phase 缺少统一 owner 与外层 timeout 兜底。 |

## Runtime 修复

| 问题 | OMP 对齐点 | LCA 修复 |
|---|---|---|
| finalization ping-pong | `tool-choice-queue` / `agent-loop` 中 pending owner 与 continuation 上界明确，terminal phase 不被无界 reopen。 | 新增 `finalization.py`，由 `FinalizationCoordinator` 统一持有 forced-final terminal ownership、aggregate finalization budget 和 unresolved gate。 |
| provider schema violation 只有普通 tool error | OMP 的 active tools 是硬边界，越界调用会被拒绝且可单独观测。 | `ToolRegistry` 对 runtime allowlist 外但名称已知的调用打上 `provider_schema_violation` metadata；`RunSummary` / benchmark 报告单列该指标。 |
| root-local 文档被错误外推 | OMP 的 context/tool evidence 带来源与生命周期，active root 不应隐式跨仓传播结论。 | `EvidenceLedger` / `ToolChoiceResult` / final steerers 现在保留 `root` + `scope` provenance；primary 文档默认只覆盖 primary。 |
| provider 不按 timeout 返回会卡住终端 | OMP 有 abort/deadline 贯穿 loop。 | `chat_runtime.py` 增加外层 timeout；T-142 进一步让普通首轮与 forced-final 的 `LlmError` 都走 terminal closure，写入 `final`、`run_summary`、`SessionFinished`，并区分 `llm_timeout` / `provider_error`。 |

## 定向验证

- 全量 unittest：**441/441** 通过。
- 离线 benchmark：**6/6** 通过。
- 关键新增回归：
  - 多个 final steerer 交替拒绝 final 时仍有上界，最终返回明确结果；
  - hanging provider 在 forced-final 阶段会被外层 timeout 切断并写出 `run_summary`；
  - provider 调用 active schema 外工具时拒绝执行并单独计入 `provider_schema_violations`；
  - additional root 的 `read_file` / `search_code` / `glob_files` 证据保留 root-local provenance。

## 百炼 live 复测

### run1

| task | session / run | 结果 | 指标 |
|---|---|---|---|
| `multi-root-code-inventory` | `20260712T001400071406Z` / `4302efab477d4275b67bcc255f31034e` | 通过 | 9 LLM、6 tools、0 error、`provider_schema_violations=0`、`finalization_attempts=6`、termination=`final` |
| `scoped-negative-source-evidence` | `20260712T001427004155Z` / `6be7fcad02f2401abf24abde31d79744` | 通过 | 10 LLM、9 tools、0 error、`provider_schema_violations=0`、`finalization_attempts=0`、termination=`final` |

### run2

| task | session / run | 结果 | 指标 |
|---|---|---|---|
| `multi-root-code-inventory` | `20260712T001444596471Z` / `cd42a5b4ba5d4980b57fe7dd350ed64d` | 通过 | 5 LLM、6 tools、0 error、`provider_schema_violations=0`、`finalization_attempts=1`、termination=`final` |
| `scoped-negative-source-evidence` | `20260712T001457282493Z` / `569ff35df0ba414393ef6ad1f019b65f` | 通过 | 6 LLM、5 tools、0 error、`provider_schema_violations=0`、`finalization_attempts=0`、termination=`final` |

## 结论

T-141/T-142 关闭了这一轮最危险的 runtime 缺口：finalization 被多个 auditor 连续打回时有统一 owner、统一预算和外层 timeout；普通首轮 provider timeout/error 也会明确收束，而不是把异常抛回 CLI。live 两轮证明 multi-root inventory 与 scoped negative evidence 可以稳定终止；pinned requirement 也保留 root-local 归属，不能再把 primary 文档外推为 sibling service 的修改指令。

残余风险：Python 无法安全强杀正在底层阻塞的 provider worker。外层 timeout 会立即让 Runtime 回到 terminal prompt，worker 是 daemon，不会阻止进程退出；若 provider 永久不返回，该线程会在进程存活期间残留。后续若真实压测出现累积线程问题，再评估可取消 HTTP client 或进程隔离，不把当前行为描述为“已取消底层请求”。

## T-142 provider terminal closure / pinned requirement provenance（2026-07-12）

- 首轮 hanging client（`request_timeout=1`）返回明确未完成结果，termination=`llm_timeout`，并写入 `final`、`run_summary`、`SessionFinished`；memory consolidation 不再额外调用故障 provider。
- 首轮立即抛出的 `LlmError` 返回 termination=`provider_error`，同样完成 session/event closure，不再把异常传到 CLI。
- `RequirementEvidence`、`search_code`、`lsp_*` evidence 都记录 canonical `root` + `scope`；pinned requirement context 明确 `root_local` 只约束来源 root，除非用户明确要求跨 root 综合。

---

# T-143 Verification Plan / Test Planner / Delivery Audit（2026-07-12）

## 目标与 OMP 对齐

本轮不复制 OMP 的平台层角色系统，复用其 `tool-choice-queue` / `agent-loop` 的核心原则：一个状态只能由被 Runtime 观察到的工具 turn 与结果推进，不能由模型在最终文本中自我宣告完成。

LCA 将 RequirementContract 的业务 acceptance 保留为 `unverified` 信息；新增的 Runtime delivery checks 只覆盖四类可机械核验事实：

1. 与最终有效写入路径相关的代码证据；
2. 最后有效写入后的、包含本轮路径的当前净 diff；
3. 最后有效写入后的测试结果或结构化 approval block；
4. post-diff deterministic reviewer 结论。

## 关闭的问题

| 问题 | 原先风险 | T-143 措施 | 回归 |
|---|---|---|---|
| 代理事实伪完成 | 任意 write/read/run_tests 可能让“实现/遵循模式/补测试”被误标完成。 | `VerificationPlan` 让 contract 业务项永远不由代理事实改为 passed；delivery checks 独立状态化。 | 任意 write、无关 README read、旧测试都不能通过。 |
| rollback / 脏 diff | rollback 后或用户已有 dirty diff 非空时，任意 `git_diff` 文本可能放行。 | `verification_timeline` 跟踪 effective write paths；diff 必须包含本轮当前路径。 | patch + rollback + README 脏 diff 仍为 pending。 |
| approval deny | 过去只能从文本猜拒绝。 | ToolRegistry 给审批拒绝加入 `execution_status=denied` / `denial_kind=approval` metadata。 | test check 标为 blocked，绝不计为 passed。 |
| 测试候选夸大 | `mvn test` / `npm test` / 全量 unittest 被称为最窄测试。 | `TestPlanner` 明确 `module` / `project` / `blocked`；当前 manifest 命令均为 project fallback。 | placeholder npm test 被识别为 blocked。 |
| 终态只靠模型措辞 | VerificationPlan 即使机械通过，模型只答 `done` 仍会丢失改动、测试与剩余风险。 | `DeliveryReport` 由 Runtime 从实际 tool timeline 追加到每个有效写入的 final/incomplete 终态。 | `done` 成功答复仍含 changed path、实际测试命令和 passed=4；失败 fixture 保留 `src/math.py`、失败命令与 failed 状态。 |
| 否定性元讨论被误判 | “不能推导出没有 Java 源码”被当成实际不存在结论，重新开放 glob 并拉长 finalization。 | 负向存在性审计改为 code/quote 排除和命中附近的 stance 判断。 | 中英文“cannot conclude / does not prove”、引用示例、无关 modal/后半句否定均有回归。 |

## 验证

- deterministic benchmark：7/7 通过，成功 fixture 锁定 `final` 与四项 delivery checks 全通过；新增“测试失败但保留变更”fixture，锁定 `incomplete_delivery`、`src/math.py` 与实际失败测试命令，不会伪装为完成。
- 百炼 live synthetic fixture：session `20260712T005706166697Z` / run `82f103e7808a43b7a652c039e9ef2449`，9.8 秒、7 LLM、8 tools、0 errors；`src/math.py` 完成 read、dry-run、真实 patch、`PYTHONPATH=. python3 -m unittest tests.test_math`、git diff，delivery checks 为 passed=4 / pending=0 / failed=0 / blocked=0 / skipped=0。
- 本轮未读取或发送企业源码；live 只使用临时 benchmark fixture。Delivery Report 本身以 deterministic fixture 验证，未为措辞重复调用 provider。

## 残余风险

- 多次交错 patch/rollback 同一路径时，effective-write 路径跟踪采取保守策略；无法证明当前本轮净变更时会要求重新验证，而不会错误放行。
- T-144 已在本文件后续章节收口：同一 Runtime 的相关 follow-up 可复用带 revision/content tag 的正向证据；负向存在性证据仍保守地不跨轮复用。

---

# T-144 Session Evidence Continuity / User Facts Layer（2026-07-12）

## 范围与 OMP 对齐

本轮只使用临时 Python fixture 和 deterministic fake provider；未读取或发送企业源码。对齐 OMP 的 session tool-result/context continuity：Runtime 可以跨同一 chat turn 延续仍新鲜的工具事实，但不把历史文本、负向结果或用户原文当作当前 repository 证据。OMP 的 `compaction/messages.ts` 将 compaction summary 映射为 `role: user` 且标记 attribution；LCA 采用同一信任边界，不复制 OMP 平台层。

## 实现与回归

| 问题 | 措施 | 回归 |
|---|---|---|
| follow-up 被迫重复 read/search/LSP | `SessionEvidenceCache` 仅缓存 positive / complete / concrete-path evidence；每次投影前对全部路径重算 content tag。 | 同 Runtime follow-up 可在 0 新工具下通过 deterministic audit；新 Runtime 不继承 cache。 |
| 外部变化、写入或 root 变更使旧事实失真 | external hash 不匹配即淘汰；write/rollback、add/remove/reset、成功 `/move` 失效；dry-run 不失效。 | 覆盖 stale eviction、实际写入、rollback、workspace lifecycle 与 move rollback。 |
| 重复观测让 cache 和上下文逐轮膨胀 | entry identity 按 canonical tagged paths 与 read range/search/LSP query 建立，不含 run id；新观察替换旧条目。 | 相对/绝对 path、缺省/显式 `start_line=1` 合并；不同 range/query 分开。 |
| user text 被 compaction 或 summary 升权 | 最新真实 user message 永远保留；prior user fact 只有相关且已丢失时才以 user-role context 恢复；system/developer 不复制 raw user text。混合 summary 为 `[Local context compaction; attribution=runtime]` user message。 | local/LLM compaction、workflow nudge、echoing summary、tool-pair validity、超长 tail 都有回归。 |

## 验证与残余风险

- 完整 unittest：**500/500**；`compileall`、`git diff --check` 通过；deterministic benchmark：**8/8**。
- live synthetic 双轮：session `20260712T013348513387Z` / second run `13193d5bb817450eb7fb20c151850d85`，cache hit=1、0 tool error、termination=`final`。provider 仍主动执行两次 `read_file`，所以严格零工具 live acceptance 为 0/1；这是百炼工具选择效率样本，不是 cache 正确性失败。
- 残余风险：缓存只在内存/当前 Runtime 生效，relevance 使用保守 token/上一轮关联；不缓存 negative/existence evidence，宁可重新搜索。后续若真实 follow-up 的重复读成本持续显著，再从 ToolChoiceQueue 的 active tools/turn ownership 观察，不以关键词硬禁工具。

---

# T-145 Epistemic Claim Taxonomy / Negative Evidence Stance（2026-07-12）

## 触发样本与 OMP 对齐

小牙 stable session `20260712T011400898209Z` / run `984902aaa5084111a62b6f50d0920a11` 中，模型表达“未发现 Java 源码，但这不等于证明 primary 无 Java”，旧的负向审计把该谨慎表述误判为绝对缺失，重开 discovery 并增加 finalization 长尾。

本轮参考 OMP `packages/coding-agent/src/session/tool-choice-queue.ts` 的有界 pending owner，以及 `packages/agent/src/agent-loop.ts` 的 active-tool/continuation 生命周期：工具事实与 Runtime 状态决定是否继续，不由多个消费者各自扫描自然语言。LCA 没有复制 OMP 平台代码，而是在 `negative_evidence.py` 建立确定性的 clause-local parser。

## Runtime 改动

| 旧行为 | T-145 改动 | 证据政策 |
|---|---|---|
| “没有/未发现/no”短语一律按缺失处理 | 结构化为 `asserted_absence`、`observed_no_match`、`epistemically_qualified`、`quoted_or_hypothetical`，保留 subject、claim-local root/scope、support requirement 与 clause span。 | absolute 缺失触发 complete discovery/Git gate；observed no-match 不等于全局不存在，但也必须有本轮、同 root、匹配 query 的真实 observation。 |
| “不能运行测试，但该 root 没有 Java”容易被前半句 modal 吞掉 | clause-local 支配关系保留后半句实际断言。 | 仍需完整、未截断、scope-matched 的 `glob_files`/Git evidence。 |
| “未发现 Java，但不等于证明没有 Java”、引用或假设也会补搜 | 限定、引用、示例、问题和假设不会升级为 absolute claim。 | qualified/quoted 不因此强迫额外工具调用；未限定的 observed no-match 仍需真实同 scope observation，不能由模型自行声称“检查过”。 |
| 负向证据策略分散在 steerer/audit | `NegativeExistenceSteerer` 与 CompletionAudit 共享 parser；session/RunSummary 记录 stance、blocked assertions/observations、qualified skips。 | cached positive search/LSP、content no-match、截断结果不能支持 absolute absence；文件/源码 observed no-match 只能由 complete `glob_files` 支持；claim 绑定最近 root marker，multi-root claim 要完整覆盖多个 root。 |

## 验证

- deterministic 回归：中文/英文限定语、引用/假设、无关 modal 后的真实断言、mixed claim、observed-without-tool、quoted connector、primary/additional 同句交叉 root、truncated/cached positive、multi-root scope 和 bounded steerer cap 均覆盖。
- 全量 unittest：**519/519**；`compileall`、`git diff --check` 通过。
- offline benchmark：**9/9**。新增 `qualified-negative-observation`：完整 `glob_files` 后的“未发现但不等于证明”自然结束，只有一次 discovery，不会触发 negative steerer。

## 百炼 live 合成压测

仅向百炼发送临时 fixture 的 `README.md` 和 benchmark 任务，不读取或发送企业源码。

| session / run | Runtime taxonomy | 结果 | 指标与结论 |
|---|---|---|---|
| `20260712T020107510742Z` / `dd5a3da31f654088b5787f90db4cfce6` | `observed_no_match=1`，无 `negative_existence` steering | strict acceptance 未通过 | 5 LLM、3 tools、0 error、termination=`final`。百炼自行追加 `list_files` 和第二次 `glob_files`；CompletionAudit forced-final 后返回原始 XML tool-call 文本，未输出要求的最终谨慎结论。taxonomy 未回归，暴露的是 provider 探索效率与 forced-final 文本质量。 |

## 后续候选

## T-146 Provider Final-Output Hygiene / Forced-Final Tool Boundary（2026-07-12）

### 归因与 OMP 对齐

T-145 live session `20260712T020107510742Z` / run `dd5a3da31f654088b5787f90db4cfce6` 的 taxonomy 已正确，但 CompletionAudit forced-final 后把百炼 provider content 中的 XML tool markup 原样显示。脱敏历史可验证的形态是完整尾部 `<tool_call><function=read_file><parameter=path>…</parameter></function></tool_call>`；没有证据证明所有 XML 或所有 provider 文本都应清洗。

OMP 的 `agent-loop.ts` 在每个 logical turn 只解析一次 active tool-choice，并以 pending/in-flight turn owner 收束 continuation；其未执行 tool call 也有显式 synthetic result 路径。LCA 采用相同边界而非复制代码：forced-final 是 terminal-only turn，schema 为空；若 provider 仍违规，终态由 `FinalizationCoordinator` 收束而非回到工具循环。

### 修复与验证

- `provider_protocol.py` 只识别 `bailian` / `dashscope` / `aliyun` 的完整、未围栏、严格语法 XML envelope；代码围栏、引用、未知/残缺 XML 与普通阶段不改写。
- forced-final structured `tool_calls` 或已分类 markup 均为 `forced_final_protocol_violation`：0 execution、0 原始参数回显，写脱敏 `provider_protocol_violation` session event、`ErrorEvent` 与 RunSummary 指标（structured calls、markup artifacts、suppressed executions）后 terminal closure。
- deterministic benchmark 10 固定复现该 markup，验证 `read_file` 只在普通阶段执行一次、forced-final schema 为空、违规文本不进入最终答复。
- 全量 unittest：**526/526**；offline benchmark：**10/10**；`compileall` 与 `git diff --check` 通过。

### 一次百炼 live 合成复测

| session / run | 结果 | 指标 | 结论 |
|---|---|---|---|
| `20260712T030231154898Z` / `06186df00300400c98f418595a891fb8` | 未复现 markup，正常 final | 21,179 ms、3 LLM、1 `read_file`、0 tool error、`finalization_attempts=1`、`forced_final_protocol_violations=0` | provider 在无工具 final turn 直接给出正常表格答案；live acceptance 不再要求必须复现违规。该次只发送临时 fixture README，未读取企业源码。 |

残余风险：目前只识别已观测的百炼 XML 信封，其他 provider-specific text protocol 会保持原样并由后续安全样本决定是否扩展；normal final 不代表 provider 在其他模型/版本上不会再次违规。T-146 不发布 stable，等待独立 review。

## T-147 Evidence Intent / Metadata Audit Closure（2026-07-12）

### 触发样本与根因

stable 黑盒样本显示三类同源问题：模型可以在没有 tool result 时说“我检查后未发现 Java”；只要求解释引号内句子语义、并明确禁止检查仓库的任务，会被 `源码`/`Java` 等词触发 read-only evidence queue；primary `git_status` 的 `git_repository=false` 结构化结果则会被 CompletionAudit 错当成缺 read/search/LSP 证据。它们都不是业务关键词问题，而是 intent、claim 和 evidence owner 没有对齐。

### 修复与 OMP 对齐

- `RequirementContract` 增加 semantic-only `inspection_forbidden` 状态。它要求同时出现语义目标和明确 no-inspection 指令；ToolChoiceQueue、active schema、soft requirement、cross-root design coverage 与 CompletionAudit 都消费同一状态。用户若同时要求仓库事实，Runtime 不会检查，而是要求把该事实标为未验证。
- `negative_evidence.py` 把 bare observed Java（例如“检查后未发现 Java”“Java 相关文件/代码未发现”）解析为 `observed_no_match`；它需要本轮、同 root、匹配 discovery evidence 才能结束。绝对“没有 Java”继续要求文件/root scope，Java 经验、依赖与版本要求不进入文件 taxonomy。
- ToolUsageEvidence 识别“已调用/使用/检查/查找工具并得到结果”的虚构工具证据，也识别明确 no-run 与建议语，避免两者互相误伤。
- Git repository 属于 primary workspace metadata owner：结构化 `git_status` non-repository 结果可以完成 Git-specific read-only contract；generic Git execution error 或 additional root 结论仍保持未验证，additional root 仍需 `/move`。
- T-146 复审同时收紧普通 phase：已识别的完整、未围栏百炼 XML tool envelope 不会再静默作为普通答案展示，而是 `provider_protocol_violation`；fenced/quoted XML 与正常 structured tool-call phase 不受影响。该做法延续 OMP 的 active-tool / turn-owner 边界，不复制其 UI 或平台层。

### 验证

- 全量 unittest：**538/538**；`compileall`、`git diff --check` 通过。
- offline benchmark：**11/11**。新增 `bare-observed-no-match`：模型先给无工具“检查后未发现 Java”，Runtime 必须先取得 `glob_files` 完整 no-match 才能 natural final。
- 未重复调用百炼 live；本批 deterministic 覆盖三组 stable 黑盒语义，避免为措辞质量重复刷外部 API。T-147 不发布 stable，等待独立 review。

残余风险：semantic-only intent 仍是保守、有限的规则识别，不做 NLP judge；未识别的 provider-specific protocol text 继续保持原样，只有被严格 adapter grammar 识别的 envelope 才会被 terminal policy 拦截。
