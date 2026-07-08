# P7 综合压测记录（2026-07-08）

本文记录 2026-07-08 对 `local-coding-agent` 的综合压测结果。它是开发过程事实源，服务于继续实现 LCA，不是 LCA 运行时项目 memory。

## 压测范围

| 项目 | 结果 | 依据 |
|---|---|---|
| 百炼模型连通性 | 通过 | `./agent --provider bailian ... "只回答 OK"` 返回 `OK`，session `20260708T024039368221Z`。 |
| LCA 自身只读综合压测 | 初次暴露重复工具循环，修复后复测通过 | session `20260708T024203733199Z` 成功调用 `todo_add/list_files/read_file/search_code/lsp_symbols`，随后重复 `search_code` 和 `todo_read`，最终由 `budget_seconds=240` 停止。修复后 session `20260708T025519414693Z` 按要求完成工具调用并输出五句总结。 |
| 企业项目本地扫描 | 通过 | `/Users/chengming/mycode/project` 下发现 `zqyl-user-center-service` 和 `crcl-open/crcl-open` 两个 Java 企业项目，合计约 7888 个 Java/XML/YAML 文件。 |
| 企业项目联网 LCA 压测 | 当前 Codex full-access 环境已可代跑；单项目和多项目压测均已能收束 | 早期 Codex 宿主不能代跑企业源码/需求外发，后续用户切到 full-access + network enabled 后，Agent 代跑 session `20260708T081827983347Z` 成功调用百炼。单项目压测最终 session `20260708T083312934017` 已验证需求文档前置读取、repeated read-file guard、空搜索词跨路径 guard 和 forced-final 收束。多项目压测进一步暴露 path escape 父目录误用、LSP 空 query 扩散和最终回答结构/证据路径漂移；补强后 session `20260708T085927874078` 已按 6 点结构输出，并读到 `CrclLimitMainBySelectController.limitConstituteAllotImport`、`CrclLimitMainBySelectApplication.limitConstituteAllotImport`、`LimitConstituteAllotImportReq`、`BatchImportConstituteDto` 等真实证据。 |
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

## 用户本机复跑命令

当前 full-access + network enabled 的 Codex 环境已可代跑该联网压测；用户也可在本机允许环境中直接运行：

```bash
cd /Users/chengming/mycode/self/local-coding-agent
./agent --provider bailian \
  --approval-mode yolo \
  --tool-approval shell=deny,run_tests=deny,apply_patch=deny,write_file=deny,memory_write=deny,rollback_patch=deny \
  --memory-consolidation off \
  --budget-seconds 600 \
  --context-char-budget 60000 \
  --summary-mode auto \
  --cwd /Users/chengming/mycode/project/crcl-open/crcl-open \
  --allow-dir /Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化 \
  "这是一次真实企业项目只读压测。请不要修改文件，不要运行 shell，不要运行测试，不要写 memory。目标：读取需求目录中的需求文档，结合当前 crcl-open 项目源码，只读分析‘例外核心企业批量导入’和‘拓展服务费结算’两个需求在代码中的可能落点。请主动使用 todo、list_files、read_file、search_code，并在有帮助时使用 LSP 工具；最后输出：1 项目结构判断；2 每个需求的候选 Controller/Application/Domain/Mapper/Feign/DTO 落点；3 证据文件路径；4 不确定点；5 如果下一步要实现，建议从哪些文件开始读。"
```

运行前后建议在目标项目执行 `git status --short` 并保留输出，用来确认只读压测没有改动业务文件。

## 2026-07-08 企业需求本地只读定位结果

本节是不调用百炼、不外发内容的本地 `rg` / `sed` 读码结果。它最初用于替代早期受限 Codex 环境无法代跑的联网只读压测；当前 full-access 环境已能代跑，但本节仍作为离线基线保留。

| 需求 | 读到的需求要点 | `crcl-open` 候选落点 | 判断依据 | 仍需确认 |
|---|---|---|---|---|
| 例外核心企业批量导入 V1.1 | 投资方案管理的企业名单模块新增 `模板下载`、`导入覆盖`；Excel 表头为 `核心企业名称`；导入后按规则去重并覆盖当前模型企业名单；云信保理校验确权方，通用资金方校验企业已认证且类型包含核心企业；失败原因包括未认证、企业类型不包含核心企业、已在其他模型维护、列表重复、不为额度方案确权方。 | `src/main/java/com/yljr/crcl/open/interfaces/facade/config/IntentionConfigManagerController.java`、`src/main/java/com/yljr/crcl/open/application/config/IntentionConfigApplication.java`、`src/main/java/com/yljr/crcl/open/interfaces/facade/config/req/AddIntentionConfigReq.java`、`UpdateIntentionConfigReq.java`、`ExemptCompanyDto.java`、`src/main/resources/mapper/crcl/IntentionExemptCompanyMapper.xml`、`src/main/java/com/yljr/crcl/open/application/feign/charge/ChargeFeignApi.java`。 | Controller 已有 `/addIntentionConfig`、`/updateIntentionConfig`、`/getIntentionConfigById`；Application 已有 `saveExemptCompanies` / `updateExemptCompanies`；但当前请求 DTO 只接受 `companyId`，没有上传文件、导入结果、失败原因结构；Mapper 表为 `T_CRCL_INTENTION_EXEMPT_COMP`；`ChargeFeignApi` 注释已有“例外供应商 > 例外核心企业 > 投资方案”的息费取值链路。 | 前端真实菜单“投资方案管理/息费信息配置/例外核心企业清单”对应的是 `IntentionConfig` 还是另一个 investment/charge 模块；Excel 解析、下载中心、企业认证/类型/额度方案确权方校验应复用哪些 feign API。 |
| 拓展服务费结算 V1.3 | 基础服务 -> 平台费用下新增拓展服务费结算；`制单` tab 筛选直接保理、订单状态已放款、订单拓展服务费 > 0、拓展服务费制单状态待制单；支持单笔和批量合并制单；结算单号 `JS-YYYYMMDD-0001` 按日递增；生成 Word；已制单支持下载和回退。 | `src/main/java/com/yljr/crcl/open/application/feign/zqylloan/ZqylLoanFeignApi.java`、`ZqylLoanFeign.java`、`dto/RealChargeFeeDTO.java`、`src/main/java/com/yljr/crcl/limit/domain/po/PlanRate.java`、`src/main/java/com/yljr/crcl/open/base/dict/BillStatusEnum.java`、`src/main/java/com/yljr/crcl/open/application/crcl/service/CrclApplication.java`。 | 代码里能看到 `getRealChargeFee(prjtId, dealType)`、`RealChargeFeeDTO` 中实际保荐商费率/佣金/税额/净额字段、`PlanRate` 中直接保理和保荐商费率字段、`CrclApplication` 多处读取实际费用；但本地搜索未发现现成“拓展服务费结算/制单/结算单号”Controller/Application/Mapper。 | 需求里的“订单状态 60=已放款”和本仓库 `BillStatusEnum` 中 60/70 语义疑似不一致，需要确认使用的是哪个系统/枚举；结算单数据表、Word 模板、下载中心接口是否在另一个仓库或待新建。 |

用户本机真实 LCA + 百炼压测已经观察到模型层面的重复工具循环：链路、权限和只读约束均基本有效，但 runtime 在重复工具探索后需要把模型拉回“基于已有证据回答”，不能只给用户一个重复工具硬停信息。

提交 `d140199` 后的复跑 session `20260708T065705459243Z` 表明重复工具硬停问题已明显改善：Agent 建立 todo、使用 LSP/search/read_file，最终输出了结构化分析。但它没有真正读取 `--allow-dir /Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化` 下的需求文档，而是尝试 `list_files {"path":"requirements"}` 并失败。这说明 multi-root 工具权限已经具备，runtime 还需要把具体 allowed-dir 路径明确告诉模型。

提交 `502362a` 后的复跑 session `20260708T070722601499Z` 仍然先调用 `list_files {"path":"requirements"}`，后续也没有读取真实需求文档；它最终输出了答案，但答案本质上是基于代码搜索的反推，而不是“需求文档 + 代码”的映射。该结果说明 roots 只放 system prompt 仍不够，必须像 OMP 的运行时上下文/工具观察一样，在 `list_files` 根目录输出、path-not-found 错误、空搜索结果中持续暴露可访问 roots。

提交 `c47fe98` 后的复跑 session `20260708T072404789287Z` 证明工具观察提示仍然不够：首个 `list_files {}` 已成功返回 roots 信息，但模型没有读取 allowed-dir 中的需求文档，而是直接进入主代码库搜索，并在 `settle|结算` 上重复搜索后由 duplicate-tool steering 收尾。该现象对应 OMP 的 ToolChoiceQueue / soft tool requirement 场景：当任务明确要求先读需求文档时，runtime 不能只提醒，而要先限制可用工具并要求 `read_file` allowed-dir 文档，满足后再释放完整工具集。

提交 `c3e3f17` 后的复跑 session `20260708T073252231781Z` 表明 allowed-dir soft requirement 已生效：前两次工具调用就是读取两个需求 md，后续还读取了 handoff 和原型 HTML。新问题是模型在后半段围绕 `HandleCrclServiceApplication.java` 做大量连续相邻范围读取，最终回答偏离原始 5 点输出要求，只总结了该文件的导出逻辑。该问题属于 OMP 所说的病态工具子循环，不应靠主步数限制，而应设置命名小上限和 runtime steering。

提交 `7468b13` 后的复跑 session `20260708T074609696125Z` 继续证明 allowed-dir soft requirement 生效：开头读取了“例外核心企业批量导入”和“拓展服务费结算”两份需求 md。新问题有两个：第一，用户 prompt 明确“只读压测”，但末尾“如果下一步要实现”命中编辑类排除词，导致同文件连续切片读取 guard 没有启用；第二，最终回答里出现“V1.1 unread”的反事实结论，说明 forced-final steering 需要把本轮已读文件作为 runtime evidence 显式带回模型。按 OMP runtime context / steering 思路，已改为显式只读优先于编辑词，并在重复工具 forced-final 消息中列出已成功 `read_file` 的路径，要求模型不要声称这些文件未读。

提交 `125c4b0` 并切换到 full-access + network enabled 后，Agent 代跑 session `20260708T081827983347Z`：前两步稳定读取两份需求 md；中段通过 LSP/search/read_file 定位到 `CrclLimitMainBySelectController.selectFeePlanList`、`CrclLimitMainBySelectApplication.selectFeePlanList`、`IncentiveFeign.queryFeePlanInfo`、`QueryFeePlanInfoDto/Req`、`HandleCrclController.importBalanceExcel`、`CrclBatchImportReq` 和 `HandleCrclServiceApplication.importBalanceExcel`；后段连续读取 `HandleCrclServiceApplication.java` 相邻范围时 repeated read-file guard 触发，下一轮成功无工具输出最终回答。仍存在一个新问题：最终回答把已经读过的 `QueryFeePlanInfoReq.java` 写成“not yet read”。这说明仅列已读文件还不够，forced-final steering 还需要带原始用户请求摘要，并明确“已读文件不得称未读；若还需深读，只能说明需要补充细节”。已按 OMP runtime context / steering 思路补强。

session `20260708T082703005777Z` 复跑时，模型围绕同一无结果关键词 `exceptionCoreEnterprise` / `ExceptionCoreEnterprise` 在多个子目录之间扩散搜索，绕过了“同参重复”guard。该 run 被中断以避免继续消耗 token。按 OMP 对 useless tool result、pruning 和 soft tool escalation 的处理思路，新增搜索词级 guard：同一搜索词忽略大小写后连续多次 `No matches`，即使路径不同，也会跳过后续搜索并 forced-final。

session `20260708T083312934017` 复跑通过：模型先读两份需求 md，再用组合关键词定位 `HandleCrclServiceApplication.java`、`BatchImportCrclErrorDto.java`、`InvestmentPlanFeignApi.java`、`CrclInvestmentPlanDto.java` 等证据；后续连续读取 `HandleCrclServiceApplication.java` 相邻范围时 repeated read-file guard 触发，最终回答按 5 点结构输出。仍有轻微准确性风险：最终回答中的“Controller 缺失/下一步找 web/controller”需要后续实现前继续用源码验证，但 runtime 收束问题已明显缓解。

多项目压测 session `20260708T084322924403Z` 暴露 path escape 体验问题：模型把主项目 `--cwd /Users/chengming/mycode/project/crcl-open/crcl-open` 误写成父目录 `/Users/chengming/mycode/project/crcl-open`，工具只返回 “Path escapes workspace”，模型未能自我纠正，导致最终仅分析 `zqyl-user-center-service`。已在公共 `resolve_workspace_path` 的越界错误中加入 primary workspace、allowed dirs、resolved path 和“父目录误用时请用 `.` 或精确 `--cwd`”提示。

多项目压测 session `20260708T084714338485Z` 证明 path hint 生效：模型先错用父目录，收到 roots hint 后改用 `.` 进入 `crcl-open`。新问题是 LSP symbol 空 query 扩散：模型连续猜测 `CoreEnterpriseBatchImportService`、`CoreEnterpriseBatchImportApplication`、`CoreEnterpriseBatchImportHandler` 等不存在符号，虽然每次参数不同但都属于同一类低价值空查询。该 run 被中断。已按 OMP soft escalation / useless result 思路增加 LSP symbol 空 query 小上限，连续一批无结果后跳过后续 LSP symbol 查询并 forced-final；一旦 LSP symbol 查询有结果则清空这批空探索计数。

多项目压测 session `20260708T085426840146Z` 已能 path 纠偏并收束，但最终回答只总结最后重读的“拓展服务费结算”需求文档，没有按用户要求输出 6 点结构。已新增 provider-bound `[Current task contract]` runtime context：每次请求都注入当前原始用户请求、最终输出结构约束，并要求最终回答中精确文件路径必须来自工具证据，猜测类名/路径必须标成未验证候选。该设计参考 OMP 将当前任务、runtime state 和 steering 持续放回模型上下文的做法。

最终多项目压测 session `20260708T085927874078` 通过：模型先读取需求目录和两份需求文档，父目录 path escape 后改用 `.`，随后在 `crcl-open` 中通过 search/LSP/read_file 定位 `CrclLimitMainBySelectController.limitConstituteAllotImport`、`CrclLimitMainBySelectApplication.limitConstituteAllotImport`、`LimitConstituteAllotImportReq`、`BatchImportConstituteDto` 等真实批量导入链路；在 `zqyl-user-center-service` 中读到 `VerifyImportBlacklistConstants`、`CustCompanySpare.archivesSettlementBank`、`SaveCompanyBaseRequest`、`CompanyFinceManageServiceImpl` 等相似/支撑线索；最终按 6 点结构输出。剩余准确性边界：模型会基于当前双项目未命中而推断“拓展服务费结算可能需要其他项目/新项目”，这应理解为“当前授权项目内无证据”，不能替代真实架构确认。

session `20260708T092554037057Z` 是 LCA 自身真实小实现压测：在 Evidence Ledger 实现尚未提交、工作区已有 `src/local_agent/agent.py` / `tests/test_agent.py` diff 的情况下，模型完成 todo、list/read/search、`apply_patch dry_run=true`、正式 `apply_patch`、`run_tests` 和 `git_diff`，最终只对 `README.md` 做一行文字优化，测试输出 `Ran 153 tests ... OK`。这次压测证明 Evidence Ledger 后小改主链路仍可跑通，同时暴露两个新问题：第一，模型最初把 `read_file` header `README.md#3988a904` 整串作为 `apply_patch.tag`，导致 dry-run 多次失败，后来才改成纯 hash；第二，`git_diff` 包含压测前已存在的 Evidence Ledger 代码 diff，模型虽然识别出那不是本轮小改，但这种归因依赖推理，后续应考虑 run start baseline 或 patch records 辅助。

session `20260708T094926471758Z` 是 T-069 归因复测：压测前工作区仅有既有未跟踪文件 `review_by_myself.md`；模型按要求调用 `git_status`、`list_files`、`read_file`、`apply_patch dry_run=true`、正式 `apply_patch`、`run_tests`、`git_diff`。最终 `git_diff` 的 `[diff attribution]` 正确列出 `review_by_myself.md` 为 run start pre-existing dirty file，`README.md` 为 this-session apply_patch file，且无 mixed 文件；模型最终回答也正确按 attribution 区分了本轮修改、运行前已有修改和测试结果。新问题是模型选择了低价值 README smoke-test 文案，并且实际 diff 是重复标题加一行 smoke-test 标记，模型最终却概括成 “exactly one insertion”；该临时 README 改动已由 Codex 撤回，压测结论只保留在本文档。

session `20260708T100128250335Z` 是 T-070 复测：压测前仍只有既有未跟踪文件 `review_by_myself.md`；模型按要求读取 README、执行 `apply_patch dry_run=true`、正式 `apply_patch`、`run_tests`、`git_diff`。这次保留的 README 改动是在默认工作流段落补充 `git_diff` 会提供 `[diff summary]` 和 `[diff attribution]`。`git_diff` 实际输出 `[diff summary]` 为 `Total: 1 file(s), +1 -1, 1 hunk(s).`，`README.md: +1 -1, 1 hunk(s).`，并给出 removed/added 片段；`[diff attribution]` 标出 `review_by_myself.md` 为 run start pre-existing dirty file，`README.md` 为 this-session apply_patch file。模型最终回答准确使用了文件数、`+1/-1`、hunk 数、本轮修改、运行前已有修改和无 mixed 文件；`run_tests` 输出 `Ran 157 tests ... OK`。T-070 复测通过，暂未发现新的 runtime 问题。

同时，用户确认当前测试项目可能无法完全覆盖该需求，尤其“拓展服务费结算”可能需要其他服务/项目配合。因此，单仓库压测结论只能说明 `crcl-open` 中的候选前置能力、缺口和跨服务依赖，不能证明完整需求在该仓库内可实现。

## 问题清单

| ID | 优先级 | 问题 | 证据 | OMP 对应处理 | LCA 措施 |
|---|---|---|---|---|---|
| PT-001 | P0 | 模型可能进入重复工具调用循环，最终拿不到总结。 | 初次 LCA 自身压测中重复调用 `search_code summary_mode`、`search_code lsp`、`todo_read`，最终由 `budget_seconds=240` 停止；修复后 session `20260708T025519414693Z` 按要求收尾。用户本机企业压测 session `20260708T062614211387Z` 又在 `feePlan` 搜索上反复探索，触发重复工具硬停且无最终需求落点分析。 | OMP 主循环不靠步数终止，但到处检查 deadline，并用 abort signal、synthetic tool result 保持 tool_call/tool_result 配对；对 soft tool requirement 设置小上限，超过后不继续无限强制同类工具，而是用 runtime steering 收敛；工具结果可标记 `useless`，compaction/pruning 可丢弃无信息或被 supersede 的结果。源码依据：`packages/agent/src/agent-loop.ts`、`packages/agent/src/compaction/pruning.ts`。 | 已在 runtime 层加入“最近窗口内同名同参工具调用熔断”；并补 OMP 风格轻量 pruning：`ToolResult.useless`、空搜索/LSP 结果 useless 标记、provider-bound useless/superseded 工具结果折叠、open todo runtime reminder。根据企业压测新增轻量 forced-final steering：重复工具被跳过后注入 runtime steering，下一次 LLM 请求发送 `tools=[]`，强制模型基于已有证据输出最终回答；保留硬停作为兜底。 |
| PT-010 | P0 | 企业需求只读压测中 `feePlan` 搜索循环导致无最终回答。 | session `20260708T062614211387Z` 成功读到 `TradeBgSwitchInfoController`、`CrclLimitMainByChangeApplication`、`ChargeFeignApi`、`QueryFeePlanInfoDto`、`LimitIncentivePo`、多个 mapper 和 DTO；但随后在 `feePlan` + `application/send` 等路径重复搜索，并以重复工具硬停结束。 | OMP 的做法不是把主循环步数调小，而是用 tool-choice/soft requirement 的小上限、queued steering、deadline/abort 和 synthetic tool result 让模型从工具探索切回回答；重复探索应成为“收束信号”，不是直接失败。 | 已实现 MVP 版 duplicate-tool final-answer steering，并新增回归测试：同参工具重复到阈值后，runtime 追加 steering 消息，下一轮不给工具 schema，模型必须返回最终内容。下一步让用户用同一企业压测命令复跑修复版。 |
| PT-011 | P0 | `--allow-dir` 的需求文档没有被稳定前置读取。 | session `20260708T065705459243Z` / `20260708T070722601499Z` 都先猜 `requirements`；session `20260708T072404789287Z` 已执行 `list_files {}` 但仍未 `read_file` allowed-dir 需求文档，而是直接搜索主代码库并靠 duplicate-tool steering 收尾。 | OMP 会把 cwd/project context/rules 放入运行上下文，并用 ToolChoiceQueue / soft tool requirement 做“先提醒，偏离后升级强制，小上限防死循环”的纠偏。多 root 需求任务不能只提示 roots，还要把读取外部需求文档作为前置工具要求。 | 已补第三版：当 prompt 明确提到需求/文档且存在 `--allow-dir` 时，runtime 创建 soft tool requirement；满足前只暴露 `list_files` / `read_file`，并要求 `read_file` allowed-dir 下的候选需求文档；若模型直接回答则重复提醒，超过小上限后明确停止而不是给伪结论。 |
| PT-013 | P0 | 模型可能在同一个大文件上连续读取相邻范围，最终偏离原始任务输出。 | session `20260708T073252231781Z` 已正确读取需求文档，但后半段连续读取 `HandleCrclServiceApplication.java` 大量相邻区间，最终回答只总结该文件里的两个导出方法，没有按用户要求输出项目结构、两个需求落点、证据、不确定点和下一步文件。 | OMP 对病态子循环设置小上限，并用 runtime steering/pruning/deadline 收束，而不是让工具探索无限延伸。工具结果可以被 supersede/prune，重复探索应切回最终回答。 | 已补同文件切片读取漂移 guard：近期同一路径 `read_file` 超过阈值后返回 tool error，注入 final-answer steering，下一轮 `tools=[]`，要求回到原始输出结构并基于已收集证据回答。 |
| PT-014 | P0 | 显式只读任务可能因“下一步实现建议”等措辞误关同文件读取 guard，且最终回答遗忘已读需求文档。 | session `20260708T074609696125Z` 开头已读取两份 allowed-dir 需求 md，但后续重复探索 DTO/大文件；最终回答错误写出“Requirement doc V1.1 unread”。根因是只读 prompt 中包含“如果下一步要实现”，触发编辑类排除词，导致 read-file drift guard 未开启。 | OMP 的做法是把用户当前硬约束和 runtime state 放在持续上下文里，并用 steering/tool-choice 小上限让模型回到原始任务；显式 permission/readonly 语义不应被后续普通业务词覆盖。 | 已改为显式只读/不要修改文件/不要写文件等文件级只读词优先于编辑词；同时 forced-final steering 会列出本轮已成功读取的文件路径，要求模型不要声称这些文件未读。新增回归测试覆盖“只读压测 + 下一步实现”场景。 |
| PT-015 | P0 | forced-final 后仍可能把已读文件列为“未读/下一步待读”。 | session `20260708T081827983347Z` 中 `QueryFeePlanInfoReq.java` 已成功 read_file，但最终回答仍写出 “not yet read”。同时最终回答虽有结构化内容，但没有完全按用户要求的 5 点结构展开。 | OMP 会把当前任务、runtime state、tool evidence 和 steering 一起放回上下文，并用 tool-choice/forced-final 明确模型下一步必须回答什么，而不是只提醒“不要继续读”。 | 已补强 forced-final steering：注入原始用户请求摘要；已读文件列表后追加“不得声称未读，如仍需实现级深读，应说明已读但缺少哪些细节”。测试覆盖该 steering 内容。 |
| PT-016 | P0 | 同一空搜索词跨路径扩散会绕过同参重复 guard。 | session `20260708T082703005777Z` 中模型反复搜索 `exceptionCoreEnterprise` / `ExceptionCoreEnterprise`，路径从全仓到 `src/main/java`、`application`、`domain`、`base` 等多级目录变化，因此同参重复 guard 没触发，最终被人工中断。 | OMP 会标记 useless tool result，并通过 pruning、tool-choice/soft escalation 小上限把模型从低价值工具探索拉回回答。 | 已新增搜索词级 guard：同一 `search_code` pattern 忽略大小写并归一空白后，多次无结果会跳过后续搜索、注入 forced-final steering，并要求基于已收集证据回答或换真正不同的业务词。session `20260708T083312934017` 未再出现该空搜索扩散并按 5 点结构收束。 |
| PT-017 | P0 | path escape 错误未告诉模型正确 workspace root，导致主项目完全未被检查。 | session `20260708T084322924403Z` 中模型把 `crcl-open/crcl-open` 误写成父目录 `/Users/chengming/mycode/project/crcl-open`，工具仅返回 path escape，最终只分析辅助项目。 | OMP 的 runtime context 会持续提供 cwd/project context，工具观察也会携带可行动纠偏信息。 | 已在公共 path resolver 的越界错误中加入 resolved path、primary workspace、allowed dirs，以及父目录误用时“用 `.` 或精确 `--cwd`”提示；session `20260708T085927874078` 已验证模型能纠正回主项目。 |
| PT-018 | P0 | LSP symbol 空 query 扩散导致低价值探索循环。 | session `20260708T084714338485Z` 中模型连续猜测 `CoreEnterpriseBatchImportService/Application/Handler/Job/Task/Controller/Dto/...`，每个 LSP 查询都无结果但参数不同，绕过同参重复 guard。 | OMP 对低价值工具探索用 useless result、pruning、ToolChoiceQueue/soft escalation 小上限收束，而不是允许无限猜测。 | 已新增 LSP symbol 空 query 小上限：连续一批 `lsp_symbols` / `lsp_workspace_symbols` / `lsp_document_symbols` 无结果后跳过并 forced-final；有命中则清空空探索计数。 |
| PT-019 | P0 | 最终回答可能只总结最后一个文件，或把猜测路径当成证据路径。 | session `20260708T085426840146Z` 最终只总结“拓展服务费结算”需求文档，没有按用户要求的 6 点结构输出；上一轮也曾出现“下一步读不存在 web/controller 路径”的倾向。 | OMP 会把当前任务、runtime state、tool evidence 和 steering 持续放回 provider context，防止长工具链后遗忘当前任务。 | 已新增 `[Current task contract]` provider-bound runtime context：保留原始用户请求和最终输出结构，要求证据文件路径必须来自工具结果，猜测类名/路径必须标成未验证候选；session `20260708T085927874078` 已按 6 点结构输出。 |
| PT-020 | P0 | 长工具链后最终回答仍需要短证据账本支撑。 | Current task contract 已要求证据路径来自工具结果，但模型仍可能在长链路后混淆证据事实、推断、下一步候选。 | OMP 会把 runtime state、tool evidence 和 steering 持续放进模型上下文；证据属于本轮 provider context，不是长期 memory。 | 已新增 Evidence Ledger：runtime 从 `read_file`、`search_code`、LSP、patch、run_tests、git 等工具结果提炼短 evidence records，注入 `[Evidence ledger]` provider context，并写 session `evidence` 事件；session `20260708T092554037057Z` 验证小改闭环通过。 |
| PT-021 | P1 | 模型可能把 `read_file` header 的 `path#tag` 整串误填给 `apply_patch.tag`。 | session `20260708T092554037057Z` 前三次 dry-run 使用 `tag=README.md#3988a904` 失败，第四次改为 `3988a904` 后成功。 | OMP 更倾向通过结构化工具观察和编辑流程减少模型手工解析参数；工具错误应给可行动纠偏。 | 已关闭：`read_file` 显式输出 `tag: <hash>`；`apply_patch` 兼容 `path#tag` / `[path#tag]` 并提取 hash，同时提示模型后续只传纯 hash。 |
| PT-022 | P1 | 脏工作区下 `git_diff` 会混入非本轮改动，最终归因依赖模型推理。 | session `20260708T092554037057Z` 的 `git_diff` 同时包含 README 小改和压测前已有的 Evidence Ledger 代码 diff；模型识别出 agent.py 不是本轮小改，但这不是 runtime 保证。 | OMP 的 task/worktree/session state 能更清楚地区分任务边界和已有 WIP；普通 CLI 也应记录 run start baseline。 | 已关闭 MVP 版：run start 保存 git status/diff baseline；`git_diff` 对照本 session patch records 追加 attribution，按 pre-existing / this-session / mixed / new unattributed 提示分组总结。 |
| PT-023 | P1 | attribution 能区分变更来源，但最终 diff 细节仍可能被模型概括错。 | session `20260708T094926471758Z` 中 `[diff attribution]` 正确区分 `review_by_myself.md` 和 `README.md`，但实际 diff 是重复 README 标题并新增 smoke-test 标记，模型最终说成 “exactly one insertion”；该低价值临时改动已撤回。T-070 复测 session `20260708T100128250335Z` 中模型已准确使用 `[diff summary]` 的 `1 file(s), +1 -1, 1 hunk(s)` 和 attribution。 | OMP 倾向把最终回答建立在更结构化的 runtime state/tool observation 上，并由 reviewer/verification 进一步约束结论；仅有原始 diff 文本仍可能让模型偷懒概括。 | 已关闭并复测通过：`git_diff` 增加 `[diff summary]`，输出文件级 `+N/-M`、hunk 数、hunk header 和 added/removed 片段；回归测试覆盖原失败场景实际为 `+3 -0`，百炼复测验证最终总结可正确引用 summary + attribution。 |
| PT-012 | P1 | 单一企业项目可能无法覆盖跨服务需求，Agent 容易把“本仓库未命中”误读成“需求不存在”。 | 用户确认本次测试项目可能无法完全覆盖需求；`拓展服务费结算` 在 `crcl-open` 里未找到直接 Controller/Application/Mapper，只有 fee plan / incentive feign 等前置线索。 | OMP 依赖明确 workspace/context 和用户提供的项目集合；当需求跨仓库时，应让模型把“缺失证据”和“需要其他项目”作为结论，而不是强行在当前仓库闭环。 | 文档记录该边界；后续真实需求压测应把相关项目也作为 `--allow-dir` 加入，或分轮让 Agent 先输出“当前项目证据 + 需要补充的项目清单”。 |
| PT-002 | P0 | 企业源码/需求发送到三方 AI API 需要明确数据外发边界。 | 用户已确认可发给百炼；早期受限 Codex 环境曾拒绝代跑该联网压测，因为会把真实企业代码和需求内容发送到三方 AI API。切换 full-access + network enabled 后，Agent 已代跑 session `20260708T081827983347Z`。 | OMP 的模型 provider 调用天然会把进入上下文的内容发给已配置 provider；它依靠 provider/config、workspace context、工具审批和 permission gate 控制执行行为，但不是“私有代码不外发”的自动保证，也不是默认禁止外发。 | LCA 不内置“企业数据不能外发”禁令；是否能跑由用户授权、provider、permission 和当前宿主环境共同决定。 |
| PT-003 | P1 | 跨项目运行时 token 配置不够顺手。 | 原先 `load_config()` 只加载目标 `--cwd/.env`；当 `--cwd` 切到企业项目时，如果 token 只在 LCA 仓库 `.env`，需要手动 source。 | OMP `AgentOptions` 中 `getApiKey(model)`、`model`、`cwd/cwdResolver` 是分离的：凭据/模型是 runtime 配置，cwd 是项目上下文。源码依据：`packages/agent/src/agent.ts` 和 `packages/agent/src/agent-loop.ts`。 | 已新增 `--env-file`；`./agent` 会自动把 LCA 安装目录 `.env` 注入为 env-file，再加载目标 workspace `.env`。优先级：真实环境变量 > env-file > workspace `.env`。 |
| PT-004 | P1 | LSP 工具命名和用户/模型认知存在缝隙。 | 压测 prompt 提到 `lsp_workspace_symbols` / `lsp_document_symbols`，原先实际工具只有 `lsp_symbols`；模型没有直接调用不存在工具，但反复 `search_code` 查这些字符串。 | OMP 通过 tool registry、tool description、tool discovery、ToolChoiceQueue/soft requirement 让模型看到准确工具集，并在必要时提醒或强制具体工具。 | 已增加只读别名工具 `lsp_workspace_symbols` / `lsp_document_symbols`，均映射到 `lsp_symbols` handler 和 schema；system prompt 已说明 alias 关系。 |
| PT-005 | P1 | compaction 解决上下文长度，但不自动解决目标收敛。 | 强压缩场景下 provider 兼容性已通过；但 LCA 自身压测里 compaction 没崩，模型仍可能重复探索。 | OMP 把 compaction、todo reminder、tool choice、queued steering、deadline/abort 组合使用；compaction 负责“装得下”，runtime steering 负责“跑得稳”。 | 已补重复工具熔断、provider-bound useless/superseded pruning 和 open todo runtime reminder；下一步只在需要时评估更完整的 ToolChoiceQueue / soft tool requirement，而不是只靠更强 summary。 |
| PT-006 | P2 | 大型 Java 企业项目的轻量 LSP 性能和准确度尚未由 Codex 代跑联网实测。 | 本地扫描确认两项目规模约 7893 个 Java/XML/YAML 文件；`crcl-open` 对投资方案/例外核心企业/息费模型更相关，`zqyl-user-center-service` 更像企业基础信息服务。联网 LCA 链路在当前 Codex 执行环境被阻断。 | OMP 更完整地结合工具发现、真实工程工具、subagents/reviewer，以及 compaction/pruning 来承载大仓库任务。 | 用户可在自己的允许环境中运行 LCA 做真实联网只读分析；LCA 已补轻量 pruning / todo steering，后续再评估更完整工程工具，而不是依赖泛关键词搜索。 |
| PT-007 | P1 | “只读分析”仍会在目标 workspace 写入 `.local-agent/sessions/*.jsonl`。 | 2026-07-08 代跑企业项目只读压测时，虽然工具策略禁止写文件和 shell，但 LCA 启动后在 `/Users/chengming/mycode/project/crcl-open/crcl-open/.local-agent/sessions/20260708T053955405637Z.jsonl` 创建了会话文件；目标仓库 `git status` 因此新增 `?? .local-agent/`。提交 `cb7400d` 后，本地配置解析确认同一 workspace 的默认 state dir 为 `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-crcl-open-966d4fe7a33b`；`git status --short -- .local-agent` 和本地目录检查未发现目标仓库存在 `.local-agent`。 | OMP 将 runtime 状态放在用户 agent 目录下：`~/.omp/agent` 或 profile 目录；session 默认目录由 `getSessionsDir(agentDir)` 加 cwd 编码派生，而不是直接写进目标 repo。项目级 `.omp/` 主要放项目 context/config/rules/skills。源码依据：`packages/coding-agent/src/session/session-paths.ts`、`session-manager.ts`、`docs/config-usage.md`。 | 已完成并本地验证：新增 `--state-dir` / `AGENT_STATE_DIR`，默认把 sessions/todos/patch logs 放到用户级 state root 下的 workspace-specific 目录；项目 memory/skills 继续项目本地。 |
| PT-008 | P2 | 目标企业仓库本身存在大量既有未提交修改，压测前后难以靠 `git status` 判断 LCA 是否污染业务文件。 | `git status --short` 在 `crcl-open/crcl-open` 输出大量既有 modified 文件；旧版本可确认的新 untracked 是 `.local-agent/`。当前版本已把 runtime state 移出目标仓库，但真实联网压测仍需压测前后快照确认业务文件未变化。 | OMP 的 task/worktree 能力会更重地管理隔离工作区和 WIP，但普通 CLI 也依赖清晰的 cwd/session/working-tree 状态。 | 后续真实企业压测前先做只读快照：记录 `git status --short` 到 LCA 压测记录，压测后对比；不在目标 repo 写快照文件。 |
| PT-009 | P1 | memory consolidation 可能让只读压测产生隐式项目 memory 写入。 | 小红新增 `--memory-consolidation off|auto|llm` 后，review 先确认默认 `off` 可避免只读任务额外调用 LLM 或写 memory；随后按 OMP local memory 位于用户 agent dir 的边界，把开启后的默认写入位置从目标 workspace `.local-agent/memory/*.md` 调整为 runtime state dir `memory/*.md`，只有显式 `--memory-scope project` 才写项目 memory。 | OMP/Claude Code 的自动记忆都属于启发式长期上下文，需要与普通 context compaction 区分；OMP local memory 不默认落到目标 repo，默认策略必须避免只读任务隐式写项目文件。 | 已接受并落地：默认 off；开启后默认 `--memory-scope state`；坏 JSON、空结果、deadline 耗尽、本轮已显式写 memory 时不写；新增 runtime 回归测试覆盖默认 off、默认 state 写入、显式 project 写入。 |

## 本次已采取的代码措施

| 措施 | 文件 | 状态 |
|---|---|---|
| 增加重复工具调用熔断 | `src/local_agent/agent.py` | 已完成 |
| 标准化工具调用签名，JSON 参数不同顺序视为同一次调用 | `src/local_agent/agent.py` | 已完成 |
| 增加回归测试，模拟同一工具参数无限重复 | `tests/test_agent.py` | 已完成 |
| 重复工具命中后强制下一轮最终回答 | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| allowed-dir 路径注入模型上下文 | `src/local_agent/agent.py` / `src/local_agent/tools/files.py` / `tests/test_agent.py` | 已完成 |
| allowed-dir 路径注入工具观察 | `src/local_agent/tools/search.py` / `tests/test_tools.py` | 已完成 |
| allowed-dir 需求文档 soft tool requirement | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| 同文件连续切片读取漂移 guard | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| 显式只读优先于编辑词，并在 forced-final steering 注入已读文件证据 | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| forced-final steering 注入原始请求和已读文件一致性规则 | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| search_code 空搜索词跨路径 guard | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| path escape roots hint | `src/local_agent/patch/anchored.py` / `tests/test_patch.py` | 已完成 |
| LSP symbol 空 query guard | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| Current task contract / evidence-backed path rule | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| Evidence Ledger | `src/local_agent/agent.py` / `tests/test_agent.py` | 已完成 |
| 增加跨项目 env-file | `src/local_agent/config.py` / `src/local_agent/cli.py` / `agent` | 已完成 |
| 增加 `ToolResult.useless` 标记 | `src/local_agent/tools/base.py` | 已完成 |
| 空搜索和空 LSP 结果标记为 useless | `src/local_agent/tools/search.py` / `src/local_agent/tools/lsp.py` | 已完成 |
| provider-bound context 折叠 useless / superseded 工具结果 | `src/local_agent/agent.py` | 已完成 |
| 未完成 todo 注入 runtime reminder | `src/local_agent/agent.py` | 已完成 |
| 增加 pruning / todo steering 回归测试 | `tests/test_agent.py` | 已完成 |
| 增加 LSP workspace/document symbols 兼容别名 | `src/local_agent/tools/lsp.py` / `src/local_agent/agent.py` / `tests/test_tools.py` | 已完成 |
| 增加 memory consolidation 默认 off 回归测试 | `tests/test_agent.py` | 已完成 |

## 后续建议

| 顺序 | 任务 | 理由 |
|---:|---|---|
| 1 | 跑完整测试并同步项目管理 Excel | 本次压测问题已转为代码和文档，需要固化基线。 |
| 2 | 用户本机复跑同一企业需求命令，重点看是否既先读取 allowed-dir 需求文档，又在足够证据后按 5 点要求收束 | 当前版本应先读需求 md；即使 prompt 包含“下一步实现建议”，只读 guard 也会生效；如果后续又连续切同一个大文件，runtime 会强制回到最终回答，并把已读文件路径作为证据提示给模型。 |
| 3 | 对跨项目需求增加相关代码项目作为 `--allow-dir` | 用户已确认当前项目可能无法完全覆盖需求；应把 incentive/settlement/用户中心等相关项目纳入只读上下文，或让 Agent 明确输出“需要哪个项目”。 |
