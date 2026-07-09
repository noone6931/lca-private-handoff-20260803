# 真实需求范围确认到源码验证压测模板

本文是开发 `local-coding-agent` 时使用的压测记录模板，用于把一次真实需求从“服务边界范围判断”推进到“源码证据验证”和“实现设计/小改”。它服务于 LCA 开发协作和压测复盘，不是 LCA 运行时 memory。

## 使用原则

- 先圈项目范围，再接源码；不要一上来把所有项目塞进上下文。
- 范围判断阶段只根据用户需求和已沉淀的企业服务边界，不扫描源码，不修改文件。
- 用户确认范围后，再用 `--cwd` 指向主项目，用 `--allow-dir` 加需求文档目录和候选相关项目。
- 源码验证阶段默认只读，必须区分“已由工具验证的事实”和“推断/待确认”。
- 进入实现前必须有证据链：需求条目、项目/服务、入口文件、核心类/方法、可运行测试或无法运行原因。
- 小改实现必须遵循 dry-run patch、真实 patch、测试、`git_diff`、run summary 记录。

## OMP 对齐点

| 维度 | OMP 思路 | LCA 当前做法 |
|---|---|---|
| 项目上下文 | 通过 project context、rules、skills、memory 提供背景 | 企业服务边界放在本机 memory，范围分析工作流放在 authored skill |
| 工具选择 | 由 system prompt、tool descriptions、ToolChoiceQueue、runtime steering 共同约束 | 使用 system prompt、soft tool requirement、Current task contract、Evidence Ledger、guard/steering |
| 可观测性 | run collector / telemetry 记录运行过程 | `RunSummary` event + `run_summary` session 事件 + `/status` 最近一轮摘要 |
| 多项目边界 | 显式 workspace/context，工具结果持续回灌 | `--cwd` 主项目 + `--allow-dir` 候选项目/需求目录，工具观察会提示 workspace roots |
| 安全编辑 | 权限、diff、回滚和 reviewer 共同约束 | approval、anchored patch、dry_run、rollback、git diff attribution、implementation-quality reviewer |

## 压测元信息

| 字段 | 内容 |
|---|---|
| 日期 | YYYY-MM-DD |
| 需求名称 |  |
| 需求来源 | 需求文档路径 / 用户口述 / 工单 |
| LCA commit | `git rev-parse --short HEAD` |
| Provider / model | bailian / qwen3-coder-next 或实际模型 |
| 主 workspace |  |
| 需求目录 |  |
| 候选项目目录 |  |
| session id |  |
| 是否只读 | 是 / 否 |
| 是否外发到 AI API | 是 / 否 / 已获用户确认 |

## 阶段 1：范围判断

目标：只根据需求和已知服务边界，输出需要关注的项目范围。

推荐命令：

```bash
cd /Users/chengming/mycode/self/local-coding-agent
./agent --provider bailian --chat
```

交互中输入：

```text
请使用 project-scope-analysis skill。仅根据我提供的需求和已知企业服务边界做项目范围判断，不扫描源码，不修改文件。

需求如下：
<<<
在这里粘贴需求摘要或需求文档关键内容
>>>

请输出 Markdown 表格，至少包含：
分类（必须关注/可能关注/暂不关注/需要我确认）、项目/服务、归属部门、判断理由、置信度、需要我确认的问题。
```

记录结果：

| 分类 | 项目/服务 | 归属部门 | 判断理由 | 置信度 | 需要确认 |
|---|---|---|---|---|---|
| 必须关注 |  |  |  | 高/中/低 |  |
| 可能关注 |  |  |  | 高/中/低 |  |
| 暂不关注 |  |  |  | 高/中/低 |  |
| 需要我确认 |  |  |  | 高/中/低 |  |

阶段验收：

- [ ] 模型读取了 `project-scope-analysis/SKILL.md` 或明确遵循该工作流。
- [ ] 输出包含必须关注、可能关注、暂不关注、需要确认四类。
- [ ] 没有扫描源码。
- [ ] 没有修改文件。
- [ ] `/status` 中记录了 run summary。

## 阶段 2：用户确认范围

用户确认记录：

| 项目/服务 | 处理决定 | 说明 |
|---|---|---|
|  | 主项目 |  |
|  | 加入 `--allow-dir` |  |
|  | 暂不接入 |  |
|  | 需要补充源码 |  |

确认后的 workspace 计划：

```text
主项目 --cwd:

需求目录 --allow-dir:

相关项目 --allow-dir:

暂不接入项目:

仍需用户补充:
```

## 阶段 3：源码只读验证

目标：用确认后的项目范围查证入口、核心调用链、DTO/配置/Mapper/Test，不修改文件。

推荐命令模板：

```bash
cd /Users/chengming/mycode/self/local-coding-agent
./agent --provider bailian \
  --approval-mode yolo \
  --tool-approval shell=deny,run_tests=deny,apply_patch=deny,write_file=deny,memory_write=deny,rollback_patch=deny \
  --cwd "<主项目路径>" \
  --allow-dir "<需求目录路径>" \
  --allow-dir "<相关项目路径1>" \
  --allow-dir "<相关项目路径2>" \
  --budget-seconds 900 \
  --context-char-budget 12000 \
  "这是一次真实需求源码只读验证。请先读取 allowed-dir 中的需求文档，再结合主项目和相关项目源码验证项目范围。不要修改文件，不要运行 shell，不要写 memory。最终用中文输出：1 需求依据；2 必须关注/可能关注/暂不关注项目表；3 已验证的源码证据表（路径、类/方法、证据说明）；4 推断与不确定项；5 下一步实现设计建议。"
```

源码证据表：

| 项目 | 路径 | 类/方法/配置 | 证据类型 | 说明 | 结论 |
|---|---|---|---|---|---|
|  |  |  | read_file/search_code/LSP |  | 已验证/待确认 |

阶段验收：

- [ ] 需求文档已通过 `read_file` 读取。
- [ ] 主项目已通过 `list_files` / `search_code` / LSP 工具验证。
- [ ] 相关项目只作为 `--allow-dir` 访问，没有误写 state 到业务仓库。
- [ ] 最终回答区分“工具证据事实”和“推断”。
- [ ] `/status` 或 session `run_summary` 已记录工具次数、guard/steering、终止原因。

## 阶段 4：实现设计

目标：不急着改代码，先形成一个可审查的小切片设计。

设计表：

| 项目 | 文件/类/方法 | 变更类型 | 设计说明 | 验证方式 | 风险 |
|---|---|---|---|---|---|
|  |  | 新增/修改/测试/配置 |  |  |  |

进入小改实现前必须满足：

- [ ] 已确认目标服务和目标文件在授权 workspace / allowed dirs 内。
- [ ] 目标文件已被 `read_file` 读取。
- [ ] 变更不是 comment-only / 文档伪实现。
- [ ] 有明确测试或编译命令；如果没有，需要说明无法运行原因。
- [ ] 用户接受实现切片范围。

## 阶段 5：小改实现压测

推荐先用 worktree 或干净分支：

```bash
git -C "<目标项目路径>" status --short
```

推荐命令模板：

```bash
cd /Users/chengming/mycode/self/local-coding-agent
./agent --provider bailian \
  --approval-mode always-ask \
  --tool-approval shell=deny,run_tests=prompt,apply_patch=prompt,write_file=prompt,memory_write=deny,rollback_patch=prompt \
  --cwd "<目标项目路径>" \
  --allow-dir "<需求目录路径>" \
  --allow-dir "<相关项目路径1>" \
  --budget-seconds 1200 \
  "这是一次真实需求小改实现压测。请先读取需求文档和目标源码，维护 todo，选择一个低风险、可回滚、可验证的小实现切片。写入前必须 apply_patch dry_run=true 预览 diff，确认方案后再真实写入。修改后必须 run_tests 或说明无法运行原因，并必须 git_diff。最终输出：1 需求依据；2 修改文件；3 测试/编译结果；4 git_diff summary/attribution；5 run summary 观察；6 暴露的 LCA 问题。"
```

小改记录：

| 项目 | 内容 |
|---|---|
| session id |  |
| 修改文件 |  |
| dry_run 是否通过 |  |
| 实际 patch 是否通过 |  |
| 测试/编译命令 |  |
| 测试/编译结果 |  |
| git_diff summary |  |
| rollback 是否需要 |  |

## 阶段 6：Run Summary 记录

从 `/status` 或 session JSONL 中记录：

| 指标 | 值 |
|---|---|
| termination_reason |  |
| elapsed_ms |  |
| llm_requests |  |
| tool_calls |  |
| tool_errors |  |
| synthetic_tool_results |  |
| compactions |  |
| tool_counts |  |
| guard_hits |  |
| steering_counts |  |

观察结论：

```text
本轮是否有重复工具调用：
本轮是否触发 compaction：
本轮是否触发 forced-final / hygiene / final-structure steering：
本轮是否有无关文件漂移：
本轮是否需要引入 OMP 风格 ToolChoiceQueue / reviewer / path-scoped rules：
```

## 问题记录

| ID | 问题 | 证据 | OMP 对应机制 | LCA 措施 | 状态 |
|---|---|---|---|---|---|
| PT-XXX |  | session / run summary / diff |  |  | 新增/已缓解/已关闭 |

## 最终结论

| 项目 | 结论 |
|---|---|
| 范围判断是否可信 | 是/否/部分 |
| 源码证据是否足够 | 是/否/部分 |
| 是否可以进入实现 | 是/否/需要补项目 |
| 是否产生代码改动 | 是/否 |
| 是否需要新 LCA 能力 | 否 / reviewer / ToolChoiceQueue / token budget / path-scoped rules / LSP 增强 |
