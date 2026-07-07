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
export AGENT_CONTEXT_CHAR_BUDGET="60000"  # optional local compaction trigger
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

长会话默认启用本地上下文压缩：当消息历史超过约 `60000` 字符时，会把早期历史折叠成系统摘要，保留最近消息，注入未完成 todo，并截断发送给模型的超大 tool 输出。可用 `--context-char-budget 0` 关闭。

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
  "先 todo，再读文件，写入前 apply_patch dry_run=true，写入后 run_tests 和 git_diff"
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

每次运行都会写入 `.local-agent/sessions/<session-id>.jsonl`。继续最近一次会话：

```bash
./agent --continue "继续刚才的问题"
```

继续指定会话：

```bash
./agent --session 20260707T060000000000Z "继续这个会话"
```

`apply_patch` 的回滚记录会写入 `.local-agent/patches/<session-id>.jsonl`，用于 `rollback_patch` 校验并恢复本 session 中的补丁。`.local-agent/` 已被 `.gitignore` 忽略。

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

- `read_file`: 读取文件，输出 `[path#hash]` 和行号。
- `list_files`: 列出项目文件，默认跳过 `.git`、`.local-agent` 和缓存目录。
- `search_code`: 使用 `rg` 搜索代码。
- `shell`: 运行本地命令，带超时和确认。
- `run_tests`: 运行测试命令，默认执行 `PYTHONPATH=src python3 -m unittest discover -s tests`。
- `git_status`: 查看本地 git 状态。
- `git_diff`: 查看本地 diff。
- `apply_patch`: 简化版 anchored patch，校验文件 hash 与旧文本后写入；支持 `replace`、`insert_before`、`insert_after`，也支持 `dry_run=true` 只预览 diff 不写文件。
- `rollback_patch`: 回滚当前 session 中由 `apply_patch` 写入的补丁；回滚前会校验文件仍然匹配补丁后的 hash。
- `write_file`: 只创建新文件；修改已有文件必须使用 `apply_patch`。
- `memory_read`: 读取 Markdown 项目记忆。
- `memory_write`: 写入 Markdown 项目记忆。
- `todo_read`: 读取当前会话 todo。
- `todo_add`: 添加当前会话 todo。
- `todo_update`: 更新当前会话 todo 状态。
- `ask_user`: 在需求不清时向用户提问；支持 `timeout_seconds` 和 `default_answer`，显式 timeout 也会被当前任务剩余预算夹紧。
- 本地 context compaction: 超过上下文预算时压缩早期历史、保留当前用户请求和未完成 todo，并截断发送给模型的超大 tool 输出。

## 设计原则

本项目默认借鉴 OMP 的成熟设计思想，但按个人本地使用、封闭 VM 友好和 MVP 复杂度做本地化裁剪。能直接采用的机制就采用；只有当它引入不必要复杂度、外部依赖、封闭 VM 风险或阶段目标膨胀时才简化：

- 单 Agent；
- 小工具集；
- 本地优先；
- 默认谨慎权限；
- `always-ask` 模式可通过 `--auto-approve-tools` 对明确工具做免确认白名单，也可用 `--tool-approval tool=allow|prompt|deny` 做更细策略；
- 工具参数会在执行前做运行时校验；
- 多步骤任务可使用 session 级 todo 追踪进度；
- 需求不清时可使用 `ask_user` 暂停并提问，也可传 `timeout_seconds` / `default_answer` 避免长任务无限等待；
- 读、搜、写默认限制在 workspace 内；
- `shell` / `run_tests` 仍然可以执行任意本地命令；危险命令黑名单只是防手滑，不是安全沙箱，真正隔离依赖封闭 VM 和人工审批；
- 读取文件有大小和行数限制；
- 明显危险的 shell 命令会被拒绝；
- patch 必须可校验；
- patch 会尽量保留原文件 BOM 和 CRLF/LF 换行风格；
- `--budget-seconds` 用墙钟时间限制单次任务，`--max-steps` 默认不限步，只作为显式安全兜底；
- 长上下文先用本地确定性 compaction，后续再评估 LLM summary；
- memory 先用 Markdown。

OMP 核心设计判断沉淀在 `docs/omp-core-architecture-notes.md`，后续不再重复翻源码确认主循环、deadline、compaction 和 step counter 的基本结论。
