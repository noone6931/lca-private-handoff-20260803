# OMP 核心架构笔记

更新时间：2026-07-07

本文记录当前项目从 oh-my-pi / OMP 源码中确认过、后续可直接复用的架构判断。它不是 OMP 全量源码分析，而是我们实现个人本地编程助手时需要反复引用的核心设计基线。

## 关键结论

OMP 的 Agent 主任务不靠 `max_steps` 终止。它把“任务是否继续”交给模型是否继续发 `tool_calls`，把“最多跑多久”交给 deadline / abort，把“上下文是否还能装下”交给 compaction。

对我们项目的直接含义：

- `max_steps` 不能作为日常任务预算。
- 默认不应该用一个小整数卡住真实任务。
- 日常控制应优先使用 `budget_seconds`。
- 长任务真正需要的是 context summary / compaction。
- 只在病态子循环上保留小上限，例如重复失败、暂停空转、强制工具重试。

## OMP 主循环

源码依据：`/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts`

OMP 使用双层循环：

```text
while true:
  while hasMoreToolCalls or pendingMessages:
    检查 deadline
    注入 pending messages
    同步上下文
    调用模型
    解析 tool calls
    执行工具
    回灌 tool results
    如果没有更多 tool calls，则结束当前 turn
```

主循环终止条件：

- 模型不再发 `tool_calls`；
- pending messages 已处理完；
- deadline 到期；
- 外部 abort；
- 异常路径被捕获并收尾。

这和简单的 `for step in range(max_steps)` 不同。步数在 OMP 里不是产品语义，而是观测数据。

## Step Counter 的角色

源码依据：

- `agent-loop.ts` 中 `stepCounter = { count: 0 }`
- `chatStepNumber = stepCounter.count; stepCounter.count += 1`

OMP 的 `stepCounter` 用途：

- telemetry；
- 事件流里的 step 统计；
- 日志和观测。

它不用于决定“第 N 步必须停”。这说明我们的 `max_steps` 应该是保险丝，而不是普通任务长度。

## Deadline / Abort

源码依据：

- `isDeadlineExceeded(deadline)`
- `config.deadline`
- `AbortSignal.any(...)`
- deadline timer

OMP 的时间控制逻辑：

```text
deadline = absolute timestamp
每轮循环检查 deadline
deadline 到期则 endAgentStream(...)
外部 abort 也会进入统一收尾路径
```

这里的 `deadline` 是 wall-clock deadline。也就是说，等待模型、执行工具、等待外部 abort/permission response 都在同一个绝对时间窗口内。OMP 不把 deadline 设计成“只统计模型和工具实际运行耗时”的 active budget。

OMP 的实现差异在于：deadline 到期会触发 `AbortController`，并把 abort signal 传入后续 LLM / tool / permission gate。ACP client 权限门里的 `requestPermission` 会和 abort signal `Promise.race(...)`，因此用户长时间不点权限时，请求可以被 deadline 取消，而不是等用户回来点完才发现过期。

我们项目对应设计：

- `--budget-seconds` 转换成运行时 deadline；
- 每次模型调用前检查；
- 不在一组 tool calls 中间随意截断，避免留下不配对的 tool result；
- 后续补 synthetic tool result 后，可以更精细地处理中断中的 tool calls。

当前实现 / 差异：

- 我们的终端 approval prompt 已使用 `select.select` 按剩余 deadline 等待 stdin；
- deadline 已过或等待超时时，会取消工具调用并返回 tool error，不执行工具；
- 用户中断仍走现有 `KeyboardInterrupt` synthetic tool result 路径；
- 与 OMP 差异：本地 Python MVP 还没有完整异步 abort signal 贯穿 permission gate，先用 wall-clock deadline cancel 覆盖主要问题。

## Context Compaction

源码依据：`/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/compaction/compaction.ts`

OMP 使用 compaction 处理长上下文，而不是靠减少 step 数。

关键原则：

- 估算上下文 token；
- 接近窗口阈值时触发压缩；
- 为下一轮 prompt 和模型输出保留 reserve；
- reserve 至少为 context window 的 15%；
- 历史消息压缩成 summary / compaction entries；
- 最近消息和关键文件操作需要保留。

我们项目当前状态：

- 已完成 deterministic compaction MVP；
- 当前使用 `context_char_budget` / `context_recent_messages` 控制触发与最近消息保留；
- 把早期 user / assistant / tool 输出折叠成 Markdown summary；
- 保留文件修改、测试结果、用户明确决策；
- summary 写入 session，并作为后续上下文注入。

下一步按 OMP 风格增强：

- 增加 token 估算，把字符阈值保留为兜底；
- 按上下文 token 预算触发 compaction，而不是只按字符数；
- 为下一轮 prompt 和模型输出预留 reserve；
- recent messages 继续保留原文，早期历史压成 summary / compaction entries；
- 优先支持 LLM summary，失败时回退 deterministic summary。

## Tool Call 配对

OMP 对 abort、error、skipped、length truncation 等情况会补合成 tool result，避免 assistant 有 `tool_calls` 但后面没有对应 tool 消息。

我们已经做过：

- session 恢复时丢弃尾部未配对 tool_calls；
- 运行中中断、超时、工具参数截断时生成 synthetic tool result；
- 把未执行原因明确告诉模型，避免恢复后 API 拒绝。

## Tool Approval / Permission Gate

源码依据：

- `docs/approval-mode.md`
- `packages/coding-agent/src/session/agent-session.ts` 中 ACP Permission Gate

OMP 的普通工具审批是两层结构：

```text
tool.approval(args) -> read / write / exec / { tier, reason, override }
tools.approvalMode -> always-ask / write / yolo
tools.approval.<toolName> -> allow / prompt / deny
```

模式语义：

- `always-ask`：自动允许 `read`，`write` / `exec` 询问；
- `write`：自动允许 `read` / `write`，`exec` 询问；
- `yolo`：默认自动允许全部 tier；
- `tools.approval.<toolName>` 在各模式下都可覆盖单个工具；
- 没有声明 approval 的工具按 `exec` 处理，这是安全默认值。

审批解析顺序：

1. 工具先给出自身 tier 或动态审批决策；
2. 读取用户 per-tool policy；
3. `yolo` 模式下，没有 per-tool policy 就直接允许；
4. 非 `yolo` 模式下，如果工具带 `override: true`，会强制 prompt，`deny` 仍阻断；
5. 否则 per-tool policy 优先生效；
6. 最后按 active `approvalMode` 和工具 tier 决定自动允许还是询问。

OMP 另有一条 ACP client 权限门：

- 只在 ACP client 连接并暴露 `requestPermission` 能力时启用；
- 默认 gate 的工具是 `bash`、`edit`、`delete`、`move`；
- `requestPermission` 会接收 abort signal，并与 abort promise 竞争；
- 提示选项包括 `Allow once`、`Always allow`、`Reject`、`Always reject`；
- `Always allow` / `Always reject` 写入当前 `AgentSession` 的内存 Map：`#acpPermissionDecisions`；
- 这个 Map 是 session 内存态，不是全局配置；
- 再次遇到相同 permission intent 时先查 Map，命中 `allow_always` 直接执行，命中 `reject_always` 直接拒绝。

对我们项目的设计含义：

- 现有 `approval_mode=ask/auto-read/yolo` 是简化版 `approvalMode`，但命名和 OMP 不完全一致；
- 已补 `tool_approval` 配置，支持每个工具 `allow` / `prompt` / `deny`；
- 保留旧的 `--auto-approve-tools`，并兼容映射成 `tool_approval.<tool>=allow`；
- 交互式终端可以补 `once / session / no`，其中 `session` 写入运行时内存态，不落全局配置；
- 本地版把 config `prompt` / `deny` 视为硬护栏，session allow 不绕过 config prompt，避免“强制询问”被误关；
- 终端 approval prompt 已支持 deadline timeout / cancel，避免同步 `input()` 长时间占满 `budget_seconds`；
- 危险 shell 规则可以比 OMP 更保守：我们本地版可继续硬拒绝明显危险命令，避免 `yolo` 绕过。

## 病态循环上限

OMP 有一些小上限，但它们不是主任务步数限制：

- paused turn continuation 上限；
- soft tool escalation 上限；
- Harmony 泄漏重试上限；
- compaction 请求超时 / 重试上限。

设计含义：

- 主任务不应该被小步数卡住；
- 但重复失败、重复空转、重复强制工具可以有明确小上限；
- 这些上限应该命名成具体风险，而不是笼统叫 `max_steps`。

## 我们项目的落地决策

当前决策：

- 默认 `max_steps=0`，表示不限步；
- 默认 `budget_seconds=600`，作为日常运行预算；
- `--budget-seconds 0` 可以关闭时间预算；
- `--max-steps N` 只作为显式保险丝；
- 一键启动优先靠当前目录、`.env` 和 provider 默认值；
- P4 优先做 context summary / compaction。

推荐日用命令：

```bash
./agent "阅读当前项目并总结下一步"
```

如果是长任务：

```bash
./agent --budget-seconds 1200 "按 docs/requirements/feature.md 完成需求并跑测试"
```

如果明确想设保险丝：

```bash
./agent --max-steps 200 "执行一个可能很多轮的任务"
```

## 后续禁止反复争论的点

- 不再把 `max_steps=100` 当作默认方案。
- 不照搬 OMP 的完整复杂实现。
- OMP 的可长跑来自 deadline + compaction + abort + synthetic tool result，不是来自把步数调大。
- 第一阶段先做本地单 Agent，LSP / DAP / subagents / Browser 继续后置。
