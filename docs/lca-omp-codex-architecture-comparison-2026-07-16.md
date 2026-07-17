# LCA / OMP / Codex 源码级架构对照

更新时间：2026-07-17

## 对照基线

| 项目 | 本次读取版本 | 定位 |
|---|---|---|
| LCA | `d0f9153b3709c420cc9affd763a5e82c2abb9485` | Python、本地优先、封闭 VM 友好的个人 coding-agent MVP |
| OMP | `b086c5d669f7f5f08a49f223dae7bd7f49cca6e1` | 多 provider、多工具、TUI、task agent、memory 与扩展体系完整的 coding-agent platform |
| Codex | `be39aab9f45fc76705590b063bc2480827571561` | Rust 原生 agent runtime、sandbox/exec policy、协议/App Server/TUI/插件体系完整的平台 |

本文区分三类结论：

- **源码事实**：能从当前 checkout 的代码直接确认。
- **架构判断**：根据三方职责边界和生命周期作出的判断。
- **建议**：针对 LCA 的个人本地、企业代码、封闭 VM 场景给出的取舍。

## 总结结论

LCA 的路线没有错，也不应该直接搬 OMP 或 Codex 的代码。当前真正的问题不是“功能太少”，而是能力投资顺序失衡：

1. LCA 已具备一个实用 coding agent 的大部分基础零件：工具循环、审批、multi-root、anchored patch、测试、diff、session、compaction、memory、skills、LSP fallback、事件协议和稳定发布。
2. LCA 对弱 provider 的只读证据质量投入过重。`task_contract.py`、CompletionAudit、read-only explore/reviewer、document consistency、safe partial 等已经形成一套约一万行的确定性语义流水线，并被默认主循环直接编排。
3. OMP 和 Codex 的核心 loop 复杂，但复杂度主要属于 turn/protocol/tool lifecycle；它们不会在每个普通任务上用大量中文/英文关键词判断业务语义并认证最终答案。
4. LCA 当前是“核心平台能力尚未完全打通，交付审查层已经很厚”。这解释了为什么测试很多、只读报告很稳，但第一个真实 S5 写交付仍然失败。
5. 正确目标不是“复制或完整追平 OMP”，而是：**Codex-first 核心骨架 + OMP-informed coding 能力 + LCA 自己的封闭 VM/企业证据工作流**。

当前阶段判断：**P12 只读分析能力可用；P13 Runtime/Command/Event/Streaming 阶段性收口；P14 只读 Explore 保留 Phase 1 但扩展暂停；P15 semantic preview 已收口；P16 已用 clean/dirty worktree、跨语言 coding 和同步 interrupt continuation 证明普通读改测 diff、最新需求覆盖与未完成 verification obligation 可恢复。首个企业写路径仍只有 Codex 隔离参考实现，尚未由 LCA 自主交付并进入生产，因此当前是“通用小中型 coding 可用、企业长链需监督”的版本。**

## 正式参考路线

如果必须选择一个总体架构母版，LCA 选择 **Codex CLI**。Codex 的 `Op/EventMsg`、Session/Turn/Step、Tool Router、ExecutionPolicy/Sandbox、state/worktree 与多前端边界更适合作为长期产品骨架，也更能约束当前 Runtime 和语义流水线继续膨胀。

OMP 不降级为普通资料，而是作为 **coding-agent 能力的第一参考**：agent loop/directive、弱 provider 适配、hashline/edit、compaction、memory/skills、task/advisor 与终端交互仍优先源码对照 OMP。LCA 不复制 Rust 或 TypeScript 实现；采用的是 Owner、生命周期和安全不变量。

| 领域 | 第一参考 |
|---|---|
| Runtime 生命周期、Command/Event、执行与前端解耦 | Codex |
| Approval action、ExecutionPolicy、Sandbox、State/Worktree | Codex |
| Agent loop 行为、Provider 兼容、ToolChoice directive | OMP |
| Edit、Compaction、Memory/Skills、Task/Advisor 体验 | OMP |
| LCA 产品约束 | Python、本地优先、封闭 VM、单 API、渐进迁移 |

迁移采用渐进方式：冻结新的默认语义 gate，先稳定协议与 Session/Turn/Step Owner，再让 CLI/TUI/Remote 共用事件边界；现有工具和已验证能力保留，不以架构调整为理由削减功能或整体重写。

### T-218 反例：不要用确定性正则理解开放用户语义

T-216 的真实失败是 `Do not rewrite tests` 被裸子串 `write test` 误判。首轮修复继续沿着确定性解析扩充中英文 action、negative grammar 和局部作用域；虽然 packaged tests 全绿，小牙的独立改写样本“无需修改或新增测试”仍触发 `requested_test_missing`。这证明问题不在词表覆盖率，而在职责边界。

- OMP 的 reviewer prompt 接收 diff/context，由模型输出 structured findings；没有多语言 test-intent parser。
- Codex 的 base instructions 让模型判断何时运行或添加测试，review rubric 检查具体可证明缺陷；没有从 raw prompt 生成“必须改测试文件”的 deterministic gate。
- LCA 因此拒绝 T-218 regex candidate，并在 R2 删除错误的语义裁判。真实 `run_tests` 退出码、post-write diff、VerificationPlan、CompletionAudit 和事实型 reviewer 继续保留；原 C1 immutable live 自然 DELIVERED 后发布 stable `20260716T075910Z-e710a783c2d9-846af66e44ab`。

以后若需要强制“必须修改测试文件”，来源必须是上游显式结构化 command/contract，或者由模型 reviewer 基于原始请求、diff 和测试结果给出 typed finding；不得由下游重新猜测自然语言。

## 粗粒度对照

| 维度 | LCA | OMP | Codex | 判断 |
|---|---|---|---|---|
| 核心 loop | 单类 Runtime 编排模型、工具、证据、reviewer、finalization | agent-loop 主要拥有 turn、tool result pairing、queue、deadline、steering | `run_turn` 主要拥有 sampling、tool follow-up、input queue、hooks、compaction | LCA loop 承担了过多语义策略 |
| Provider | OpenAI-compatible chat completions + tool-call-safe streaming，百炼优先 | 多 provider、native/in-band tool dialect、流式 | OpenAI/Responses 为核心，也有本地 provider 与连接层 | LCA 已有 streaming MVP，但 provider 广度、能力描述和稳健重试仍弱 |
| 工具系统 | 28 个本地工具，Python schema/tier/registry | 大量内置工具、task/job/browser/LSP/DAP/memory/skills | typed router/runtime、MCP/apps/plugins、exec/apply patch | LCA MVP 覆盖不错，平台扩展能力不足 |
| Approval | mode + per-tool + session allow/reject | tier + dynamic approval + wrapper 单边界 | typed action + approval + execpolicy + sandbox + hooks/guardian | LCA 只有审批，没有真正执行隔离 |
| 编辑 | hash + line + old_text anchored patch，dry-run/rollback | hashline/edit/ast edit/write/checkpoint | apply_patch + diff tracker + sandbox | LCA 的 MVP 设计正确，应保留 |
| 上下文 | token 近似 + reserve + local/LLM summary | 成熟 compaction、provider/model 适配 | token window、local/remote compact、mid-turn rollover | LCA 已达到可用 MVP |
| Memory/Skills | Markdown memory、可选 consolidation、项目 skill discovery | Mnemopi/Hindsight、多 backend、managed skills | SQLite memory pipeline、AGENTS、skills/plugins | LCA 当前取舍适合封闭 VM，不应急着上向量库 |
| Reviewer | 默认 read-only 高风险场景使用独立 reviewer + schema repair | task/reviewer/advisor 是可组合角色 | hooks、roles、subagents，可按任务使用 | LCA 能力有价值，但不应成为默认核心路径 |
| Event/Frontend | dataclass Command/Event + 单一 CommandDispatcher；terminal/CLI 走同一提交边界 | Runtime/session 与成熟 TUI/ACP/扩展层 | `Op`/`EventMsg` 协议、TUI、App Server | LCA 已完成同步解耦 MVP，async/cancel/remote 仍未实现 |
| LSP | 外部 LSP + lightweight fallback；已有 rename 和 Code Action 只读 preview | 完整 LSP/DAP/AST 工具，rename/code action 可 apply | 当前 core 没有原生 LSP 工具体系 | LCA 已有 semantic preview 优势，auto-apply/事务型 writer 仍待真实收益支持 |
| Sandbox | 无；依赖审批、workspace 和封闭 VM | bash 主要依赖 approval，非 Codex 级 OS sandbox | macOS/Linux/Windows sandbox、network policy、exec policy | 若要给别人使用，LCA 必须补安全边界 |
| 离线/封闭 VM | 最强：标准库零必选依赖，单 API | 可裁剪但 Bun/native/平台依赖重 | 可分发原生 binary，但构建和平台依赖重 | LCA 的差异化方向正确 |

## 1. Core Loop 与停止条件

### LCA 源码事实

主循环位于 `src/local_agent/agent.py::_run_prompt()`。它除了 deadline、provider 调用、tool dispatch 和 tool-result pairing，还直接编排：

- RequirementContract 生成；
- workspace evidence root 投影；
- session evidence hydrate；
- soft tool requirement；
- ToolChoiceQueue；
- provider dialect/terminal recovery；
- final-answer steering；
- read-only reviewer；
- read-only explore budget；
- patch review、重复/无效探索 guard；
- finalization 和 memory consolidation。

T-215 后文件已经从历史峰值降到 1,808 行/71 methods，但行数下降不等于 loop 已经薄。`_run_prompt()` 仍然知道较多产品语义阶段，后续继续按生命周期 Owner 收敛，不以机械搬运为目标。

### OMP 源码事实

`packages/agent/src/agent-loop.ts` 当前约 2,262 行，绝对行数甚至高于 LCA。它主要处理：

- deadline/abort；
- steering、aside、follow-up queue；
- 每 turn 的 tool-choice directive；
- soft requirement 的 reminder -> bounded force；
- provider stream；
- tool call/result pairing；
- truncated/aborted/skipped synthetic result；
- 有工具调用或 pending message 时继续，否则停止。

OMP 的关键不是“小文件”，而是 loop 中的大部分分支属于协议和 turn lifecycle。todo、task、memory、browser、LSP、approval 等具体策略由 session、tool 或扩展 Owner 承担。

### Codex 源码事实

`codex-rs/core/src/session/turn.rs::run_turn()` 负责：

- pre-sampling compaction；
- world state、skills/plugins、hooks 注入；
- sampling；
- tool/pending-input follow-up；
- token window rollover；
- stop hooks；
- 无 follow-up 时结束。

具体工具经 `codex-rs/core/src/tools/router.rs` 和 registry/runtime 分发，审批在 `tools/approvals.rs`，sandbox/execpolicy 在独立 crate。

### 架构判断

此前把 `agent.py <= 2100` 当成核心验收是一个有用的临时 ratchet，但不是长期架构指标。OMP 和 Codex 都允许大型 lifecycle aggregate；真正应锁的是：

- core loop 只能依赖通用 turn/tool/context/session 接口；
- 业务语义、文档类型、证据措辞不进入 core loop；
- 可选工作流不能改变普通任务的默认停止条件；
- 新策略必须通过 hook/profile/directive 接入，并有明确最大次数。

## 2. 确定性语义 Harness

### LCA 源码事实

当前下列模块共同实现语义审查：

- `task_contract.py`：约 935 行，使用大量中英文 marker/regex 将 prompt 分类成 read-only、implementation、document、owner/design/readiness 等 contract。
- `completion_audit.py`：约 1,093 行，判断最终回答和工具结果是否满足完成条件。
- `steering/evidence.py`：约 985 行，检查路径、工具、数值、负向结论等回答措辞。
- `read_only_explore.py`、`explore_handoff.py`、`runtime_read_only_review*.py`、`read_only_reviewer_*`、`document_consistency.py`、`safe_partial_report.py`：形成 explore -> typed handoff -> isolated reviewer -> repair/rewrite -> safe partial 的完整流水线。

这些机制修复了真实弱模型问题，也带来了三类成本：

1. 普通自然语言很容易被 marker 误分类。
2. 新失败样本会诱导继续增加 taxonomy、regex 和终态分支。
3. Runtime 为了装配这些 Owner，仍然承担大量阶段知识。

### OMP/Codex 对照

OMP 更依赖 system/project prompt、工具描述、ToolChoiceQueue、task role 和 bounded yield。Codex 更依赖 developer instructions、tool router、hooks、roles 和独立 agent。两者都有针对 provider/protocol 的 guard，但没有发现与 LCA 等价的“默认对普通 prompt 做大规模关键词合同分类，再由 Runtime 认证回答语义”的核心体系。

### 建议

不删除现有能力，改为三个 workflow profile：

| Profile | 默认状态 | 内容 |
|---|---|---|
| `coding` | 默认 | scope/read/patch/test/diff；只保留安全、协议和交付事实硬门 |
| `enterprise-evidence` | 显式或任务模板启用 | Evidence Ledger、multi-root owner、负向证据、document coverage |
| `readiness-audit` | 显式启用 | isolated reviewer、document consistency、typed BLOCKED/safe partial |

迁移前先冻结功能，不做删除。用 S1-S10 telemetry 证明每个 gate 的触发率和价值，再决定合并、降级 advisory 或删除。

## 3. Approval、Exec 与 Sandbox

### LCA

`src/local_agent/tools/base.py` 在 ToolRegistry 单点执行 mode、per-tool 和 session policy，这是正确方向。但当前审批提示主要以 tool name/tier 为单位，缺少对 command、cwd、patch files 和权限范围的结构化展示。

T-207 初版将 `run_tests` 从 `shell=True` 改为 `shell=False`，拒绝 shell control syntax 并保留真实 exit code，解决了测试证据伪成功问题。初版独立 review 随后复现：

- `/tmp/python3 -m unittest`、`/tmp/mvn test` 仅凭 basename 被接受；
- `LD_PRELOAD`、`NODE_OPTIONS`、显式 `PATH` 等注入环境被接受。

更重要的是，测试 runner 天生会执行仓库代码。因此 `run_tests=allow` 的真实语义只能是“允许执行测试/构建代码”，不能被描述成无副作用安全工具。

### OMP

`packages/agent/src/types.ts` 的 ToolApproval 支持静态 tier 或按 args 动态决定；`packages/coding-agent/src/extensibility/extensions/wrapper.ts` 在一个 wrapper 中执行 approval。OMP 的 bash 保留完整 shell 语义，没有一个名字不同但仍任意 shell 的隐蔽通道。

### Codex

`codex-rs/core/src/tools/approvals.rs` 的 ApprovalAction 显式携带 command argv、cwd、sandbox permissions、additional permissions、justification 或 patch/files。审批之外还有 execpolicy、命令安全解析、OS sandbox 和 network policy。

### 建议

T-207 R1 `3d792ec` 已按这一边界完成并发布 stable：拒绝 runner path（仅允许 canonical cwd 内 wrapper）、先从进程 PATH 固定 bare runner、拒绝直接加载型 env，并把工具说明与 metadata 明确标为 exec-tier、非 sandbox。独立门禁为 963/963 unittest、62/62 benchmark、13/13 architecture checks；immutable 黑盒证明合法 runner 成功、指定五类危险调用全部 `not_run` 且 marker 干净。没有新增 Runtime gate、LLM attempt 或业务关键词。残余是 metadata 尚未进入用户可见审计流，归后续 ExecutionPolicy observability，而不是继续扩 runner blacklist。

近期只做两件事：

1. 将 T-207 作为**命令完整性和验证证据修复**发布并持续观察，不继续增加庞大 runner 规则。
2. 将真正安全目标交给独立 `ExecutionPolicy/Sandbox` Owner。给其他人使用前，至少提供 read-only、workspace-write、full-exec 三种明确 profile，并在 approval 中展示完整 action。

## 4. Context、Compaction 与 Memory

### LCA 已有优势

- 本地 token 近似 + char fallback；
- 为下一轮 prompt/output 保留 reserve；
- `auto/local/llm` summary；
- recent messages、todo 和超大 tool output 处理；
- compaction summary 不提升为 system role；
- Markdown project/state memory；
- consolidation 默认 off；
- authored skill 只注入 metadata，正文按需读取。

这些取舍适合封闭 VM，而且比一开始引入 RAG/向量库更稳妥。

### 与 OMP/Codex 的差距

- OMP 有更成熟的 model/provider-aware compaction 和 Mnemopi/Hindsight backend。
- Codex 有 token-window rollover、local/remote compact、SQLite memory pipeline、memory citation、插件/skill service。
- LCA 启动时主要按固定 Markdown 文件和字符预算注入，不具备 relevance retrieval、path scope、过期/冲突治理和 memory citation。

### 建议

当前不引入向量数据库。先补：

1. path-scoped rules；
2. memory item provenance、last-verified 和冲突标记；
3. consolidation 候选人工/策略审核；
4. 只有 memory 规模和误召回成为真实问题后，再评估 SQLite/FTS 或 embedding。

## 5. Tools、编辑与验证

### 值得保留的 LCA 设计

- `read_file` 返回 hash tag、行号和截断 metadata；
- `apply_patch` 同时校验 file hash、line range 和 `old_text`；
- `write_file` 不覆盖已有文件；
- dry-run、patch log、hash 校验 rollback；
- `glob_files`、`search_code`、`list_files` 职责分离；
- Git diff attribution 和 verification timeline。

这是符合 OMP hashline/edit 思想、又适合 Python MVP 的裁剪，不需要替换成 OMP 代码。

### 主要差距

- LCA shell 不是 sandbox；
- test planner 仍容易受多模块/私有构建环境影响；
- LSP 已支持 rename 和 Code Action 只读 preview，但尚无 auto-apply/事务型多文件 writer；
- 无 AST edit；
- 无长进程统一 session/PTY owner；
- 无 MCP/browser/debug/task agent。

### 建议

先让一个真实业务切片完成 read -> patch -> test -> diff -> delivery。之后的工具优先级为：

1. unified exec session + sandbox/policy；
2. 真实 LSP Code Action 收益验证，再决定 rename/code-action apply；
3. task/subagent explore/reviewer；
4. MCP；
5. browser；
6. AST edit/DAP/job。

## 6. Event Protocol、Terminal 与 TUI

### LCA 源码事实

T-219 已让 `CommandDispatcher` 成为 prompt/status/workspace/approval 的单一消费入口，`AgentRuntime.run()` 只保留同路径兼容 facade；事件具备 `command_id`/`run_id` correlation，并用 TurnStarted/RunSummary/TurnFinished 表达每轮生命周期。T-220 又增加单一 Provider Stream Owner：OpenAI-compatible SSE/JSON 在同一次请求内解析，text/tool delta 分离，完整 tool call 才能进入 ToolRegistry；`AssistantDelta` 与最终消息共享 identity，terminal 可增量显示且不重复 final。

因此“Frontend 只发 Command、Runtime 只产 Event”已经达到同步 MVP；尚未完成的是 async queue、cancel/interrupt command、worker/runtime 隔离、event replay 恢复和多前端并发，不应宣称完全追平 Codex。

### OMP/Codex 对照

- OMP 的 `packages/agent/src/types.ts` 定义 agent/turn/message/tool 生命周期，`agent-loop.ts` 在 provider stream 消费点产出 `message_update`，coding session 订阅并转发 core events。
- Codex 的 `codex-rs/protocol/src/protocol.rs` 用 `Op` 和 `EventMsg` 作为客户端/Runtime 协议；`CodexThread.submit()` 与 `next_event()` 通过唯一 submission loop 形成双向 conduit，TUI、App Server 和其他客户端共享该核心边界。

### 建议

此前选择 `prompt_toolkit + rich` 是正确的，但完整 TUI 不应抢在协议生命周期之前。T-219/T-220 已完成同步 command dispatcher、command correlation、Turn lifecycle 和 tool-call-safe provider streaming。TUI 仍需补齐：

1. cancel/interrupt/approval interaction 通过显式 typed command 支持，而不是伪装已有；
2. Runtime 可在独立 worker 中运行，前端不直接调用内部方法；
3. event replay 和 session restore 语义稳定；
4. S6-S10 证明失败恢复、连续性和权限状态在真实多轮任务中可靠。T-221 已完成该项 hard safety 验收；S6B 的“测试失败后再恢复”因首改直接通过而保留为 INCONCLUSIVE。

满足后再实现 fullscreen TUI，不需要切 Go/Rust，也不需要复制 OMP renderer。

## 7. 对用户关键决策的独立评估

| 决策 | 判断 | 原因 |
|---|---|---|
| 用 OMP 做架构明灯，不直接照搬 | 正确 | OMP 的生命周期和 Owner 边界值得参考，但 LCA 的 Python/封闭 VM目标不同 |
| 拉取 Codex 做第二参考 | 正确 | Codex 在协议、sandbox、execpolicy、hooks 上补足了 OMP 参照的盲区 |
| Python + 标准库起步 | 正确 | 迭代快、离线部署简单，当前性能不是瓶颈 |
| dataclass event model，暂不用 Pydantic | 正确 | 进程内 MVP 足够；真正跨进程/版本化时再引入 schema/codegen |
| prompt_toolkit + rich 做第一版 terminal | 正确 | 适合当前 Runtime 和封闭 VM，且保留原生 scrollback |
| Markdown memory，不先做 RAG | 正确 | 规模小、可审计、误召回风险低；与 Claude Code/Codex 的显式上下文路线相符 |
| 用真实“拓展服务费结算”母需求压测 | 非常正确 | 它暴露了 Owner、私有依赖、权限和测试证据问题，远比合成 demo 有价值 |
| 先把只读证据流水线做得极稳 | 当时合理，现在过度 | 已从质量补偿变成产品主线，投入超过真实写交付 |
| 用 `agent.py` 行数作为架构健康核心指标 | 仅适合临时 ratchet | 能阻止回流，但不能证明职责正确；OMP/Codex 也有大型 lifecycle 文件 |
| `run_tests` 可单独 auto-allow | UX 有价值，但必须改定义 | 测试就是代码执行；它可避免 shell 拼接和伪 exit，不能替代 sandbox |
| 今天一路做到完整 TUI | 不建议 | 会改善外观，但不能解决当前最重要的真实需求交付失败 |
| 最终追平 OMP 全 platform | 不建议作为产品 KPI | 对个人项目成本过高，会让 MCP/browser/TUI/memory marketplace 挤压核心可靠性 |

## 8. 建议的目标架构

```text
CLI / Terminal / Future TUI / Remote
                |
          Command + Event Protocol
                |
       Core Runtime (保持通用)
  Turn Loop / Tool Router / Session / Context
  Approval + ExecutionPolicy / Compaction / Hooks
                |
        Workflow Profiles (可替换)
  coding | enterprise-evidence | readiness-audit
                |
       Tools / Skills / Agents / Extensions
```

核心约束：

1. Core Runtime 不解析“服务费、Owner、设计、文档一致性”等语义。
2. 安全/协议不变量可以 hard gate；业务语义质量默认 advisory 或 profile-scoped。
3. workflow 通过 hook/directive/role 接入，不在主循环增加 domain branch。
4. 每个额外模型调用、repair、rewrite 都必须有固定上限和 telemetry。
5. 新机制必须至少有两个独立跨场景失败样本，或者属于明确安全漏洞。

## 9. 下一步优先级

| 优先级 | 工作 | 验收 |
|---|---|---|
| 已完成 | T-215 Workflow Profiles Phase 1 并发布 stable | stable `20260716T062420Z-b3838a315d0b-9e078342994d`；普通 coding 与企业证据/readiness 重链分离，能力保留 |
| 已完成 | T-208~T-214 S5-1 验证与隔离参考交付 | LCA/Bailian 模型执行失败与业务实现分账；不再为单一样本追加 Harness gate |
| 已完成 | T-217 Typed ExecutionPolicy Observability Phase 1 | stable `20260716T070613Z-b38d28d87a1c-ce35b66fc179`；execute/preapproved 单一 policy Owner、脱敏 telemetry、真实 unsandboxed 状态，未冒充 OS sandbox |
| P0 | 冻结新的 read-only gate/taxonomy | 除安全漏洞或两个独立跨场景复现外，不扩 Harness |
| 已完成 | 将重型只读流水线变成 workflow profile | `auto/coding/enterprise-evidence/readiness-audit` typed lifecycle；缺 contract fail closed |
| 已完成 | T-218 移除 raw-language test-intent hard gate | regex candidate 未发布；R2 移除 parser/`requested_test_missing`，原 C1 自然 DELIVERED，真实 post-write 测试与事实型 reviewer 保持；stable `20260716T075910Z-e710a783c2d9-846af66e44ab` |
| P1 | ExecutionPolicy Phase 2 / Sandbox 设计 | 在 Phase 1 typed observability 上定义 read-only/workspace-write/full-exec；实现前明确 OS 隔离边界 |
| 已完成 | T-219 Runtime Command/Event Boundary Phase 1 | commit `a92af68`；同步 CommandDispatcher 成为 prompt/status/workspace/approval 单一入口，run 仅为兼容 facade；事件有 command correlation 和正确 Turn lifecycle，前端不直接修改 Runtime；1003/62/16 与 ordinary coding live 通过，stable `20260716T090437Z-a92af683944e-1e2171155546` |
| 已完成 | T-220 Provider streaming | commit `cee3ece`；OpenAI-compatible SSE/JSON 对 content/tool-call delta 安全聚合，partial/malformed/incomplete/timeout 为 0 execution；AssistantDelta/final identity、Bailian XML 抑制和 terminal final 通过 1031/62/17、immutable matrix 与真实 live；stable `20260716T095838Z-cee3eceba38d-14a0a50a6634` |
| 已完成 | T-221 S6-S10 真实回归 | stale patch、approval reject、session resume、always-ask/write/yolo+deny、需求变更和最终审计 hard safety 通过；无 P0/P1；S6B 失败后恢复未自然触发，记 INCONCLUSIVE |
| 已完成 | T-222 Explicit Read-only Explore Subagent Phase 1 | commits `ee01ce2` + `d838e45`；default-off、同步单 child、独立上下文/预算、同 roots、read-only whitelist、禁止递归、bounded typed yield 和父子 correlation 已落地。1056/62/18 与 deterministic matrix 通过；live 安全 fail-closed 和 parent direct verification 通过，成功 typed handoff live 仍 INCONCLUSIVE；stable `20260717T024300Z-d838e4527425-44487380458f` |
| 已完成 | T-223 Explore Subagent 真实收益验证 | 小 fixture 成功 handoff；真实三仓 enabled 时 delegate 从第 4 turn 起已暴露但模型未选择，21 LLM/49 tools/9 compactions 且覆盖不足；default-off 同样覆盖不足。无 Runtime/权限/证据 lifecycle 缺陷，不加 Queue gate，P14 扩展暂停 |
| 已完成 | T-224 LSP Semantic Rename Preview Phase 1 | commit `dee9a09`；对照 OMP symbol-aware rename/WorkspaceEdit 全量校验，提供零写盘 bounded preview，parent direct read 后复用现有 `apply_patch`、test、diff。1078/62/19 与真实 jdtls 流程通过；stable `20260717T050233Z-dee9a09c5ce9-d94382225258` |
| 已完成 | T-225 Semantic Tool Cross-language Benefit Gate | Java/jdtls 真实跨文件 preview 与现有 patch/test/diff 闭环 PASS；TypeScript/Vue 因无 external server 记 `ENVIRONMENT_BLOCKED / INCONCLUSIVE`。不联网安装、不归因 Runtime、不增加 Queue gate |
| 已完成 | T-226 LSP Code Action Preview Phase 1 | commit `e70f20a`；只 list/preview text-only WorkspaceEdit，command/resource operation/server applyEdit/auto-apply 全部 fail closed。独立 OMP 对照发现并修正缺失 `codeActionLiteralSupport`；1095/62/20，stable `20260717T054823Z-e70f20a948d4-d8ee2a1faf0d` |
| 已完成 | T-227 真实 jdtls Code Action Benefit Gate | 缺 List import 的 Java fixture 完成 list -> preview -> parent reread -> existing patch -> Maven exit 0 -> diff；同时发现 jdtls 默认生成 Eclipse metadata，未误归因为 WorkspaceEdit/command |
| 已完成 | T-228 JDTLS Read-tier Metadata Containment | commit `24d6daa`；LSP process/config Owner 通过 child-only property=false 收住 `.classpath/.project/.settings`，完整 server config 参与 client cache identity。1100/62/21、14/14 matrix 与真实 jdtls micro 通过；不声称 OS sandbox、不清理构建缓存、不削减 LSP。stable `20260717T081918Z-24d6daa4f827-c67e5ebe043c` |
| 已完成 | T-229 Ordinary Coding Cross-language Stable Batch | T-228 stable 在 Python、Java/Maven、Node 同 session 需求变更三类 clean fixture 上 3/3 完成自然 read/patch/test/diff；独立测试与 lifecycle 通过。provider schema/todo 噪声安全失败或恢复，不据孤立样本新增 Queue/gate。`7/7 unverified` 保留为业务 contract 与机器 delivery checks 的有意分账 |
| 已完成 | T-230 Dirty Worktree / State Benefit Gate | unrelated tracked/untracked WIP 保留并与本轮 patch 分账；same-file mixed attribution 正确；外部 commit 使旧 tag fail closed，重读后只修改新 baseline。Case A 模型 test command 失败被诚实标记未交付；无通用 State 缺陷，不搬完整 Codex worktree manager |
| 已完成 | T-231/T-232 Synchronous Interrupt Recovery + Typed Session Continuity | approval/长测试中断、child 回收和单一 Turn lifecycle 通过；随后修复已写入任务在 fresh `unclear` contract 下丢失 test/diff/review obligation。Session Owner 只继承经 HEAD/path/patch-record/hash 验证的 typed state，不复用旧结果、不解析“继续”关键词；repeated stop 保留 carried A+B。1109/62/22 与独立 immutable matrix 通过，stable `20260717T111651Z-b30240a91398-5bf520393764` |
| P2 | reviewer/implement subagent 扩展 | 仅在后续多个真实任务证明 Explore 有稳定收益后重启；写入隔离、冲突处理和审批路由必须先明确 |
| P2 | LSP rename apply/code action、MCP、Browser | rename apply 需先有事务型多文件 writer；其余每项由真实任务收益驱动 |
| P3 | fullscreen TUI / Remote | 协议和 streaming 稳定后实现 |

## 最终原则

OMP 是路线参照，Codex 是另一组成熟答案，LCA 不应成为它们的缩小复制品。LCA 最有价值的产品形态是：

> 一个可在本地或封闭 VM 中运行、能使用企业私有依赖、对真实代码和交付证据负责，同时不过度替模型做语义推理的个人 coding agent。

短期成功标准不是新增多少功能或通过多少合成 benchmark，而是同一套通用 Runtime 能否连续完成多个真实需求切片，并在失败时给出可信、可恢复、可审计的结果。
