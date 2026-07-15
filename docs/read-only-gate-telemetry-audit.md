# Read-only Gate Telemetry Audit

更新时间：2026-07-15

本审计只复盘现有 gate 的独立安全价值和运行代价，不新增、删除或调整 Runtime gate。当前 stable 为 T-203 `20260715T074350Z-7ccc7ad323dc-7bf1bbf4c507`。T-202 完成控制流合并后，三次 S4 证明安全边界有效、用户可用性仍未达标；T-203 通过 typed BLOCKED terminal assembly 将同一失败转为可行动交付，950/61/13 独立离线门禁与三次 fresh S4 均通过。

## 事实样本

| 样本 | Session / Run | 结果 |
|---|---|---|
| T-196 stable S2 | `20260714T231342759912Z` / `64081ebafc604049a4155cab50739e70` | 8 tools、1 次有界 candidate-read error、0 schema/protocol；reviewer revise、8 findings、2 rewrites、closure accepted。 |
| T-196 stable S3 | `20260714T231517222450Z` / `828d162bd79b42c6a539ea472a738559` | 5 tools、0 errors/schema/protocol；reviewer pass、typed submit 1、transport projection pruned 2。 |
| T-196 stable S4 | `20260715T055133273716Z` / `a177ef13f39d4780aa504d91ce3acb21` | 小牙原始命令：60.2s、10 LLM、14 tools、3 errors、2 compactions；pre-review 三轮耗尽、reviewer 0 次，最终 safe partial，用户可用性 FAIL。 |
| T-199 S4 | `20260715T030054927597Z` / `c6092ebba93d4f11856e20586e275090` | required materials 与双 code-root bounded outcome 完成；reviewer 接受 8 findings，随后 readiness/top-level schema correction 耗尽。 |
| T-200 S4 pre-review | `20260715T031706719408Z` / `147faa55b8cd463fbb25fe02ccef5da3` | 13 LLM、15 tools、3 errors、1 schema；pre-review 三轮后安全终止，reviewer 未启动。 |
| T-200 S4 transport | `20260715T031950855735Z` / `3c611eca649844ee8d220d30154ae2dd` | 10 LLM、12 tools、1 error、0 schema/protocol；transport rewrite 1 次后仍 incomplete，安全终止，reviewer 未启动。 |
| T-202 S4 run 1 | `20260715T070004098813Z` / `defce741167040ada14a51fc60e12308` | 152.8s、13 tools、2 errors、2 compactions、0 protocol；reviewer output lifecycle 因 claim role/readiness shape 耗尽，safe partial。材料与双 code root 均调查，hard PASS、usability FAIL。 |
| T-202 S4 run 2 | `20260715T070307135181Z` / `dc70aaf4c398495ca91e93c898fb11ac` | 78.5s、12 tools、0 errors、3 compactions、0 schema/protocol；共享一次 preparation 后仍有 requirement evidence/source-grounded numeric finding，safe partial。hard PASS、usability FAIL。 |
| T-202 S4 run 3 | `20260715T070454590806Z` / `08af527a92324fa78d87ee001b422f48` | 89.0s、9 tools、0 errors、3 compactions、0 schema/protocol；共享一次 preparation 后仍有 requirement evidence/source-grounded numeric finding，safe partial。hard PASS、usability FAIL。 |
| T-203 S4 run 1 | `20260715T073532860547Z` / `0818d15e1c11492d8bfcb37ea7505231` | 129.4s、17 tools、2 errors、3 compactions、3 schema/0 protocol；termination 保持 `read_only_reviewer_unverified`，最终为完整 typed BLOCKED。首轮 HTML 整体读取失败后分段读取成功，报告保留了该历史 error；作为离群可用性瑕疵记录，不影响另外两次 clean usable。 |
| T-203 S4 run 2 | `20260715T073812698491Z` / `b06bedfe5c1f4c23a795a1af0f940dcf` | 66.1s、10 tools、0 errors、3 compactions、0 schema/protocol；三份材料与双 code root direct read，完整 typed BLOCKED，无 rejected candidate 泄漏。hard PASS、usability PASS。 |
| T-203 S4 run 3 | `20260715T073935987033Z` / `fdcf2c20c45c4c449299792737f1a8da` | 99.0s、9 tools、0 errors、2 compactions、0 schema/protocol；三份材料与双 code root direct read，完整 typed BLOCKED，无 rejected candidate 泄漏。hard PASS、usability PASS。 |

## Gate 结论

| Gate | 独立安全价值 | 代价 / 失败贡献 | 建议 |
|---|---|---|---|
| material gate | 将主文档、显式 HTML 和 image 分别记为 observed/unavailable，避免“读一份即覆盖全部材料”。T-199 样本证明该边界有效。 | 增加前置 tool-choice turn；材料路径/模态绑定错误会推迟代码探索。 | **keep**：授权、路径和 modality 仍应由 typed owner 负责。 |
| read_only_explore | 要求每个 code root 有 direct source read 或 root-local unlocated，避免把需求目录或弱命中当 owner 证据。T-196 S2 正常释放依赖该边界。 | 多 root fallback/candidate-read 会增加工具轮次；provider 错参仍可能带来一次有界 error。 | **keep**：保持 root-fair 和 hard budget，不并入终态文案审计。 |
| pre_review audit | 在 isolated reviewer 前聚合 deterministic evidence/role/provenance finding，能阻止明显未闭合候选进入发布。T-200 `147faa...` 未泄漏草稿。 | 旧路径最多 2 次准备改写，可能在 reviewer 前消耗延迟。 | **merged in T-202**：保留 typed audit，与 claim transport 共用一次 candidate preparation；不新增 gate。 |
| claim transport | claim-scoped locator、完整窗口与 omission fail-closed 防止 reviewer 在缺证据时误判 unsupported。T-196 S3 pruned 2 后仍完整；T-200 `3c611...` 安全阻断。 | 长候选在共享一次 preparation 后仍可能失败，造成可用性损失。 | **merged in T-202**：保留 omission hard gate，共享一次 preparation；不得提高 cap 掩盖。 |
| isolated reviewer | 独立语义审查真实 path/provenance、repo-wide absence、proposal/事实边界。T-196 S2 的 8 findings 与 S3 pass 都有独立价值。 | provider schema/模型 finding 噪声会增加 turn；不能做无限 certification loop。 | **keep**：一次 semantic review + deterministic closure，继续 prefer silence/pass。 |
| schema/output lifecycle | 保持 assistant tool-call / tool-result pairing，对 malformed final 给脱敏、typed correction，不自动补造 verdict。T-199 样本暴露其必要性。 | provider 仍可能耗尽既有 correction budget；T-202 run 1 即为此类失败。 | **merged in T-202**：shape contract/rejection hint 由纯 `reviewer_correction_contract` Owner 统一，预算单一；validator 严格度不降。 |
| rewrite closure | 检查初审 accepted findings 是否全部改变、transport/document consistency 是否仍合法；不启动 fresh moving-goalpost reviewer。T-196 S2 closure accepted。 | 最多一次 correction 会增加 finalization turn；仅靠文本变化不能承担新语义审查。 | **keep**：维持 deterministic closure，并由既有 negative/provenance owner拦同义过宽事实。 |
| safe partial | provider/deadline/transport/reviewer 失败时只交付 typed observations 与限制，不泄漏被拒候选。T-202 三次原始候选确含虚构 API/字段/表和过宽 absence，最终均未泄漏。 | 三次报告都是 evidence dump，无法直接决定是否进入实现，也没有完整 typed blocked contract，usability 0/3。 | **productized in T-203**：不新增/放宽 gate；readiness 失败从可信 typed state 装配完整 `BLOCKED` 交付，RunSummary 保留真实 reason。独立 review 同时锁定 typed source role 优先于 `.html/.md` 后缀启发式。 |
| exact tool-choice | 同一 directive 的 detour 被 suppress/paired，并以有界 force 或 root-local degradation 收束，防止 host 偷执行模型意图。 | provider malformed args 会产生错误与额外 turn；高风险/写任务不能共享只读降级语义。 | **keep**：read-only readiness 可 root-local degrade；写任务仍 fail closed。 |

## OMP 对照边界

- OMP `tool-choice-queue.ts` 管 directive 的 in-flight、resolve、reject 与显式 requeue；`agent-loop.ts` 管 soft requirement 的 bounded detour 和 tool-result pairing。
- OMP `prompts/agents/reviewer.md` 要求 finding 可证明、可行动且由 patch 引入；`task/yield-assembly.ts` 将 incremental sections 与 terminal payload 分开组装。
- LCA 的 material/evidence taxonomy、claim matrix、locator transport、implementation readiness、schema repair 和 safe partial 是本地只读安全增强。T-201 只按职责拆 Owner；T-202 合并重复生命周期；T-203 仅借鉴 typed terminal assembly，不声称 OMP 有这些同名机制。

## 后续顺序

T-202 已完成审计建议中的两项合并，三次 S4 hard invariant 3/3、usability 0/3 证明继续堆 gate 或 rewrite 没有产品价值。T-203 typed blocked terminal assembly 通过 950/61/13 独立离线门禁；同一母需求三次 fresh S4 为 hard invariant 3/3、typed BLOCKED usability 3/3，其中两次为零 tool/schema/protocol error 的 clean usable。T-203 已发布 stable，下一步进入 S5 写路径真实交付；run 1 的 schema/error 与历史失败展示只记残余 telemetry，不启动新一轮局部补丁。
