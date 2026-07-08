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

## OMP 如何让用户不用指定工具顺序

结论：OMP 不是靠用户每次说“先读文件、再搜索、再改、再测试”。它把默认工作流拆成三层：系统上下文告诉模型应该如何工作，工具 schema / description 告诉模型每个工具该何时使用，runtime 在关键位置用 tool choice、todo reminder、permission、synthetic result 等机制纠偏。

### 1. 系统提示内置工程工作流

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/system-prompt.md:87`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/system-prompt.md:105`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/system-prompt.md:123`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/system-prompt.md:160`

具体做法：

- `TOOL POLICY` 要求模型在能提升正确性、完整性、grounding 时使用工具，不允许停在第一个看似合理的答案。
- `Specialized Tools` 明确要求文件/目录读取用 `read`，内容搜索用 `grep` 而不是 shell 里的 `grep/rg/awk`，glob 用 `glob`，精细修改用 `edit`，代码智能用 `lsp`。
- `Exploration` 要求先定位目标：用 `grep` 找目标、用 `glob` 看结构、用 `read` 的 offset/limit 读必要片段，不鼓励盲读整仓。
- `EXECUTION WORKFLOW` 固化为 `Scope -> Research Before Editing -> Decompose -> Implement -> Verify -> Cleanup`。其中明确要求多文件工作先研究既有代码和约定，编辑前读取相关内容，非平凡工作交付前必须有测试、E2E 或 QA 证据。

这意味着 OMP 用户可以只说“实现这个需求”，模型会从系统提示里得到默认流程，而不是要求用户在 prompt 里手写工具调用顺序。

### 2. 项目上下文自动注入

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/project-prompt.md:9`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/project-prompt.md:20`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/project-prompt.md:32`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/project-prompt.md:46`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:608`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:611`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:631`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:792`

具体做法：

- `project-prompt.md` 会把 context files 注入为必须遵守的上下文。
- 目录级规则如 `AGENTS.md` / `CLAUDE.md` / `.cursorrules` 由 prompt 构建阶段预加载；模型不需要自己搜索这些规则文件。
- 可选 `workspace-tree` 会把当前工作目录的概要结构注入系统上下文，模型不用第一步一定先 `ls` 才知道大概目录。
- `project-prompt.md` 还写明：每次响应都必须推进任务；当工具或 repo context 能回答时，不要问用户确认；行为变更交付前必须验证。
- `buildSystemPrompt()` 会并行加载系统 prompt 定制、项目 context files、workspace tree、skills、active repo context，再渲染 `system-prompt.md` 和 `project-prompt.md`。

这层解决的是“用户没说先读哪些项目规则时怎么办”：OMP 在 agent start 前先把项目规则、cwd、workspace tree、日期、环境等放进系统上下文。

### 3. 工具 registry 和工具描述进入模型上下文

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/index.ts:443`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/index.ts:489`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/sdk.ts:1666`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/sdk.ts:2217`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/sdk.ts:2561`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:701`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:708`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:730`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/system-prompt.ts:788`

具体做法：

- `BUILTIN_TOOLS` 注册 `read`、`bash`、`edit`、`ast_grep`、`ast_edit`、`ask`、`eval`、`glob`、`grep`、`lsp`、`browser`、`todo`、`write`、memory 等工具。
- `createTools()` 根据设置过滤工具，例如 `bash.enabled`、`grep.enabled`、`lsp.enabled`、`todo.enabled`、`browser.enabled`、`memory.backend`。
- SDK 启动时先 `createTools()`，再构建 `toolRegistry`，再调用 `buildSystemPrompt()`。
- `buildSystemPrompt()` 从 active tools 里取 name、wireName、label、description、parameters、examples，生成 `toolRefs` 和 `toolInventory`。
- 当 provider 使用 native tool calling 时，系统 prompt 可以只列工具名；当需要 inline tool descriptors 时，会把完整工具说明渲染进系统 prompt。两种情况下，模型都会知道有哪些工具以及怎么用。

这层解决的是“模型怎么知道 read/search/edit/test 这些能力存在”：不是用户告诉它，而是 runtime 把工具 catalog 和工具 schema 放进每轮模型上下文。

### 4. 工具描述承担具体使用规范

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/read.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/grep.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/glob.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/patch.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/write.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/bash.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/todo.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/tools/ask.md`

具体做法：

- `read.md` 定义文件、目录、文档、SQLite、archive、URL、internal URI 的读取方式，并说明代码文件无 selector 时返回结构摘要，footer 指向需要补读的范围。
- `grep.md` 明确要求内容搜索必须用内置 `grep`，不要通过 bash 调 `grep/rg/ag/awk`；open-ended 多轮搜索应该转 task/explore，而不是无限 chained grep。
- `glob.md` 定义目录和文件枚举，用 mtime 排序、分组输出，并支持 semicolon 多路径。
- `patch.md` 把 `edit` 定义为已有文件的 primary edit tool，要求编辑前必须读目标文件，anchor/context 必须原样复制，失败后必须重新读当前内容再生成新 patch，不能重复提交同一个失败 diff。
- `write.md` 明确写整文件或创建文件时可用，但修改既有文件优先用 `edit`。
- `bash.md` 把 shell 限定为真实二进制和短事实 pipeline，复杂控制流应转 `eval`，并说明长输出会进入 artifact。
- `todo.md` 定义什么时候建 todo：3+ 步任务、用户明确要求、多任务清单、新指令中途到达；并要求完成后立即标记。
- `ask.md` 要求默认行动，只在存在用户必须决定且权衡明显不同的方案时才问。

这层解决的是“模型知道工具名但不知道怎么用”的问题。OMP 把很多我们之前写在用户 prompt 里的要求，沉到了工具说明里。

### 5. Runtime 会主动插入 todo / task 提醒

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/eager-todo.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/prompts/system/mid-run-todo-nudge.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:11085`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:11137`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:7438`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:7461`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:11202`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:11327`

具体做法：

- `eager-todo.md` 是隐藏 system reminder：任务开始前建议或强制先建 phased todo，要求覆盖 investigation、implementation、verification。
- `#createEagerTodoPrelude()` 只在合适时机注入，例如首个用户消息、todo enabled、没有已有 todo、不是 plan mode、todo 工具 active。
- 当 `todo.eager=always` 且模型支持强制 tool choice 时，OMP 不只是提示，还会通过 tool choice 强制下一轮调用 `todo`。
- `mid-run-todo-nudge.md` 在长任务中提醒模型有未完成 todo；`#takeMidRunTodoNudge()` 会在模型已经做了若干 mutating tool result 但没更新 todo 时注入隐藏提醒。
- `#checkTodoCompletion()` 在 agent 停下但还有未完成 todo 时，会注入 reminder 并 schedule continuation，让模型继续完成或标记任务。

这层解决的是“用户没说维护 todo，模型也可能忘记”的问题。OMP 用隐藏 reminder 和必要时的 forced tool choice 把 todo 变成 runtime 能力。

### 6. ToolChoiceQueue 和软/硬工具要求纠偏

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/tool-choice-queue.ts:101`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/tool-choice-queue.ts:128`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/tool-choice-queue.ts:219`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:2891`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:2902`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:2937`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:811`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:976`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:989`

具体做法：

- `ToolChoiceQueue` 可以排入 hard tool choice，例如用户强制某个工具、eager todo 强制 todo。
- `nextToolChoiceDirective()` 优先取 hard forced choice；如果有 pending preview action，则返回 soft requirement。
- soft requirement 不是马上强制 provider tool_choice，而是先插入 reminder；如果模型没调用要求的工具，agent-loop 会把模型偏离调用标记为 skipped，不执行副作用工具，然后下一轮升级成 forced tool choice。
- `MAX_SOFT_TOOL_ESCALATIONS` 防止这个纠偏循环无界。

这层解决的是“模型没按流程调用必要工具怎么办”：OMP runtime 可以先提醒，再强制；偏离工具不会被执行，避免不该发生的副作用。

### 7. Agent-loop 执行并回灌工具结果

源码依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:776`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:806`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:850`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:945`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1021`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1033`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1037`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1178`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1184`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/agent/src/agent-loop.ts:1268`

具体做法：

- agent-loop 每轮模型调用前会 `syncContextBeforeModelCall`，确保系统 prompt、active tools、session 状态是最新的。
- 每轮把 AgentMessage 转成 provider 消息，并带上 normalized tools。
- 如果 provider 不支持 native tool calling，OMP 还支持 owned / in-band tool dialect，把工具 catalog 放进 prompt，并自己解析工具调用。
- 模型返回 tool calls 后，agent-loop 执行 `executeToolCalls()`，把 tool results 追加回 conversation，再继续下一轮。
- 如果输出 `length` 截断、deadline 到期、abort/error 等导致 tool call 不能执行，OMP 会补 synthetic tool result，保证后续 API 看到的 tool_call / tool_result 是配对的。

这层解决的是“工具调用链如何继续直到完成”：用户只给目标，模型和 runtime 多轮协作；模型没有 tool calls 且没有 pending messages 时才停。

## 对我们 LCA 的直接落地方案

我们不需要完整复制 OMP 的复杂度，但应该按同样分层落地：

1. 系统 prompt 固化默认工作流：
   - 理解任务：先用 `list_files` / `search_code` / `read_file` 获取足够上下文；
   - 修改前：必须读目标文件；
   - 修改时：已有文件用 `apply_patch`，写入前优先 `dry_run=true`；
   - 修改后：根据项目类型运行合适测试，并调用 `git_diff`；
   - 复杂任务：维护 todo；
   - 不确定且工具/源码不能回答时才 `ask_user`。
2. 工具 description 对齐真实能力：
   - `read_file` 说明支持行范围和 hash tag；
   - `search_code` 说明用于内容搜索，结果路径相对 workspace；
   - `apply_patch` 明确是修改已有文件主路径，要求 tag / old_text / mode / dry_run；
   - `write_file` 明确只创建新文件，避免再误导模型覆盖既有文件；
   - `shell` 明确不是文件搜索/读取/编辑的首选，并说明安全边界。
3. Runtime 层做最小纠偏：
   - 对复杂任务可注入一次 hidden todo reminder，但不强制所有任务都 todo；
   - 对 `apply_patch dry_run=true` 可以作为 prompt 规范，暂不做强制 gate；
   - 对工具失败后的重试规则写进 prompt：失败后必须重新读当前文件，不要重复同一 patch；
   - 继续保留 approval、deadline、compaction、synthetic tool result 这些硬边界。
4. 用户日用命令应简化：

```bash
./agent --provider bailian --cwd /path/to/project "根据需求实现，改完跑测试并总结 diff"
```

如果需求文档在另一个目录，下一步应做 multi-root workspace：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --allow-dir /path/to/requirements \
  "读取需求文档并在代码项目中实现"
```

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

## Memory / Skills / Autolearn

源码与文档依据：

- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/memory-backend/resolve.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/memory-backend/types.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/memory-backend/local-backend.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/memory-recall.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/memory-retain.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/learn.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/tools/manage-skill.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/autolearn/managed-skills.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/packages/coding-agent/src/extensibility/skills.ts`
- `/Users/chengming/mycode/opensource/oh-my-pi/docs/memory.md`
- `/Users/chengming/mycode/opensource/oh-my-pi/docs/skills.md`

OMP 的 memory 是 backend selector，而不是单一文件：

```text
memory.backend = off | local | hindsight | mnemopi
```

具体语义：

- `off` 是默认值；
- `local` 是本地 rollout-summary pipeline，会从历史 session 提炼长期项目知识，写出 `MEMORY.md`、`memory_summary.md` 和 `skills/`；
- `hindsight` 是远端长期记忆，提供 recall / retain / reflect；
- `mnemopi` 是本地 SQLite 记忆，提供 recall / retain / reflect / edit；
- memory backend 需要非阻塞：启动、搜索、保存、清理失败都不能破坏主 agent loop。

OMP 的 local memory 会在后续 session 注入 Memory Guidance：

- 注入内容来自 `memory_summary.md` 和 `learned.md`；
- memory 被标成 heuristic / advisory，不能覆盖当前 repo state 和用户最新指令；
- 注入有 token 上限；
- consolidation 输出会做 secret redaction；
- generated memory playbooks 可通过 `memory://root/skills/<name>/SKILL.md` 读取。

OMP 的 skills 也分层：

- authored skills：用户或项目手写的 `<skills-root>/<name>/SKILL.md`；
- managed skills：auto-learn 生成的 `~/.omp/agent/managed-skills/<name>/SKILL.md`；
- system prompt 只列 name / description，正文按需读取；
- managed skills 优先级最低，永远不能覆盖 authored skills；
- `manage_skill` 工具受 `autolearn.enabled` 控制；
- `learn` 工具在 `autolearn.enabled` 且 memory backend 为 `local` / `hindsight` / `mnemopi` 时可用，可以同时写 lesson 和 managed skill。

我们项目当前状态：

- 已有 Markdown memory：`.local-agent/memory/project.md`、`decisions.md`、`conventions.md`、`learned.md`；
- 这些 memory 会作为 advisory context 进入新 session system prompt；
- 已有 `learn` 工具，可写入 `.local-agent/memory/learned.md`；
- 已有 authored skills discovery：`.local-agent/skills/<name>/SKILL.md` 启动时只注入 name / description / source path，正文按需读取；
- 尚无 managed skills / autolearn；
- 已新增详细方案：`docs/memory-skills-implementation-plan.md`。

后续落地顺序：

1. 用真实项目压测 memory / learn / authored skills 的组合体验；
2. 再评估 managed skills，默认关闭，generated skills 与 authored skills 隔离；
3. 暂不做 Hindsight、Mnemopi、向量检索和 stop 后自动学习。

与 LLM summary 的边界：

- LLM summary 解决当前 session 内的上下文压缩；
- memory 解决跨 session 的长期项目背景；
- skills 解决可复用工作流；
- 三者都可以进入 system prompt，但必须分别标注来源和权威级别。

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
- 不再把“不复制 OMP”当成限制；OMP 的成熟设计可以直接采纳。
- 但不无判断地搬入 OMP 的完整复杂度；每个能力仍按个人本地使用、封闭 VM、无公网依赖和 MVP 边界裁剪。
- OMP 的可长跑来自 deadline + compaction + abort + synthetic tool result，不是来自把步数调大。
- 第一阶段先做本地单 Agent，LSP / DAP / subagents / Browser 继续后置。
