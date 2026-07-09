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
- T-088 已完成：read-only evidence gate 会拦截代码证据/源码/不推测/怎么处理类问题的无证据最终回答，要求先用 `search_code` / LSP 定位并 `read_file` 关键实现文件；search/LSP no-match 负向证据可明确收束。

### PT-033：语义级路径探索扩散

现象：

- 密码加密问答压测 review 中，用户要求“不要推测，找代码证据”后，模型开始大量猜路径。
- 探索模式不是完全相同参数重复，而是同一模块/父目录下的相似 `list_files`、父子目录扩散和多次 `Path not found`。
- 既有 exact duplicate guard 只能挡完全同参调用，触发太晚，无法及时拦住语义重复探索。

OMP 对应思路：

- OMP 对病态子循环不靠主步数限制，而是用 soft escalation、小上限、tool result pruning 和 runtime steering 收束。
- 重复失败或低价值探索应变成“换策略/收束回答”的信号，而不是继续猜路径。

LCA 措施：

- T-089 已完成：新增 semantic exploration guard。`list_files` 会按模块/父目录归一语义探索 key，同一模块或同一 Path-not-found 父路径超过小上限后跳过目录猜测。
- 命中后 runtime 会追加 steering，并临时只开放 `search_code` / `read_file` / LSP 证据工具，要求模型回到命中文件证据或基于已有证据收束回答。

### PT-034：终端输入混入工具日志

现象：

- 密码加密问答压测 transcript 中出现 `33333333333[tool:start] ...`。
- 根因不是模型或工具协议，而是一次性 CLI 运行期间 stdin 仍处于普通 TTY echo 状态，用户误敲字符会被终端直接回显到同一个 transcript。

OMP 对应思路：

- OMP 将 runtime event、TUI renderer、permission prompt 和用户输入分层处理，用户输入不会直接和工具事件混在同一条输出流里。
- LCA 当前不引入完整 async command bus，但应先把“运行中普通输入”和“真正需要用户确认/回答的输入”分开。

LCA 措施：

- T-090 已完成：新增 `terminal_io`，一次性 CLI、REPL 和 terminal chat 在 `runtime.run()` 期间临时关闭 TTY echo。
- approval / ask_user 进入真实输入阶段时会临时恢复 echo，并 flush 运行期间误敲的输入缓冲；结束后重新静默，最终退出时恢复终端原状态。

### PT-035：Vue 模板改动被 comment-only reviewer 误判风险

现象：

- 外部 review 指出 implementation-quality reviewer 的 `_looks_like_comment_line` 使用全局规则，可能把 `.vue` `<template>` 中的 `<p>` / `<li>` 等模板 markup 当成注释-only。
- 这会导致真实 Vue UI/模板改动被误报为“只改注释/文档”，影响后续实现质量判断。

OMP 对应思路：

- OMP 的 reviewer / tool result 判断会结合工具类型、文件类型和上下文，不应把不同语言的语法规则混成一个全局判断。
- 对“实现是否有效”的判断应尽量基于 language-aware evidence；没有完整语言能力时，也要避免过度泛化的误报。

LCA 措施：

- T-091 已完成：comment-only 判断改为按文件后缀区分。
- JavaDoc markup 如 `<p>` / `<li>` 只在 `.java` 文件中按注释辅助标记处理；`.vue` 模板 markup 不再算 comment-only。
- 新增回归测试：Vue `<p>{{ oldTitle }}</p>` 替换为 `<p>{{ title }}</p>` 不再触发 implementation-quality warning。

### PT-036：`agent.py` 继续膨胀，compaction 关注点未分离

现象：

- 外部 review 按 OMP 架构原则指出：LCA 的 `agent.py` 同时承担主循环、compaction、evidence、memory、steering、startup context、run summary 等职责。
- 这会让后续继续补 LSP、reviewer、ToolChoiceQueue 时越来越难控制回归面。

OMP 对应思路：

- OMP 不是把所有能力塞进一个循环文件，而是把 compaction/tokenizer/context、telemetry/run-collector、LSP clients、tool handling 等拆成独立模块，主循环只负责调度。
- 大型运行时能力应按“一职责一文件”渐进拆分，并用测试守住行为不变。

LCA 措施：

- T-092 已完成第一步渐进拆分：新增 `src/local_agent/compaction.py`。
- 已迁出压缩阈值/reserve、provider-safe 消息清理、tool output pruning、recent message 修剪、summary transcript、LLM summary request formatting 和 summary cache key 等纯函数。
- `agent.py` 保留主循环编排与 runtime 状态；后续继续按低风险边界拆 `evidence.py`、`run_collector.py`、`startup_context.py` 和 `memory_consolidation.py`，暂不一次性重写 Steerer 协议。

### PT-037：Java/Vue 轻量 LSP 与 Python AST 输出无置信度区分

现象：

- 外部 review 指出：Python LSP 使用 AST 真解析，而 Java/Vue 当前只是 regex/delimiter fallback，但工具输出格式没有提示差异。
- 模型可能把 Java/Vue 轻量结果当成完整 LSP server 的精确结论，进而过度断言“无引用/无定义”。

OMP 对应思路：

- OMP 把 LSP 做成独立子系统和语言 client，诊断/定义/引用能力来自实际 language server 或明确的能力边界。
- 如果 LCA 先保留无依赖轻量实现，就必须在工具结果里暴露 confidence，避免模型过度相信 fallback。

LCA 措施：

- T-092 已完成 MVP 修复：Java/JavaScript/TypeScript/Vue 的 `lsp_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics` 命中结果前会追加 `[lsp confidence]`。
- 提示说明 Python 使用 AST，Java/JS/TS/Vue 使用 lightweight regex/delimiter fallback，结果是 best-effort、可能漏报。
- 后续若继续贴近 OMP，再拆 `lsp/clients` provider，并评估可选 tree-sitter / 外部 LSP server adapter。

### PT-038：企业 Java/JS/Vue 项目需要更完整 LSP 证据

现象：

- 用户明确希望 LSP 覆盖 Java、JavaScript、Vue 等主流语言；企业项目代码量大，仅靠 regex fallback 做 definition/reference/diagnostic 容易漏报。
- 继续把完整 LSP server 作为“后续候选”会影响真实需求设计压测中的证据质量，尤其是跨模块调用链、Spring/Vue/TS 项目。

OMP 对应思路：

- OMP 把 LSP 做成真子系统：按语言 client 管理 server 进程、能力、诊断台账和多路复用。
- 关键设计不是“默认强依赖”，而是“可用则增强、不可用时能力边界清楚”，并通过工具结果告诉模型当前证据可靠性。

LCA 措施：

- T-093 已完成：新增 `src/local_agent/lsp/`，实现 stdio JSON-RPC LSP client 和可选外部 adapter。
- 默认 `AGENT_LSP_MODE=auto`：Java 通过 `jdtls`，TypeScript/JavaScript 通过 `typescript-language-server --stdio`，Vue 通过 `vue-language-server --stdio`；支持嵌套项目 root marker。
- `AGENT_LSP_MODE=light` 可强制轻量回退，`AGENT_LSP_MODE=external` 可强制外部 LSP 并在缺依赖时报错；`lsp_status` 用于诊断当前 VM 依赖是否可用。
- 运行时不自动下载 npm/maven/pip 依赖；封闭 VM 需要提前预置，或通过 `AGENT_LSP_*_COMMAND` 指向离线安装路径。

### PT-039：真实企业项目 LSP 可用性压测

压测对象：

- `/Users/chengming/mycode/project/crcl-open/crcl-open`
- `/Users/chengming/mycode/project/zqyl-user-center-service`

压测会话：

- `crcl-open`：`20260709T082448561892Z`
- `zqyl-user-center-service`：`20260709T082459082275Z`
- `zqyl-user-center-service` 精确路径复测：`20260709T082540210824Z`

现象：

- 当前机器有 `mvn` 和 `npm`，但没有 `jdtls`、`typescript-language-server`、`vue-language-server` 命令。
- `lsp_status` 在两个 Maven 企业项目中均正确输出：`auto external with lightweight fallback`，并提示未找到外部 LSP server command。
- `crcl-open` 样本 `IntentionConfigManagerController.java` 可通过 `lsp_symbols` / `lsp_definition` 定位类和方法符号。
- `zqyl-user-center-service` 首轮 `lsp_symbols` 里模型把路径 `interfaces/controller` 误写成 `interfaces.controller`，导致一次 `Path not found`；同轮 `lsp_definition` 使用正确路径成功定位 `Oauth2Controller`，精确路径复测 `lsp_symbols` 成功定位 `Oauth2Controller`、`authorize`、`getToken`。

OMP 对应思路：

- OMP 的 LSP 能力依赖真实 language server/client 子系统；环境依赖缺失时，工具层应该把可用性和能力边界反馈给模型。
- 对 path-sensitive 工具，OMP 通过持续注入 cwd/project context、工具观察和纠偏机制减少路径漂移；LCA 已有 path-not-found roots hint，但本次是“路径内部字符误写”，不属于越界。

LCA 措施：

- T-094 已完成：真实项目只读压测证明 external LSP adapter 的 availability reporting 和 light fallback 主链路可用。
- 当前不自动安装 jdtls/npm language servers，保持封闭 VM 边界；如果要验证完整外部 Java LSP，下一步应做 T-095：预置/配置 jdtls 并复测。
- 路径字符误写暂记录观察，不立刻加新 guard；已有精确路径复测通过，后续若同类问题在真实需求中重复出现，再考虑给 path-not-found 增加“相似已知路径/拼写纠偏”提示。

### PT-040：jdtls 预置后 strict external Java LSP 复测

环境变化：

- 已通过 Homebrew 安装 `jdtls 1.60.0`，命令路径 `/opt/homebrew/bin/jdtls`。
- Homebrew 同步安装/升级了 jdtls 依赖，包括 `openjdk`、`python@3.14`、`sqlite` 等。
- jdtls workspace cache 位于 `~/Library/Caches/jdtls/`。

压测会话：

- `crcl-open` strict external：`20260709T083446478774Z`
- `zqyl-user-center-service` strict external：`20260709T083459445319Z`
- LSP fallback 合并复测：`20260709T084323683100Z`

现象：

- `lsp_status` 在两个真实企业项目中均能检测到 external `jdtls`。
- `lsp_diagnostics` 在两个真实企业项目中均走 external provider，并返回 `OK`。
- `lsp_symbols` 在两个真实企业项目中均返回 external unavailable：没有找到指定 class symbol。
- 极小临时 Maven 项目验证通过：external `jdtls` 能返回 `Hello` class symbol、definition 和 diagnostics。
- 复测时发现 `close_all_clients()` 关闭 jdtls 可能卡在 pipe close；已在代码中修复为先 terminate/kill 进程，再 best-effort close pipe。
- 进一步用 jdtls server command 探针确认：真实企业项目 `java.project.getAll` 为空、`java.project.listSourcePaths` 为空，说明 jdtls 没有成功导入 Java project。
- 用 Maven 本身验证根因：`crcl-open` 缺 `com.yljr:parent:pom:0.0.5-SNAPSHOT`，`zqyl-user-center-service` 缺 `com.yljr:parent:pom:0.0.4-SNAPSHOT`，因此本机离线 Maven project model 不完整。
- 补协议后复测：LSP client 已发送 `rootPath` / `workspaceFolders`，并响应 `workspace/workspaceFolders`、`workspace/configuration`、`client/registerCapability` 等 server-initiated requests；但缺 parent POM 的企业项目仍无法获得完整 external symbols。
- 运行时策略已改为 OMP 风格多来源收敛：external LSP 有结果时优先使用；external 空结果时在输出中说明 provider 边界，并自动合并 lightweight fallback。session `20260709T084323683100Z` 验证模型能正确说明 external jdtls 可用但本项目回退 fallback。

OMP 对应思路：

- OMP 的 LSP 是长期运行的工程子系统，真实 language server 需要考虑 project import、workspace folders、server-initiated requests、workspace cache、diagnostics ledger、server-specific commands 和超时/关闭。
- 对大型企业 Maven 项目，external LSP 的“server 可启动”不等于“definition/reference/symbols 全部可用”；运行时必须把 provider 能力边界反馈给模型。

LCA 措施：

- T-095 已完成依赖预置与 strict external 复测：当前 external Java diagnostics 可用，小项目 code navigation 可用。
- 已补 LSP 初始化参数和 server request 响应：`rootPath`、`workspaceFolders`、`workspace/workspaceFolders`、`workspace/configuration`、`client/registerCapability` 等。
- 已补 external 空结果 fallback 合并：严格 external 模式下如果 jdtls 空结果但 lightweight fallback 能定位符号，工具会返回 provider failure + fallback evidence，而不是直接失败。
- 真实企业项目 code navigation 的根因是本机 Maven 模型不完整。若要获得完整 type-aware Java navigation，需要补齐公司内部 parent POM / 私服配置 / Maven 本地缓存；在此之前，真实需求分析继续使用 fallback evidence，并保留 `[lsp confidence]`。

### PT-041：Java LSP 韧性按 OMP 继续补齐

用户要求：

- Java 是当前主要语言，LSP 韧性要尽量和 OMP 一样，而不是停留在“jdtls 能启动”。

OMP 对应机制：

- OMP LSP client 会在 initialize 时传 `rootUri`、`rootPath`、`workspaceFolders` 和完整 capabilities。
- OMP 会响应 server-initiated requests，例如 `workspace/workspaceFolders`、`workspace/configuration`、`workspace/applyEdit`、`window/workDoneProgress/create`、`client/registerCapability`。
- OMP 会跟踪 `$/progress`，用 `projectLoaded` 等待语言服务器完成初始项目加载；对慢 server 有超时兜底。
- OMP 不会凭空修复本地缺 Maven 私服/parent POM 的问题；依赖不完整时，type-aware navigation 仍可能无结果，但工具层会把 no-result / useless 状态交给上层收束。

LCA 措施：

- 已新增 `$/progress` 处理：记录 begin/end token，并在初始化后给 project load 一个等待窗口。
- 已增强 `workspace/configuration` 响应：对 Java 返回 Maven/Gradle import enabled 和 `java.configuration.updateBuildConfiguration=automatic`。
- 已保留并扩展 server request 响应：workspace folders、configuration、dynamic registration、workDoneProgress create、showDocument、applyEdit read-only 拒绝。
- 已新增回归测试覆盖 workspace folders 和 configuration 反向请求。
- 真实企业项目 strict external 复测保持稳定：缺 parent POM 时 jdtls diagnostics OK，symbols/definition 自动输出 external 边界并合并 fallback Java 类/方法定位。

剩余边界：

- 若要做到真正 type-aware Java navigation，必须补齐本机 Maven 私服配置、公司 parent POM 和依赖缓存；这是环境条件，不是 Agent 代码可以单独绕过的。
- 后续可选增强：暴露 `lsp_request` / `lsp_capabilities` 调试工具，或在 `lsp_status` 中报告 jdtls project import/source path 状态。
