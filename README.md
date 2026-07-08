# local-coding-agent

一个个人本地编程助手 Agent 的 MVP。第一阶段目标是：

- 运行在本机或封闭 VM；
- 只访问一个 OpenAI-compatible AI API；
- 读取/搜索/修改本地代码；
- 运行本地命令和测试；
- 生成 diff；
- 用 Markdown 沉淀项目级记忆；
- 不依赖公网搜索，不自动下载依赖，不做远程控制，不做多 Agent。

## 运行前需要

如果你用的是阿里云百炼 / DashScope token，最少只需要：

```bash
export DASHSCOPE_API_KEY="your-token"
./agent "阅读这个项目并总结入口"
```

`--provider bailian` 默认使用：

- `base_url`: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `model`: `qwen-plus`

也可以把 token 写到当前项目的 `.env`，之后就不用每次 `export`：

```bash
DASHSCOPE_API_KEY=your-token
```

`.env` 已被 `.gitignore` 忽略，不会进入提交。

跨项目使用时，推荐把 token 放在 `local-coding-agent` 安装目录的 `.env` 中，然后用这里的 `./agent --cwd /path/to/other-project ...` 启动。`./agent` 会自动把安装目录 `.env` 作为 `--env-file` 传给 CLI，使 provider 凭据和目标 workspace 解耦。优先级是：真实环境变量 > 显式 `--env-file` > 目标 `--cwd/.env`。

如果不用 `./agent` 启动，也可以显式指定：

```bash
local-agent \
  --env-file /path/to/local-coding-agent/.env \
  --cwd /path/to/code-project \
  "阅读这个项目并总结入口"
```

如果控制台里开通了更适合代码的模型，可以显式指定：

```bash
./agent \
  --model qwen-plus \
  "帮我找一下测试失败原因"
```

通用 OpenAI-compatible API 需要这三项：

```bash
export AI_API_BASE_URL="https://your-api.example.com/v1"
export AI_API_KEY="your-token"
export AI_MODEL="your-model"
```

可选：

```bash
export AGENT_APPROVAL_MODE="always-ask"   # always-ask | write | yolo; ask/auto-read are legacy aliases
export AGENT_BUDGET_SECONDS="300"  # optional wall-clock budget per run
export AGENT_AUTO_APPROVE_TOOLS="run_tests,git_diff"  # optional ask-mode allowlist
export AGENT_TOOL_APPROVAL="shell=deny,run_tests=allow"  # optional per-tool allow/prompt/deny
export AGENT_CONTEXT_CHAR_BUDGET="60000"  # optional approximate compaction window
export AGENT_SUMMARY_MODE="auto"  # auto | local | llm
export AGENT_ALLOWED_DIRS="/path/to/requirements:/path/to/other-read-write-root"  # optional extra roots
```

## 本地运行

推荐从目标仓库目录直接启动：

```bash
./agent "阅读这个项目并总结入口"
```

不带 prompt 会进入 REPL：

```bash
./agent
```

安装成包后也可以用：

```bash
local-agent "帮我找一下测试失败原因"
```

工具调用日志默认输出到 stderr，例如：

```text
[tool:start] read_file {"path": "README.md"}
[tool:end] read_file ok (1234 chars)
```

如果只想看最终回答，可以加：

```bash
--hide-tools
```

长任务建议设置墙钟预算，而不是用很小的步数截断：

```bash
./agent \
  --budget-seconds 1200 \
  "按 docs/requirements/feature.md 完成需求并跑测试"
```

默认 `--budget-seconds` 是 600 秒。`--budget-seconds 0` 可以关闭时间预算。

`--max-steps` 默认是 0，表示不限步；它只作为显式防失控保险丝。日常限制任务时优先使用 `--budget-seconds`。

如果需求文档和代码项目不在同一个目录，可以让 `--cwd` 指向代码项目，再用 `--allow-dir` 授权需求目录：

```bash
./agent \
  --cwd /path/to/code-project \
  --allow-dir /path/to/requirements \
  "读取需求目录里的文档，按要求修改当前代码项目并验证"
```

`--allow-dir` 可以传多次。它只扩展文件、搜索、LSP 和 patch 类工具的可访问根目录；shell、git 和显式项目 memory/skills 仍锚定在 `--cwd`，session/todo/patch logs 和默认自动 consolidation memory 走 runtime state dir。

可以放用户级和项目级常驻上下文：

```text
~/.config/local-coding-agent/AGENTS.md
.local-agent/AGENTS.md
```

也可以放 sticky rules：

```text
~/.config/local-coding-agent/RULES.md
.local-agent/RULES.md
```

`AGENTS.md` 会在新 session 启动时作为 advisory context 注入；`RULES.md` 会在每次发送模型请求前重新附加，用于“不要自动 commit/push”“总结验证结果”这类短规则。可用 `AGENT_CONFIG_DIR` 改用户级配置目录。当前用户指令与最新读取的源码证据具有最高优先级。

长会话默认启用 OMP 风格上下文压缩策略：`--context-char-budget` 近似表示上下文窗口，runtime 会至少预留 15% 给下一轮 prompt/输出；超过阈值后压缩早期历史，保留最近消息和未完成 todo，并截断发送给模型的超大 tool 输出。发送给模型的上下文还会折叠空搜索/LSP 这类 useless 结果，以及被新等价读取/搜索 supersede 的旧工具结果；session 日志仍保留原文。默认 `--summary-mode auto`：小历史不摘要，触发 compaction 时自动调用当前配置的 AI API 生成语义摘要，失败回退本地确定性摘要。可用 `--summary-mode local` 强制只用本地摘要，`--summary-mode llm` 强制在 compaction 时尝试 LLM 摘要，`--context-char-budget 0` 关闭压缩。

自动记忆整理默认关闭，避免只读分析时隐式写入长期 memory。需要时可用 `--memory-consolidation auto` 或 `AGENT_MEMORY_CONSOLIDATION=auto` 开启；每轮结束后会让当前 provider 从本轮 session 中抽取长期可复用的 project/decisions/conventions/learned。默认 `--memory-scope state` 会追加到 runtime state 目录的 `memory/*.md`，对齐 OMP 的用户 agent dir 思路；只有显式 `--memory-scope project` 或 `AGENT_MEMORY_SCOPE=project` 才写入项目 `.local-agent/memory/*.md`。坏 JSON、空结果、预算耗尽、本轮已经显式调用 `learn` / `memory_write` 时不会写入。`--memory-consolidation llm` 会跳过 auto 的小会话启发式，直接尝试抽取。

项目内可放可复用工作流 skill：

```text
.local-agent/skills/code-review/SKILL.md
```

推荐写 frontmatter：

```markdown
---
name: code-review
description: Use when reviewing a patch before commit.
---

# Code Review

...
```

启动时只会把 `name`、`description` 和 `SKILL.md` 路径注入 system prompt；不会注入正文。Agent 需要使用某个 skill 时，应先用 `read_file` 读取对应 `SKILL.md`。设置 `hide: true` 可以让某个 skill 不进入启动提示。

普通代码任务不需要每次手写“先 list_files、再 read_file、再 dry_run、再 run_tests、再 git_diff”。系统提示和 runtime reminder 已固化默认工作流：Agent 会按任务需要自行探索、维护 todo、修改前读取文件、用 anchored patch 写入、修改后验证并总结 diff。

如果 `always-ask` 模式下某些工具你已经确认安全，可以只对白名单工具免确认：

```bash
./agent \
  --approval-mode ask \
  --auto-approve-tools run_tests,git_diff \
  "跑测试并总结 diff"
```

更细的工具策略可以使用 `--tool-approval`：

```bash
./agent \
  --approval-mode always-ask \
  --tool-approval shell=deny,run_tests=allow,apply_patch=prompt \
  "跑测试并总结结果"
```

策略含义：`allow` 直接允许，`prompt` 强制询问，`deny` 直接拒绝。显式 `tool_approval` 会优先于旧的 `--auto-approve-tools`，其中 `prompt` / `deny` 是配置级护栏，不会被会话内 always allow 绕过。

`write` 模式会自动允许 `read` / `state` / `interaction` / `write` 工具，`exec` 工具仍会询问。`yolo` 模式默认允许全部工具，但 `tool_approval` 中的 `prompt` / `deny` 仍会生效，危险 shell 命令也仍会被硬拒绝。

需要人工确认的 approval prompt 会受 `--budget-seconds` 约束；如果用户一直没有确认，deadline 到期后会取消该工具调用并把错误结果回传给模型。

常用模板：

```bash
# 只读分析：禁止 shell 和写入类工具
./agent --provider bailian \
  --approval-mode always-ask \
  --tool-approval shell=deny,write_file=deny,memory_write=deny \
  "阅读当前项目并总结架构"

# 小改任务：run_tests 免确认，apply_patch 走 y/s/n/d 会话审批
./agent --provider bailian \
  --approval-mode always-ask \
  --tool-approval shell=deny,run_tests=allow,write_file=deny,memory_write=deny,rollback_patch=prompt \
  --budget-seconds 600 \
  --context-char-budget 60000 \
  "修复 README 里的一个小问题并验证"
```

如果希望 `apply_patch` 出现 `y/s/n/d` 并可用 `s` 记住当前 session，不要把 `apply_patch` 写成 `tool_approval=prompt`。显式 `prompt` 是配置级硬护栏，会每次询问。

REPL 中可以临时调整当前会话的权限：

```text
/approval
/approval mode write
/approval allow run_tests
/approval prompt shell
/approval deny write_file
/approval reset shell
```

`/approval` 命令会校验工具名，输错工具名会直接提示错误。

## 会话恢复

每次运行都会把会话写到用户级 runtime state 目录，默认是：

```text
${XDG_STATE_HOME:-~/.local/state}/local-coding-agent/workspaces/<workspace-key>/sessions/<session-id>.jsonl
```

也可以用 `--state-dir` 或 `AGENT_STATE_DIR` 指定 state root；LCA 会在其下按 workspace 编码分目录保存。继续最近一次会话：

```bash
./agent --continue "继续刚才的问题"
```

继续指定会话：

```bash
./agent --session 20260707T060000000000Z "继续这个会话"
```

`todo` 和 `apply_patch` 的回滚记录也写入同一个 runtime state 目录下的 `todos/` 与 `patches/`，用于 `todo_read` 和 `rollback_patch`。显式项目 memory 和项目 skills 仍保留在 workspace 的 `.local-agent/` 中；自动 memory consolidation 默认写 runtime state 的 `memory/`，需要共享到项目时再显式切到 `--memory-scope project`。

## 本地测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall src tests
```

## 开发项目管理同步

开发 `local-coding-agent` 时，项目管理的事实源是 `docs/project-management.md`。这套 Markdown/Excel 给参与开发本项目的人和协作 Agent 使用，不是 LCA 运行时自己的 memory。更新它之后生成 Excel：

```bash
python3 scripts/sync_project_excel.py
```

生成结果是 `docs/local-coding-agent-project-management.xlsx`。

## 当前能力

- `read_file`: 读取文件，输出 `[path#hash]`、纯 `tag: hash` 和行号；`apply_patch.tag` 应传纯 hash。
- `list_files`: 列出项目文件，默认跳过 `.git`、`.local-agent` 和缓存目录。
- `search_code`: 使用 `rg` 搜索代码。
- `shell`: 运行本地命令，带超时和确认。
- `run_tests`: 运行测试命令，默认执行 `PYTHONPATH=src python3 -m unittest discover -s tests`。
- `git_status`: 查看本地 git 状态。
- `git_diff`: 查看本地 diff，并在有 run start baseline 时提示 pre-existing、本轮 apply_patch 和未归因变更。
- `apply_patch`: 简化版 anchored patch，校验文件 hash 与旧文本后写入；支持 `replace`、`insert_before`、`insert_after`，也支持 `dry_run=true` 只预览 diff 不写文件；若误传 `[path#hash]` 会提取 hash 并提示下次传纯 tag。
- `rollback_patch`: 回滚当前 session 中由 `apply_patch` 写入的补丁；回滚前会校验文件仍然匹配补丁后的 hash。
- `write_file`: 只创建新文件；修改已有文件必须使用 `apply_patch`。
- `memory_read`: 读取 Markdown 项目记忆。
- `memory_write`: 写入 Markdown 项目记忆。
- `learn`: 把可复用项目经验写入 `.local-agent/memory/learned.md`。
- `todo_read`: 读取当前会话 todo。
- `todo_add`: 添加当前会话 todo。
- `todo_update`: 更新当前会话 todo 状态。
- `ask_user`: 在需求不清时向用户提问；支持 `timeout_seconds` 和 `default_answer`，显式 timeout 也会被当前任务剩余预算夹紧。
- `lsp_symbols`: 列出 Python、Java、JavaScript、TypeScript、Vue 的轻量符号。
- `lsp_workspace_symbols` / `lsp_document_symbols`: `lsp_symbols` 的只读兼容别名，方便从 OMP/Codex 风格提示迁移。
- `lsp_definition`: 查找 Python、Java、JavaScript、TypeScript、Vue 的轻量符号定义。
- `lsp_references`: 查找这些语言中的标识符引用。
- `lsp_diagnostics`: 运行轻量诊断；Python 使用 `compile()`，Java/JS/TS/Vue 使用本地括号/分隔符检查。
- context compaction: 按 OMP 风格 reserve 阈值压缩早期历史、保留当前用户请求和未完成 todo，并截断发送给模型的超大 tool 输出；空搜索/LSP 结果会标记 useless，发送给模型的上下文会折叠 useless/superseded 工具结果；默认 `--summary-mode auto` 会在触发压缩时尝试 LLM 摘要并失败回退 local。
- startup context injection: 新 session 启动时会读取用户级和项目级 `AGENTS.md`，作为 advisory context 注入 system prompt。
- sticky rules injection: 每次发送模型请求前会读取用户级和项目级 `RULES.md` 并追加到 provider-bound context。
- startup memory injection: 新 session 启动时会读取项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/{project,decisions,conventions,learned}.md`，并作为 advisory context 注入 system prompt；当前用户指令和最新源码证据优先。
- memory consolidation: 可选 `--memory-consolidation auto|llm`，在一轮结束后用当前 provider 抽取长期经验；默认 `off`，开启后默认写 state dir，`--memory-scope project` 才写项目 `.local-agent/memory/*.md`。
- authored skills discovery: 新 session 启动时会扫描 `.local-agent/skills/<name>/SKILL.md`，只注入 name、description 和 source path，正文按需读取。

## 设计原则

本项目默认借鉴 OMP 的成熟设计思想，但按个人本地使用、封闭 VM 友好和 MVP 复杂度做本地化裁剪。能直接采用的机制就采用；只有当它引入不必要复杂度、外部依赖、封闭 VM 风险或阶段目标膨胀时才简化：

- 单 Agent；
- 小工具集；
- 本地优先；
- 默认谨慎权限；
- `always-ask` 模式可通过 `--auto-approve-tools` 对明确工具做免确认白名单，也可用 `--tool-approval tool=allow|prompt|deny` 做更细策略；
- 工具参数会在执行前做运行时校验；
- 多步骤任务可使用 session 级 todo 追踪进度；未完成 todo 会作为 runtime reminder 进入模型上下文，帮助长任务保持方向；
- 需求不清时可使用 `ask_user` 暂停并提问，也可传 `timeout_seconds` / `default_answer` 避免长任务无限等待；
- 读、搜、写默认限制在 workspace 内；显式 `--allow-dir` / `AGENT_ALLOWED_DIRS` 可授权额外目录给文件、搜索、LSP 和 patch 工具；
- `shell` / `run_tests` 仍然可以执行任意本地命令；危险命令黑名单只是防手滑，不是安全沙箱，真正隔离依赖封闭 VM 和人工审批；
- 读取文件有大小和行数限制；
- 明显危险的 shell 命令会被拒绝；
- patch 必须可校验；
- patch 会尽量保留原文件 BOM 和 CRLF/LF 换行风格；
- `--budget-seconds` 用墙钟时间限制单次任务，`--max-steps` 默认不限步，只作为显式安全兜底；
- 默认工作流已沉到 system prompt 和 runtime reminder，用户可以直接用自然语言描述任务；
- 用户级和项目级 `AGENTS.md` 提供常驻上下文，`RULES.md` 提供短 sticky rules；二者都是 advisory，当前用户指令和源码证据优先；
- 项目 authored skills 只把 metadata 注入启动上下文，正文按需读取，避免 prompt 膨胀；
- 长上下文默认使用 OMP 风格 auto compaction：超过阈值才尝试 LLM summary，失败回退本地确定性摘要，并在发送给模型的上下文中折叠 useless/superseded 工具结果；
- LSP 能力先做封闭 VM 友好的多语言静态导航工具，覆盖 Python、Java、JavaScript、TypeScript、Vue，不启动外部语言服务器；
- memory 使用 Markdown，启动时作为 advisory context 注入；可用 `learn` 显式沉淀长期经验，也可显式开启 memory consolidation 从 session 中抽取长期经验。

OMP 核心设计判断沉淀在 `docs/omp-core-architecture-notes.md`，后续不再重复翻源码确认主循环、deadline、compaction 和 step counter 的基本结论。
