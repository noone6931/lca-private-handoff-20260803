# T-072 真实需求实现压测记录

日期：2026-07-09

本文记录 T-072 首轮真实需求实现压测。结论：压测成功暴露了一个必须处理的问题，LCA 当前在真实实现任务中可能从正确需求漂移到无关配置文件，并产生无业务价值 patch。因此 T-073 应从候选项升级为下一步：补轻量 reviewer / pre-edit relevance gate，必要时再补 ToolChoiceQueue。

## 压测目标

| 项目 | 内容 |
|---|---|
| 需求目录 | `/Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化` |
| 需求文档 | `需求文档-例外核心企业批量导入V1.1/需求文档-例外核心企业批量导入V1.1.md` |
| 主代码项目 | `/Users/chengming/mycode/project/crcl-open/crcl-open` |
| 压测 worktree | `/Users/chengming/mycode/project/crcl-open-lca-t072-worktree` |
| 辅助项目 | `/Users/chengming/mycode/project/zqyl-user-center-service` |
| LCA session | `20260709T013441841983Z` |
| session JSONL | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/sessions/20260709T013441841983Z.jsonl` |
| patch log | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/patches/20260709T013441841983Z.jsonl` |

## 压测准备

原 `crcl-open/crcl-open` 工作区存在大量历史 modified 文件，不适合直接压测实现。因此按 OMP worktree/task isolation 的思路创建干净 worktree：

```bash
git worktree add -b lca-t072-pressure-test /Users/chengming/mycode/project/crcl-open-lca-t072-worktree HEAD
```

干净 worktree 中确认存在：

- `pom.xml`
- `src/main/java`
- `deployMessage`
- `src/main/resources`

## 运行命令

```bash
./agent --provider bailian \
  --approval-mode yolo \
  --tool-approval shell=deny,write_file=deny,memory_write=deny,rollback_patch=allow,run_tests=allow,apply_patch=allow \
  --budget-seconds 900 \
  --summary-mode auto \
  --context-char-budget 60000 \
  --memory-consolidation off \
  --cwd /Users/chengming/mycode/project/crcl-open-lca-t072-worktree \
  --allow-dir /Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化 \
  "这是 T-072 真实需求实现压测。请读取 allowed-dir 中的《需求文档-例外核心企业批量导入V1.1.md》，再结合当前 crcl-open 代码，选择一个低风险、可回滚、可验证的小实现切片。不要实现完整需求，不要大面积改动。优先考虑导入链路中与需求相关的健壮性/校验/DTO 解析小改；如果发现当前授权项目证据不足，请明确说明并选择最小安全改动。必须维护 todo；修改前必须 read_file 目标文件；写入前必须 apply_patch dry_run=true 预览；确认后再 apply_patch 写入；修改后必须 run_tests（优先用 mvn -q -DskipTests compile 或说明无法运行原因）和 git_diff。最终用中文汇报：1 读到的需求依据；2 修改了什么文件；3 测试/编译结果；4 git_diff summary/attribution 摘要；5 这轮暴露的 LCA 问题或下一步是否需要 reviewer/ToolChoiceQueue。"
```

## 实际工具链表现

| 阶段 | 实际表现 | 判断 |
|---|---|---|
| 需求读取 | 首步成功读取 allowed-dir 中真实需求文档 | 通过 |
| 初始定位 | 先用 `lsp_workspace_symbols` / `search_code` 查企业导入相关词 | 可接受 |
| 路径探索 | 误入 `deployMessage/nacos`，读取 Nacos/Redis 配置 | 失败 |
| 写入前预览 | 对无关 Redis 配置执行 `apply_patch dry_run=true` | 流程执行了，但目标错误 |
| 实际写入 | 修改 `deployMessage/nacos/20220708/crcl-open-redis.properties`，新增 T-072 注释 | 失败 |
| 测试/编译 | 未调用 `run_tests`；最终错误声称当前 workspace 无 `pom.xml` / `src` | 失败 |
| 最终总结 | 把无关配置注释包装成“契约锚点”，并建议需要 reviewer/ToolChoiceQueue | 暴露问题，不能接受为有效实现 |

## 关键失败

### PT-024：真实实现任务会产生无关 patch

现象：

- 需求是“例外核心企业批量导入”。
- 真实代码落点应在 `src/main/java/com/yljr/crcl/limit/...` 等 Java 导入链路。
- LCA 最终修改了 `deployMessage/nacos/20220708/crcl-open-redis.properties`。
- 该修改只是新增注释，不具备需求实现价值。

原因判断：

- `list_files {"path": ".", "max_results": 50}` 在大仓库中只给了模型根目录前若干项，模型被 `deployMessage` 吸走。
- 当前 runtime 没有“编辑目标必须与需求/证据相关”的硬门槛。
- `git_diff` summary/attribution 能告诉模型改了什么，但不能阻止模型改错地方。
- `apply_patch dry_run=true` 校验的是 patch 语法和锚点，不校验业务相关性。

OMP 对应思路：

- OMP 有更丰富的 task/runtime context、tool observations、ToolChoiceQueue、reviewer/subagents 等机制，可把“任务目标、证据、编辑目标”持续绑定。
- 对偏离流程的工具调用，OMP 可通过 soft/hard tool choice、skipped tool result、review/continuation 让模型回到必要步骤。
- 对重要编辑，OMP 的工程化链路更强调 verification 和 reviewer，而不是只靠模型最后自述。

LCA 建议措施：

- T-073 启动轻量 pre-edit relevance gate。
- 在 `apply_patch` 真写入前，runtime 检查目标文件是否出现在近期证据账本、todo、当前任务相关 search/read/LSP 结果中。
- 如果目标文件只来自低相关目录（如 `deployMessage/nacos`）且当前任务是 Java 代码实现，先返回 tool error，要求模型解释相关性或重新定位。
- 对真实需求实现任务，补一个轻量 reviewer：写入前或写入后要求模型用证据说明“为什么这个文件是实现点”。

### PT-025：模型最终回答出现反事实 workspace 判断

现象：

- worktree 根目录真实存在 `pom.xml` 和 `src/main/java`。
- LCA 最终回答称“当前 workspace 无 `pom.xml`、无 Java 源码结构”。

原因判断：

- 模型只根据自己最近读到的 `deployMessage` 片段下结论，遗忘了 workspace root。
- Current task contract / Evidence Ledger 还不足以约束“不能根据局部目录否定整个仓库结构”。
- `list_files` 根目录输出可能没有形成强 evidence，或被后续无关读取淹没。

OMP 对应思路：

- OMP 会把 cwd/project context、workspace tree、active repo context 作为系统上下文的一部分持续注入。
- OMP 的 runtime observations 不只记录文件证据，也会记录项目根、worktree、工具结果状态。

LCA 建议措施：

- Evidence Ledger 增加 workspace-root evidence：当 `list_files "."` 或启动时发现 `pom.xml/src`，在 provider-bound context 中短期保留。
- 最终回答前如果模型声称“无 pom/src/测试不可运行”，应要求该结论必须来自工具证据；否则标记为未验证。

### PT-026：T-072 命令的权限策略过宽

现象：

- 为了无人值守压测，本轮把 `apply_patch=allow`，导致无关 patch 被直接写入。

判断：

- 对真实业务实现压测，`apply_patch=allow` 太宽。
- 即使在临时 worktree 中，也应该保留 `apply_patch=prompt` 或引入 runtime relevance gate。

建议：

- 下一轮真实实现压测默认不使用 `yolo`。
- 若需要无人值守，应先实现 relevance gate/reviewer，再放开 `apply_patch=allow`。

## worktree 改动状态

LCA 产生的无关 patch：

```diff
diff --git a/deployMessage/nacos/20220708/crcl-open-redis.properties b/deployMessage/nacos/20220708/crcl-open-redis.properties
@@
-spring.session.store-type=redis
+spring.session.store-type=redis
+# T-072: ExceptionCoreEnterprise import validation rules anchor
```

该 patch 不应进入业务仓库。压测后已尝试移除注释行；临时 worktree 仍需最终清理或重置后再复用。

## 决策

T-072 首轮结论：真实需求实现链路尚未通过。失败不是 patch 语法、LLM provider 或工具权限问题，而是“编辑目标相关性”问题。

下一步：启动 T-073，但优先做轻量 reviewer / pre-edit relevance gate，而不是一上来实现完整 ToolChoiceQueue。

推荐优先级：

1. `apply_patch` relevance gate：真写入前检查目标文件与当前任务证据是否相关。
2. `git_diff` reviewer：最终前检查本轮 diff 是否与需求目标一致。
3. workspace-root evidence：把 `pom.xml`、`src/main/java` 等项目根事实加入 Evidence Ledger。
4. 若仍出现关键工具不用/乱用，再补完整 ToolChoiceQueue。

## 结论

这轮压测是失败的，但失败非常有价值：它证明 LCA 已经不再卡在“能不能调用工具”，而是进入了更真实的 Agent 质量问题：能不能把需求、证据、编辑目标和最终总结牢牢绑在一起。
