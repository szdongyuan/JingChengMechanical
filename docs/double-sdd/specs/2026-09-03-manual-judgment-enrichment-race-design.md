# 人工判定与图表补齐竞态修复设计

## 1. 状态与范围

- 日期：2026-09-03
- 状态：规格审查通过，用户已书面确认
- 适用范围：两阶段分析中，Decision 已显示但 Enrichment 尚未投影时，用户修改人工判定并立即开始新录音的时序

本修复只处理人工判定与图表补齐之间的竞态。不修改录音、自动判定、AI、PLC、数据库 schema、图表算法、界面布局或性能目标。

## 2. 已确认的现象与根因

复现时序：

1. 一条录音完成，Decision 的 PASS/FAIL 与摘要已经显示，图表仍为空。
2. 用户选择故障类型，产生 `TERMINAL_RESULT_REPLACED` 人工结果。
3. 用户立即开始新录音，结果页自动跟随新录音。
4. 旧录音的 Enrichment 随后完成，但旧录音的图表没有显示。
5. 再次选择旧录音的故障类型后，图表出现。

根因有两层：

- `MultiRunCoordinator._dispatch_result_history()` 在人工结果替换时删除该 run 的 `_decision_projections`，导致稍后到达的 `EnrichmentCommittedEvent` 无法映射为 UI 图表投影。
- `ResultDashboardModule.apply_enrichment_projection()` 要求当前历史卡片 revision 与 Decision 提交时的 `expected_revision` 完全相等。人工判定会合法地增加卡片 revision，因此已经属于同一分析 attempt 的 Enrichment 也会被拒绝。

第二次人工选择能够显示图表，是因为 Enrichment 已经完成 durable artifact 替换，后续人工结果读取到了完整 artifact。这只是偶然绕过竞态，不是正确的数据流。

## 3. 目标行为

1. Decision 已显示后，用户可以立即选择或修改人工 PASS/FAIL/故障类型，不必等待图表。
2. 用户可以立即开始新录音；不得为了图表阻塞、延迟或取消录音。
3. 属于原自动分析 attempt 的 Enrichment 完成后，旧录音的完整图表必须被保留。
4. 若旧录音仍被选中，图表自动补齐；若结果页已跟随新录音，之后重新选择旧录音时必须直接显示完整图表。
5. Enrichment 只能补充图表和兼容完整 payload，不能覆盖最新人工 PASS/FAIL、故障类型、人工 revision、历史卡片状态或 PLC 值。
6. 同一 Enrichment 重放仍然幂等，不得重复创建终态、重复 PLC 写入或重复图表物化。
7. 重新分析开始后，旧 attempt 的 Enrichment 必须继续被拒绝；新 attempt 只能接收自己的 Enrichment。

## 4. 选定方案

采用“不可变分析身份 + 可变人工覆盖层”的正交投影模型。

### 4.1 分析身份

图表归属继续由不可变身份确定。端到端的权威身份仍是现有
`AnalysisBundleIdentity`：

`(run_id, analysis_attempt_id, content_revision, algorithm_revision,
config_identity, calibration_identity, channel_mapping_identity,
analysis_window)`

`content_revision` 仍是必需字段，没有被 `terminal_revision` 替代。Persistence boundary
先按完整 `AnalysisBundleIdentity` 校验并提交 Enrichment；只有通过该权威校验后，Coordinator
才生成内部 UI 投影键 `(run_id, analysis_attempt_id, terminal_revision)`。这里的 attempt id
唯一指向已经校验过的完整 bundle identity，因此 UI 不重复传输或再次校验 content revision。

Decision 首次投影时记录内部投影键。人工判定只改变同一 run 的业务显示覆盖层，不改变音频、分析 attempt 或图表归属。

### 4.2 人工判定覆盖层

人工结果替换继续按现有 CAS revision 提交和投影，并保持最新人工结论为权威显示值。它不得删除仍待 Enrichment 的 Decision 归属。

Coordinator 在人工结果成功投影后，将该 Decision 状态的当前 history revision 前移到人工结果的新 revision，但保持 attempt、terminal revision、自动结论和 enrichment 状态不变。

### 4.3 Enrichment 接收与 UI 合并

Enrichment 仍必须匹配 run、attempt、terminal revision 和自动 judgment。history revision 不再被解释为“人工判定后身份失效”，而是作为投影顺序游标：

- 若当前卡片仍是同一 Decision 的终态，且只发生了人工结果替换，则允许图表补齐到当前卡片 revision。
- UI 只从 Enrichment 取 chart payload 及图表所需的冻结配置数据。
- UI 的 PASS/FAIL、故障类型、人工 revision 和结果摘要继续来自当前历史卡片/人工结果，不得被 Enrichment 中的自动摘要覆盖。
- 旧 run 未被选中时，完整图表按不可变分析身份有界缓存；选择该 run 时投影。

Durable 数据仍按现有分层保存：完整自动分析 payload 替换 attempt 的轻量 artifact；最新人工
`judgment`、`judgment_source`、`fault_type` 和 `manual_judgment_revision` 保存在 run 权威记录中。
Enrichment artifact 替换不得修改这些 run 字段。历史、详情和导出读取时继续把完整 payload 的图表
与 run 的最新人工事实组合，不能让 artifact 内的自动摘要覆盖人工结论。

### 4.4 必须继续拒绝的情况

- run 已被淘汰或删除；
- 重新分析已经开始；该动作永久退役旧 attempt 的 Enrichment 投影资格，即使本次重新分析随后取消，旧资格也不恢复；
- 重新分析已产生新的 attempt 或 terminal revision；
- Enrichment 的 run、attempt、terminal revision 或自动 judgment 不匹配；
- 相同 Enrichment 已经应用；
- 应用或结果页已经关闭。

## 5. 并发与顺序

人工结果和 Enrichment 可能以任一顺序到达：

- Enrichment 先到：图表先物化，随后人工结果只更新人工覆盖层，图表保持。
- 人工结果先到：Decision 归属保留并前移 history revision，随后 Enrichment 只补图。
- 两者已分别从不同线程排队：Qt 端按最终不可变分析身份和当前卡片状态重新判断，不依赖事件恰好按某一顺序到达。
- 新录音开始：只改变选择/活动卡片，不清除旧 run 已完成或待完成的图表。

所有状态继续受现有锁、Qt owning thread 和有界历史容量约束，不增加后台线程或重试循环。

## 6. 错误处理

- 身份不匹配或已失效的 Enrichment 沿用现有忽略/诊断路径，不新增用户可见错误状态。
- 图表补齐失败不得回滚人工判定或自动 Decision。
- 不通过再次触发人工判定、重新分析或重新读取音频来恢复图表。
- 不增加层层校验；只保留解决已复现竞态所需的身份、状态和顺序判断。

## 7. 测试与验收

必须使用确定性的测试覆盖：

1. Decision 空图显示 → 人工选择故障类型 → 新录音开始 → 原 Enrichment 到达 → 选择旧 run，图表立即存在且故障类型不变。
2. Decision 空图显示 → 人工判定 → Enrichment 到达，旧 run 仍被选中时自动显示图表。
3. Enrichment 先到 → 人工判定，图表保持，人工结论更新。
4. 待补齐期间连续修改故障类型，最终只保留最新人工结论，图表只应用一次。
5. 人工判定后开始重新分析，旧 attempt 的 Enrichment 被拒绝；新 attempt 的 Enrichment 正常显示。
6. 重新分析开始后再取消，旧 attempt 的迟到 Enrichment 仍被拒绝，不因恢复旧卡片而复活。
7. late Enrichment 完成 artifact 替换后，历史、详情和导出读取到完整图表，同时仍返回最新人工 PASS/FAIL、故障类型和人工 revision。
8. 新录音开始不会阻塞或改变录音生命周期，也不会使旧 run 的 Enrichment 状态泄漏或无界增长。
9. 现有 UI 布局、Decision paint acknowledgement、历史选择、Enrichment 幂等、PLC 不重复写和录音完整性测试继续通过。

## 8. Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: 用户可见结果与图表行为、界面布局、录音生命周期、PASS/FAIL 与故障类型、PLC 协议及写入语义、数据库 schema、artifact/payload 格式、历史/详情/导出、重新分析语义
- Allowed breakage: none
- Migration strategy: none

## 9. 非目标

- 不改变图表完成速度或既有 0.5/1.5 秒性能指标。
- 不改变人工判定按钮、故障类型列表或界面样式。
- 不让人工判定重新计算 DSP 或重新生成图表。
- 不让 Enrichment 修改自动或人工业务结论。
- 不重构无关 coordinator、历史存储、结果卡片或录音代码。
