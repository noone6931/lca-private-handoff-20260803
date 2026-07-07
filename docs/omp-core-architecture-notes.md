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

我们项目对应设计：

- `--budget-seconds` 转换成运行时 deadline；
- 每次模型调用前检查；
- 不在一组 tool calls 中间随意截断，避免留下不配对的 tool result；
- 后续补 synthetic tool result 后，可以更精细地处理中断中的 tool calls。

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

我们项目 P4 的最小设计：

- 先不做复杂 token 精算；
- 用字符数或粗略 token 估算作为触发阈值；
- 把早期 user / assistant / tool 输出折叠成 Markdown summary；
- 保留最近若干轮原文；
- 保留文件修改、测试结果、用户明确决策；
- summary 写入 session，并作为后续上下文注入。

## Tool Call 配对

OMP 对 abort、error、skipped、length truncation 等情况会补合成 tool result，避免 assistant 有 `tool_calls` 但后面没有对应 tool 消息。

我们已经做过：

- session 恢复时丢弃尾部未配对 tool_calls；

我们还没做：

- 运行中中断、超时、工具参数截断时生成 synthetic tool result；
- 把未执行原因明确告诉模型，避免恢复后 API 拒绝。

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
