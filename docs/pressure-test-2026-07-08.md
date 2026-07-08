# P7 综合压测记录（2026-07-08）

本文记录 2026-07-08 对 `local-coding-agent` 的综合压测结果。它是开发过程事实源，服务于继续实现 LCA，不是 LCA 运行时项目 memory。

## 压测范围

| 项目 | 结果 | 依据 |
|---|---|---|
| 百炼模型连通性 | 通过 | `./agent --provider bailian ... "只回答 OK"` 返回 `OK`，session `20260708T024039368221Z`。 |
| LCA 自身只读综合压测 | 初次暴露重复工具循环，修复后复测通过 | session `20260708T024203733199Z` 成功调用 `todo_add/list_files/read_file/search_code/lsp_symbols`，随后重复 `search_code` 和 `todo_read`，最终由 `budget_seconds=240` 停止。修复后 session `20260708T025519414693Z` 按要求完成工具调用并输出五句总结。 |
| 企业项目本地扫描 | 通过 | `/Users/chengming/mycode/project` 下发现 `zqyl-user-center-service` 和 `crcl-open/crcl-open` 两个 Java 企业项目，合计约 7888 个 Java/XML/YAML 文件。 |
| 企业项目联网 LCA 压测 | 当前 Codex 环境阻断代跑 | 用户已明确确认可发给百炼；2026-07-08 代跑 `./agent --provider bailian --cwd /Users/chengming/mycode/project/crcl-open/crcl-open --allow-dir /Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化 ...` 时，sandbox 内先因网络 DNS 失败停止，随后按要求申请外部执行被当前 Codex 执行环境策略拒绝。LCA 产品设计本身不内置“企业数据不能外发”禁令。 |
| 企业项目本地只读扫描 | 通过 | 已对 `zqyl-user-center-service` 和 `crcl-open/crcl-open` 做本地 `find` / `rg` 扫描，不调用模型、不外发内容。 |
| 外部需求目录 multi-root 场景 | 本地确认 | `/Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化` 包含 `HERMES-BORROW.md`、`CODE-HANDOFF.md`、两个需求文档和原型 HTML，适合用 `--allow-dir` 验证需求到代码映射。 |

## 真实项目结构判断

| 项目 | 结构 | 适合压测的能力 |
|---|---|---|
| `zqyl-user-center-service` | Maven 多模块项目，根 `pom.xml` 聚合 `usercenter-api`、`usercenter-mapper-parent`、`user-basic-*`、`usercenter-biz-*` 等模块。 | 多模块 Java 结构理解、Mapper/API/service 链路搜索、轻量 LSP Java symbols/references、跨目录需求读取。 |
| `crcl-open/crcl-open` | Spring Boot 单体项目，主类 `com.yljr.crcl.StartApplication`，含 MyBatis、RocketMQ、Redisson、MongoDB、EasyExcel、deploy SQL/Nacos/gateway 历史材料。 | 大型业务单体搜索、Controller/Application/Domain/Mapper 落点推断、SQL/部署材料关联、长上下文 compaction。 |

## 企业项目本地只读扫描结论

| 项目 | 本地扫描结果 | 初步判断 |
|---|---|---|
| `zqyl-user-center-service` | 根 `pom.xml` 声明 Java 8 Maven 聚合模块；本地统计约 5309 个 Java/XML/YAML 文件，约 1501 个 Controller/Service/Mapper 类或 XML。对“例外核心企业、息费模型、导入覆盖、模板下载”等强特征词未命中，对“结算单、核心企业”等泛词有大量命中。 | 更像用户中心/企业基础信息/认证/Mapper 服务；“例外核心企业批量导入”业务落点可能不在该仓库，若涉及企业校验则可能复用企业查询、企业类型、认证状态相关 API。 |
| `crcl-open/crcl-open` | 本地统计约 2584 个 Java/XML/YAML 文件，约 481 个 Controller/Service/Mapper 类或 XML。命中 `ChargeFeignApi` 注释“例外供应商 > 例外核心企业 > 投资方案”、`ChargeRateAuditDto` 息费类型、`TradeBgManagerController` 投资方案贸易背景、批量模板下载相关 controller/application。 | 更接近“投资方案、例外核心企业、息费模型、模板下载/导入”需求语义，后续应优先在该项目继续追 `charge`、`investment`、`tradebg`、`limit` 相关链路。 |
| 需求目录 | `需求文档-例外核心企业批量导入V1.1.md` 明确：投资方案管理的企业名单模块新增模板下载和导入覆盖；校验产品类型，云信保理校验额度方案确权方，通用资金方校验已认证且企业类型包含核心企业。`需求文档-拓展服务费结算V1.3.md` 明确：平台费用下新增拓展服务费结算，筛选直接保理、已放款、拓展服务费大于 0、待制单订单。 | 后续真实实现任务应先让 LCA 本地读取需求，再在 `crcl-open` 做更窄的只读定位；如果必须联网模型分析，需换成企业允许的数据路径、本地模型或脱敏样本。 |

## 2026-07-08 企业需求本地只读定位结果

本节是不调用百炼、不外发内容的本地 `rg` / `sed` 读码结果，用于替代当前 Codex 环境无法代跑的联网只读压测。

| 需求 | 读到的需求要点 | `crcl-open` 候选落点 | 判断依据 | 仍需确认 |
|---|---|---|---|---|
| 例外核心企业批量导入 V1.1 | 投资方案管理的企业名单模块新增 `模板下载`、`导入覆盖`；Excel 表头为 `核心企业名称`；导入后按规则去重并覆盖当前模型企业名单；云信保理校验确权方，通用资金方校验企业已认证且类型包含核心企业；失败原因包括未认证、企业类型不包含核心企业、已在其他模型维护、列表重复、不为额度方案确权方。 | `src/main/java/com/yljr/crcl/open/interfaces/facade/config/IntentionConfigManagerController.java`、`src/main/java/com/yljr/crcl/open/application/config/IntentionConfigApplication.java`、`src/main/java/com/yljr/crcl/open/interfaces/facade/config/req/AddIntentionConfigReq.java`、`UpdateIntentionConfigReq.java`、`ExemptCompanyDto.java`、`src/main/resources/mapper/crcl/IntentionExemptCompanyMapper.xml`、`src/main/java/com/yljr/crcl/open/application/feign/charge/ChargeFeignApi.java`。 | Controller 已有 `/addIntentionConfig`、`/updateIntentionConfig`、`/getIntentionConfigById`；Application 已有 `saveExemptCompanies` / `updateExemptCompanies`；但当前请求 DTO 只接受 `companyId`，没有上传文件、导入结果、失败原因结构；Mapper 表为 `T_CRCL_INTENTION_EXEMPT_COMP`；`ChargeFeignApi` 注释已有“例外供应商 > 例外核心企业 > 投资方案”的息费取值链路。 | 前端真实菜单“投资方案管理/息费信息配置/例外核心企业清单”对应的是 `IntentionConfig` 还是另一个 investment/charge 模块；Excel 解析、下载中心、企业认证/类型/额度方案确权方校验应复用哪些 feign API。 |
| 拓展服务费结算 V1.3 | 基础服务 -> 平台费用下新增拓展服务费结算；`制单` tab 筛选直接保理、订单状态已放款、订单拓展服务费 > 0、拓展服务费制单状态待制单；支持单笔和批量合并制单；结算单号 `JS-YYYYMMDD-0001` 按日递增；生成 Word；已制单支持下载和回退。 | `src/main/java/com/yljr/crcl/open/application/feign/zqylloan/ZqylLoanFeignApi.java`、`ZqylLoanFeign.java`、`dto/RealChargeFeeDTO.java`、`src/main/java/com/yljr/crcl/limit/domain/po/PlanRate.java`、`src/main/java/com/yljr/crcl/open/base/dict/BillStatusEnum.java`、`src/main/java/com/yljr/crcl/open/application/crcl/service/CrclApplication.java`。 | 代码里能看到 `getRealChargeFee(prjtId, dealType)`、`RealChargeFeeDTO` 中实际保荐商费率/佣金/税额/净额字段、`PlanRate` 中直接保理和保荐商费率字段、`CrclApplication` 多处读取实际费用；但本地搜索未发现现成“拓展服务费结算/制单/结算单号”Controller/Application/Mapper。 | 需求里的“订单状态 60=已放款”和本仓库 `BillStatusEnum` 中 60/70 语义疑似不一致，需要确认使用的是哪个系统/枚举；结算单数据表、Word 模板、下载中心接口是否在另一个仓库或待新建。 |

本次替代压测没有观察到 LCA 模型层面的重复工具循环或工具名漂移，因为真实 LCA + 百炼链路被当前 Codex 宿主策略阻断；以上结论只代表本地搜索/阅读能否定位业务线索。

## 问题清单

| ID | 优先级 | 问题 | 证据 | OMP 对应处理 | LCA 措施 |
|---|---|---|---|---|---|
| PT-001 | P0 | 模型可能进入重复工具调用循环，最终只靠 `budget_seconds` 截断，拿不到最终总结。 | 初次 LCA 自身压测中重复调用 `search_code summary_mode`、`search_code lsp`、`todo_read`、搜索不存在的 `lsp_workspace_symbols/lsp_document_symbols`，最终输出 `Stopped after reaching budget_seconds=240.`；修复后 session `20260708T025519414693Z` 按要求收尾。 | OMP 主循环不靠步数终止，但到处检查 deadline，并用 abort signal、synthetic tool result 保持 tool_call/tool_result 配对；对 soft tool requirement 设置 `MAX_SOFT_TOOL_ESCALATIONS=3` 防止特定强制工具子循环无限扩张；工具结果可标记 `useless`，compaction/pruning 可丢弃无信息或被 supersede 的结果。源码依据：`packages/agent/src/agent-loop.ts`、`packages/agent/src/compaction/pruning.ts`。 | 已在 runtime 层加入“最近窗口内同名同参工具调用熔断”；并补 OMP 风格轻量 pruning：`ToolResult.useless`、空搜索/LSP 结果 useless 标记、provider-bound useless/superseded 工具结果折叠、open todo runtime reminder。 |
| PT-002 | P0 | 企业源码/需求发送到三方 AI API 需要明确数据外发边界。 | 用户已确认可发给百炼；本次 Codex 执行环境策略拒绝代跑该联网压测，因为会把真实企业代码和需求内容发送到三方 AI API。 | OMP 的模型 provider 调用天然会把进入上下文的内容发给已配置 provider；它依靠 provider/config、workspace context、工具审批和 permission gate 控制执行行为，但不是“私有代码不外发”的自动保证，也不是默认禁止外发。 | 不绕过当前 Codex 执行环境策略；改为本地只读扫描。用户在自己的允许环境中运行 LCA 时，可按 provider/permission 策略执行联网只读分析。 |
| PT-003 | P1 | 跨项目运行时 token 配置不够顺手。 | 原先 `load_config()` 只加载目标 `--cwd/.env`；当 `--cwd` 切到企业项目时，如果 token 只在 LCA 仓库 `.env`，需要手动 source。 | OMP `AgentOptions` 中 `getApiKey(model)`、`model`、`cwd/cwdResolver` 是分离的：凭据/模型是 runtime 配置，cwd 是项目上下文。源码依据：`packages/agent/src/agent.ts` 和 `packages/agent/src/agent-loop.ts`。 | 已新增 `--env-file`；`./agent` 会自动把 LCA 安装目录 `.env` 注入为 env-file，再加载目标 workspace `.env`。优先级：真实环境变量 > env-file > workspace `.env`。 |
| PT-004 | P1 | LSP 工具命名和用户/模型认知存在缝隙。 | 压测 prompt 提到 `lsp_workspace_symbols` / `lsp_document_symbols`，原先实际工具只有 `lsp_symbols`；模型没有直接调用不存在工具，但反复 `search_code` 查这些字符串。 | OMP 通过 tool registry、tool description、tool discovery、ToolChoiceQueue/soft requirement 让模型看到准确工具集，并在必要时提醒或强制具体工具。 | 已增加只读别名工具 `lsp_workspace_symbols` / `lsp_document_symbols`，均映射到 `lsp_symbols` handler 和 schema；system prompt 已说明 alias 关系。 |
| PT-005 | P1 | compaction 解决上下文长度，但不自动解决目标收敛。 | 强压缩场景下 provider 兼容性已通过；但 LCA 自身压测里 compaction 没崩，模型仍可能重复探索。 | OMP 把 compaction、todo reminder、tool choice、queued steering、deadline/abort 组合使用；compaction 负责“装得下”，runtime steering 负责“跑得稳”。 | 已补重复工具熔断、provider-bound useless/superseded pruning 和 open todo runtime reminder；下一步只在需要时评估更完整的 ToolChoiceQueue / soft tool requirement，而不是只靠更强 summary。 |
| PT-006 | P2 | 大型 Java 企业项目的轻量 LSP 性能和准确度尚未由 Codex 代跑联网实测。 | 本地扫描确认两项目规模约 7893 个 Java/XML/YAML 文件；`crcl-open` 对投资方案/例外核心企业/息费模型更相关，`zqyl-user-center-service` 更像企业基础信息服务。联网 LCA 链路在当前 Codex 执行环境被阻断。 | OMP 更完整地结合工具发现、真实工程工具、subagents/reviewer，以及 compaction/pruning 来承载大仓库任务。 | 用户可在自己的允许环境中运行 LCA 做真实联网只读分析；LCA 已补轻量 pruning / todo steering，后续再评估更完整工程工具，而不是依赖泛关键词搜索。 |
| PT-007 | P1 | “只读分析”仍会在目标 workspace 写入 `.local-agent/sessions/*.jsonl`。 | 2026-07-08 代跑企业项目只读压测时，虽然工具策略禁止写文件和 shell，但 LCA 启动后在 `/Users/chengming/mycode/project/crcl-open/crcl-open/.local-agent/sessions/20260708T053955405637Z.jsonl` 创建了会话文件；目标仓库 `git status` 因此新增 `?? .local-agent/`。 | OMP 将 runtime 状态放在用户 agent 目录下：`~/.omp/agent` 或 profile 目录；session 默认目录由 `getSessionsDir(agentDir)` 加 cwd 编码派生，而不是直接写进目标 repo。项目级 `.omp/` 主要放项目 context/config/rules/skills。源码依据：`packages/coding-agent/src/session/session-paths.ts`、`session-manager.ts`、`docs/config-usage.md`。 | 已完成 MVP 修复：新增 `--state-dir` / `AGENT_STATE_DIR`，默认把 sessions/todos/patch logs 放到用户级 state root 下的 workspace-specific 目录；项目 memory/skills 继续项目本地。 |
| PT-008 | P2 | 目标企业仓库本身存在大量既有未提交修改，压测前后难以靠 `git status` 判断 LCA 是否污染业务文件。 | `git status --short` 在 `crcl-open/crcl-open` 输出大量既有 modified 文件；本次可确认的新 untracked 只有 `.local-agent/`，但业务文件是否本来已脏需要压测前快照。 | OMP 的 task/worktree 能力会更重地管理隔离工作区和 WIP，但普通 CLI 也依赖清晰的 cwd/session/working-tree 状态。 | 后续真实企业压测前先做只读快照：记录 `git status --short` 到 LCA 自己的压测记录，压测后对比；不在目标 repo 写快照文件。 |

## 本次已采取的代码措施

| 措施 | 文件 | 状态 |
|---|---|---|
| 增加重复工具调用熔断 | `src/local_agent/agent.py` | 已完成 |
| 标准化工具调用签名，JSON 参数不同顺序视为同一次调用 | `src/local_agent/agent.py` | 已完成 |
| 增加回归测试，模拟同一工具参数无限重复 | `tests/test_agent.py` | 已完成 |
| 增加跨项目 env-file | `src/local_agent/config.py` / `src/local_agent/cli.py` / `agent` | 已完成 |
| 增加 `ToolResult.useless` 标记 | `src/local_agent/tools/base.py` | 已完成 |
| 空搜索和空 LSP 结果标记为 useless | `src/local_agent/tools/search.py` / `src/local_agent/tools/lsp.py` | 已完成 |
| provider-bound context 折叠 useless / superseded 工具结果 | `src/local_agent/agent.py` | 已完成 |
| 未完成 todo 注入 runtime reminder | `src/local_agent/agent.py` | 已完成 |
| 增加 pruning / todo steering 回归测试 | `tests/test_agent.py` | 已完成 |
| 增加 LSP workspace/document symbols 兼容别名 | `src/local_agent/tools/lsp.py` / `src/local_agent/agent.py` / `tests/test_tools.py` | 已完成 |

## 后续建议

| 顺序 | 任务 | 理由 |
|---:|---|---|
| 1 | 跑完整测试并同步项目管理 Excel | 本次压测问题已转为代码和文档，需要固化基线。 |
| 2 | 在允许环境中复测企业需求链路 | `--state-dir` 已完成；下一次压测应确认目标企业仓库不再出现 `.local-agent/sessions`。当前 Codex 执行环境不能代跑真实企业代码/需求外发；用户本机 LCA 可按用户/provider/permission 策略执行。 |
| 3 | 评估 OMP 风格 ToolChoiceQueue / soft tool requirement | 只有当真实任务仍出现“该用某工具但不用/反复偏航”时再引入更重 runtime steering。 |
