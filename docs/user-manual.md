# LCA 使用手册

本文给普通使用者使用，不讲内部实现。LCA 是一个本地代码 Agent：它在你的机器或封闭 VM 里运行，按你授权的目录读取/搜索/修改代码，并把必要的上下文发送给你配置的 AI API。

## 1. 使用前准备

### 1.1 准备 API Key

如果使用阿里云百炼 / DashScope，在 LCA 安装目录放一个 `.env`：

```bash
DASHSCOPE_API_KEY=your-token
```

然后运行时使用：

```bash
./agent --provider bailian "阅读当前项目并总结入口"
```

如果使用其他 OpenAI-compatible API，`.env` 写：

```bash
AI_API_BASE_URL=https://your-api.example.com/v1
AI_API_KEY=your-token
AI_MODEL=your-model
```

注意：不要把 `.env`、API Key、session 日志发给别人。

### 1.2 从哪里启动

推荐从 LCA 安装目录使用 `./agent`，再用 `--cwd` 指向目标代码项目：

```bash
cd /path/to/local-coding-agent

./agent \
  --provider bailian \
  --cwd /path/to/code-project \
  "阅读当前项目并总结入口"
```

也可以进入目标项目目录后运行 `local-agent`，前提是已经安装成 Python 包。

## 2. 三种常用运行方式

### 2.1 一次性任务

适合明确的小问题：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  "找出用户登录接口在哪里实现，并给出代码证据"
```

### 2.2 交互模式

适合连续追问、边分析边调整：

```bash
./agent --provider bailian --cwd /path/to/code-project --chat
```

也可以：

```bash
./agent --provider bailian --cwd /path/to/code-project chat
```

交互模式常用命令：

```text
/help
/status
/tools
/approval
/approval mode always-ask
/approval mode write
/approval mode yolo
/approval allow run_tests
/approval prompt shell
/approval deny write_file
/approval reset shell
/exit
```

### 2.3 继续上次会话

一次性命令之间默认不是同一个对话。如果要继续上下文，用：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --continue \
  "继续刚才的问题"
```

继续指定 session：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --session 20260709T065111054226Z \
  "继续这个会话"
```

## 3. 权限模式怎么选

LCA 默认是 `always-ask`，读文件、搜代码、todo、ask_user 这类低风险工具会自动执行；写文件、跑命令、跑测试等会询问。

### 3.1 推荐模式

| 场景 | 推荐参数 | 说明 |
|---|---|---|
| 只读分析 | `--approval-mode yolo` + 禁写工具 | 让读文件/搜代码少打断，但禁止 shell 和写入。 |
| 小改代码 | `--approval-mode always-ask` | 修改前确认，安全稳妥。 |
| 本地可信仓库快速跑 | `--approval-mode write` | 读/写类工具自动允许，shell/exec 仍询问。 |
| 完全可信封闭环境 | `--approval-mode yolo` | 默认允许全部工具，但显式 deny/prompt 仍生效。 |

### 3.2 只读分析模板

用于看需求、看源码、找证据，不允许修改：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --approval-mode yolo \
  --tool-approval shell=deny,run_tests=deny,apply_patch=deny,write_file=deny,memory_write=deny,learn=deny,rollback_patch=deny \
  "只读分析这个项目的登录流程。不要修改文件，不要运行 shell。最终给出代码证据。"
```

### 3.3 小改任务模板

适合让它改少量代码并验证：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --approval-mode always-ask \
  --tool-approval shell=deny,run_tests=prompt,write_file=deny,memory_write=deny,rollback_patch=prompt \
  --budget-seconds 900 \
  "修复 xxx 问题，修改前先定位代码证据，修改后跑相关测试并总结 diff。"
```

说明：

- `allow`：直接允许该工具。
- `prompt`：每次都询问，属于配置级硬护栏。
- `deny`：直接拒绝。
- 如果希望 `apply_patch` 询问时可以按 `s` 记住本 session，不要把 `apply_patch=prompt` 写进 `--tool-approval`；让它走默认审批即可。

审批提示含义：

```text
y: 本次允许
s: 当前 session 总是允许
n: 本次拒绝
d: 当前 session 总是拒绝
```

## 4. 多目录使用

如果需求文档和代码不在同一个目录，用 `--cwd` 指向主代码项目，用 `--allow-dir` 授权额外目录：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --allow-dir /path/to/requirements \
  --allow-dir /path/to/related-project \
  --approval-mode yolo \
  --tool-approval shell=deny,run_tests=deny,apply_patch=deny,write_file=deny,memory_write=deny,learn=deny,rollback_patch=deny \
  "先读取需求目录中的需求文档，再结合主项目和相关项目源码做只读范围分析。"
```

`--allow-dir` 只扩展文件读取、搜索、LSP 和 patch 类工具的访问范围；shell、git、项目 memory/skills 仍以 `--cwd` 为主。

## 5. 常见任务怎么问

### 5.1 需求文档分析

```text
请读取 allowed-dir 里的需求文档，告诉我这个需求要做什么。不要修改文件。
最终输出：1 需求目标；2 涉及页面/接口/数据；3 业务规则；4 不确定项；5 下一步需要验证的代码位置。
```

### 5.2 源码证据定位

```text
不要推测。请从代码里找证据：前端密码是怎么处理的，后端又是怎么处理的。
最终给出文件路径、类/方法/函数、关键逻辑说明。没有找到就说明搜索过哪些关键词。
```

### 5.3 跨项目范围判断

```text
这是一次只读源码验证。请先读需求文档，再判断这个需求必须关注、可能关注、暂不关注哪些项目。
最终输出表格：项目/服务、判断、代码证据、推断或不确定项。
```

### 5.4 小代码修改

```text
请修复 xxx 问题。要求：
1. 先定位相关代码和测试。
2. 修改前说明会改哪些文件。
3. 使用最小改动。
4. 修改后运行相关测试。
5. 最终总结变更、验证结果和剩余风险。
```

## 6. 常用工具能力

用户不需要直接调用工具名，但理解能力边界有帮助：

| 能力 | 工具 |
|---|---|
| 读文件/列目录/搜代码 | `read_file`、`list_files`、`search_code` |
| 代码导航 | `lsp_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics` |
| 修改和回滚 | `apply_patch`、`write_file`、`rollback_patch` |
| 测试和命令 | `run_tests`、`shell` |
| Git 查看 | `git_status`、`git_diff` |
| 任务跟踪 | `todo_read`、`todo_add`、`todo_update` |
| 记忆 | `memory_read`、`memory_write`、`learn` |
| 向用户确认 | `ask_user` |

当前版本还没有内置 Chrome/browser 自动化工具；需要网页操作时先作为后续能力处理。

## 7. 记忆和规则

### 7.1 项目规则

可以在目标项目放：

```text
.local-agent/AGENTS.md
.local-agent/RULES.md
```

也可以放用户级：

```text
~/.config/local-coding-agent/AGENTS.md
~/.config/local-coding-agent/RULES.md
```

`AGENTS.md` 适合写项目背景、目录说明、常用流程。`RULES.md` 适合写短规则，例如“不要自动 commit/push”“最终回答必须说明验证结果”。

### 7.2 长期记忆

默认不会自动写长期记忆。需要开启时：

```bash
./agent --provider bailian \
  --cwd /path/to/code-project \
  --memory-consolidation auto \
  "完成任务后，把有长期价值的项目经验整理进 state memory。"
```

默认写到用户级 runtime state。只有显式加 `--memory-scope project` 才会写目标项目的 `.local-agent/memory`。

## 8. 状态、日志和排查

查看当前状态：

```text
/status
```

查看可用工具：

```text
/tools
```

排查 Java LSP / jdtls 是否真正导入项目：

```bash
AGENT_LSP_MODE=external ./agent --provider bailian \
  --cwd /path/to/java-project \
  "请调用 lsp_status probe=true，并解释 Java project health。"
```

如果输出里 `java.project.getAll` 或 `java.project.listSourcePaths` 是 0，说明 jdtls 虽然启动了，但 Maven/Gradle 项目没有成功导入；通常需要补齐公司 parent POM、私服配置或本地依赖缓存。LCA 会继续用 lightweight fallback 给出类/方法定位，但这不是完整 type-aware navigation。

隐藏工具日志，只看最终回答：

```bash
./agent --hide-tools "阅读项目并总结"
```

常见问题：

| 问题 | 原因 | 处理 |
|---|---|---|
| 第二次问“还有呢”没有上下文 | 新开了一次 session | 用 `--chat`、`--continue` 或 `--session`。 |
| 工具说需要 approval 但不能输入 | 当前不是交互终端 | 用交互终端运行，或调低权限阻断，或在可信环境使用 `write/yolo`。 |
| 一直等确认后任务停了 | `budget-seconds` 到期 | 增加 `--budget-seconds`，或提前设置工具策略。 |
| 它说没有权限读需求目录 | 需求目录不在 `--cwd` 下 | 加 `--allow-dir /path/to/requirements`。 |
| 担心它改文件 | 用只读模板，deny `apply_patch/write_file/shell/run_tests/memory_write/learn`。 |

## 9. 安全边界

- LCA 不会自动复制整个仓库给别人，但它会把读取到的文件内容、搜索结果和工具输出发送给配置的 AI API。
- 不要让它读取你不允许外发的源码、密钥、客户数据或生产配置。
- 不要把 `.env`、session JSONL、state dir、`.local-agent/memory` 随便发给别人。
- 只读分析优先用只读模板。
- `yolo` 只在可信目录、可信模型和封闭环境里使用。
- 让别人使用时，最好给他们固定命令模板，而不是让他们自由组合高权限参数。

## 10. 培训时的 5 分钟讲法

1. 这是本地代码 Agent，先指定代码目录 `--cwd`。
2. 连续聊天用 `--chat`；一次性问答用命令后面直接接问题。
3. 需求文档在别处时，用 `--allow-dir` 授权。
4. 只读分析用只读模板，避免误改。
5. 真要改代码时，用 `always-ask`，看到写入、测试、shell 审批再决定。
6. 中途看状态用 `/status`，改权限用 `/approval`。
7. 想接着上次问，用 `--continue` 或 `--session`。
