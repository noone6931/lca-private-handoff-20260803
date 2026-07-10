# Local Coding Agent 项目状态

更新时间：2026-07-09

本文档是开发 `local-coding-agent` 时给参与开发的人和协作 Agent 读取的项目管理基线。`docs/local-coding-agent-project-management.xlsx` 继续作为人工查看的表格视图；本 Markdown 文件作为后续开发时优先读取的项目状态、路线、Todo 和决策来源。它不是 LCA 运行时自己的 memory 或用户项目记忆。

## 最终目标

构建一个个人本地编程助手 Agent，第一阶段只面向个人本地使用，并且可以运行在封闭 VM 中。

目标能力：

- 读取本地代码和文档。
- 搜索本地代码。
- 通过安全、可审计的 patch 修改代码。
- 运行本地命令和测试。
- 生成并展示 git diff。
- 沉淀项目级记忆。
- 只访问指定的 OpenAI-compatible AI API，例如阿里云百炼。
- 不依赖公网搜索，不自动下载依赖，不做远程控制。

第一阶段仍暂不做：

- 多 Agent 并行。
- DAP / LSP 写入类重构能力（rename、code action）。
- Browser 工具。
- 自动联网搜索。
- 远程仓库控制。
- AST 级复杂编辑。

## 当前进度

当前项目已完成 P8 前端协议与交互基础 MVP，P9 真实需求使用准备已覆盖项目边界、源码验证、LSP 韧性和服务费结算链路压测，并进入 P10 Intelligence Runtime 骨架。用户可以用自然语言描述任务，而不是每次手写工具顺序；Runtime 通过 RequirementContract、Planner/Explore、MiniToolChoiceQueue、CompletionAudit 和二阶段 Patch Reviewer 保持任务目标、证据、写入、测试和 diff 的闭环。2026-07-10 的 T-113 新增写后独立审查：它基于已收集的 `git_diff` / tool evidence 检查显式要求的测试是否进入 diff、公开 API 是否检索调用方、低相关或 comment-only patch 是否被处理；发现问题时只开放受控修复、验证和回滚工具，随后才由 CompletionAudit 收口。Runtime 继续产出 typed events 和 `run_summary`，CLI/session/tool 日志和 terminal frontend 共用事件流。

已具备的核心能力：

- Python 标准项目结构：`pyproject.toml`、`src/`、`tests/`、`docs/`。
- CLI 入口：推荐 `./agent`，安装后可用 `local-agent`，源码入口仍是 `python3 -m local_agent.cli`。
- 支持 `bailian` provider，对接阿里云百炼 OpenAI-compatible API。
- Agent Runtime 已支持工具调用循环。
- 工具注册、schema 校验、审批模式已经可用。
- 文件读取、目录浏览、代码搜索、shell、测试、git 状态、git diff、anchored patch、patch rollback、Markdown memory、learn 已经可用。
- `apply_patch` 已支持 `replace`、`insert_before`、`insert_after`，并兼容 Python 3.12。
- 非交互审批、LLM 非 JSON 响应、session 恢复坏尾部、search_code 绝对路径泄漏等问题已经修复。
- 已完成 Agent 自举测试：能够通过百炼模型调用工具读取、修改、测试和查看 diff。
- 测试基线：253 个测试在正常本地环境通过。

当前已具备：

- 仓库已有初始 git commit。
- 长任务已有初版 `--budget-seconds` 墙钟预算，默认 600 秒。
- `max_steps` 默认值为 0，表示不限步；只在用户显式设置时作为安全保险丝。
- 已有 Agent 可维护的 session 级 todo 工具。
- 已有 ask_user 工具，需求歧义时可以主动暂停提问，并支持 `timeout_seconds` / `default_answer`；显式 timeout 会被当前 budget 剩余时间夹紧。
- 审批模型已支持 `always-ask` / `write` / `yolo`，并支持每工具 `allow` / `prompt` / `deny` 和 session 内 always allow / always reject。
- approval prompt 会按 `budget_seconds` 剩余时间等待输入；deadline 到期会取消工具调用并返回 tool error。
- deadline 到期、用户中断工具执行、模型输出 `length` 截断时，会补齐 synthetic tool result，避免 session 留下未配对 tool_calls。
- `apply_patch` 支持 `dry_run=true`，可在不写文件的情况下预览 diff。
- `rollback_patch` 可回滚当前 session 中由 `apply_patch` 写入的补丁，并在回滚前校验当前文件 hash。
- OMP 默认工作流源码依据已固化：system prompt、project prompt、tool registry、tool descriptions、todo reminders、ToolChoiceQueue、agent-loop 的具体实现已写入 `docs/omp-core-architecture-notes.md`。
- LCA 默认工作流已沉到 system prompt 和 runtime workflow reminder：自然语言代码任务会默认先理解、必要时 todo、修改前读取、patch 写入、修改后测试和 diff。
- OMP 风格 auto summary 已落地：默认 `--summary-mode auto`，小历史不摘要，超过 reserve 阈值后调用当前 provider 生成语义摘要，失败回退本地摘要；`local` / `llm` 仍可显式指定。
- LSP / light fallback 工具已落地：`lsp_symbols`、`lsp_workspace_symbols`、`lsp_document_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics`、`lsp_status` 覆盖 Python、Java、JavaScript、TypeScript、Vue；默认 `AGENT_LSP_MODE=auto`，有 root marker 和 server 命令时启用外部 LSP，没有依赖时自动回退轻量静态解析；workspace/document symbols 是兼容别名；Java/JavaScript/TypeScript/Vue fallback 输出会带 `[lsp confidence]` best-effort 提示。
- Multi-root workspace 已落地：`--allow-dir` / `AGENT_ALLOWED_DIRS` 可显式授权额外目录给文件、搜索、LSP 和 patch 工具；system prompt、`list_files` 根目录输出、path-not-found 错误和带 allowed-dir 的空搜索会列出 primary `--cwd` 和 allowed dirs；需求/文档类任务会触发 OMP 风格 soft tool requirement，先要求 `read_file` allowed-dir 文档，再释放完整工具集；shell、git、显式项目 memory/skills 仍锚定 `--cwd`，session/todo/patch logs 和默认 consolidation memory 走 state dir。
- 跨项目 env-file 已落地：CLI 支持显式 `--env-file`，`./agent` 会自动把 LCA 安装目录 `.env` 作为 env-file 加载，使 token/provider 配置与目标 `--cwd` 解耦。优先级是：真实环境变量 > 显式 env-file > 目标 workspace `.env`。
- 用户级 / 项目级常驻上下文已落地：新 session 会读取用户级 `AGENTS.md` 和项目级 `.local-agent/AGENTS.md`，作为 advisory context 注入。
- Sticky rules 已落地：每次发送模型请求前会读取用户级 `RULES.md` 和项目级 `.local-agent/RULES.md`，用于短规则在长会话中保持可见。
- Markdown memory 启动注入已落地：新 session 会读取项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/{project,decisions,conventions,learned}.md` 并作为 advisory context 注入。
- `learn` 工具已落地：可把可复用经验写入 `.local-agent/memory/learned.md`，默认仍按写工具审批。
- Memory consolidation 已落地 MVP：默认 `off`；显式 `--memory-consolidation auto|llm` 后，一轮结束时从 session 中抽取长期 project/decisions/conventions/learned；默认 `--memory-scope state` 写 state dir `memory/*.md`，显式 `project` 才写 `.local-agent/memory/*.md`。
- 已完成 memory consolidation review：默认 `off` 不会额外调用 LLM，也不会写 memory；已补 runtime 级回归测试覆盖默认 off、默认 state scope 和显式 project scope。
- Authored skills discovery 已落地：新 session 会扫描 `.local-agent/skills/<name>/SKILL.md`，只注入 name、description 和 source path，正文按需读取。
- OMP memory / skills / autolearn 设计已核实并形成 LCA 裁剪方案：见 `docs/memory-skills-implementation-plan.md`。
- P7 综合压测记录已落地：见 `docs/pressure-test-2026-07-08.md`。
- 重复工具调用熔断和 forced-final steering 已落地：最近窗口内同名同参工具调用超过阈值会返回 tool error；重复命中后 runtime 会注入 steering，并让下一次 LLM 请求 `tools=[]`，强制模型基于已有证据输出最终回答；连续命中仍有硬停兜底。
- 同文件连续切片读取漂移 guard 已落地：只读/分析类任务中，近期同一路径 `read_file` 超阈值后会返回 tool error，并强制下一轮无工具最终回答，避免长任务偏成“只总结最后一个大文件”；编辑类任务不触发。
- OMP 风格 tool result pruning / todo steering 已落地：空搜索/LSP 结果会标记 useless；发送给模型的上下文会折叠 useless/superseded 工具结果并注入未完成 todo reminder，session 原文仍保留。
- Path escape roots hint 已落地：越界路径错误会返回 resolved path、primary workspace 和 allowed dirs；父目录误用时提示使用 `.` 或精确 `--cwd`。
- LSP symbol 空 query guard 已落地：连续一批 `lsp_symbols` / `lsp_workspace_symbols` / `lsp_document_symbols` 无结果后跳过并 forced-final；有命中则清空该批空探索计数。
- Current task contract 已落地：每次 provider request 注入当前原始用户请求、最终输出结构约束和 evidence-backed path 规则，防止长工具链后只总结最后一个文件或把猜测路径当证据。
- Evidence Ledger 已落地：runtime 从工具结果中央提炼本轮短证据账本，provider request 注入 `[Evidence ledger]`，并写入 session JSONL `evidence` 事件，帮助最终回答区分证据事实和推断。
- T-073 relevance gate / reviewer 已落地：真实 `apply_patch` 写入前会检查目标文件是否已被本轮 `read_file` 读取；代码实现任务若要修改部署/配置类低相关路径且用户未提配置，会返回 tool error 要求重新定位或确认；workspace-root evidence 会进入 Evidence Ledger；`git_diff` 会对本轮 patch 触及低相关路径追加 `[diff reviewer]` 提醒；patch log 对 workspace 内绝对路径归一为相对路径，修正 attribution 对不齐问题。
- T-074 implementation-quality reviewer / safe new-file policy 已落地：`git_diff` 会对本轮 comment-only 代码实现 patch 追加 `[diff reviewer]`，禁止把注释/文档改动包装成行为、校验、解析或测试覆盖变化；`write_file dry_run=true` 可预览新文件 diff，真实创建会写 patch log，`rollback_patch` 可删除本 session 创建的新文件。
- T-075 no-edit final hygiene 已落地：实现任务如果准备以“无法安全实现/当前仓库不包含目标服务/未修改文件”等 no-edit 结论结束，但尚未做 git/todo 收束，runtime 会追加 steering，并临时只暴露 `todo_read` / `todo_add` / `todo_update` / `git_status` / `git_diff`，让停止路径也可审计。
- T-076 Event/Command Protocol v1 已落地：`src/local_agent/protocol/events.py` / `commands.py` 提供 dataclass event/command shape；`AgentRuntime` 支持注入 `EventSink`，并把关键运行事件写入 session JSONL 的 `event_v1`；现有 CLI 仍保持原样输出。
- T-077/T-080 Terminal Frontend MVP 已落地：`./agent`、`./agent --chat`、`./agent chat` 会进入 terminal-native 交互前端；可选 `prompt_toolkit` 负责历史/多行输入，`rich` 负责结构化输出，未安装时降级为普通终端输入输出；支持 `/help`、`/status`、`/tools`、`/approval`；approval prompt 仍是同步 stdin，但会写入 `ApprovalRequested` / `ApprovalResult` 事件。
- T-078 项目边界分析 MVP 已落地：本机 `.local-agent/memory/enterprise-service-boundary.md` 保存企业服务边界，`.local-agent/skills/project-scope-analysis/SKILL.md` 保存只读分析工作流；代码层新增 analysis-only 任务识别、named skill soft requirement、自定义 memory_read 安全读取和 final structure gate，避免“范围分类”被实现任务 no-edit hygiene 带偏，并防止模型只说“ready to output”不输出表格。
- T-081 Claude review 行动计划已落地：见 `docs/claude-review-action-plan-2026-07-09.md`；结论是接受 OMP 架构原则，但不先做 P0 大拆分，优先 run summary/coverage、真实压测和渐进模块化。
- T-082 Run summary / coverage MVP 已落地：每轮结束写 session `run_summary` 和 typed `RunSummary` event，包含终止原因、耗时、LLM 请求数、工具调用/错误/无效结果、synthetic result、compaction、tool counts、guard hits 和 steering counts；`/status` 会显示最近一轮摘要。
- T-083 真实需求范围确认到源码验证压测模板已落地：`docs/real-requirement-pressure-test-template.md` 把“范围判断 → 用户确认 → 源码只读验证 → 实现设计 → 小改压测 → run summary”固化成可复用模板。
- T-084 qwen3-coder-next 只读源码验证压测已完成：session `20260709T071219747931Z` 从 `YXK-397 云信通用优化25.1` SQL 线索定位 `IntentionConfig*` 实体、Mapper、Controller 和 user-center 辅助文档，最终正常收束；新暴露 todo 参数误用、重复读取过多和最终回答轻微结构漂移问题。
- T-085 todo 工具误参纠偏已落地：`todo_add` / `todo_update` 兼容压测中真实出现的 `key -> id`、`content -> task`，成功结果会提示下次使用规范参数；缺参、无更新字段、未知 id 会返回正确调用示例和已知 todo id。
- T-086 evidence-aware read repetition guard 已落地：只读/分析任务中，同一路径同范围成功 `read_file` 多次后，后续重复读取会返回已有 evidence 摘要并触发 forced-final steering；编辑类任务仍不启用该 guard，避免影响实现前必要读取。
- T-087 final structure / evidence hygiene 已落地：final gate 会检查“项目范围表”是否含项目/服务列；当用户要求证据状态或回答中含推断性表达时，会要求输出包含已验证/推断等证据状态标签；Current task contract 也补充了证据事实与推断分离规则。
- T-088 read-only evidence gate 已落地：代码证据/源码/不推测/怎么实现/怎么处理类问题若准备无工具回答且本轮没有成功 `read_file`，runtime 会要求先用 `search_code` / LSP 定位并 `read_file` 关键实现文件；如果 search/LSP 已给出 no-match 且最终明确“未找到代码证据”，允许负向结论。
- T-089 semantic exploration guard 已落地：`list_files` 会按模块/父目录归一语义探索 key，同一模块或同一 Path-not-found 父路径反复扩散超过小上限后跳过目录猜测，并临时只开放 `search_code` / `read_file` / LSP 证据工具，要求模型回到代码证据或收束回答。
- T-090 terminal input/output isolation 已落地：一次性 CLI、REPL 和 terminal chat 在 `runtime.run()` 期间会临时关闭 TTY echo；approval / ask_user 会临时恢复输入并 flush 运行期间误敲的缓冲，减少用户键盘输入混入工具日志。
- T-091 Vue diff reviewer 误报修复已落地：implementation-quality reviewer 的 comment-only 判断改为按文件类型处理，JavaDoc `<p>/<li>` 仅在 Java 中按注释处理，Vue 模板 markup 不再误触 comment-only 警告。
- T-092 compaction 渐进模块化已落地：`src/local_agent/compaction.py` 承载压缩阈值、provider-safe 清理、tool output pruning、recent message 修剪、summary transcript/cache 等纯函数；`agent.py` 保留主循环编排，开始按 OMP 一职责一文件方向降低上帝对象风险。
- T-093 可选外部 LSP adapter 已落地：新增 `src/local_agent/lsp/`，支持 stdio JSON-RPC LSP client、Java `jdtls`、TypeScript `typescript-language-server --stdio`、Vue `vue-language-server --stdio`、嵌套项目 root marker 发现、`AGENT_LSP_MODE=auto|light|external` 和 `AGENT_LSP_*_COMMAND`；运行时不自动下载依赖，外部 server 不可用时回退 light fallback。
- T-094 真实项目 LSP 可用性压测已完成：`crcl-open/crcl-open` session `20260709T082448561892Z` 和 `zqyl-user-center-service` sessions `20260709T082459082275Z` / `20260709T082540210824Z` 已验证当前机器未安装外部 LSP server 命令，`lsp_status` 能正确报告 fallback，Java 样本 `IntentionConfigManagerController` / `Oauth2Controller` 能通过 light fallback 定位符号。
- T-095 jdtls 预置、协议修复和 strict external 复测已完成：已通过 Homebrew 安装 `jdtls 1.60.0`；极小 Maven 项目 external `symbols` / `definition` / `diagnostics` 全通；真实企业项目 `diagnostics` 走 external jdtls 且 OK，但因缺公司内部 parent POM 无法导入 Maven project，external `symbols` / `definition` 为空；已按 OMP LSP client 思路补 `rootPath` / `workspaceFolders` 初始化和 server-initiated request 响应，并在 external 空结果时自动合并 light fallback，避免 Agent 在企业项目中失明。验证 session：`20260709T084323683100Z`。
- T-096 Java LSP 韧性对齐 OMP 已落地：LSP client 会处理 `$/progress` 项目加载进度、等待项目加载窗口、响应 `workspace/configuration` 并返回 Java Maven/Gradle import 配置、响应 `workspace/workspaceFolders` / `window/workDoneProgress/create` / dynamic registration 等 server request；真实企业项目缺 parent POM 时仍会明确 external 边界并合并 fallback evidence。
- T-097 Java project health 探针已落地：`lsp_status` 支持 `probe=true` / `path`，可启动匹配的 external server 并调用 jdtls `java.project.getAll` / `java.project.listSourcePaths`，输出 project count、source path count 和“不完整时检查 parent POM/私服/本地依赖缓存”的行动提示。
- T-098 Maven parent probe 已落地：`lsp_status probe=true` 会静态解析最近 `pom.xml` 的 parent 链，检查 `relativePath` 与本地 `~/.m2/repository` parent POM；真实企业项目已定位 `crcl-open` 缺 `com.yljr:parent:0.0.5-SNAPSHOT`，`zqyl-user-center-service` 缺 `com.yljr:parent:0.0.4-SNAPSHOT`。
- T-099 Maven environment probe 已落地：`lsp_status probe=true` 会报告 `mvn` 可用性、`settings.xml` 是否存在、本地 Maven 仓库位置，以及 mirror/server/profile/repository/activeProfile 数量；不会输出私服 URL、账号或密码。
- T-100/T-101 Java LSP fallback 真实复测与修复已落地：`crcl-open` session `20260709T090748481226Z` 证明 jdtls project incomplete 时，`lsp_symbols` / `lsp_definition` 能从项目根返回 fallback 类/方法证据；fallback 现在会优先扫描文件名/路径匹配 query/symbol 的候选文件，避免大仓库前 300 文件窗口漏掉目标类。
- T-102 拓展服务费结算真实需求链路压测已完成范围判断和源码只读验证：scope analysis 输出 `zqyl-manager`、`zqyl-loan-application`、`ysd-provider` 为主候选；当前本机只有 `crcl-open`、`zqyl-user-center-service`、`zqylpaymentmaster9d423763`、`mpspaymasterce6ca65`，源码验证仅发现弱相关证据，不能进入实现设计，需补主候选源码。
- T-103 provider-safe invalid tool_call normalization 已落地：模型生成空工具名时，assistant message 入历史前会把无效 tool_call 规范成 `__invalid_tool_call` 并保持 tool_result 配对，避免下一轮百炼因空 `function.name` 返回 400。
- T-104 用户确认 msp-pay / zqylpayment 范围后的源码复核已完成：session `20260709T092317272887Z` 以 `zqylpaymentmaster9d423763` 为 cwd、`mpspaymasterce6ca65` 和需求目录为 allow-dir，结论是该前后端范围“部分支持”服务费结算需求，但源码证据只闭环到平台缴费/支付基础能力，仍未找到结算单核心实体、制单/已制单/回退状态机、状态 60 枚举和下载中心接口，暂不应进入小改实现。
- T-105 msp-pay / zqylpayment 窄范围证据补全已完成：session `20260709T092951071920Z` 读取 `PreOrderStatusEnum`、`PlatOrderStatusEnum`、`OrderStatusEnum`、`FeeDetailEntity`、`PlatformOrderController`、`preOrderManagement/list.vue` 等证据，结论是平台缴费/预制单框架可复用，但未找到拓展服务费结算专属实体、状态 60 闭环或完整结算单流程。
- T-106 FinalAnswer Steerer / source-grounded numeric guard 已落地：最终回答相关 read-only evidence、no-edit hygiene、final structure、source-grounded numeric steering 已迁出到 `src/local_agent/steering/final_answer.py`；枚举值、状态码、接口、字段等数字事实若与本轮已读源码不一致，会强制无工具重写。
- T-107 token budget + reserve MVP 已落地：`compaction.py` 新增本地 token 估算和 15% reserve；配置支持 `context_token_budget`、`AGENT_CONTEXT_TOKEN_BUDGET` 和 `--context-token-budget`，token 或 char 任一超阈值都会触发 compaction，字符预算继续作为 fallback。

真实缺口：

- Path-scoped rules 还未实现，作为后续候选。
- Managed skills / autolearn 继续暂缓。
- 企业项目联网压测：当前 full-access + network enabled 环境已可由 Agent 代跑。单项目压测 session `20260708T083312934017` 已按 5 点结构收束；多项目压测连续暴露 path escape 父目录误用、LSP 空 query 扩散和最终回答结构漂移，已分别补 path escape roots hint、LSP 空 query guard、Current task contract；session `20260708T085927874078` 已按 6 点结构输出，并定位 `CrclLimitMainBySelectController.limitConstituteAllotImport`、`CrclLimitMainBySelectApplication.limitConstituteAllotImport`、`LimitConstituteAllotImportReq`、`BatchImportConstituteDto` 等真实证据。T-070 复测 session `20260708T100128250335Z` 已验证百炼模型能正确引用 `git_diff` summary + attribution。
- 用户后来确认“服务费结算”前端大概率是 `msp-pay`、后端大概率是 `zqylpayment`；T-104 复核后更新判断为：这两个项目是合理候选，但当前只证明到平台缴费/支付基础层，后续应先在这两个项目内补齐 entity/mapper/service/router/views 证据，再判断是复用现有能力还是新建结算能力。
- Runtime state 与 workspace 已解耦：`--state-dir` / `AGENT_STATE_DIR` 可指定用户级 state root；默认 `${XDG_STATE_HOME:-~/.local/state}/local-coding-agent/workspaces/<workspace-key>/`；sessions/todos/patch logs 已不再默认写入目标 `--cwd/.local-agent`。显式项目 memory/skills 仍保留在 workspace 中，自动 consolidation 默认写 state dir。
- 已对 `/Users/chengming/mycode/project/crcl-open/crcl-open` 做本地 state-dir 验证：默认 state dir 为 `/Users/chengming/.local/state/local-coding-agent/workspaces/mycode-project-crcl-open-crcl-open-966d4fe7a33b`，目标仓库当前未发现 `.local-agent`。
- 百炼真实只读压测会话 `20260707T093557800154Z` 已验证：在 `context_char_budget=2500` 的强压缩场景下，模型完成指定 5 个工具调用后停止探索，并按要求输出三句话总结。
- LCA 自身综合压测会话 `20260708T024203733199Z` 暴露重复工具调用循环，已用窗口式重复工具熔断缓解；修复后复测会话 `20260708T025519414693Z` 已按要求完成工具调用并输出总结。企业压测 session `20260708T062614211387Z` 又暴露“硬停但无最终回答”，因此新增 forced-final steering。
- 百炼真实小改复测会话 `20260707T094246132064Z` 已验证 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff 主链路可跑通；最终仅新增一个测试 docstring。
- 已有本地 token budget + reserve MVP，但还没有 provider/model 专用 tokenizer；当前 token 估算用于触发 compaction，字符窗口仍保留为 fallback。
- 已有裁剪版 MiniToolChoiceQueue：基于 RequirementContract 和本轮工具摘要，对只读证据任务、需求文档前置读取、写后测试/diff hygiene 做阶段性工具收窄和 runtime steering；这不是完整 OMP ToolChoiceQueue，复杂 reviewer/subagents/多队列调度继续后置。
- T-084 暴露的同路径整文件重复读取已用 T-086 缓解；后续真实压测继续观察是否还需要更强的 evidence-sufficient final steering。
- T-084 暴露的最终回答结构漂移和证据状态缺失已用 T-087 缓解；后续真实压测继续观察是否需要更完整 reviewer。
- 2026-07-09 密码加密问答压测 review 暴露：证据型问题先输出推测已用 T-088 缓解；用户纠正后路径探索扩散已用 T-089 缓解；终端运行中用户输入混入工具日志已用 T-090 缓解，后续继续观察是否需要完整异步 command bus。
- 目标服务接入/真实实现压测仍保留为后续任务：T-075 已补 no-edit 收束规范；后续用户会给项目边界定义，再让 LCA 分析具体需要哪些项目，随后接入目标项目做需求实现设计。
- LSP 目前已支持可选外部只读导航，但不支持 rename / code action / DAP；这些写入类工程能力继续后置。
- Java/JavaScript/TypeScript/Vue 在外部 server 不可用时仍会回退 regex/delimiter fallback，已标注 best-effort；封闭 VM 若需要完整定义/引用/诊断，应预置 `jdtls`、`typescript-language-server`、`vue-language-server` 或通过 `AGENT_LSP_*_COMMAND` 指定离线路径。
- T-095 后本机已有 `jdtls 1.60.0`；真实企业项目缺 `com.yljr:parent:pom`，jdtls 无法建立完整 Java project，因此 external Java `symbols` / `definition` 仍依赖 fallback 合并策略。T-099 已能进一步提示 Maven settings/localRepository/mirror/profile 环境状态；T-101 已增强 query-aware fallback，缓解大仓库根路径查询漏扫目标类的问题。后续若要获得真正 type-aware Java navigation，需要补齐本地 Maven 私服/parent POM/依赖缓存，或在封闭 VM 中预置完整 Maven 仓库。
- `agent.py` 已拆出 `compaction.py` 和 `steering/final_answer.py`，但 Evidence Ledger、run collector、startup context、memory consolidation、semantic exploration steering 仍在主文件内；后续继续按低风险模块边界拆分。
- 完整异步 Command Bus 尚未实现；当前 Terminal Frontend 复用同步 `AgentRuntime.run()`，approval prompt 仍由工具层同步读取 stdin，但已经产生 approval events。后续只有在真实交互压测显示需要取消/并发/远程 UI 时，再升级为完整 async permission command bus。
- provider 请求失败发生在 assistant tool_call 之前，当前会以 `LlmError` 停止；后续可继续优化用户提示。

## 阶段路线图

| 阶段 | 名称 | 状态 | 目标 |
|---|---|---|---|
| P0 | OMP 分析与 MVP 设计 | 已完成 | 明确优先吸收 OMP 成熟设计，并按本地优先、封闭 VM 友好和 MVP 边界做裁剪。 |
| P1 | 基础 Agent Loop | 已完成 | CLI、Provider、Agent Runtime、基础工具、patch、memory、session、测试基线。 |
| P2 | 项目管理与可见性 | 已完成 | 建立 Excel + Markdown 项目状态，让目标、进度、风险、Todo 一目了然。 |
| P3 | 长任务运行基础 | 已完成 | 引入 deadline / budget-seconds、提高 max_steps 兜底值、todo、ask_user、per-tool approval。 |
| P4 | 上下文治理 | 已完成 MVP 版 | 初版 summary / compaction、工具输出折叠、长需求文件工作流。 |
| P5 | 安全与恢复增强 | 已完成并收口 | synthetic tool result、patch preview、回滚策略、非信任仓库提示、OMP 风格 approval model、approval prompt deadline cancel；真实小改复测通过。 |
| P6 | 日用体验与默认工作流固化 | 已完成 MVP 版 | OMP 默认工作流本地化：system prompt、工具描述、轻量 runtime nudge。 |
| P7 | 高级工程能力轻量版 | 已完成 MVP 版 | 已完成 OMP 风格 auto summary、多语言 LSP/light fallback、LSP 兼容别名、multi-root workspace roots 与工具观察提示、allowed-dir soft tool requirement、Markdown memory 启动注入、learn、可选 memory consolidation、authored skills discovery、综合压测记录、重复工具调用熔断、duplicate-tool forced-final steering、同文件切片读取漂移 guard、search_code 空搜索词跨路径 guard、path escape roots hint、LSP 空 query guard、Current task contract、Evidence Ledger、tool result pruning、todo steering、跨项目 env-file、用户级 `--state-dir` runtime state 分层、relevance gate、implementation-quality reviewer、safe new-file policy 和 no-edit final hygiene；path-scoped rules、DAP、subagents、完整 reviewer、AST edit、managed skills 继续后置。 |
| P8 | 前端协议与交互基础 | 已完成 MVP 版 | T-076 已完成 Event/Command Protocol v1、EventSink、CLI stderr renderer 和 session `event_v1`；T-077 已完成 terminal-native frontend，而不是 fullscreen 重 TUI。 |
| P9 | 真实需求使用准备 | 已完成阶段性 MVP | 已完成项目边界分析、真实需求模板、企业项目只读源码验证、Java LSP 韧性、拓展服务费结算链路压测和服务范围复核；后续继续按真实需求推进设计/实现切片。 |
| P10 | Intelligence Runtime 骨架 | 进行中 | 按 OMP 架构原则补单 Agent 内部的目标契约、工具选择队列、完成审计、两阶段计划和 reviewer。当前已完成 RequirementContract、CompletionAudit、裁剪版 MiniToolChoiceQueue、Planner/Explore 两阶段、二阶段 Patch Reviewer 和 provider-safe tool_call 参数清洗；下一步真实小改复测。 |

## 已完成功能

| 能力 | 状态 | 依据 |
|---|---|---|
| 项目骨架 | 已完成 | `pyproject.toml`、`src/local_agent/`、`tests/`、`docs/` 已存在。 |
| CLI | 已完成 | `src/local_agent/cli.py` 提供命令行入口。 |
| 配置加载 | 已完成 | `src/local_agent/config.py` 支持 provider、cwd、approval mode、session、max steps 等参数。 |
| 一键启动 | 已完成 | 仓库根目录 `./agent` 会自动设置 `PYTHONPATH=src` 并启动 CLI。 |
| `.env` / `--env-file` 加载 | 已完成 | 当前 workspace 的 `.env` 可提供 `DASHSCOPE_API_KEY` 等本地配置；`./agent` 会自动加载安装目录 `.env`，也可显式传 `--env-file`。 |
| 百炼 Provider | 已完成 | 支持 `bailian`，默认 OpenAI-compatible endpoint 和 `qwen-plus`。 |
| Agent Runtime | 已完成 | `src/local_agent/agent.py` 实现模型调用、工具分发和循环。 |
| Tool Registry | 已完成 | `src/local_agent/tools/base.py` 管理工具、审批、异常包装。 |
| 文件工具 | 已完成 | `read_file`、`list_files`、`write_file` 已可用，写文件为 create-only。 |
| 搜索工具 | 已完成 | `search_code` 使用 `rg`，输出 workspace 相对路径并做总结果截断。 |
| Shell / Test 工具 | 已完成 | `shell`、`run_tests` 可用，执行类工具需要审批。 |
| Git 工具 | 已完成 | `git_status`、`git_diff` 可用，空 diff 时提示 untracked 文件；`git_diff` 追加 diff summary 和 run attribution。 |
| Anchored Patch | 已完成 | `apply_patch` 使用 tag、line、old_text 校验，并返回 diff。 |
| Patch Preview | 已完成 | `apply_patch dry_run=true` 复用 anchored 校验，只返回 diff，不写文件。 |
| Patch Rollback | 已完成 MVP 版 | `rollback_patch` 只回滚本 session 的 patch 记录，且要求当前文件仍匹配 after tag。 |
| Markdown Memory | 已完成 | `memory_read` 可读取项目级安全命名 Markdown 记忆；`memory_write` / `learn` 仍限制在内置长期记忆桶。 |
| Session | 已完成 | JSONL session 支持继续会话，并处理坏尾部。 |
| 兼容性修复 | 已完成 | patch 读写使用 bytes，避免 Python 3.12 的 `newline` 参数问题。 |
| 错误处理修复 | 已完成 | 非交互审批和 LLM 非 JSON 响应已有明确错误路径。 |
| 时间预算 | 已完成 | `--budget-seconds` / `AGENT_BUDGET_SECONDS` 控制单次任务墙钟预算。 |
| 预算细粒度检查 | 已完成 | LLM 请求和 shell/run_tests timeout 会按剩余预算夹紧，tool 调用后也会检查 deadline。 |
| 不限步主循环 | 已完成 | `max_steps=0` 表示不限步，任务主要靠 `budget_seconds` 控制。 |
| Todo 工具 | 已完成 | `todo_read`、`todo_add`、`todo_update` 维护 session 级任务清单。 |
| 用户澄清工具 | 已完成 | `ask_user` 可在交互式终端中向用户提问，支持超时、默认答案和 budget 上限；显式 timeout 也会被剩余 budget 夹紧。 |
| Per-tool approval | 已完成 | 支持 `always-ask` / `write` / `yolo`、`--tool-approval`、旧白名单兼容映射、config prompt/deny 硬护栏、REPL 工具名校验和 approval deadline cancel。 |
| OMP 核心架构笔记 | 已完成 | `docs/omp-core-architecture-notes.md` 固化 OMP 主循环、deadline、compaction、stepCounter、tool approval、默认工作流分层结论。 |
| OMP 默认工作流源码依据 | 已完成 | 已记录 system prompt、project prompt、tool registry、tool descriptions、todo reminders、ToolChoiceQueue、agent-loop 如何让用户不用指定工具顺序。 |
| 本地 Context Compaction | 已完成 | 超过 `context_char_budget` 时折叠早期历史，保留最近消息和当前用户请求，注入未完成 todo，截断发送给模型的超大 tool 输出，并保持单 system 消息。 |
| OMP 风格 Auto Summary | 已完成 MVP 版 | 默认 `--summary-mode auto`；小历史不摘要，超过 reserve 阈值后调用当前 provider 总结早期历史；失败回退 local summary。 |
| 默认工作流 | 已完成 MVP 版 | system prompt 固化探索、todo、ask_user、patch preview、验证和 diff；runtime workflow reminder 会注入非平凡代码任务。 |
| LSP / Light fallback 工具 | 已完成 MVP 版 | `lsp_symbols`、`lsp_workspace_symbols`、`lsp_document_symbols`、`lsp_definition`、`lsp_references`、`lsp_diagnostics`、`lsp_status` 支持 Python、Java、JavaScript、TypeScript、Vue；可选外部 server，不可用则回退本地静态解析。 |
| Multi-root Workspace | 已完成 MVP 版 | `--allow-dir` / `AGENT_ALLOWED_DIRS` 支持显式授权额外目录给文件、搜索、LSP、patch 工具；workspace roots 会进入模型上下文。 |
| Cross-project Env File | 已完成 MVP 版 | `src/local_agent/cli.py` 支持 `--env-file`；`./agent` 自动加载 LCA 安装目录 `.env`，使 provider 凭据与目标 `--cwd` 解耦。 |
| Runtime State Dir | 已完成 MVP 版 | `--state-dir` / `AGENT_STATE_DIR`；默认写入用户级 state root 下的 workspace-specific 目录。 |
| Startup Context / Sticky Rules | 已完成 MVP 版 | 用户级和项目级 `AGENTS.md` 启动注入；用户级和项目级 `RULES.md` 每次 provider request 前注入。 |
| Markdown Memory 启动注入 | 已完成 MVP 版 | 项目 `.local-agent/memory/{project,decisions,conventions,learned}.md` 和 state dir `memory/*.md` 会作为 advisory context 注入 system prompt。 |
| Learn 工具 | 已完成 MVP 版 | `learn` 写入 `.local-agent/memory/learned.md`，用于显式沉淀可复用经验。 |
| Memory Consolidation | 已完成 MVP 版 | `--memory-consolidation auto|llm` 从 session 抽取长期经验；默认 `off`，开启后默认写 state dir，`--memory-scope project` 才写 `.local-agent/memory/*.md`。 |
| Authored Skills Discovery | 已完成 MVP 版 | `.local-agent/skills/<name>/SKILL.md` 启动时只注入 name、description、source path，正文按需读取。 |
| Memory / Skills 方案 | 已完成设计 | `docs/memory-skills-implementation-plan.md` 明确 Markdown memory 注入、`learn`、skills discovery、managed skills/autolearn 的分阶段方案。 |
| P7 综合压测记录 | 已完成 | `docs/pressure-test-2026-07-08.md` 记录压测证据、OMP 对应机制和 LCA 措施。 |
| 重复工具调用熔断 / forced-final steering | 已完成 MVP 版 | 最近窗口内同名同参工具调用超过阈值时跳过；重复命中后下一轮不给工具 schema，强制模型基于已有证据输出最终回答；连续命中仍有硬停兜底。 |
| Tool Result Pruning | 已完成 MVP 版 | `ToolResult.useless` 支持标记无信息结果；空搜索/LSP 结果标记 useless；发送给模型的上下文会把 useless 和 superseded 工具结果折叠成 notice，session 原文保留。 |
| Semantic Exploration Guard | 已完成 MVP 版 | `list_files` 语义路径按模块/父目录归一计数；同一模块或同一 Path-not-found 父路径探索超过小上限后跳过目录猜测，并 steering 回 `search_code` / `read_file` / LSP 证据工具。 |
| Todo Steering | 已完成 MVP 版 | 未完成 todo 会作为 runtime reminder 注入发送给模型的 system context，即使未触发 compaction 也能帮助模型保持方向。 |
| Evidence Ledger | 已完成 MVP 版 | `src/local_agent/agent.py` 从工具结果提炼短证据记录，注入 provider-bound `[Evidence ledger]`，并写 session `evidence` 事件；测试覆盖 read_file 后账本注入。 |
| Synthetic Tool Result | 已完成 MVP 版 | deadline 到期、用户中断、`finish_reason=length` 时会补齐剩余 tool_call 的 tool result。 |
| Event/Command Protocol | 已完成 MVP 版 | `src/local_agent/protocol/events.py` / `commands.py` 定义 dataclass event/command；Runtime 通过 `EventSink` 产出事件，CLI 通过 `StderrEventSink` 渲染 session/tool 日志；session JSONL 写入 `event_v1`。 |
| Terminal Frontend | 已完成 MVP 版 | `src/local_agent/frontends/terminal/` 提供 append-only terminal frontend；`./agent`、`./agent --chat`、`./agent chat` 可进入交互；支持 `/help`、`/status`、`/tools`、`/approval`；可选 `prompt_toolkit` / `rich` 增强输入和输出，缺失时降级。 |
| Terminal Input Isolation | 已完成 MVP 版 | `src/local_agent/terminal_io.py` 在 agent run 期间关闭 TTY echo；approval / ask_user 通过 `terminal_input_prompt` 恢复输入并 flush 误敲缓冲。 |
| Run summary / coverage | 已完成 MVP 版 | `src/local_agent/agent.py` 在每轮结束产出 `RunSummary` event 和 `run_summary` session 记录；`/status` 展示最近一轮摘要。 |
| CompletionAudit | 已完成 MVP 版 | `src/local_agent/completion_audit.py` 在最终回答前按 RequirementContract 核对验收项、证据项和验证项；证据型只读缺路径/证据状态、实现任务写后缺测试/diff 会触发 `completion_audit` final steerer。 |
| Planner/Explore | 已完成 MVP 版 | `src/local_agent/planner.py` 渲染实现任务阶段上下文；`ToolChoiceQueue` 在实现任务还没有本地代码/需求证据前只开放探索工具，读到证据后再释放写入工具。 |
| 二阶段 Patch Reviewer | 已完成 MVP 版 | `src/local_agent/patch_reviewer.py` 在写后、CompletionAudit 前消费 write / `git_diff` / search/LSP 证据；显式要求测试却没有测试 diff、公开 API 未做写后调用方检索、低相关或 comment-only patch 均会触发 runtime steering，并只开放受控修复/验证/回滚工具。 |
| 测试基线 | 已完成 | 本地正常环境下 253 个测试通过。 |

## 下一步 Todo

| ID | 任务 | 状态 | 优先级 | 说明 |
|---|---|---|---|---|
| T-001 | 确认项目管理基线 | 已完成 | P0 | Excel 已被人工复核，结论可信。 |
| T-002 | 建立 `docs/project-status.md` | 已完成 | P0 | 已将 Excel 内容转成开发协作 Agent 可读 Markdown，作为后续开发基线。 |
| T-003 | 创建初始 git commit | 已完成 | P0 | 初始提交已创建，作为后续开发可回滚基线。 |
| T-004 | 增加 `--budget-seconds` / deadline | 已完成 | P1 | 已支持 CLI、环境变量和配置文件中的墙钟预算。 |
| T-005 | 将 `max_steps` 调整为不限步保险丝 | 已完成 | P1 | 默认值为 0，表示不限步；日常任务预算交给 `budget_seconds`。 |
| T-006 | 增加 todo 工具 | 已完成 | P1 | Agent 可维护 session 级待办、进行中、已完成、阻塞、跳过状态。 |
| T-007 | 增加 ask_user 工具 | 已完成 | P1 | 需求不清时允许 Agent 在交互式终端中暂停并向用户提问。 |
| T-008 | 增加 per-tool approval policy | 已完成 | P2 | 已支持 ask 模式下按工具名免确认，并支持 allow / prompt / deny。 |
| T-009 | 更新 README 安全工作流 | 已完成 | P2 | 已明确 shell 不是沙箱，并补充预算和审批白名单说明。 |
| T-010 | 初版 context summary / compaction | 已完成 | P3 | 已实现本地确定性 compaction；超过字符预算时折叠早期历史并注入未完成 todo。 |
| T-011 | synthetic tool result | 已完成 MVP 版 | P3 | deadline 到期、用户中断和模型 `length` 截断已补齐 tool_call 配对。 |
| T-012 | patch preview / rollback | 已完成 MVP 版 | P4 | 已完成 `dry_run` 预览和 session 级 hash 校验 rollback。 |
| T-013 | 评估 LSP / TUI / subagents / AST edit | 已部分完成 | P5/P7 | 轻量 LSP 已做；fullscreen 重 TUI、subagents、AST edit、DAP 继续后置；轻量 Terminal Frontend 已拆为 T-076/T-077。 |
| T-014 | 固化 OMP 核心架构笔记 | 已完成 | P1 | 已新增 `docs/omp-core-architecture-notes.md`，避免重复翻 OMP 源码。 |
| T-015 | 简化一键启动命令 | 已完成 | P1 | 已新增 `./agent`；支持 `.env` token；默认当前目录为 workspace。 |
| T-016 | 细化 budget deadline 执行检查 | 已完成 | P1 | LLM/tool timeout 使用剩余预算；到期时为未执行工具补 synthetic result。 |
| T-017 | 处理模型输出截断的 synthetic result | 已完成 | P5 | LLM 层已暴露 `finish_reason`，`length` 截断会补齐 synthetic tool result 并停止。 |
| T-018 | ask_user timeout / default | 已完成 | P5 | `ask_user` 支持 `timeout_seconds`、`default_answer`，显式 timeout 也受当前 budget 剩余时间约束。 |
| T-019 | tool_approval allow / prompt / deny | 已完成 | P5 | 支持配置每个工具 allow、prompt、deny；旧 auto approve 白名单兼容映射为 allow。 |
| T-020 | approvalMode / session decision / REPL commands | 已完成 | P5 | 支持 `always-ask` / `write` / `yolo`、session allow/reject always、REPL `/approval` 命令。 |
| T-021 | approval prompt deadline / abort | 已完成 MVP 版 | P5 | approval prompt 使用 deadline-aware timed stdin；deadline 已过或等待超时会取消工具调用，保留 `y/s/n/d` 和 session allow/reject。 |
| T-022 | approval 优先级和工具名校验修复 | 已完成 | P5 | 新 `tools.*` 配置优先于旧顶层字段；config prompt/deny 不被 session allow 绕过；REPL 未知工具名会报错。 |
| T-023 | ask_user timeout clamp / compaction tool truncation | 已完成 | P5 | 显式 `timeout_seconds` 会被剩余 budget 夹紧；recent tool 输出只在发送模型副本中截断，session 原文保留。 |
| T-024 | compaction 保持单 system 消息 | 已完成 | P5 | 压缩摘要合并进首个 system prompt，降低 OpenAI-compatible provider 对多 system 消息的兼容风险。 |
| T-025 | 百炼只读压测后的目标漂移修复 | 已完成 | P5 | 真实百炼压测确认 provider 接受 compaction，但极小上下文预算下模型会被续读提示带偏；已强保留当前用户请求并弱化 read_file 续读提示。 |
| T-026 | 复测百炼只读 compaction 压测 | 已完成 | P5 | 会话 `20260707T093557800154Z` 严格完成 5 个指定工具调用后输出三句话总结，未继续额外读文件。 |
| T-027 | 真实小改任务压测 | 已完成 | P5 | 复测会话 `20260707T094246132064Z` 跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff；最终仅新增一个测试 docstring。 |
| T-028 | 修正 `write_file` schema 描述误导 | 已完成 | P5 | `write_file` 描述已改为 create-only，并新增测试确保描述不再宣称 `fully overwrite`。 |
| T-029 | P5 收口检查 | 已完成 | P5 | README 已补日用模板；项目状态和 Excel 已同步；90 个测试、compileall、xlsx、diff check 通过。 |
| T-030 | P6 取舍评估 | 已完成首轮 | P6 | 已决定优先做 OMP 默认工作流本地化；随后按用户要求补 LLM summary 和轻量 LSP。 |
| T-031 | 固化 OMP 默认工作流源码依据 | 已完成 | P6 | `docs/omp-core-architecture-notes.md` 已新增“OMP 如何让用户不用指定工具顺序”，引用具体源码文件。 |
| T-032 | 固化 LCA 默认工作流 system prompt | 已完成 MVP 版 | P6 | 已把理解、修改、验证、todo、ask_user、patch preview、diff 的默认规则写入系统提示，并用测试覆盖 runtime reminder。 |
| T-033 | 增强工具描述与真实能力一致性 | 已完成 MVP 版 | P6 | 新增 LSP 工具描述；既有 create-only `write_file`、patch dry_run 等描述与实现保持一致并有测试。 |
| T-034 | 实现轻量 runtime workflow nudge | 已完成 MVP 版 | P6 | 非平凡代码任务会注入 runtime workflow reminder；短 prompt 如“只回答 OK”不会注入。 |
| T-035 | 评估 multi-root workspace allow-dir | 已完成 MVP 版 | P6 | 支持读取需求文档目录并修改另一个代码 workspace；`--allow-dir` / `AGENT_ALLOWED_DIRS` 已落地。 |
| T-036 | 实现 OMP 风格 auto summary | 已完成 MVP 版 | P7 | 默认 `summary_mode=auto`，按 reserve 阈值触发 LLM 摘要，空结果或 LLM 错误会回退本地摘要。 |
| T-037 | 实现轻量 LSP 工具 | 已完成并被 T-093 增强 | P7 | 第一版使用 AST/静态扫描提供 Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics；T-093 已补可选外部 LSP adapter。 |
| T-038 | 固化 Memory / Skills 方案 | 已完成 | P7 | 已新增 `docs/memory-skills-implementation-plan.md`，并在 OMP 架构笔记补充 memory backend、learn、managed skills、skills discovery。 |
| T-039 | Markdown memory 启动注入 | 已完成 MVP 版 | P7 | 读取项目 `.local-agent/memory/*.md` 和 state dir `memory/*.md`，并以 advisory block 注入 system prompt，带 source path 和字符预算。 |
| T-040 | 实现 `learn` 工具 | 已完成 MVP 版 | P7 | 把可复用 lesson 写入 `.local-agent/memory/learned.md`，限制长度并清洗会进入 prompt 的字段。 |
| T-041 | Authored skills discovery | 已完成 MVP 版 | P7 | 先扫 `.local-agent/skills/<name>/SKILL.md`，system prompt 只列 name / description / source path，正文按需读取。 |
| T-042 | Managed skills / autolearn | 暂缓 | P7 | 默认关闭，后续按 OMP 风格加入 `manage_skill`，generated skills 与 authored skills 隔离且优先级最低。 |
| T-043 | P7 综合压测记录 | 已完成 | P7 | 新增 `docs/pressure-test-2026-07-08.md`，记录压测证据、OMP 对应机制和 LCA 措施。 |
| T-044 | 重复工具调用熔断 | 已完成 MVP 版 | P7 | 最近窗口内同名同参超过 3 次会返回 tool error；连续命中 8 次停止本轮，避免只靠 budget 截断。 |
| T-045 | 企业项目外发策略确认 | 用户已确认，full-access 已代跑 | P7 | 用户确认可把企业源码/需求发给百炼；早期受限 Codex 环境拒绝代跑，切换 full-access + network enabled 后已由 Agent 代跑 session `20260708T081827983347Z`。 |
| T-046 | 跨项目 env-file / launcher env 加载 | 已完成 MVP 版 | P7 | CLI 支持 `--env-file`；`./agent` 自动加载安装目录 `.env`，让 token 配置与目标 `--cwd` 解耦。 |
| T-047 | OMP 风格 tool result pruning / todo steering | 已完成 MVP 版 | P7 | 已新增 `ToolResult.useless`、空搜索/LSP useless 标记、provider-bound useless/superseded pruning 和 open todo runtime reminder；session 原文保留。 |
| T-048 | LSP workspace/document symbols 兼容别名 | 已完成 MVP 版 | P7 | `lsp_workspace_symbols` / `lsp_document_symbols` 已注册为 `lsp_symbols` 只读别名，减少 OMP/Codex 风格提示迁移摩擦。 |
| T-049 | OMP 风格 runtime state 与 workspace 解耦 | 已完成 MVP 版 | P7 | `--state-dir` / `AGENT_STATE_DIR` 已落地；默认 sessions/todos/patch logs 使用用户级状态目录，避免只读跨项目分析在目标仓库写 `.local-agent/sessions`。 |
| T-050 | 用户级/项目级 AGENTS 与 sticky RULES | 已完成 MVP 版 | P7 | 支持 `AGENT_CONFIG_DIR` 下的用户级 `AGENTS.md` / `RULES.md`，以及 workspace `.local-agent/AGENTS.md` / `RULES.md`；AGENTS 启动注入，RULES 每次 provider request 前注入。 |
| T-051 | Session memory consolidation | 已完成并 review | P7 | `memory_consolidation=off|auto|llm` 和 `memory_scope=state|project` 已落地；默认 off，开启后默认追加到 state dir `memory/*.md`，显式 project 才写 `.local-agent/memory/*.md`；测试覆盖默认 state、显式 project、坏 JSON 不写、默认 off 不额外调用 LLM/不写 memory。 |
| T-052 | 重复工具后强制最终回答 steering | 已完成 MVP 版 | P7 | 用户本机企业压测 session `20260708T062614211387Z` 暴露 `feePlan` 重复搜索后硬停且无最终分析；runtime 现在会在重复工具命中后注入 steering，并让下一次 LLM 请求 `tools=[]`。 |
| T-053 | allowed-dir workspace roots 注入 | 已完成 MVP 版 | P7 | 用户本机复跑 session `20260708T065705459243Z` 暴露模型不知道 `--allow-dir` 绝对路径；system prompt/provider-bound context 现在会列出 primary workspace 和 allowed dirs。 |
| T-054 | 跨项目需求覆盖边界记录 | 已完成记录 | P7 | 用户确认当前测试项目可能无法完全覆盖需求；压测记录已说明单仓库只能输出候选前置能力和缺口，后续需要把相关项目也作为 `--allow-dir`。 |
| T-067 | Evidence Ledger MVP | 已完成并小改压测通过 | P7 | provider-bound `[Evidence ledger]` 已落地；session `20260708T092554037057Z` 验证小改闭环仍可跑通。 |
| T-068 | apply_patch tag 参数易误填 `path#tag` | 已完成 | P7 | `read_file` 现在显式输出 `tag: <hash>`；`apply_patch` 兼容误传的 `path#tag` / `[path#tag]` 并提示下次传纯 hash，hash 校验不放宽。 |
| T-069 | git_diff 区分已有修改与本轮修改 | 已完成 MVP 版 | P7 | 每轮 run start 捕获 git baseline 并写 session；`git_diff` 追加 attribution 小节，按 pre-existing、this-session apply_patch、mixed、new unattributed 提示模型分开总结。 |
| T-070 | 最终 diff 细节概括准确性 | 已完成并复测通过 | P7 | `git_diff` 已追加 `[diff summary]`，按文件输出 `+N/-M`、hunk 数、hunk header 和少量 added/removed 片段；测试覆盖重复标题 + smoke-test 行实际为 `+3 -0`；百炼复测 session `20260708T100128250335Z` 已正确总结 `+1/-1`、1 hunk 和 attribution。 |
| T-071 | P7 阶段回顾与 OMP 差距决策 | 已完成 | P7 | 新增 `docs/stage-review-2026-07-09.md`，整理当前与 OMP 的差距、已关闭风险、剩余 P7 候选项，并决定先进入真实需求实现压测。 |
| T-072 | 真实需求实现压测 | 首轮完成但未通过 | P7 | session `20260709T013441841983Z` 读取真实需求后漂移到 `deployMessage/nacos`，修改无关 Redis 配置并错误声称 worktree 无 `pom.xml/src`；问题已记录到 `docs/pressure-test-2026-07-09.md`。 |
| T-073 | 轻量 reviewer / pre-edit relevance gate | 已完成并复跑 | P7/P8 | 已新增真实写入前 relevance gate、workspace-root evidence、`git_diff` reviewer 和 patch log 相对路径归一；复跑 session `20260709T021349259159Z` 未再触碰 `deployMessage/nacos`，也未再声称无 `pom.xml/src`。 |
| T-074 | 真实实现质量 gate / safe new-file policy | 已完成并复跑 | P7/P8 | 已新增 comment-only 代码实现 reviewer、`write_file dry_run=true` 新文件预览、创建文件 patch log 和 rollback 删除；复跑 session `20260709T025706579604Z` 未再产生 comment-only patch，也未因新文件权限降级乱改，而是在证据不足时停止说明功能属于 `zqyl-investment-plan`。 |
| T-075 | no-edit final hygiene / 跨服务目标接入 | 已完成 MVP 版 | P7/P8 | 已新增 no-edit final hygiene provider context 和 runtime steering：实现任务准备无改动停止时，如果缺 git/todo 收束，会临时只开放 todo/git 收束工具；测试覆盖 provider context 和过早 final 被 steering 到 todo_add + git_status。下一步把真实目标服务作为 `--cwd/--allow-dir` 接入继续压测。 |
| T-076 | Event/Command Protocol v1 | 已完成 MVP 版 | P8 | 新增 dataclass 事件和命令协议：`event_id/session_id/run_id/seq/timestamp/type/payload`；Runtime 通过 EventSink 产出事件，CLI 先作为消费者打印，session JSONL 可重放关键事件；暂不引入 Pydantic。 |
| T-077 | Terminal Frontend MVP | 已完成 MVP 版 | P8 | 第一版是 terminal-native interactive frontend，不是 fullscreen TUI；支持 `./agent` / `--chat` / `chat` 入口；可选 `prompt_toolkit` 做多行输入、历史和快捷键，`rich` 做 assistant/tool/error/approval 输出；保留原生 scrollback，不用 Rich Live 做主渲染。 |
| T-078 | 项目边界驱动的项目清单分析压测 | 已完成 MVP 版 | P8/P9 | 已按 OMP authored skills / memory 思路落地为本机上下文，不新增专用工具：企业服务边界放入 `.local-agent/memory/enterprise-service-boundary.md`，范围分析工作流放入 `.local-agent/skills/project-scope-analysis/SKILL.md`；runtime 新增 analysis-only 任务识别、named skill soft requirement、自定义 memory_read 安全读取和 final structure gate。下一步用用户给定真实需求跑“项目范围确认 → 源码验证 → 实现设计”。 |
| T-080 | Terminal Frontend 命令可发现性 | 已完成 MVP 版 | P8/P9 | 已按 `docs/architecture.md` 的 terminal-native 设计补 `/help`、`/status`、`/tools` 和启动提示；不引入 fullscreen，不改变同步 runtime。 |
| T-081 | Claude review 行动计划 | 已完成 | P9 | 新增 `docs/claude-review-action-plan-2026-07-09.md`，明确接受 agent.py 拆分、token budget、LSP provider、run collector 等方向，但先做日用压测和 run summary，再渐进拆模块。 |
| T-082 | Run summary / coverage MVP | 已完成 | P9 | Runtime 已记录 `run_summary` 和 `RunSummary` event，包含终止原因、耗时、LLM 请求数、工具调用/错误/无效结果、synthetic result、compaction、tool counts、guard hits 和 steering counts；`/status` 展示最近一轮摘要。 |
| T-083 | 真实需求范围确认到源码验证压测模板 | 已完成 | P9 | 新增 `docs/real-requirement-pressure-test-template.md`，把真实需求从范围判断推进到源码验证和小改压测的步骤、命令、验收和问题记录固化下来。 |
| T-084 | qwen3-coder-next 只读源码验证压测 | 已完成并记录问题 | P9 | session `20260709T071219747931Z` 正常收束：153 秒、35 次 LLM 请求、78 次工具调用、33 次 compaction、18 次 LLM summary；读到 YXK-397 SQL 并定位 `IntentionConfig*` 证据链。新增 PT-030~PT-032。 |
| T-085 | todo 工具误参纠偏 | 已完成 | P9 | T-084 中模型用 `key/content` 调 `todo_add`、用错误 id 调 `todo_update`；已兼容 `key -> id` / `content -> task`，并在缺参、未知 id、无更新字段错误中返回正确示例。 |
| T-086 | evidence-aware read repetition guard | 已完成 | P9 | T-084 中 `read_file` 54 次，多次重复读取同一路径但 `guard_hits=0`；已参考 OMP soft escalation/pruning，对同路径同范围成功读取超过阈值后返回 evidence 摘要并 forced-final。 |
| T-087 | final structure / evidence hygiene 增强 | 已完成 | P9 | T-084 最终把“项目表”退化为“表名表”，并对类作用有过度断言；已增强 final gate：项目范围表必须含项目/服务列，证据状态要求会触发已验证/推断标签检查。 |
| T-088 | read-only evidence gate | 已完成 | P9 | 密码加密问答压测中，模型在未读关键登录/密码文件前先给行业推测；已参考 OMP current task / evidence context：代码证据/源码/不推测/怎么处理类问题若无成功 `read_file` 就准备回答，会被要求先查证据；no-match 负向证据可收束。 |
| T-089 | semantic exploration guard | 已完成 | P9 | 密码加密问答压测中，用户要求代码证据后出现同模块/父子目录/Path not found 扩散；已参考 OMP soft escalation/pruning，对 `list_files` 语义路径按模块/父目录归一计数，超过小上限后跳过目录猜测并引导回 evidence tools。 |
| T-090 | terminal input/output isolation | 已完成 | P9/P10 | 压测日志出现用户键盘输入混入工具日志；已新增 TTY echo 静默与 prompt 期恢复/flush，覆盖一次性 CLI、REPL 和 terminal chat。 |
| T-091 | Vue diff reviewer comment-only 误报修复 | 已完成 | P9 | 已把 comment-only 判断改为按文件类型处理：JavaDoc `<p>/<li>` 仅在 Java 中作为注释标记，Vue 模板 markup 不再算 comment-only；新增回归测试覆盖 Vue `<p>` 模板替换。 |
| T-092 | compaction 渐进模块化与 LSP 置信度提示 | 已完成 | P9/P10 | 已新增 `src/local_agent/compaction.py`，迁出压缩阈值、provider-safe 清理、tool output pruning、summary transcript/cache helpers；Java/JS/TS/Vue LSP 输出新增 `[lsp confidence]` best-effort 提示。 |
| T-093 | 可选外部 LSP adapter | 已完成 | P9/P10 | 新增 `src/local_agent/lsp/`，支持 stdio JSON-RPC LSP client、Java/TypeScript/Vue server 自动发现、嵌套项目 root marker、`lsp_status` 和 `AGENT_LSP_MODE=auto|light|external`；不自动下载依赖，不可用时回退 light fallback。 |
| T-094 | 真实项目 LSP 可用性压测 | 已完成并记录问题 | P9/P10 | `crcl-open` 与 `zqyl-user-center-service` 均能由 LCA 调用 `lsp_status` / `lsp_symbols` / `lsp_definition`；当前机器无外部 LSP server 命令，因此正确回退 light fallback；user-center 首轮出现一次路径字符误写，精确路径复测通过。 |
| T-095 | jdtls 预置与 strict external 复测 | 已完成 | P9/P10 | Homebrew 已安装 `jdtls 1.60.0`；极小 Maven 项目 external symbols/definition/diagnostics 全通；真实企业项目 diagnostics 走 jdtls 且 OK，但因缺公司内部 parent POM 无法导入 Maven project，external symbols/definition 为空。已补 LSP `workspaceFolders` / server request 响应，并在 external 空结果时合并 light fallback；session `20260709T084323683100Z` 验证 Agent 可正确说明 external 与 fallback。 |
| T-096 | Java LSP 韧性对齐 OMP | 已完成 | P9/P10 | 参考 OMP LSP client：跟踪 `$/progress`、等待 project load、响应 `workspace/configuration` 的 Java import 配置、处理 workspace folders / dynamic registration / progress create；企业项目缺 Maven parent 时保持 external 边界说明 + fallback evidence。 |
| T-097 | Java project health 探针 | 已完成 | P9/P10 | `lsp_status probe=true` 会启动匹配 server 并检查 jdtls project/source path 状态；真实企业项目已验证输出 `java.project.getAll: 0` / `java.project.listSourcePaths: 0` 和 Maven parent/私服/缓存修复提示。 |
| T-098 | Maven parent probe | 已完成 | P9/P10 | `lsp_status probe=true` 会解析 `pom.xml` parent 链，检查相对 parent POM 和 `~/.m2` parent POM；真实企业项目已直接指出缺失的 `com.yljr:parent` 版本，便于补私服/缓存。 |
| T-099 | Maven environment probe | 已完成 | P9/P10 | `lsp_status probe=true` 会报告 `mvn`、Maven settings、本地仓库和 mirror/server/profile/repository/activeProfile 数量，帮助判断缺 parent 是缓存缺失还是私服配置未准备；不输出私服 URL、账号或密码。 |
| T-100 | Java LSP fallback 真实复测 | 已完成 | P9/P10 | session `20260709T090514754843Z` / `20260709T090748481226Z` 证明 LCA 能先报告 jdtls/Maven 边界，再用 LSP fallback、search_code、read_file 收束 `IntentionConfigManagerController` 调用链。 |
| T-101 | query-aware LSP fallback | 已完成 | P9/P10 | `lsp_symbols` / `lsp_definition` 带 query/symbol 时优先扫描文件名/路径匹配候选，避免大仓库根目录 fallback 因 `MAX_LSP_FILES=300` 漏掉目标类；新增 320 dummy Java 文件回归测试。 |
| T-102 | 拓展服务费结算真实需求链路压测 | 已完成阶段 1-3 | P9 | 已用 `需求文档-拓展服务费结算V1.3.md` 跑“范围判断 → 本机源码可用性核对 → 源码只读验证”；结论是当前源码不足，需补 `zqyl-manager`、`zqyl-loan-application`、`ysd-provider` 后再进入实现设计。 |
| T-103 | provider-safe invalid tool_call normalization | 已完成 | P9/P10 | T-102 首轮源码验证暴露空工具名 tool_call 会导致下一轮百炼 400；已在 assistant message 入历史前规范化无效 tool_call，并补回归测试。 |
| T-104 | msp-pay / zqylpayment 用户确认范围源码复核 | 已完成 | P9 | 用户确认服务费结算前端应为 `msp-pay`、后端应为 `zqylpayment`；session `20260709T092317272887Z` 复核显示两者是合理候选但只部分支持，仍缺结算单核心实体、状态 60、制单/回退/下载中心等证据，下一步做更窄的证据补全而不是直接小改。 |
| T-105 | msp-pay / zqylpayment 窄范围证据补全 | 已完成 | P9 | session `20260709T092951071920Z` 已补预制单状态、平台缴费单状态、费用明细、制单/回退、下载中心和前端页面证据；当前可进入设计阶段，但不能直接小改。 |
| T-106 | FinalAnswer Steerer 第一块抽象 + source-grounded numeric guard | 已完成 | P9/P10 | T-105 暴露枚举状态码误报；已把最终回答 steering 迁到 `src/local_agent/steering/final_answer.py`，并新增数字/状态码源码一致性 guard。 |
| T-107 | Context token budget + reserve MVP | 已完成 | P9/P10 | 已新增本地 token 估算、`context_token_budget`、`AGENT_CONTEXT_TOKEN_BUDGET` 和 `--context-token-budget`；按 token 或 char 任一超阈值触发 compaction，并至少预留 15% 下一轮预算。 |
| T-108 | source-grounded numeric / token budget 复测 | 已完成并记录问题 | P10 | session `20260709T095626110047Z` 已验证百炼不再因 provider-safe tool_call 崩溃，枚举数字回答正确；但模型在 `msp-pay` 大搜索结果后未继续读取关键文件就给“未找到证据”结论，说明仍需要更硬的需求契约和工具选择队列。 |
| T-109 | RequirementContract MVP | 已完成并集成 | P10 | 新增 `src/local_agent/task_contract.py`，本地 deterministic 生成目标、范围、验收项、证据要求、验证要求和风险；runtime 每轮 provider request 注入 `[Requirement contract]`，长任务压缩后仍保留验收边界。 |
| T-110 | CompletionAudit 完整版 | 已完成 | P10 | 新增 `src/local_agent/completion_audit.py`，最终回答前按 RequirementContract 逐项核对验收项、证据项和验证项；证据型只读缺源码路径/证据状态会强制无工具重写，实现任务写后缺 `run_tests` / `git_diff` 会只开放缺失验证工具。 |
| T-111 | MiniToolChoiceQueue MVP | 已完成并集成 | P10 | 新增 `src/local_agent/tool_choice_queue.py`，根据 contract/task kind 和工具摘要对只读证据、需求文档前置读取、写后测试/diff 做工具收窄与 runtime steering；不是完整 OMP ToolChoiceQueue。 |
| T-112 | Planner/Explore 两阶段 MVP | 已完成 | P10 | 新增 `src/local_agent/planner.py`，实现任务按 explore / ready_to_implement / verify 阶段注入 runtime context；MiniToolChoiceQueue 在 explore 阶段隐藏写入/执行工具，只开放 list/read/search/LSP/todo/ask/git 状态检查工具。 |
| T-113 | 二阶段 Patch Reviewer MVP | 已完成 | P10 | 新增 `src/local_agent/patch_reviewer.py` 和 runtime steerer：用已收集的 diff/reviewer 信号独立核对 RequirementContract；缺显式测试 diff、公开 API 未查调用方、低相关或 comment-only patch 会在 final 前转入受控修复/验证/回滚，随后才进入 CompletionAudit。ToolChoiceQueue 已调整为已有 diff 但尚待验证时保留 focused repair 工具，避免 reviewer 与 queue 互相阻塞。 |

## 风险清单

| ID | 风险 | 状态 | 影响 | 应对 |
|---|---|---|---|---|
| R-001 | 仓库没有初始 commit | 已关闭 | 后续修改缺少稳定回滚基线。 | 已创建初始 commit。 |
| R-002 | 长任务上下文持续膨胀 | 已进一步缓解，继续增强 | 多轮工具调用后 token 成本和失败率上升。 | 已增加 OMP 风格 reserve 阈值、auto LLM summary、当前用户请求保留、超大 tool 输出截断、单 system 摘要合并和 token budget MVP；后续再评估 provider/model 专用 tokenizer 和输出 reserve。 |
| R-011 | 工具 schema 描述与实现不一致 | 已关闭首例，持续关注 | 模型会相信工具描述并据此修改文档或代码，错误 schema 会直接造成错误结果。 | 已修正 `write_file` 描述并新增测试；后续压测继续关注 schema/实现一致性。 |
| R-003 | 没有 todo 工具 | 已关闭 | 长需求中不容易追踪完成项和遗漏项。 | 已增加 session 级 todo 工具。 |
| R-004 | 没有 ask_user 工具 | 已关闭 | 遇到歧义时模型只能猜。 | 已增加 ask_user 工具。 |
| R-005 | ask 模式确认次数多 | 已缓解 | 日用体验偏慢。 | 已增加 per-tool approval 白名单和 allow / prompt / deny 策略；默认仍保持谨慎。 |
| R-006 | shell 工具不是安全沙箱 | 开放 | 命令可以越过 workspace 访问系统。 | 文档明确风险；封闭 VM 作为真正边界。 |
| R-007 | 恶意仓库 prompt injection | 开放 | 文件内容可能诱导模型执行不安全操作。 | 不信任仓库禁用 `yolo`，保留人工审批。 |
| R-008 | 中断时 tool_call 配对仍可增强 | 已关闭 MVP 版 | 恢复会话时可能遇到兼容性问题。 | deadline、用户中断和输出截断已补齐。 |
| R-009 | ask_user 会阻塞等待用户 | 已缓解 | 带预算的长任务如果触发 ask_user，会等待人工输入。 | 已支持 `timeout_seconds` / `default_answer`，并自动受剩余 budget 约束；显式 timeout 也会被剩余 budget 夹紧。 |
| R-010 | approval prompt 等待耗尽预算 | 已关闭 MVP 版 | 用户长时间不确认工具调用时，确认后工具可能执行成功，但下一次 deadline 检查立刻停止。 | approval prompt 已按剩余 deadline 等待 stdin；deadline 到期直接取消并返回 tool error。 |
| R-012 | 日用命令仍依赖用户手写工具流程 | 已关闭 MVP 版 | 用户不应每次提示“先 list/read，再 dry_run，再 test/diff”；否则 LCA 更像压测脚本而不是本地编程助手。 | 已采纳 OMP 分层设计：system prompt 固化默认流程，tool descriptions 说明工具规范，runtime nudge 做轻量纠偏。 |
| R-013 | Memory / skills 注入长期 prompt injection 或陈旧事实 | 已缓解，managed skills 仍暂缓 | memory 和 generated skills 会跨 session 影响模型，错误或恶意内容可能持续放大。 | memory 和 authored skills 注入区已标注 advisory；已设置注入预算并清洗 learned / skill description 字段；managed skills 默认关闭且 authored skills 优先。 |
| R-014 | 重复工具调用循环导致 budget 耗尽且无最终回答 | 已进一步缓解并复跑通过 | 模型可能在同一工具参数或同一无结果关键词上循环，用户只得到预算停止或重复工具硬停信息。 | 已补最近窗口重复工具调用熔断、`ToolResult.useless`、空结果标记、provider-bound useless/superseded pruning、open todo runtime reminder、duplicate-tool forced-final steering，以及 search_code 空搜索词级 guard；session `20260708T083312934017` 已验证能产出最终分析。 |
| R-015 | 企业项目源码和需求可能被发送到三方 AI API | 用户已确认，full-access 已代跑 | 联网 LCA 压测会把进入上下文的企业代码/需求发给百炼。 | 用户已确认可外发；早期受限 Codex 环境拒绝代跑，full-access + network enabled 后已由 Agent 代跑。LCA 自身不内置禁止外发，按 OMP 思路由用户、provider、permission 和运行环境策略决定。 |
| R-016 | 跨项目运行时 token 配置绑定目标 workspace `.env` | 已关闭 MVP 版 | `--cwd` 切到其他项目后，LCA 仓库 `.env` 不会自动加载。 | 已新增 `--env-file` 和 `./agent` 安装目录 `.env` 自动加载；凭据配置与 `--cwd` 解耦。 |
| R-017 | 只读任务仍在目标 workspace 写 runtime 状态 | 已关闭 MVP 版 | 目标仓库会出现 `.local-agent/sessions`，不利于企业项目零业务落盘压测。 | 已参考 OMP 将 sessions 放在用户 agent dir 的设计，实现 `--state-dir`；sessions/todos/patch logs 与 workspace 解耦。 |
| R-018 | AGENTS/RULES 长期注入可能与当前任务冲突 | 已缓解，持续关注 | 用户级或项目级规则如果过期，会跨 session 影响模型判断。 | 注入区明确 advisory；system prompt 明确当前用户指令和源码证据优先；RULES 适合短规则，长背景放 AGENTS 或 memory。 |
| R-019 | 自动 memory consolidation 可能隐式写入陈旧或敏感内容 | 已进一步缓解，持续关注 | session 中的企业信息、临时结论或模型误判如果自动写入 memory，会跨 session 放大。 | 默认 `off`；显式开启后默认写用户级 state dir 的 memory，只有 `memory_scope=project` 才写项目 `.local-agent/memory`；只接受严格 JSON 的四类短条目；坏 JSON、空结果、deadline 耗尽、本轮已显式写 memory 时不写；memory 仍是 advisory。 |
| R-020 | multi-root allowed dir 没有稳定进入模型操作路径 | 已复跑通过 | 模型会猜 `requirements` 等不存在目录，或看到 roots 后仍不读取真实需求文档；session `20260708T072404789287Z` 证明仅提示和工具观察不够。 | 参考 OMP ToolChoiceQueue / soft tool requirement：需求/文档类任务在 allowed-dir 文档读取前只暴露 `list_files` / `read_file`，并要求先读取候选需求文档；session `20260708T083312934017` 已验证先读真实需求目录中的两份需求 md。 |
| R-021 | 单仓库无法覆盖跨服务需求 | 已记录，持续关注 | 如果需求实际涉及 incentive/settlement/用户中心等其他项目，单仓库分析会误把“当前仓库未命中”当成完整结论。 | 参考 OMP 对 workspace/context 的依赖边界，后续把相关项目也作为 `--allow-dir`，或让 Agent 明确输出“需要补充哪个项目”。 |
| R-022 | 同文件连续切片读取导致任务漂移 | 已补并复跑通过 | session `20260708T073252231781Z` 中模型连续读取同一大文件多个相邻区间；session `20260708T074609696125Z` 中显式只读任务因“下一步实现”措辞误关 guard；session `20260708T083312934017` 中 repeated read-file guard 成功收束并按 5 点结构输出。 | 参考 OMP 病态子循环小上限、runtime steering 和 runtime context：显式只读/不要修改文件/不要写文件优先于编辑词；近期同一路径 `read_file` 超阈值后强制下一轮无工具最终回答，并在 steering 里列出已读文件路径、原始请求和已读一致性规则。 |
| R-023 | 同一空搜索词跨路径扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T082703005777Z` 中模型对同一无结果关键词反复切换 path 搜索，因参数不同绕过 exact duplicate guard。 | 参考 OMP useless tool result / pruning / soft escalation：按 pattern 而非完整参数统计无结果搜索，多次无结果后 forced-final。 |
| R-024 | path escape 纠偏不足会让模型漏读主项目 | 已补并复跑通过 | session `20260708T084322924403Z` 中模型误用父目录后没有恢复，最终只分析辅助项目。 | 参考 OMP cwd/project context 和工具观察：公共 path escape 错误已列出 primary workspace/allowed dirs；session `20260708T085927874078` 已验证可恢复。 |
| R-025 | LSP 空 query 扩散导致 token 浪费 | 已补并复跑通过 | session `20260708T084714338485Z` 中模型猜测大量不存在符号名，参数不同绕过同参重复 guard。 | 参考 OMP useless result / pruning / soft escalation：新增 LSP symbol 空 query 小上限并 forced-final。 |
| R-026 | 最终回答结构和证据路径可能漂移 | 已补并复跑通过 | session `20260708T085426840146Z` 最终只总结最后一个需求文档；此前也出现把未验证路径当下一步建议路径的倾向。 | 参考 OMP 当前任务和 runtime evidence 持续注入：新增 Current task contract 和 evidence-backed path rule。 |
| R-027 | 模型可能把 `path#tag` 整串误当成 patch tag | 已关闭 | session `20260708T092554037057Z` 中 `apply_patch dry_run` 因 `tag=README.md#3988a904` 连续失败。 | 已参考 OMP 结构化工具观察/编辑参数提示：read_file 显式给出 pure tag，apply_patch 兼容 `path#tag` 并提示模型。 |
| R-028 | 脏工作区下最终 diff 摘要可能混入非本轮改动 | 已关闭 MVP 版 | session `20260708T092554037057Z` 的 `git_diff` 同时包含 README 小改和正在开发的 Evidence Ledger 代码 diff。 | 参考 OMP task/worktree/session state：已记录 run start baseline，并按 pre-existing / this-run patch / mixed / new unattributed 分组提示。 |
| R-029 | 最终 diff 细节可能被模型过度简化或说错 | 已关闭并复测通过 | session `20260708T094926471758Z` 中 attribution 分类正确，但模型没有准确描述实际 diff hunk；低价值 README smoke-test 改动已撤回。 | 参考 OMP runtime observation 思路：已给 `git_diff` 增加 diff stats/hunk summary；session `20260708T100128250335Z` 验证最终总结可正确引用 summary + attribution。 |
| R-030 | 过早补完整 reviewer / ToolChoiceQueue 会增加复杂度但未必命中当前痛点 | 已触发裁剪版 MVP，继续受控 | T-108/T-105 类压测证明仅靠 prompt 和 final steering 仍会出现关键证据没读够就下结论；但完整 OMP ToolChoiceQueue/reviewer/subagents 仍过重。 | 已按 OMP 工具调度原则裁剪出 MiniToolChoiceQueue，只覆盖只读证据、需求文档前置读取、写后测试/diff hygiene；完整 reviewer、Planner/Explore、subagents 继续等真实失败样本触发。 |
| R-031 | 真实实现任务可能产生无关 patch | 已缓解，继续观察 | T-072 session `20260709T013441841983Z` 读取正确需求后漂移到 Nacos/Redis 配置，并把无关注释当成实现锚点；这说明 dry_run/hash 校验只能保证位置正确，不能保证业务相关。 | 已完成 T-073：真实写入前要求目标文件已读；代码实现任务修改部署/配置类低相关路径会被拦截或要求用户确认；workspace-root evidence 进入 Evidence Ledger；`git_diff` 增加 reviewer 提示。T-073 复跑未再触碰 `deployMessage/nacos`。 |
| R-032 | 真实实现可能退化成低价值注释 patch | 已缓解并复跑 | T-073 复跑 session `20260709T021349259159Z` 中模型定位到相关 Java 文件，但因 `write_file` 被 deny，最终只给 DTO 字段补 JavaDoc；这不能算真实业务实现。 | T-074 已补 implementation-quality reviewer：本轮代码 diff 若只有注释/文档改动，`git_diff` 会提醒不能声称行为、校验、解析或测试覆盖变化。复跑 session `20260709T025706579604Z` 没有再做 comment-only patch，而是在当前仓库缺目标实现时停止说明。 |
| R-053 | 源码证据型最终回答可能误报数字/状态码 | 已关闭 MVP 版，继续压测 | T-105 中模型读到 `PreOrderStatusEnum.MAKING(2)` / `MADE(3)`，最终却误报为 `50/60`。 | 参考 OMP runtime evidence / steering：已新增 source-grounded numeric final steerer，最终回答涉及枚举、状态码、接口、字段等数字事实且与已读源码不一致时，会强制无工具重写。 |
| R-054 | `agent.py` steering/guard 继续膨胀 | 已开始缓解，继续拆 | review 指出 compaction 已拆出，但 final/read-only/semantic guard 继续堆在 `agent.py`。 | 已完成 FinalAnswer Steerer 第一块抽象；后续继续拆 Evidence Ledger、run collector、startup context、memory consolidation 和 semantic exploration steerer。 |
| R-055 | 无效 tool_call 参数会污染下一轮 provider 请求 | 已关闭 MVP 版 | T-108 首轮复测中百炼拒绝历史消息：无效工具调用的 `function.arguments` 为空或畸形 JSON，导致下一轮 HTTP 400。 | 已在 assistant message 入历史前把工具名、id、arguments 统一归一为 provider-safe JSON object 字符串；空/畸形参数写入 `_invalid_arguments`，并补回归测试。 |
| R-056 | read_file 行号会干扰源码数字事实比对 | 已关闭 MVP 版 | T-108 窄复测中，模型把枚举状态误报成 1/3/5；numeric guard 因 read_file 内容含 `1:`、`3:`、`5:` 行号而误以为这些数字有源码证据。 | source numeric guard 比对前会剥离 read_file 行号前缀，再判断状态码/枚举值是否出现在源码内容中；新增回归测试覆盖错误数字刚好等于行号的情况。 |
| R-033 | no-edit 停止路径可能跳过收束工具 | 已关闭 MVP 版 | T-074 复跑中模型正确停止，但没有维护 todo，也没有调用 `git_diff` 输出“无改动”证据；这会降低最终报告的可审计性。 | T-075 已参考 OMP current task / tool-choice steering 思路落地：no-edit stop 前缺 git/todo 收束会被 runtime steering 纠偏，并临时限制工具到 todo/git hygiene 集合。 |
| R-034 | 过早做 fullscreen 重 TUI 可能拖慢核心能力 | 新增，中 | 如果把第一版前端理解成 Textual/fullscreen/pane/mouse/overlay，容易提前引入 scrollback、copy/paste、resize、输入法和渲染刷新问题。 | 第一版明确命名为 Terminal Frontend：`prompt_toolkit` 只管输入，`rich` 只管结构化输出，保留原生 terminal scrollback；先做 Event/Command Protocol，后续有真实瓶颈再升级 Textual/Bubble Tea/Ratatui/自研 renderer。 |
| R-035 | Runtime 与前端输出耦合会阻碍后续终端体验 | 已关闭 MVP 版 | 如果工具日志、审批显示和最终输出继续散落在 Runtime/CLI print 中，后续 `prompt_toolkit + rich` 前端会难以复用和 replay。 | T-076 已参考 OMP runtime/TUI 分层思路，落地 dataclass Event/Command Protocol 和 `EventSink`；Runtime 产出 typed events，CLI 只是第一消费者。 |
| R-036 | 完整 async command bus 过早引入会扩大复杂度 | 新增，受控 | T-077 已满足本地 terminal 交互，但 approval/cancel/interrupt 仍是同步路径；如果立刻搬完整异步 command bus，会影响当前稳定的单 Agent runtime。 | 参考 OMP 分层但按 LCA 裁剪：MVP 先保留同步 `AgentRuntime.run()`，把 event/replay/terminal 输入输出打通；等真实交互压测需要取消、远程 UI 或并发审批时，再升级 Command Bus。 |
| R-037 | 纯分析任务被实现任务 hygiene 带偏 | 已关闭 MVP 版 | T-078 压测中，“仅根据需求和服务边界圈项目范围”曾被识别为实现任务，导致最终回答偏向 git/todo/no-edit 审计，或停在“ready to output”而不是输出表格。 | 已新增 analysis-only 任务识别，包含“服务边界/项目范围/仅根据/禁止扫描源码”等信号；analysis-only 不加 coding workflow nudge，不触发 no-edit final hygiene；纯只读分析默认跳过 todo；final structure gate 会在缺表格/缺指定段落/ready-to-output 时强制无工具重答。 |
| R-038 | 点名 authored skill 但模型不读正文 | 已关闭 MVP 版 | T-078 压测中，模型能看到 skill metadata，但曾未主动读取 `project-scope-analysis/SKILL.md`，导致规则只停留在描述层。 | 已参考 OMP soft tool requirement 思路：prompt 点名已发现的 project skill 时，runtime 会软性要求先 `read_file` 对应 `SKILL.md`，读完后再继续。 |
| R-039 | TUI 命令不可发现会降低日用体验 | 已关闭 MVP 版 | 交互入口已有，但用户需要记 `/approval` 等命令，且缺少当前 runtime 状态视图。 | 已参考 terminal frontend 设计文档，在 append-only 前端内新增 `/help`、`/status`、`/tools`，不做 fullscreen。 |
| R-040 | 过早大拆 `agent.py` 可能打断真实使用验证 | 新增，受控 | Claude review 指出 `agent.py` 已大，但 P0 大拆分会扩大回归面，影响今天可用目标。 | 接受架构方向但调整顺序：先做 run summary/coverage 和真实压测，再按 startup_context/evidence/compaction/memory_consolidation/steering 分批抽模块。 |
| R-041 | 压测复盘缺少结构化 run coverage | 已关闭 MVP 版 | 只有 session 原文和最终回答时，很难判断模型卡在哪个 guard、用了多少工具、是否触发 compaction 或为什么结束。 | 已参考 OMP run-collector 思路，新增每轮 `run_summary`：工具次数、guard/steering、compaction、termination reason 统一落 session 和事件流。 |
| R-042 | 只读源码验证中重复读取过多 | 已缓解 | T-084 中 `read_file` 54 次、`list_files` 10 次，重复读取同一批证据文件但没有 guard/steering 命中。 | 已参考 OMP pruning / soft escalation / evidence sufficiency：对已读同范围做 evidence-aware repetition guard，达到阈值后返回已有 evidence 摘要并触发 final-answer steering；编辑任务不启用该 guard。 |
| R-043 | 最终回答轻微结构漂移和过度断言 | 已缓解 | T-084 要求项目表，但最终输出表名表；还把 `IntentionConfigApplication` 表述为 Spring Boot 启动/配置类，证据不足。 | 已增强 Current task contract 和 final gate：项目范围表必须含项目/服务列；证据状态要求会触发已验证/推断标签检查；后续再观察是否需要完整 reviewer。 |
| R-044 | 证据型只读问题先输出推测 | 已缓解 | “前端密码加密/后端怎么处理”问题中，模型未读关键登录/密码文件就先给“可能 HTTPS 明文 + 后端哈希”的推测。 | T-088 已完成：代码证据/源码/不推测/怎么处理类问题若无成功 `read_file` 就准备回答，会被 runtime steering 拦住并临时只开放证据工具；no-match 负向证据可明确收束。 |
| R-045 | 语义级路径探索扩散 | 已缓解 | 用户纠正后出现多次相似目录 list_files、父子目录扩散、Path not found 和大目录读取；exact duplicate guard 太晚才命中。 | T-089 已完成：semantic exploration guard 按模块/父目录/Path-not-found pattern 计数，超过阈值后跳过 `list_files` 目录猜测，并临时只开放 search_code/read_file/LSP 证据工具。 |
| R-046 | 终端输出被用户输入污染 | 已缓解 | 日志出现 `33333333333[tool:start]`，说明一次性 CLI 运行中键盘输入被终端 echo 到 transcript。 | T-090 已完成：一次性 CLI、REPL 和 terminal chat 在 agent run 期间关闭 TTY echo；approval / ask_user 会恢复输入并 flush 误敲缓冲。 |

## 架构决策

| ID | 决策 | 依据 |
|---|---|---|
| ADR-001 | 优先采纳 OMP 成熟设计，按本地目标裁剪。 | 不为了“避免复制”而绕开好设计；判断标准是收益是否大于复杂度，并且不破坏个人本地使用、封闭 VM、无公网依赖和第一阶段 MVP 边界。 |
| ADR-002 | `max_steps` 只作为安全保险丝，不作为主要预算。 | OMP 主循环不靠步数终止，而靠模型是否继续请求工具、时间预算和上下文预算。 |
| ADR-008 | 默认不限步，默认使用时间预算。 | 避免 `100` 这类硬上限卡住真实任务；默认 `budget_seconds=600`，`max_steps=0`。 |
| ADR-009 | 固化 OMP 核心架构笔记。 | OMP 的主循环、deadline、compaction、synthetic tool result 等结论写入 `docs/omp-core-architecture-notes.md`，后续不再重复扫描。 |
| ADR-010 | P6 优先实现 OMP 默认工作流的本地 MVP 版。 | 已直接采纳 OMP 的分层设计：系统上下文、工具描述、runtime 纠偏共同让用户不用指定工具顺序；完整 ToolChoiceQueue、subagents 等复杂能力继续后置。 |
| ADR-011 | 默认采用 OMP 风格 auto summary。 | 小历史不摘要；超过 reserve 阈值才调用已配置 AI API 做 LLM summary；失败回退 local summary；`local` / `llm` 仍可显式指定。 |
| ADR-012 | LSP 第一版做轻量多语言静态工具。 | 满足 Python、Java、JavaScript、TypeScript、Vue 的 symbols/definition/references/diagnostics，不引入外部 language server、npm/pip 依赖或后台进程；完整 LSP/DAP 后置。 |
| ADR-013 | Memory / skills 按 OMP 思路分阶段本地化。 | Markdown memory 启动注入、显式 `learn` 和 authored skills discovery 已落地；最后才评估 managed skills/autolearn；不引入 Hindsight、Mnemopi、向量库或插件市场。 |
| ADR-014 | Runtime 问题优先采用 OMP 已验证设计。 | 对 deadline、compaction、permission、synthetic tool result、todo/tool-choice steering、pruning 这类 OMP 已经覆盖的机制，不再为了“自己造一套”而绕开；LCA 不内置“企业数据不能外发”禁令，但必须尊重当前执行宿主或企业环境的策略拦截。 |
| ADR-017 | 解决 runtime/工具/上下文问题时先查 OMP 做法。 | 用户明确要求后续解决问题都参考 OMP；本项目原则更新为先找 OMP 已验证设计，再按本地个人 Agent、封闭 VM、单 Agent 和无自动下载边界裁剪落地。 |
| ADR-018 | Evidence Ledger 是本轮 provider-bound runtime context，不是长期 memory。 | 工具证据服务于当前会话最终回答和审计，不能替代 session 原文，也不应默认写入项目长期 memory；参考 OMP runtime state / tool evidence / steering 持续入上下文的思路。 |
| ADR-019 | P7 后续先进入真实需求实现压测，reviewer / 完整 ToolChoiceQueue 条件触发。 | 阶段回顾显示当前主链路已具备低风险实战条件；完整 reviewer / ToolChoiceQueue 应根据真实实现压测暴露的问题裁剪，而不是在缺少失败样本时提前做重。 |
| ADR-020 | T-073 优先做轻量 relevance gate / reviewer，不先做完整 ToolChoiceQueue。 | T-072 失败点是无关 patch 和反事实 workspace 判断；最小有效修复是写入前目标相关性检查、workspace-root evidence 和最终 diff reviewer。完整 ToolChoiceQueue 继续作为工具选择失控时的后补。 |
| ADR-021 | T-074 先补实现质量 gate 和受控新文件策略，再决定是否上完整 ToolChoiceQueue。 | T-073 复跑证明 relevance gate 能挡无关目录漂移，但真实实现仍可能退化为 comment-only patch；T-074 已先解决“什么算有效实现”和“何时允许新文件”的 MVP 问题。 |
| ADR-022 | 实现任务允许诚实停止，但 no-edit final 也要可审计。 | T-074 复跑证明“证据不足时停止”比强行注释 patch 更好；T-075 已用 provider context + runtime steering 让停止路径保持 todo/git 证据，而不是只靠最终文字。 |
| ADR-023 | 第一版前端定位为 Terminal Frontend，而不是 fullscreen TUI。 | 采用 `prompt_toolkit + rich`，但通过 dataclass Event/Command Protocol 与 Runtime 解耦；Runtime 不直接 import 前端库，前端只消费事件和发送命令。第一版保留原生 terminal scrollback，不做 Rich Live 主渲染、复杂 pane、mouse、overlay 或可交互 diff viewer。 |
| ADR-024 | Runtime 先产出 replayable typed events，再做 Terminal Frontend。 | 已落地 T-076；参考 OMP runtime/TUI engine 分层，但本地化为 Python dataclass、`EventEmitter`、`EventSink` 和 session `event_v1`，不引入 Pydantic、异步队列或重 UI。 |
| ADR-025 | Terminal Frontend MVP 保持同步 runtime，先不引入完整 async command bus。 | 已落地 T-077；`./agent` / `--chat` / `chat` 共用事件 sink，approval 仍走同步 stdin 但发 approval events；可选 `prompt_toolkit` / `rich` 增强体验，缺依赖时降级，符合封闭 VM 可预置依赖原则。 |
| ADR-026 | 企业服务边界用 memory/skill 承载，不新增专用工具。 | 已落地 T-078；这类组织边界是用户个人长期上下文，不是通用 Agent tool。参考 OMP authored skills / project memory 思路，把边界表放本机 `.local-agent/memory`，把“如何用边界分析需求范围”放 `.local-agent/skills`，代码只补通用 analysis-only、named skill soft requirement、custom memory read 和 final-structure runtime 能力。 |
| ADR-027 | Claude review 先转为行动计划，不立即做 P0 大拆分。 | 已落地 T-081；OMP 架构原则继续作为方向，但 LCA 当前以真实日用闭环为先。先补 TUI 可发现性和 run summary/coverage，再用压测数据驱动模块拆分、token budget 和 LSP provider 增强。 |
| ADR-028 | Run summary 先做 runtime 内轻量 collector，暂不拆大模块。 | 已落地 T-082；参考 OMP run-collector 的可观测性原则，但当前先把计数和终止原因汇总到 `RunSummary` / `run_summary`，服务压测和 `/status`；等数据稳定后再抽 `run_collector.py` 或 Steerer 协议。 |
| ADR-029 | 默认编码模型切到 `qwen3-coder-next` 做日用压测。 | 阿里云百炼 Qwen-Coder 文档把 `qwen3-coder-next` 作为代码任务/tool interaction 示例模型；本地连通性已验证 OK。`.env` 是本机运行配置，不提交 token。 |
| ADR-030 | P9 压测问题优先补 runtime steering，不先做大重构。 | T-084 暴露的是重复读、todo 参数纠偏、最终结构/证据卫生；这些更适合在工具错误、evidence-aware guard、final gate 层小步修复，不需要立刻大拆 `agent.py` 或上完整 ToolChoiceQueue。 |
| ADR-031 | OMP 架构差距用渐进模块化关闭，不做一次性大搬家。 | Claude review 对 `agent.py` 过大的判断成立；但一次性 Steerer/ToolChoiceQueue 大改回归面太大。先把纯函数和边界清楚的子系统抽出，保持行为不变、测试先行。 |
| ADR-032 | LSP 按 OMP client 思路做可选外部 adapter，保留 light fallback。 | Java/TypeScript/Vue 的完整代码导航应优先交给成熟 language server；但 LCA 仍不在运行时自动下载依赖，也不把外部 server 作为默认强依赖。封闭 VM 可预置 jdtls/npm 包，或用 `AGENT_LSP_*_COMMAND` 指向离线安装路径。 |
| ADR-033 | Final-answer steering 先抽统一 Steerer 协议的一块，不再把最终回答 guard 塞进 `agent.py`。 | 对标 OMP“主循环调度，guard/observer/reviewer 分离”的原则；本轮先抽 final-answer steering，直接修 T-105 枚举误报，后续再拆 semantic/evidence/run collector。 |
| ADR-034 | Token budget 采用本地估算 + reserve，字符预算保留兜底。 | OMP 按 token/context window 管理 compaction 并预留下一轮预算；LCA 当前不引入重 tokenizer 依赖，先用 CJK/ASCII 轻量估算触发压缩，`context_char_budget` 继续作为 fallback。 |
| ADR-035 | P10 采用裁剪版 Intelligence Runtime，不一次性复制 OMP 大系统。 | T-108 证明需要比 prompt 更硬的目标契约、完成审计和工具调度；但 LCA 仍是单用户、单 Agent、本地 MVP。当前已落地 RequirementContract + CompletionAudit + MiniToolChoiceQueue + Planner/Explore，后续再补 reviewer。 |
| ADR-015 | 人工上下文按 AGENTS/RULES 分层。 | 参照 Claude Code 与 OMP 的上下文文件/Sticky rules 分层：`AGENTS.md` 作为启动背景，`RULES.md` 作为短规则每轮注入；二者不同于长期 memory 和 session summary。 |
| ADR-016 | Session memory consolidation 默认关闭；开启后默认写 state memory。 | 这一步不同于只发给模型的 context compaction；默认 off 可以保护只读分析，开启后默认写用户级 state dir，只有显式 `memory_scope=project` 才写项目 `.local-agent/memory`。 |
| ADR-003 | Excel 作为人工视图，Markdown 作为开发协作 Agent 可读事实源。 | 这套文档服务于开发 LCA 的过程；`.xlsx` 是二进制展示产物，不适合作为协作 Agent 的事实源。 |
| ADR-004 | 第一阶段 memory 使用 Markdown。 | Markdown 简单、可审计、封闭 VM 友好；暂不引入 SQLite 或向量库。 |
| ADR-005 | 第一阶段使用 anchored patch，不做 AST edit。 | hash + old_text + line 校验已经足够支撑 MVP 的可控修改。 |
| ADR-006 | 长需求应写入文件，让 Agent 读取。 | 直接把大段需求塞进 prompt 会挤占上下文，不利于长任务。 |
| ADR-007 | 封闭 VM 目标优先于联网能力。 | 第一阶段只允许访问指定 AI API，不引入公网搜索和自动下载依赖。 |

## P5 收口结论

| 项目 | 结论 | 依据 |
|---|---|---|
| 主链路 | 通过 | 百炼真实小改复测已跑通 todo、dry_run、apply_patch、session allow、rollback、run_tests、git_diff。 |
| 测试 | 通过 | P5 收口时 90 个 unittest、compileall、xlsx 检查、diff check 均通过；P10 当前代码已跑通 253 个 unittest。 |
| 日用入口 | 通过 | README 已补只读分析和小改任务命令模板。 |
| 开放风险 | 可接受 | shell 仍非沙箱、prompt injection 仍需靠审批和封闭 VM；provider/model 专用 tokenizer、输出 reserve、managed skills、完整 reviewer 和完整 OMP ToolChoiceQueue 继续后置评估。 |
| 下一阶段 | P10 Intelligence Runtime 真实复测 | T-109/T-110/T-111/T-112/T-113 已补 RequirementContract、CompletionAudit、MiniToolChoiceQueue、Planner/Explore 和 Patch Reviewer；下一步跑显式要求测试的小改任务，验证 reviewer 的真实行为。 |

## 推荐工作流

处理普通代码任务：

1. 用户用自然语言描述目标。
2. Agent 先 `list_files` 和 `read_file` 理解项目。
3. 修改前必须读取目标文件。
4. 修改已有文件必须使用 `apply_patch`。
5. 修改后必须运行相关测试。
6. 最后调用 `git_diff` 展示变更。

处理复杂需求：

1. 将需求写入 `docs/requirements/*.md`。
2. Agent 读取需求文件，不把整篇长需求直接塞进 prompt。
3. Agent 使用 todo 工具拆解任务。
4. 每完成一小步运行测试或局部验证。
5. 最终输出已完成项、未完成项、测试结果和 diff 摘要。

审批建议：

- 默认使用 `always-ask`。旧的 `ask` / `auto-read` 会兼容映射为 `always-ask`。
- 需要允许写文件但继续管住命令执行时，可以使用 `write`。
- `read`、`state`、`interaction` tier 工具默认不额外审批；当前 `state` 用于 session todo，`interaction` 用于 `ask_user`。
- `yolo` 只用于完全可信仓库和封闭 VM。
- `--tool-approval tool=allow|prompt|deny` 可覆盖单个工具；`prompt` / `deny` 是配置级护栏，不被 session allow 绕过；REPL 中可用 `/approval` 临时调整当前会话策略。
- approval prompt 会按剩余 `budget_seconds` 等待输入；超时会取消工具调用并回传 tool error。
- shell / run_tests / apply_patch 都应保留可审计日志。

## 下一步开发入口

用户确认本文件后，建议按以下顺序继续：

1. 跑一次显式要求“改代码并补测试”的真实小改，观察 Patch Reviewer 是否能驱动模型补测试、查调用方或诚实停止。
2. 继续真实需求设计链路：基于 `msp-pay` / `zqylpayment` 现有证据输出服务费结算实现设计，明确复用点、缺口和必须补充的项目/数据来源。
3. 若仍出现关键工具不用/乱用，再扩展 MiniToolChoiceQueue；若 reviewer 在真实任务中误报或漏报，再按失败样本加强规则。
