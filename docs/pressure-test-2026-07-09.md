# T-072 真实需求实现压测记录

日期：2026-07-09

本文记录 T-072 首轮真实需求实现压测，以及随后 T-073 relevance gate / reviewer、T-074 implementation-quality reviewer / safe new-file policy 复跑。T-072 成功暴露了一个必须处理的问题：LCA 在真实实现任务中可能从正确需求漂移到无关配置文件，并产生无业务价值 patch。T-073 已缓解该跑偏问题，但复跑又暴露真实实现可能退化成 comment-only 低价值 patch。T-074 已缓解 comment-only 伪实现和新文件权限降级问题；复跑中模型没有强行修改，而是在当前仓库缺少目标服务实现时停止说明。

2026-07-09 后续又完成一次 qwen3-coder-next 只读压测：模型能从 `YXK-397 云信通用优化25.1` 上线 SQL 进入 `IntentionConfig*` 实体、Mapper、Controller 和 user-center 辅助文档，最终正常收束；但暴露了 todo 参数易错、重复读取过多、最终回答部分过度断言和“项目范围表”退化为“表名范围表”等问题。

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

## T-073 复跑记录

复跑时间：2026-07-09

| 项目 | 内容 |
|---|---|
| LCA session | `20260709T021349259159Z` |
| session JSONL | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/sessions/20260709T021349259159Z.jsonl` |
| patch log | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/patches/20260709T021349259159Z.jsonl` |
| 压测 worktree | `/Users/chengming/mycode/project/crcl-open-lca-t072-worktree` |
| 复跑后 worktree | 已清理，`git status --short` 为空 |

T-073 已落地的改动：

- `apply_patch` 真实写入前调用 runtime relevance gate。
- 目标文件必须在本轮被 `read_file` 读取过；`dry_run=true` 仍允许预览。
- 当前请求像代码实现任务时，如果目标是 `deployMessage/nacos/*.properties`、`*.yml` 等部署/配置类低相关路径，且用户没有明确要求改配置，会返回 tool error，要求重新定位或确认。
- workspace root evidence 会进入 Evidence Ledger，包含 `pom.xml`、`src/main/java`、`src/main/resources` 等根事实。
- `git_diff` 对本轮 patch 触及低相关路径追加 `[diff reviewer]`。
- patch log 对 workspace 内绝对路径归一为相对路径，避免 `git_diff` attribution 把本轮绝对路径 patch 错判为 unrecorded relative diff。

复跑结果：

| 观察点 | 结果 | 判断 |
|---|---|---|
| 是否读取需求 | 首步读取 allowed-dir 需求文档 | 通过 |
| 是否进入主源码 | 进入 `src/main/java`，读取 `ChargeFeignApi`、`ChargeRateAuditDto`、`IntentionConfigManagerController`、`ExemptCompanyDto`、`UpdateIntentionConfigReq` | 通过 |
| 是否再次写 `deployMessage/nacos` | 没有触碰 | 通过 |
| 是否再次声称无 `pom.xml/src` | 没有 | 通过 |
| 是否执行 dry-run | 对 `ExemptCompanyDto.java` 先 dry-run | 通过 |
| 是否执行真实写入 | 修改 `ExemptCompanyDto.java` 的 JavaDoc | 通过但价值低 |
| 是否运行测试 | 执行 `mvn -q -DskipTests compile`，因父 POM `com.yljr:parent:pom:0.0.5-SNAPSHOT` 不可解析失败 | 环境/依赖问题 |
| 是否输出 diff | 输出 diff summary 和 attribution | 通过 |

实际 patch：

```diff
diff --git a/src/main/java/com/yljr/crcl/open/interfaces/facade/config/req/ExemptCompanyDto.java b/src/main/java/com/yljr/crcl/open/interfaces/facade/config/req/ExemptCompanyDto.java
@@ -19,6 +19,11 @@ public class ExemptCompanyDto implements Serializable {
     /**
      * 企业ID
+     * <p>业务规则：</p>
+     * <ul>
+     *   <li>云信保理产品：须为当前额度方案的确权方</li>
+     *   <li>通用资金方产品：须已认证且企业类型含“核心企业”</li>
+     * </ul>
      */
     @NotNull(message = "企业ID不能为空")
     private BigDecimal companyId;
```

该 patch 已从临时 worktree 清理，业务仓库未留下改动。

## T-073 结论

T-073 解决了 T-072 的核心跑偏问题，但没有让真实实现任务真正过关：

- 好消息：模型没有再被 `deployMessage/nacos` 带走，workspace root 反事实也没有复现。
- 坏消息：模型尝试新建 validator 注解和目录时被 `shell=deny` / `write_file=deny` 拦住，随后退化为只加 JavaDoc 注释，并把它包装成“健壮性/校验相关”小实现。

这说明 LCA 已从“防无关副作用”推进到“判断实现是否有业务价值”的阶段。

## 新增问题

### PT-027：真实实现可能退化成 comment-only patch

现象：

- 模型定位到了 `ExemptCompanyDto.java`，这是相关 Java DTO。
- 但最终只给 `companyId` 字段补 JavaDoc。
- 该 patch 不改变校验逻辑、不改变导入解析、不返回错误枚举，不能算需求实现。

OMP 对应思路：

- OMP 不只依赖 patch 成功，还会把任务目标、todo、verification、reviewer/subagent 和 tool-choice steering 组合起来判断工作是否完成。
- 对“实现类任务”，reviewer 应关注 patch 是否改变了运行行为，或是否明确声明只是文档/注释改动。

LCA 措施：

- T-074 增加 implementation-quality gate。
- 对代码实现任务，如果 diff 只有注释/JavaDoc/README，需要要求模型明确标记为“文档改动”并说明没有实现业务逻辑，或重新定位真实逻辑切片。
- 最终回答中不能把 comment-only patch 说成“实现/校验/健壮性”。

### PT-028：真实实现可能需要受控新文件策略

现象：

- 模型尝试新增 `ValidCompanyId.java` 和 validator 目录。
- 当前压测配置 `shell=deny,write_file=deny` 阻止了新文件/目录。
- 模型没有停下来说明权限不足，而是降级成低价值注释 patch。

OMP 对应思路：

- OMP permission model 会按工具和动作请求权限，而不是一刀切让模型绕开权限。
- 对新文件创建，应由审批策略、workspace 边界和任务相关性共同控制。

LCA 措施：

- T-074 设计 safe new-file policy。
- 候选规则：父目录已被 `list_files/read_file` 观察；目标路径在 workspace 或 allowed-dir；当前任务需要新增类/测试；`write_file` 走 prompt 或 per-tool allow；必要时先 dry-run/preview 计划。
- 如果策略不允许新文件，模型应停止并报告“权限不足，无法安全实现”，而不是改注释凑完成。

## T-074 复跑记录

复跑时间：2026-07-09

| 项目 | 内容 |
|---|---|
| LCA session | `20260709T025706579604Z` |
| session JSONL | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/sessions/20260709T025706579604Z.jsonl` |
| 压测 worktree | `/Users/chengming/mycode/project/crcl-open-lca-t072-worktree` |
| 复跑后 worktree | `git status --short` 为空 |

T-074 已落地的改动：

- `git_diff` 对本轮代码实现 diff 增加 implementation-quality reviewer。
- 如果本轮 source code diff 只有注释、JavaDoc、README 或纯文档变化，reviewer 会提醒模型不能把它声称为行为、校验、解析或测试覆盖变化。
- `write_file` 支持 `dry_run=true`，可预览新文件 unified diff 而不写入。
- 真实 `write_file` 创建新文件时会写 patch log，记录 `before_exists=false`、before/after tag 和 diff。
- `rollback_patch` 可回滚本 session 创建的新文件：校验当前 after tag 后删除该文件。

复跑命令的核心策略：

```bash
./agent --provider bailian \
  --approval-mode yolo \
  --tool-approval shell=deny,write_file=allow,memory_write=deny,rollback_patch=allow,run_tests=allow,apply_patch=allow \
  --budget-seconds 900 \
  --summary-mode auto \
  --context-char-budget 60000 \
  --memory-consolidation off \
  --cwd /Users/chengming/mycode/project/crcl-open-lca-t072-worktree \
  --allow-dir /Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化 \
  "这是 T-074 真实需求实现复测。请读取 allowed-dir 中的《需求文档-例外核心企业批量导入V1.1.md》，再结合当前 crcl-open 代码，选择一个低风险、可回滚、可验证的小实现切片。不得只修改注释、JavaDoc、README 或纯文档；如果当前授权/依赖不足以安全做出有业务逻辑价值的改动，请停止并明确说明。"
```

复跑结果：

| 观察点 | 结果 | 判断 |
|---|---|---|
| 是否读取需求 | 首步读取 allowed-dir 需求文档 | 通过 |
| 是否重新触碰 `deployMessage/nacos` | 没有 | 通过 |
| 是否产生 comment-only patch | 没有任何 patch | 通过 |
| 是否因新文件权限降级为注释 patch | 没有；`write_file=allow` 后也未乱建文件 | 通过 |
| 是否识别服务边界 | 识别 `crcl-open` 只有 `InvestmentPlanFeignApi` / `InvestmentPlanFeign` 调用，真实实现应在 `zqyl-investment-plan` | 通过 |
| 是否维护 todo | 未调用 todo 工具 | 待改进 |
| 是否输出 git diff | 无改动但未调用 `git_diff` 证明 | 待改进 |

核心结论：

- T-074 对 PT-027 有效：模型没有再把 JavaDoc/注释改动包装成“业务实现”。
- T-074 对 PT-028 有效：新文件策略已支持 dry-run、patch log 和 rollback；复跑中也未因权限变化而乱建无用文件。
- 当前 `crcl-open` 证据显示它是投资方案服务的调用方，不是“例外核心企业批量导入”的实现归属仓库。强行在当前仓库新增 Controller/Service/DTO 会造成跨服务边界污染。

### PT-029：no-edit 停止路径缺少 todo/git_diff 收束

现象：

- T-074 复跑选择了正确方向：不强行修改。
- 但 prompt 明确要求“必须维护 todo”和最终 `git_diff`，模型没有执行。
- 对无改动停止来说，这不是业务风险，但会降低可审计性：用户无法从工具结果直接看到“确实没改”。

OMP 对应思路：

- OMP 会持续把当前任务、todo、工具证据和 runtime steering 放回上下文。
- 当任务进入停止/收束阶段时，runtime 可以通过 tool-choice / steering 要求模型先完成必要观察，再最终回答。

LCA 措施：

- T-075 已补 no-edit final hygiene：当实现任务选择“无法安全实现/目标服务缺失/证据不足”时，如果尚未做 git/todo 收束，runtime 会追加 steering，并临时只开放 `todo_read` / `todo_add` / `todo_update` / `git_status` / `git_diff`。
- 测试覆盖：provider context 会注入 `[No-edit final hygiene]`；过早 no-edit final 会被 steering 到 `todo_add` + `git_status` 后再最终回答。
- 如果后续提供 `zqyl-investment-plan` 路径，应把它作为主 `--cwd` 或 `--allow-dir`，继续真实实现压测。

## T-084 qwen3-coder-next 只读源码验证压测

压测时间：2026-07-09

| 项目 | 内容 |
|---|---|
| Provider / model | `bailian / qwen3-coder-next` |
| LCA session | `20260709T071219747931Z` |
| session JSONL | `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-lca-t072-worktree-5a7a7365d7ed/sessions/20260709T071219747931Z.jsonl` |
| 主 workspace | `/Users/chengming/mycode/project/crcl-open-lca-t072-worktree` |
| allowed-dir 1 | `/Users/chengming/mycode/project/crcl-open-lca-t072-worktree/deployMessage/YXK-397 云信通用优化25.1` |
| allowed-dir 2 | `/Users/chengming/mycode/project/zqyl-user-center-service` |
| 是否只读 | 是，显式 deny `shell/run_tests/apply_patch/write_file/memory_write/rollback_patch/git_status/git_diff` |

运行命令核心：

```bash
./agent --provider bailian \
  --approval-mode yolo \
  --tool-approval shell=deny,run_tests=deny,apply_patch=deny,write_file=deny,memory_write=deny,rollback_patch=deny,git_status=deny,git_diff=deny \
  --cwd /Users/chengming/mycode/project/crcl-open-lca-t072-worktree \
  --allow-dir "/Users/chengming/mycode/project/crcl-open-lca-t072-worktree/deployMessage/YXK-397 云信通用优化25.1" \
  --allow-dir /Users/chengming/mycode/project/zqyl-user-center-service \
  --budget-seconds 600 \
  --context-char-budget 12000 \
  "这是一次使用 qwen3-coder-next 的真实企业项目只读压测..."
```

Run summary：

| 指标 | 值 |
|---|---:|
| termination_reason | `final` |
| elapsed_ms | 153338 |
| llm_requests | 35 |
| tool_calls | 78 |
| tool_errors | 6 |
| compactions | 33 |
| llm_context_summaries | 18 |
| synthetic_tool_results | 0 |
| useless_tool_results | 0 |
| guard_hits | 0 |
| steering_counts | 0 |

Tool counts：

| 工具 | 次数 |
|---|---:|
| `read_file` | 54 |
| `list_files` | 10 |
| `search_code` | 7 |
| `todo_add` | 4 |
| `todo_update` | 2 |
| `todo_read` | 1 |

压测结果：

| 观察点 | 结果 | 判断 |
|---|---|---|
| 模型切换 | `qwen3-coder-next` 连通性测试返回 `OK` | 通过 |
| 需求/上线线索读取 | 先读取 `YXK-397 云信通用优化25.1/sql/表结构变更.sql` | 通过 |
| 源码定位 | 定位 `IntentionConfig.java`、`IntentionMethod.java`、`IntentionExemptCompany.java`、`IntentionConfigMapper.xml`、`IntentionConfigManagerController.java` 等 | 通过 |
| 辅助项目判断 | 读取 user-center 的 `READ.md`、`工作量统计_涉及表.md` 等，判断暂不需要 user-center 直接配合 | 基本通过 |
| 只读约束 | 没有写文件、没有跑 shell、没有跑测试、没有 git 操作 | 通过 |
| 任务收束 | 最终正常输出 6 段回答，没有 budget 停止 | 通过 |
| 工具效率 | 78 次工具调用，`read_file` 54 次，多次重复读同一批文件 | 不理想 |
| todo 使用 | 首次 `todo_add` 用错参数 `key/content`，后续 `todo_update` 用错 id | 不理想 |
| 最终回答准确性 | 能给出大方向，但把“项目表”写成“表名表”，并对 `IntentionConfigApplication` 做了“Spring Boot 启动类/配置类”的过度断言 | 待改进 |

### PT-030：todo 工具 schema 仍容易被模型误用

现象：

- 首次 `todo_add` 调用参数为 `key/content/status`，工具返回 `Missing required argument(s): id, task.`
- 后续 `todo_update` 又出现 `Todo not found: step1` 和 `Todo not found: in_progress`。
- 模型最终能继续完成任务，但 todo 状态没有成为可靠的任务台账。

OMP 对应思路：

- OMP 的 todo 是高频状态工具，工具描述、schema、系统提示和 UI 都应让参数名稳定可见。
- 当模型传错参数时，工具错误应尽量给出可直接复制的正确调用形态。

LCA 措施：

- T-085 已完成：`todo_add` / `todo_update` 兼容 `key -> id`、`content -> task`，成功结果会提示下次使用规范参数。
- 缺参、未知 id、无更新字段错误会返回可直接照抄的正确调用示例；未知 id 还会列出当前已知 todo id，避免模型继续猜错。

### PT-031：只读源码验证中重复读取过多但未触发收束

现象：

- 本轮总计 78 次工具调用，其中 `read_file` 54 次。
- `表结构变更.sql`、`IntentionConfig.java`、`IntentionConfigManagerController.java`、`IntentionConfigMapper.xml` 等被多次重复读取。
- `guard_hits` 和 `steering_counts` 均为 0，说明现有 repeated read guard 没有覆盖“同一路径整文件重复读”的场景。

OMP 对应思路：

- OMP 不只靠 exact duplicate guard，而是把工具结果可用性、superseded/useless pruning、soft escalation 和当前任务收束结合起来。
- 当同一路径已有足够 evidence 时，runtime 可以把重复读取转为“已读过，请基于已有证据回答”的 steering，而不是继续消耗预算。

LCA 措施：

- T-086 已完成：增加 evidence-aware read repetition guard。只读/分析任务中，对同一路径同范围成功读取多次后，后续重复读取会返回带 evidence 摘要的 tool error，并触发 final-answer steering。
- 编辑任务不启用该 guard，避免影响实现前必要读取；后续压测再观察是否需要更强的“evidence sufficient” final-answer steering。

### PT-032：最终回答存在轻微结构漂移和证据过度断言

现象：

- 用户要求“必须关注/可能关注/暂不关注项目表”，模型输出列名是“表名”，范围从项目退化为数据库表。
- `IntentionConfigApplication.java` 被表述为“Spring Boot 启动类/配置类”，但这需要进一步核实，不能仅由文件名推断。
- 对部分文件使用“已读取首行，确认存在”这类说法，与实际 `read_file` 工具读取全文/片段的证据表达不一致。

OMP 对应思路：

- OMP 会持续注入当前任务 contract 和 runtime evidence，并让最终回答区分 verified fact / inference。
- 更成熟的 reviewer 或 final-answer check 可以在输出前检查：表头是否符合用户要求、路径是否来自证据、推断是否明确标注。

LCA 措施：

- T-087 已完成：增强 final structure / evidence hygiene。项目范围表必须包含项目/服务列；用户要求证据状态或回答含推断性表达时，final gate 会要求已验证/推断标签。
- T-088 仍保留给 read-only evidence gate：解决“最终回答前必须先找代码证据，而不是先给推测型答案”的问题。
