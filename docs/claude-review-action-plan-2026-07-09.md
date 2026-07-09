# Claude Review Action Plan - 2026-07-09

本文记录对外部 Claude review 的采纳结论。评审标准是 OMP 的架构原则，但 LCA 的目标仍是个人本地、单 Agent、封闭 VM 友好和默认无重依赖。

## 结论

Claude review 的方向基本成立：`agent.py` 已经偏大，context budget 仍是字符近似，Java/Vue LSP 只是轻量静态扫描。run summary/coverage 已完成 MVP 版，但还未拆成独立 run collector 模块。

但执行顺序不采用“先 P0 大拆分”。当前首要目标是让 LCA 今天能稳定用于真实需求分析和小改实现；因此先做低风险、可验证、直接改善日用体验的改动，再在真实压测暴露稳定失败形态后拆模块。

## 已立即采纳

| 项目 | 状态 | LCA 处理 |
|---|---|---|
| Terminal Frontend 继续保持 append-only | 已采纳 | 不做 fullscreen，不用 Rich Live 主渲染，不引入 Textual/Bubble Tea/Ratatui。 |
| TUI 命令可发现性不足 | 已采纳 | 新增 `/help`、`/status`、`/tools`，启动横幅提示 `/help`。 |
| Authored skill 只靠 metadata 不够 | 已采纳 | 点名已发现 skill 时，runtime 会软性要求先读取对应 `SKILL.md`。 |
| 自定义 memory 需要参与项目边界分析 | 已采纳 | `memory_read` 支持安全命名的自定义 memory 文件；写入仍限制在内置桶。 |
| 纯分析任务不应走实现 hygiene | 已采纳 | analysis-only 不加 coding workflow nudge、不触发 no-edit hygiene；纯只读分析默认跳过 todo。 |
| run summary / coverage 需要结构化 | 已采纳 MVP 版 | 每轮结束写 `run_summary` session 事件和 `RunSummary` typed event；`/status` 展示最近一轮摘要。 |

## 接受但后置

| Claude 建议 | 判断 | 后置原因 | 触发条件 |
|---|---|---|---|
| 拆解 `agent.py` | 接受 | 大拆分风险高，且当前真实使用链路仍在校准 | 连续压测稳定后，按 compaction/evidence/startup_context/memory_consolidation/steering 分批抽出。 |
| Steerer 协议 | 接受 | 当前已有多个 guard/steering，值得统一，但要先保留行为测试 | 新增 run summary 后能看清哪些 steerer 最常触发，再抽接口。 |
| token + reserve budget | 接受 | 当前 char budget 可用，但长任务需要更准 | 真实长任务出现预算误判、上下文超限或过早压缩时优先做。 |
| Java/Vue LSP provider 拆分 | 接受 | 企业项目会用到，但不应默认引入重依赖 | 先增强 best-effort 输出和置信度；可选 parser extra 后置。 |
| 独立 run collector 模块 | 接受 | MVP 统计已落在 runtime 内，先服务压测；独立模块化可等数据形态稳定 | 当 `agent.py` 拆分开始时，把 `RunStats` / summary 逻辑平移到 `run_collector.py`。 |

## 明确非目标

| 项目 | 原因 |
|---|---|
| DAP/debugger | 不进入当前目标；企业需求实现前期不需要。 |
| browser | 封闭 VM/本地 coding agent 第一阶段不需要。 |
| 多 Agent/subagents | 当前单 Agent 已足够验证主链路，避免并发复杂度。 |
| fullscreen TUI | 与当前 terminal-native 设计冲突，真实需要出现前不做。 |
| 默认重解析器/LSP server 依赖 | 破坏封闭 VM 和 dependencies 轻量目标；只能作为 optional extra。 |

## 推荐顺序

1. 继续真实需求链路压测：边界圈定 -> 用户确认 -> 源码只读验证 -> 小改实现。
2. 结合 run summary 数据，拆出第一批小模块：`startup_context.py`、`evidence.py`、`compaction.py`。
3. 增强 Java/Vue LSP：先拆 provider 文件和置信度输出，再评估 optional parser extra。
4. 最后才做统一 Steerer 协议和更完整 ToolChoiceQueue。

## 当前判断

LCA 与 OMP 的架构原则继续靠拢，但不把 OMP 的完整产品形态搬进来。近期重点不是“照着 OMP 补全所有子系统”，而是把 OMP 的成熟控制思想小步落进 LCA：runtime/event 分层、soft requirement、evidence ledger、permission、compaction、可观测 summary 和可回滚编辑闭环。

## 2026-07-09 复核补充

第二轮 review 再次指出 `agent.py` 行数继续上升、Java/Vue LSP 仍是轻量正则、token budget 仍是字符近似。这个警告有效：后续新增 steering/guard 时应优先考虑抽到独立模块，避免继续把所有控制逻辑塞进 `agent.py`。

但执行顺序仍不调整为“立刻 P0 大拆分”。最新真实压测暴露的是 evidence-first、语义级探索扩散和最终回答结构/证据卫生，这些是今天能否用起来的直接问题。当前策略是：先用小而可测的 runtime gate 关闭 P9 实战缺口；一旦 T-087/T-088/T-089 收束，再开始第一批低风险模块化（优先 `evidence.py`、`compaction.py`、`run_collector.py`），并把 Java/Vue LSP provider 拆分列入下一阶段。
