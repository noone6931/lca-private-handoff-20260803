# Harness Engineering Governance

更新时间：2026-07-15

## 目标

LCA 使用 Harness 提供安全边界、工具协议、可观测性和有界纠偏，但不尝试把概率模型改造成由无限文本规则驱动的确定性状态机。OMP 源码是首要架构参照；LCA 根据 Python、本地运行、封闭 VM、百炼 provider、单 Agent 和企业多 root 场景做裁剪适配，不做无依据照搬。

## 三层职责

| 层 | 允许承担的职责 | 不应承担的职责 |
|---|---|---|
| Hard invariant | workspace/approval 边界、工具参数与结果配对、写入 hash/path 校验、deadline/abort、未验证写任务不得伪报完成 | 判断回答是否“像人一样好”、要求固定措辞、为单一业务样本增加关键词规则 |
| Soft steering | system prompt、tool schema、ToolChoice directive、一次提醒后有界升级、已有 evidence 复用提示 | 无限 force/rewrite、多个 Owner 重复解释同一语义错误 |
| Evaluation | isolated reviewer、黑盒场景、RunSummary、统计成功率、人工验收 | 把单次模型波动直接升级为 Runtime gate |

## 新机制准入

1. 开始前必须定位 OMP 对应源码、Owner、状态机和终止条件；没有直接对应时明确记录 LCA 本地增强及理由。
2. 权限越界、数据破坏、协议损坏和工具结果不配对可由单个可靠样本立即修复。
3. 普通语义或收敛问题必须至少有两个独立可复现样本，或一个跨 provider/跨场景复现，才能进入 Runtime 设计。
4. 新机制必须只有一个 Owner，写明最大尝试次数、deadline 行为、telemetry、失败终态和未来 merge/delete 条件。
5. 稳定性批次默认不增加 gate 数量；新增 gate 必须替换或合并既有机制，例外需要 ADR。
6. 禁止业务关键词特判、固定回答模板和为 benchmark acceptance 反向塑造生产逻辑。

## 停止规则

- 单个失败样本先记录和分类，不直接补丁。
- 同一问题连续两次修复仍转移到下一 gate 时，停止局部补丁，回到完整生命周期和 OMP Owner 边界复审。
- semantic reviewer 默认一次；修正默认最多一次；额外轮次必须有明确安全不变量和 ADR。
- 一个问题簇的架构稳定化默认最多两个工作日，超时后保留风险并回到产品主线，除非存在安全或数据完整性阻塞。
- safe partial 是安全终态，不是产品可用性 PASS。

## 复杂度预算

- `tests/test_architecture_boundaries.py` 对所有 production Python 模块应用默认 900 行上限；历史债务和薄 facade 使用显式只降不升 ceiling。
- 行数只是预警，不替代职责、import direction、公开 API 和阶段顺序测试。
- 新增模块不能只把原大文件机械搬家；必须形成单向依赖和明确 Owner。
- 正常开发周期至少 70% 投入用户能力与真实交付，Harness/架构治理最多 30%；安全事故可临时例外。

## 评测与发布

- Hard invariant 在每次运行中必须 100% 满足。
- 语义可用性使用不少于三次 fresh-state 样本报告分布，不以固定措辞判定；单个离群失败不自动触发代码修改。
- 当前 S4 的阶段验收目标是至少 2/3 运行产出可用 `final` 或业务上诚实且完整的 typed blocked 报告，同时 3/3 不得幻觉真实 Owner/表/接口、不得越权写入。
- offline unittest/benchmark 证明确定性合同，不能替代真实 provider 黑盒成功率。
- candidate 只有在独立 review、离线门禁和约定 live gate 都通过后才能 promote stable。

## 分工

- 大猛：实现、测试、immutable candidate 和结构化原始证据。
- 小牙：immutable stable/candidate 的真实用户黑盒压测，不修改实现。
- 小红：OMP 对照、架构归因、复杂度治理、独立 review、状态文档和发布判断。
