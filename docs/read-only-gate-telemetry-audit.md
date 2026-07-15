# Read-only Gate Telemetry Audit

更新时间：2026-07-15

本审计只复盘现有 gate 的独立安全价值和运行代价，不新增、删除或调整 Runtime gate。当前 stable 仍是 T-196 `20260714T232335Z-1a9d3bf23544-d5388ba3386a`；T-199/T-200 均为未发布 candidate。

## 事实样本

| 样本 | Session / Run | 结果 |
|---|---|---|
| T-196 stable S2 | `20260714T231342759912Z` / `64081ebafc604049a4155cab50739e70` | 8 tools、1 次有界 candidate-read error、0 schema/protocol；reviewer revise、8 findings、2 rewrites、closure accepted。 |
| T-196 stable S3 | `20260714T231517222450Z` / `828d162bd79b42c6a539ea472a738559` | 5 tools、0 errors/schema/protocol；reviewer pass、typed submit 1、transport projection pruned 2。 |
| T-196 stable S4 | `20260715T055133273716Z` / `a177ef13f39d4780aa504d91ce3acb21` | 小牙原始命令：60.2s、10 LLM、14 tools、3 errors、2 compactions；pre-review 三轮耗尽、reviewer 0 次，最终 safe partial，用户可用性 FAIL。 |
| T-199 S4 | `20260715T030054927597Z` / `c6092ebba93d4f11856e20586e275090` | required materials 与双 code-root bounded outcome 完成；reviewer 接受 8 findings，随后 readiness/top-level schema correction 耗尽。 |
| T-200 S4 pre-review | `20260715T031706719408Z` / `147faa55b8cd463fbb25fe02ccef5da3` | 13 LLM、15 tools、3 errors、1 schema；pre-review 三轮后安全终止，reviewer 未启动。 |
| T-200 S4 transport | `20260715T031950855735Z` / `3c611eca649844ee8d220d30154ae2dd` | 10 LLM、12 tools、1 error、0 schema/protocol；transport rewrite 1 次后仍 incomplete，安全终止，reviewer 未启动。 |

## Gate 结论

| Gate | 独立安全价值 | 代价 / 失败贡献 | 建议 |
|---|---|---|---|
| material gate | 将主文档、显式 HTML 和 image 分别记为 observed/unavailable，避免“读一份即覆盖全部材料”。T-199 样本证明该边界有效。 | 增加前置 tool-choice turn；材料路径/模态绑定错误会推迟代码探索。 | **keep**：授权、路径和 modality 仍应由 typed owner 负责。 |
| read_only_explore | 要求每个 code root 有 direct source read 或 root-local unlocated，避免把需求目录或弱命中当 owner 证据。T-196 S2 正常释放依赖该边界。 | 多 root fallback/candidate-read 会增加工具轮次；provider 错参仍可能带来一次有界 error。 | **keep**：保持 root-fair 和 hard budget，不并入终态文案审计。 |
| pre_review audit | 在 isolated reviewer 前聚合 deterministic evidence/role/provenance finding，能阻止明显未闭合候选进入发布。T-200 `147faa...` 未泄漏草稿。 | 三轮改写可消耗明显延迟，且 reviewer 0 次意味着语义关没有纠偏机会。 | **merge-candidate**：后续优先合并重复 deterministic category/continuation，不新增第四类 gate。 |
| claim transport | claim-scoped locator、完整窗口与 omission fail-closed 防止 reviewer 在缺证据时误判 unsupported。T-196 S3 pruned 2 后仍完整；T-200 `3c611...` 安全阻断。 | 长候选可能在 reviewer 前一次 rewrite 后仍失败，造成可用性损失。 | **merge-candidate**：保留 omission hard gate，评估与 pre-review 的单次压缩合并；不得提高 cap 掩盖。 |
| isolated reviewer | 独立语义审查真实 path/provenance、repo-wide absence、proposal/事实边界。T-196 S2 的 8 findings 与 S3 pass 都有独立价值。 | provider schema/模型 finding 噪声会增加 turn；不能做无限 certification loop。 | **keep**：一次 semantic review + deterministic closure，继续 prefer silence/pass。 |
| schema/output lifecycle | 保持 assistant tool-call / tool-result pairing，对 malformed final 给脱敏、typed correction，不自动补造 verdict。T-199 样本暴露其必要性。 | `c6092...` 中 5 次 schema correction 耗尽，是直接失败来源。 | **merge-candidate**：统一 shape contract 与 rejection hint owner，减少同义 repair 分支；validator 严格度不降。 |
| rewrite closure | 检查初审 accepted findings 是否全部改变、transport/document consistency 是否仍合法；不启动 fresh moving-goalpost reviewer。T-196 S2 closure accepted。 | 最多一次 correction 会增加 finalization turn；仅靠文本变化不能承担新语义审查。 | **keep**：维持 deterministic closure，并由既有 negative/provenance owner拦同义过宽事实。 |
| safe partial | provider/deadline/transport/reviewer失败时只交付 typed observations 与限制，不泄漏被拒候选。T-200 两个 fresh run 均证明安全价值。 | 用户拿不到完整设计，不能把安全终止记作可用性 PASS。 | **keep**：作为最后防线；RunSummary 必须保留真实失败 reason。 |
| exact tool-choice | 同一 directive 的 detour 被 suppress/paired，并以有界 force 或 root-local degradation 收束，防止 host 偷执行模型意图。 | provider malformed args 会产生错误与额外 turn；高风险/写任务不能共享只读降级语义。 | **keep**：read-only readiness 可 root-local degrade；写任务仍 fail closed。 |

## OMP 对照边界

- OMP `tool-choice-queue.ts` 管 directive 的 in-flight、resolve、reject 与显式 requeue；`agent-loop.ts` 管 soft requirement 的 bounded detour 和 tool-result pairing。
- OMP `prompts/agents/reviewer.md` 要求 finding 可证明、可行动且由 patch 引入；`task/yield-assembly.ts` 将 incremental sections 与 terminal payload 分开组装。
- LCA 的 material/evidence taxonomy、claim matrix、locator transport、implementation readiness、schema repair 和 safe partial 是本地只读安全增强。T-201 只按上述职责拆 owner，不声称 OMP 有这些同名机制，也不改变 gate 顺序或预算。

## 后续顺序

本表是 T-201 初始审计，不是永久保留清单。下一批优先验证 `pre_review audit + claim transport` 是否可共享一次候选压缩，以及 reviewer schema contract 是否可减少重复 repair；若某 gate 的安全价值已被其他 Owner 完整覆盖，应进入 `delete-candidate`，不能仅因已有测试就永久保留。任何合并或删除都必须保持 omission、document consistency、write-task exact failure 和 safe-partial 合同，并按 `harness-engineering-governance.md` 做统计式 S4 A/B。
