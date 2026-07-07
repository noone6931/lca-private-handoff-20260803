# Memory 与 Skills 实现方案

更新时间：2026-07-07

本文是给后续实现者读取的交接方案。它基于已核实的 OMP memory / autolearn / skills 设计，但按 LCA 第一阶段目标裁剪：个人本地使用、封闭 VM 友好、只访问指定 OpenAI-compatible API、可审计、默认不隐藏写入。

## OMP 参考设计

OMP 的 memory 不是一个单一 Markdown 文件，而是 backend abstraction：

- `memory.backend=off`：默认关闭。
- `memory.backend=local`：本地 rollout summary pipeline，启动时从历史 session 抽取长期知识，写出 `MEMORY.md`、`memory_summary.md` 和 `skills/`。
- `memory.backend=hindsight`：远端 Hindsight memory，提供 recall / retain / reflect。
- `memory.backend=mnemopi`：本地 SQLite memory，提供 recall / retain / reflect / edit。

OMP 的 skills 也有两条路径：

- 普通 authored skills：项目或用户写好的 `<skills-root>/<name>/SKILL.md`，启动时发现，系统 prompt 只放 name / description，正文按需读取。
- managed skills：`autolearn.enabled` 后，模型可通过 `manage_skill` 或 `learn` 创建 / 更新 / 删除 `~/.omp/agent/managed-skills/<name>/SKILL.md`。managed skills 优先级最低，不能覆盖用户手写 skills。

关键原则：

- memory 是 heuristic context，不覆盖当前 repo state 和用户指令。
- memory / generated skill 写入要有上限和注入清洗，避免长期 prompt injection。
- auto-learn 默认关闭，先手动 learn，再评估 session stop 后自动捕捉。
- generated skills 与用户 authored skills 隔离，且 authored skills 优先。

## LCA 当前状态

当前 LCA 已有：

- `memory_read` / `memory_write`：写入 `.local-agent/memory/{project,decisions,conventions}.md`。
- deterministic context compaction：用于单个 session 内上下文治理，不等同长期 memory。
- 工作树中已有 LLM summary 和轻量 Python LSP 的未提交实现痕迹，需 review 和压测后再改状态为完成。

当前 LCA 还没有：

- 启动时自动注入 Markdown memory。
- `learn` 工具。
- skills discovery / skill metadata prompt。
- managed skills / autolearn。
- memory backend selector。

## 设计目标

第一阶段先做可审计的本地 MVP：

1. 让现有 Markdown memory 真正参与后续 session，而不是只能手动 `memory_read`。
2. 增加显式 `learn`，让 agent 能沉淀可复用经验，但仍然是可见、可控的写入。
3. 增加轻量 skills discovery，让项目可放可复用工作流。
4. 最后再做 managed skills，默认关闭，用户明确打开后才允许生成。

暂不做：

- 远端 Hindsight。
- SQLite / 向量检索。
- 插件市场。
- session stop 自动学习。
- 让 generated skill 覆盖用户 authored skill。

## 分阶段方案

### M1：Markdown Memory 启动注入

目标：让 `.local-agent/memory/*.md` 在新 session 启动时进入 system prompt。

建议实现：

- 新增配置：
  - `memory_enabled: bool = True`
  - `memory_injection_char_budget: int = 6000`
- 在 `AgentRuntime.__init__` 构建 system prompt 时读取：
  - `.local-agent/memory/project.md`
  - `.local-agent/memory/decisions.md`
  - `.local-agent/memory/conventions.md`
  - 后续 M2 的 `.local-agent/memory/learned.md`
- 注入格式建议：

```text
[Project memory]
Memory is advisory. Prefer current repo state and current user instructions when they conflict.
Source: .local-agent/memory/project.md
...
```

验收标准：

- 没有 memory 文件时 system prompt 不变化。
- 有 memory 文件时只注入截断后的内容，并带 source path。
- memory 明确标记为 advisory。
- 单测覆盖 budget 截断和多文件合并顺序。

### M2：Learn 工具

目标：用一个更明确的工具沉淀经验，避免把所有长期事实都混进 `project.md`。

建议实现：

- 新增 `learn` 工具，tier=`write`。
- 输入：
  - `memory`: 自包含经验，说明 what / when / why。
  - `context`: 可选来源上下文。
- 写入 `.local-agent/memory/learned.md`。
- 每条记录带 UTC 时间、source、context。
- 每条 content 限制 2000 chars，context 限制 400 chars，文件保留最近 100 条。
- 对会进入 system prompt 的单行字段做清洗：控制字符、尖括号、反引号、连续 fence。

验收标准：

- `learn` 写入可读 Markdown。
- 空内容拒绝。
- 超长内容被截断。
- `memory_enabled` 打开时 learned lessons 会参与 M1 注入。

### M3：Memory Consolidation

目标：当 Markdown memory 变长后，用总结文件降低上下文占用。

建议实现：

- 不做后台自动任务，先做显式命令或工具：
  - `memory_compact` 或 CLI `--compact-memory`。
- 输入来自 `project.md`、`decisions.md`、`conventions.md`、`learned.md`。
- 输出 `.local-agent/memory/summary.md`。
- 若 LLM summary 可用，用 LLM 总结；失败时保留 deterministic 前 N 条 / 后 N 条摘要。
- 注入优先级：`summary.md` 优先，原始 memory 作为按需 `memory_read` 的资料。

验收标准：

- LLM 失败不破坏 agent 启动。
- summary 明确来自 memory，不当作当前事实源。
- 不删除原始 memory。

### M4：Authored Skills Discovery

目标：支持用户或项目维护可复用工作流。

建议实现：

- 先只扫项目内：
  - `.local-agent/skills/<name>/SKILL.md`
- `SKILL.md` frontmatter：
  - `name`
  - `description`
  - `hide` 可选
- 启动时在 system prompt 注入 skills metadata，不注入全文：

```text
[Available skills]
- code-review: Use when reviewing a patch before commit.
Read .local-agent/skills/code-review/SKILL.md before using it.
```

- 初版可以让模型用 `read_file` 读取 skill 文件，因为它在 workspace 内。
- 后续再加 `skill_read`，用于用户级 skills 或更强路径隔离。

验收标准：

- 无 skills 时 prompt 不变化。
- 有 skills 时只列 name / description。
- description 经过清洗并截断。
- `hide=true` 不进入 prompt，但文件仍可手动读取。

### M5：Managed Skills

目标：允许 agent 在用户显式开启后，把 repeatable procedure 生成成 skill。

建议实现：

- 配置默认关闭：
  - `autolearn_enabled: bool = False`
- 生成目录：
  - `.local-agent/managed-skills/<name>/SKILL.md`
- 新增 `manage_skill` 工具，tier=`write`：
  - `create`
  - `update`
  - `delete`
- 名称限制：小写字母、数字、连字符，1 到 64 字符。
- 文件大小上限：64KB。
- managed skills 优先级最低：
  - `.local-agent/skills/<name>` 存在时，拒绝创建同名 managed skill。
  - discovery 时 authored skill 胜出。
- 写入前校验路径必须在 managed root 内，拒绝 symlink escape。

验收标准：

- 默认不暴露 `manage_skill`。
- 开启 `autolearn_enabled` 才暴露。
- 创建同名 authored skill 时返回明确错误。
- generated skill 下次 session 可被发现并列入 metadata。

## 与 LLM Summary / LSP 的协调

这几项可以并行，但边界要清楚：

- LLM summary 负责单个 session 的上下文压缩，不负责长期记忆。
- Memory injection 负责跨 session 的长期项目背景，不替代当前 repo 读取。
- 轻量 LSP 负责 Python 代码导航，不等于完整 language server，不应在文档里承诺 rename / type-aware reference / 多语言诊断。
- Skills 负责可复用工作流，不是可执行工具；执行副作用仍必须通过普通工具和 approval。

如果大猛已实现 LLM summary / LSP，请先 review：

- LLM summary 是否有 deterministic fallback、timeout、budget clamp、单 system message 合并、测试覆盖。
- LSP 是否只读、路径受 workspace 限制、跳过大文件和 cache 目录、有单测覆盖 symbols / definition / references / diagnostics。
- 文档状态必须写成“轻量 MVP”，不要写成完整 LSP。

## 推荐落地顺序

1. Review 当前 LLM summary 和轻量 LSP 未提交实现，跑测试。
2. 做 M1 memory startup injection。
3. 做 M2 learn 工具。
4. 做 M4 authored skills discovery。
5. 做 M5 managed skills。
6. 真实项目压测后再考虑 M3 consolidation 和 autolearn autoContinue。

## 主要风险

| 风险 | 应对 |
|---|---|
| memory / skills prompt injection | 注入区明确 advisory；清洗 description / learned metadata；默认不 yolo 不信任仓库 |
| 上下文膨胀 | memory 注入预算和 summary 优先 |
| generated skill 质量差 | 默认关闭 autolearn；managed skills 隔离；authored skills 优先 |
| 隐式写入难审计 | M1/M2/M4 都是显式或静态文件；M5 默认关闭 |
| LLM summary 与 memory 混淆 | 文档和 prompt 分开命名：context compaction vs project memory |
