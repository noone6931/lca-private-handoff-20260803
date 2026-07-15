# 拓展服务费结算 S5 最小业务架构契约

## 1. 文档定位

本文是 LCA 真实写路径压测的业务契约，不是完整生产方案。目标是给 T-203 stable LCA 一道边界完整、可验证、可回滚的真实企业任务，避免因需求输入缺失而把业务不确定性误判为 Harness 缺陷。

本文严格区分：

- **源码事实**：现有仓库已经证明的行为。
- **需求事实**：V1.3 明确要求的行为。
- **架构决策**：本轮为闭合 S5 而作出的方案选择。
- **后续项**：不进入第一个写路径切片的能力。

范围锁定：本轮唯一母需求是《拓展服务费结算需求文档 V1.3》。同级目录中的“例外核心企业批量导入”不属于本任务，不作为源码搜索词、相邻实现、候选切片或验收材料。

## 2. 已确认事实

### 2.1 需求事实

- 待制单数据同时满足：直接保理、60-已放款、拓展服务费大于 0、制单状态为待制单。
- 上线后只处理新进入流程的订单，不做历史数据初始化。
- 列表需要云信编号、业务单号、融资金额、融资申请方、确权方、额度发起方、资金方、归属公司、申请日、付款日、到期日和拓展服务费。
- Word 表格中的“保荐商净服务费”取订单拓展服务费。

依据：`/Users/chengming/mynote/1_projects/0630_YXR-971_平台通用优化/需求文档-拓展服务费结算/需求文档-拓展服务费结算V1.3.md`。

### 2.2 源码事实

- 云信融资的直接保理底层值为 `FACTOR_TYPE=4`；`104` 是业务组合码，不是数据库字段原值。
- 放款完成为 `T_FINANCE_PROJECT.APPLY_STATUS=60`。
- 云信业务 `XF0003` 展示为“拓展服务费”；`YJ0001` 是独立的保荐商佣金，不得混用。
- crcl `findDetailList` 返回的是 `T_PRJT_PLAN_CHARGE_FEE.CHARGE_EXPECT_VALUE`，映射到旧名 `expectApplySponsorTaxFee`，它不是放款后的实际费用。
- finance-base 在状态 60 事件中同时读取计划费用与 loan-base 实际费用，实际金额来自 `LoanBaseFee.chargeRealValue`。
- loan-base 是实际息费事实 Owner：`T_LOAN_BASE_FEE` 保存 `CHARGE_NO`、`SOURCE_ID`、`DEAL_TYPE` 与 `CHARGE_REAL_VALUE`，并已有按业务源和费用项读取有效记录的 API/Mapper。
- loan-base 在放款审核事务中先计算并保存实际息费，再将放款状态更新为审核通过；finance-base 收到放款结果并更新为 60 后才发布状态事件，因此状态 60 listener 读取实际 `XF0003` 不存在已知的先发事件、后落费用时序空洞。
- loan-application 已通过 Feign 调用 loan-base 的实际费用 API，并携带 `LoanBaseFee` DTO；它是应用编排/展示调用方，不持有实际费用表，也未发现拓展服务费候选或结算生命周期。
- loan-base 与 loan-application 均未发现“待制单、已制单、结算单编号、制单状态、回退整单”等目标生命周期；它们不能替代 payment 的结算候选 Owner。
- 现有 finance-base 状态消息只把多费用按付费方汇总为 `PayerInfo.finalAmount`；该值不能作为 `XF0003` 单项金额。
- 状态消息已经包含确权方、资金方、融资申请方、额度名称和三个日期，但没有完整携带归属公司与单项费用列表。
- payment 会把 `businessParam` 原文保存为 JSON，并将部分字段更新到 `BusinessOrderEntity`；现有结构不能支持完整待制单筛选。
- mpspay 的页面、路由和 API 映射已有清晰 Owner：`src/views`、`src/router`、`src/assets/interface/pay/platformPayment.js`。

关键依据：

- `/Users/chengming/mycode/project/zqylfinancebasemasterfccb090b/src/main/java/com/yljr/finance/base/application/listener/ApplyStatusChangeEventListener.java`
- `/Users/chengming/mycode/project/zqylfinancebasemasterfccb090b/src/main/java/com/yljr/finance/base/application/mq/mqprovider/dto/PlatformPaymentRequestVo.java`
- `/Users/chengming/mycode/project/zqylloanbasemaster4a6bf5fe0/src/main/java/com/yljr/loan/base/domain/entity/LoanBaseFee.java`
- `/Users/chengming/mycode/project/zqylloanbasemaster4a6bf5fe0/src/main/resources/mapper/LoanBaseFeeMapper.xml`
- `/Users/chengming/mycode/project/zqylloanbasemaster4a6bf5fe0/src/main/java/com/yljr/loan/base/domain/platform/impl/PlatformLoanDomainServiceImpl.java`
- `/Users/chengming/mycode/project/zqylloanbasemaster4a6bf5fe0/src/main/java/com/yljr/loan/base/interfaces/facade/chargefee/ChargeFeeController.java`
- `/Users/chengming/mycode/project/zqylloanapplicationmasterd6cdba20/src/main/java/com/yljr/loan/application/application/feign/loanbase/ZqylLoanBaseFeign.java`
- `/Users/chengming/mycode/project/zqylcrclfinancemaster7b875cd3c/src/main/java/com/yljr/crcl/finance/domain/finance/service/impl/PrjtPlanChargeFeeServiceImpl.java`
- `/Users/chengming/mycode/project/zqylpaymentmaster9d423763/src/main/java/com/yljr/payment/payment/mq/consumer/strategy/BusinessStatusChangeConsumer.java`
- `/Users/chengming/mycode/project/zqylpaymentmaster9d423763/src/main/java/com/yljr/payment/payment/domain/entity/BusinessOrderEntity.java`

## 3. Owner 决策

| 能力 | Owner | 决策理由 |
|---|---|---|
| 实际费用事实 | loan-base | `T_LOAN_BASE_FEE.CHARGE_REAL_VALUE` 及其有效记录查询由 loan-base 持有；S5-1 复用，不新增或修改该服务。 |
| 计划费用与融资订单事实 | crcl-finance + finance-base | crcl-finance 提供计划费用，finance-base 持有融资状态/订单上下文；两者都不能替代实际 `XF0003`。 |
| 状态消息契约 | finance-base producer + zqylpayment consumer | 现有放款状态消息已经承担平台费用数据同步，扩展可保持事件时序。 |
| 拓展服务费候选快照 | zqylpayment | 用户已确认目标后端；结算、下载和相邻预制单能力也在该服务。 |
| 拓展服务费结算主从模型 | zqylpayment | 后续制单、回退必须在一个本地事务内完成。 |
| 页面与 API 调用 | mpspay | 用户已确认目标前端，且现有平台费用页面位于该仓。 |
| LCA Harness | local-coding-agent | S5 期间冻结；只有完整契约下出现可复现 Harness 缺陷才另开修复。 |

### 3.1 跨服务复用审计

S5-1 开写前已对当前可见服务按“数据表、生成事务、读取 API、目标生命周期”四层审计，而不是只按中文关键词判断：

| 服务 | 已有能力 | S5-1 决策 |
|---|---|---|
| zqyl-loan-base | 实际费用生成、`T_LOAN_BASE_FEE` 持久化、按 source/dealType/chargeNo 查询 | 只读复用事实与 API，不写该服务。 |
| zqyl-loan-application | loan-base Feign、放款应用编排与费用 DTO | 只作链路证据，不新增结算逻辑。 |
| finance-base | 状态 60 Owner、现有 payment 状态事件、实际费用 API 消费 | 扩展现有通用消息契约，不重建费用计算。 |
| crcl-finance | 计划费用与相邻平台费用协议能力 | 只复用计划费用字段；不把预计金额或协议签署当结算实现。 |
| zqylpayment | 状态消息消费、本地事务、平台费用后端边界 | 持有候选快照和后续结算状态。 |

当前可见源码中没有发现与“拓展服务费候选 + 制单/回退生命周期”等价的现成 C 服务实现，因此 S5-1 写范围仍是 finance-base + zqylpayment。后续若新增服务源码暴露同一数据模型和生命周期，必须先修订本契约再写代码，不能在既定方案上叠加第二套对象。

禁止方案：

- 不从 `BusinessPayer.finalAmount` 推导拓展服务费。
- 不把 `expectApplySponsorTaxFee` 当作放款后的实际金额。
- 不让 zqylpayment 直接连接 finance-base/crcl 数据库。
- 不复制预制缴费单领域对象充当拓展服务费结算对象。
- 不在 LCA 中增加“拓展服务费”关键词 gate 或专用工具调度规则。

## 4. 第一个 S5 切片

### 4.1 名称

`S5-1：放款后拓展服务费候选快照 + 待制单后端列表`

### 4.2 包含范围

1. finance-base 状态消息增加通用单项费用列表和缺失的订单字段。
2. zqylpayment 消费状态 60 消息时，提取 `chargeNo=XF0003` 的实际金额并幂等写入候选快照。
3. 非直接保理、非 60、实际 `XF0003 <= 0` 或实际金额缺失时，不产生待制单候选，并记录可定位日志。
4. zqylpayment 提供待制单分页查询接口，支持需求中的四个主体模糊条件与两个日期范围。
5. 默认按融资付款日倒序。
6. 添加 producer 映射、consumer 幂等和查询过滤的自动化测试。

### 4.3 明确不包含

- mpspay 页面。
- 结算单编号。
- 合并制单事务。
- Word 生成。
- 整单回退。
- Excel/下载中心。
- 历史数据回灌。

这些能力在 S5-1 通过后按候选 2 至 5 继续，不挤进首个切片。

## 5. 跨服务消息契约

### 5.1 Producer 扩展

在 `PlatformPaymentRequestVo` 增加可选字段：

```text
feeInfoList: List<FeeInfo>

FeeInfo:
  chargeNo
  chargeName
  payerId
  payerName
  chargeMethod
  chargeMethodActual
  chargeExpectValue
  chargeExpectRate
  chargeRealValue
  chargeRealRate
```

`BusinessParam` 增加：

```text
bizId
factorType
applyCrclNo
factorCrclNo
applicantId
applicantName
funderId
confirmPartyId
limitId
limitName
vestTaxNo
vestCompanyName
```

既有字段保持，新增字段全部可选，以保证新 producer 对旧 consumer 的向后兼容。`feeInfoList` 使用通用费用结构，不把消息协议写死成单一拓展服务费功能。

### 5.2 金额口径

架构决策：S5-1 只接受 `chargeNo=XF0003` 的 `chargeRealValue`。

- 不回退 `chargeExpectValue`。
- 实际值为空或不大于 0 时不进入待制单。
- 原因：数据范围是已放款订单，结算不能用预计金额静默替代实际金额。

该决策需要用一笔脱敏真实订单在业务验收时复核，但不影响代码契约的确定性。

## 6. zqylpayment 候选快照

### 6.1 表职责

新增独立候选快照表，建议表名：`T_EXT_SERVICE_FEE_ORDER`。

它是 zqylpayment 对上游放款事实的本地只读快照，也是后续制单状态的 Owner；不修改融资订单原表。

建议核心字段：

| 字段 | 说明 |
|---|---|
| `ID` | 主键 |
| `INNER_APP_ID` | 接入应用 |
| `BUSINESS_NO` | 业务单号 |
| `BIZ_ID` | 上游项目 ID |
| `BIZ_TYPE` | 业务类型 |
| `FACTOR_TYPE` | 保理类型，直接保理为 4 |
| `BUSINESS_STATUS` | payment 本地业务状态，已放款为 2 |
| `MAKE_STATUS` | 0 待制单，1 已制单 |
| `APPLY_CRCL_NO` | 融资申请云信编号 |
| `FACTOR_CRCL_NO` | 融资生成云信编号 |
| `FINANCE_AMOUNT` | 融资金额 |
| `APPLICANT_ID/NAME` | 融资申请方 |
| `CONFIRM_PARTY_ID/NAME` | 确权方 |
| `LIMIT_ID/NAME` | 额度发起方 |
| `FUNDER_ID/NAME` | 资金方 |
| `VEST_TAX_NO/VEST_COMPANY_NAME` | 归属公司 |
| `FINANCE_APPLY_DATE` | 融资申请日 |
| `FINANCE_LOAN_DATE` | 融资付款日 |
| `DUE_DATE` | 到期日 |
| `EXT_SERVICE_FEE` | `XF0003.chargeRealValue` 快照 |
| `SETTLEMENT_ID` | 后续制单关联，首切片为空 |
| `VERSION` | 乐观锁版本 |
| 审计字段 | 创建/修改人和时间 |

约束：

- 唯一键：`(INNER_APP_ID, BUSINESS_NO)`。
- 查询索引至少覆盖 `(MAKE_STATUS, BUSINESS_STATUS, FINANCE_LOAN_DATE)`。
- 金额使用与现有费用表一致的 decimal 精度，由实际数据库规范确认，不在 Harness 中猜测。

### 6.2 幂等与状态

- 状态 60 / payment 状态 2：满足 `factorType=4` 且实际 `XF0003>0` 时 upsert。
- 重复状态消息：按唯一键更新未制单快照，不重复插入。
- 退回或删除消息：将候选标记为不可制单；S5-1 不物理删除。
- 后续一旦 `MAKE_STATUS=1`，状态消息不得覆盖已经制单的金额快照。

### 6.3 事务边界

候选 upsert 必须加入 payment 现有 `handleBusinessOrderAndSaveConsumerRecord` 本地事务，与业务单状态、payer、businessParam 和消费记录共同提交或回滚。

MQ 消费异常继续使用现有重试与消息事务记录，不增加第二套重试系统。

## 7. 待制单查询契约

建议接口：

```text
POST /web/middle/extensionServiceFee/pendingPage
```

查询条件：

```text
confirmPartyKeyword
limitKeyword
funderKeyword
vestCompanyKeyword
financeLoanDateStart / financeLoanDateEnd
financeApplyDateStart / financeApplyDateEnd
page / pageRow
```

固定过滤：

```text
FACTOR_TYPE = 4
BUSINESS_STATUS = 2
MAKE_STATUS = 0
EXT_SERVICE_FEE > 0
```

返回 S5-1 所需全部列表字段，同时返回 `applyCrclNo` 与 `factorCrclNo`。最终页面“云信编号”展示哪一个字段留到 UI 切片前由业务样本确认，不在后端静默合并。

## 8. 测试与验收

### 8.1 自动化测试

finance-base：

- 状态 60 且 `XF0003` 有实际值时，消息包含完整 `feeInfoList` 与订单字段。
- `XF0003` 预计值与实际值不同，消息保留两者且 S5 候选口径使用实际值。
- 直接保理发送 `factorType=4`，不发送组合码 104。

zqylpayment：

- 首次消息插入一条候选快照。
- 重复消息只更新同一条未制单快照。
- 非直接保理、非已放款、`XF0003` 缺失/为 0 不进入待制单。
- payer 多费用合计与 `XF0003` 不同，候选仍只取单项实际值。
- 查询固定过滤、主体模糊搜索、日期范围和付款日倒序正确。
- consumer 任一步骤失败时业务状态、候选快照和消费记录共同回滚。

### 8.2 构建前置条件

两个后端仓都依赖私有 `com.yljr:parent:0.0.5-SNAPSHOT` 和内部依赖。T-206 已把私有 Maven 制品与缺失的 iText 5.5.3 制品放入独立 artifact bundle；真正 S5 运行仍必须先在隔离副本做 Maven preflight，并满足以下任一项：

1. 现有 Maven `settings.xml` 能访问公司 Nexus 并解析私有 parent；或
2. 已完整缓存私有依赖的 Maven repository。

preflight 不输出 `settings.xml` 中的凭据。若现有配置无法连接，则由用户提供可用配置或离线缓存；否则 LCA 即使生成 patch，也不能满足“实际测试通过”的 S5 验收，必须诚实 BLOCKED。

目标生产工具链是 Oracle JDK `1.8.0_121`。宿主机默认 JDK 不代表项目构建边界：本地隔离门禁使用显式 `JAVA_HOME` 指向兼容 Java 8 发行版，且不得为适配宿主 JDK 修改业务 POM；交付前再在生产同款 Oracle 8u121 环境复核。T-206 本地 Java 8 工具链为 Amazon Corretto `1.8.0_472`，宿主默认 JDK 25 保持不变。

### 8.3 LCA 黑盒验收

必须由 immutable T-203 stable 在 `/private/tmp` 隔离副本执行：

```text
Scope -> Research -> Preview -> Patch -> Targeted tests -> Diff -> Reviewer -> Delivery audit
```

通过标准：

- 不修改业务原目录。
- 不新增业务关键词 Harness gate。
- 不把预计费用或 payer 汇总金额当实际 `XF0003`。
- 有真实 patch、测试退出码、diff、reviewer 和交付审计。
- 失败时能恢复或给出带证据的 BLOCKED，不伪报完成。

## 9. 回滚边界

S5-1 的回滚单位是整个跨仓切片：

- finance-base：删除新增可选消息字段和组装逻辑。
- zqylpayment：删除新增 consumer 投影、查询模块和候选表 DDL。
- 数据库：上线前提供对应 rollback DDL；由于不回灌历史数据，候选表可整体回滚。
- 原有平台缴费消息字段与消费行为保持兼容，不修改其既有语义。

## 10. 后续切片

1. `S5-2`：结算单主表/明细表、日流水编号、行锁与制单事务。
2. `S5-3`：mpspay 制单/已制单页面和接口接入。
3. `S5-4`：Word 模板生成与下载；模板编号、复核人和签章冲突先确认。
4. `S5-5`：整单回退与并发复验。
5. `S5-6`：导出与下载中心。

每个切片都沿用同一条规则：业务契约先闭合，stable LCA 后执行；只有可复现的 Runtime/Harness 缺陷才修改 LCA。
