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
PYTHONPATH=src python3 -m local_agent.cli --provider bailian --cwd /path/to/repo "阅读这个项目并总结入口"
```

`--provider bailian` 默认使用：

- `base_url`: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `model`: `qwen-plus`

如果控制台里开通了更适合代码的模型，可以显式指定：

```bash
PYTHONPATH=src python3 -m local_agent.cli \
  --provider bailian \
  --model qwen-plus \
  --cwd /path/to/repo \
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
export AGENT_APPROVAL_MODE="ask"   # ask | auto-read | yolo
```

## 本地运行

```bash
python3 -m local_agent.cli --cwd /path/to/repo "阅读这个项目并总结入口"
```

如果没有安装为包，可以直接指定源码路径：

```bash
PYTHONPATH=src python3 -m local_agent.cli --cwd /path/to/repo "帮我找一下测试失败原因"
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

## 会话恢复

每次运行都会写入 `.local-agent/sessions/<session-id>.jsonl`。继续最近一次会话：

```bash
PYTHONPATH=src python3 -m local_agent.cli --provider bailian --continue "继续刚才的问题"
```

继续指定会话：

```bash
PYTHONPATH=src python3 -m local_agent.cli --provider bailian --session 20260707T060000000000Z "继续这个会话"
```

## 本地测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall src tests
```

## 当前能力

- `read_file`: 读取文件，输出 `[path#hash]` 和行号。
- `list_files`: 列出项目文件，默认跳过 `.git`、`.local-agent` 和缓存目录。
- `search_code`: 使用 `rg` 搜索代码。
- `shell`: 运行本地命令，带超时和确认。
- `run_tests`: 运行测试命令，默认执行 `PYTHONPATH=src python3 -m unittest discover -s tests`。
- `git_status`: 查看本地 git 状态。
- `git_diff`: 查看本地 diff。
- `apply_patch`: 简化版 anchored patch，校验文件 hash 与旧文本后写入；支持 `replace`、`insert_before`、`insert_after`。
- `write_file`: 只创建新文件；修改已有文件必须使用 `apply_patch`。
- `memory_read`: 读取 Markdown 项目记忆。
- `memory_write`: 写入 Markdown 项目记忆。

## 设计原则

这不是 OMP 的复刻版，而是从 OMP 借鉴核心思想后的瘦身 MVP：

- 单 Agent；
- 小工具集；
- 本地优先；
- 默认谨慎权限；
- 工具参数会在执行前做运行时校验；
- 读、搜、写默认限制在 workspace 内；
- `shell` / `run_tests` 仍然可以执行任意本地命令；危险命令黑名单只是防手滑，不是安全沙箱，真正隔离依赖封闭 VM 和人工审批；
- 读取文件有大小和行数限制；
- 明显危险的 shell 命令会被拒绝；
- patch 必须可校验；
- patch 会尽量保留原文件 BOM 和 CRLF/LF 换行风格；
- memory 先用 Markdown。
