# Enrichment 结果卡片刷新修复设计

## 1. 状态与范围

- 日期：2026-09-04
- 状态：规格审查通过，用户已书面确认
- 当前基线：`master_0811` 最新代码；不得假设 2026-09-03 之后的 UI、配置或持久化代码未变化
- 适用范围：两阶段分析中，Decision 已显示后，Enrichment 完成但历史结果卡片的声压级、响度等摘要值仍为 `--` 的问题

本修复只补齐 Enrichment 到结果卡片的显示投影。不修改录音、DSP/AI 算法、PLC、数据库 schema、artifact 格式、界面布局或性能目标。

## 2. 已确认的证据与根因

从最新生产结果读取到的完整 Enrichment artifact 已包含正确标量：

- SPL 峰值 `58.14 dB`；
- SPL 平均值 `53.71 dB`；
- 响度峰值 `11.10 sone`；
- 响度平均值 `8.08 sone`。

因此音频、计算和 artifact 持久化不是故障点。

当前数据流为：

1. 0.5 秒 Decision 路径为了延迟目标，允许把未参与限值判定的 SPL/响度等显示指标留为 `--`。
2. Enrichment 随后生成完整 `summary_payload` 和 `chart_payload`。
3. `ResultDashboardModule.apply_enrichment_projection()` 只缓存完整 payload，并通过 `_merge_chart_overlay()` 把 `chart_payload` 合并到右侧图表投影。
4. `ResultHistoryModule` 的 payload provider 仍指向 `ResultHistoryStore` 中的轻量 Decision payload；Enrichment 不是第二个终态事件，因此卡片没有新 transition，也没有重新调用摘要更新。
5. 结果卡片顶部偶尔可见 SPL 数值来自实时指标缓存的终态兜底，不代表完整结果摘要已经刷新；响度实时缓存缺失时仍显示 `--`。

根因是 Enrichment 的显示投影只覆盖图表，没有覆盖同一 payload 中已经完成的结果摘要标量。

## 3. 目标行为

1. Decision 提交和绘制时序保持不变；未参与快速判定的重型指标可暂时显示 `--`。
2. Enrichment 投影成功后，同一 run 的结果卡片必须自动刷新完整摘要，不要求用户切换结果、选择故障类型或再次操作。
3. 刷新结果卡片当前已经显示的六行中的 Enrichment 测量字段及顶部平均指标，不新增行：
   - SPL 峰值；
   - SPL 平均值；
   - 响度峰值；
   - 响度平均值；
   - 频谱最低得分；
   - AI 行保持 Decision 值，不由 Enrichment 改写。

完整 overlay 可以继续保留 `summary_payload` 中未被卡片渲染的 FFT、octave 等行，供既有其他消费者使用；本修复不在结果卡片中增加这些行或改变表格结构。
4. 当前选中的 run 与未选中的历史 run 都必须更新；后者再次选中时直接显示完整值。
5. Enrichment 只能补充测量摘要和图表，不能改变：
   - 自动 Decision 的 PASS/FAIL、AI 结果和限值结论；
   - 最新人工 PASS/FAIL、故障类型和人工 revision；
   - 历史卡片 revision、选择、顺序、终态时间或操作权限；
   - PLC 值或写入次数。
6. 相同 Enrichment 重放仍然幂等，不得重复终态、重复卡片或重复 PLC 写入。
7. 重新分析、attempt/terminal identity 不匹配、淘汰或关闭后的 Enrichment 继续按现有规则拒绝。

## 4. 选定方案

采用“Enrichment 显示覆盖层 + 现有卡片原位刷新”。

### 4.1 Enrichment 显示覆盖层

把现有 `_merge_chart_overlay()` 扩展为职责明确的 Enrichment display overlay。输入为：

- `current`：当前 `ResultHistoryStore` 中的权威 Decision/人工显示 payload；
- `enrichment`：同一不可变 `(run_id, attempt_id, terminal_revision)` 的完整 payload。

输出规则：

1. `chart_payload` 使用 Enrichment 完整值。
2. `summary_payload.rows` 使用 Enrichment 完整测量行，以 `item_key` 为稳定身份。
3. `summary_payload.overall_status`、`overall_label`、人工故障显示及错误状态继续使用 `current`，防止完整自动 artifact 覆盖人工判定。
4. AI/限值结论不得从不匹配的 payload 重算；身份和 judgment 仍由现有 persistence/coordinator fence 保证。
5. Dashboard config 继续使用 `current.dashboard_config`，避免图表补齐改变布局或通道选择。

若 Enrichment 缺少合法摘要，则只补图表；不得清空现有摘要。

### 4.2 结果卡片原位刷新

`ResultHistoryModule` 增加一个仅在其 Qt owning thread 调用的 payload refresh 接口：

- 只接受当前已缓存、终态、run id 匹配的卡片；
- 不修改 `ResultHistoryStore` 或 `ResultHistoryCardSnapshot`；
- 调用现有卡片摘要更新路径更新表格；
- 同时重放终态 metric view，使顶部“平均声压级/平均响度”使用新摘要；
- 保持卡片 QWidget 实例、manual judgment 控件父子关系、滚动位置、选中状态和 revision 不变；
- run 不存在、仍活动或已从 `_card_entries` 所有权映射移除时返回未应用，不新增错误状态。模块在隐藏/`deleteLater` 前先移除映射，因此不为理论上的“已销毁但仍登记”状态增加第二层 QObject 检查。

`ResultHistoryModule` 的 payload provider 必须改为 overlay-aware provider：对于同一有效
Decision identity，始终返回“当前权威 Decision/人工 payload + 已缓存 Enrichment”的最新 display
overlay；没有有效 Enrichment 时才返回原 `ResultHistoryStore` payload。这样后续人工判定、选择切换、
普通 history transition、refresh/retry 或卡片重建都不会把已经补齐的测量值恢复为 `--`。

若采用显式重放而非 provider 包装，必须在每个兼容 history transition 后重新应用同一 overlay，并用测试
证明不存在回退。优先采用 overlay-aware provider，因为它只有一个读取入口，不增加新的业务事实。

### 4.3 Enrichment 投影顺序

匹配的 Enrichment 到达 Dashboard 后：

1. 按现有不可变身份、revision、reanalysis、duplicate 和 lifecycle fence 验证。
2. 缓存完整 Enrichment。
3. 基于当前权威 payload 生成 display overlay。
4. 原位刷新对应历史结果卡片，即使该 run 当前未被选中。
5. 若该 run 当前被选中，再用同一 overlay 更新右侧图表/详情投影。
6. 后续任何兼容的历史卡片重绘或人工判定 transition 都通过 overlay-aware provider 读取同一完整摘要；只有重新分析、identity 变化、淘汰或关闭才清除 overlay。

历史卡片刷新失败不得回滚已经 durable commit 的 Enrichment；沿用现有 UI 诊断/后续重投影恢复语义。

## 5. 并发与相邻操作

- Enrichment 在人工判定前到达：先显示完整测量值；后续人工判定只改人工结论，测量值保持。
- Enrichment 在人工判定后到达：使用完整测量行，但保留最新人工结论、故障类型和 revision。
- 新录音开始后旧 run 未选中：旧卡片仍刷新；重新选择时直接显示完整摘要和图表。
- 连续 Enrichment 重放：只应用一次。
- 重新分析开始：旧 Enrichment 按现有不可逆退役规则拒绝；新 attempt 使用自己的完整摘要。

不增加线程、定时轮询、数据库读取或 DSP 重算。

## 6. 测试与验收

必须使用确定性的 Qt/集成测试覆盖：

1. 轻量 Decision 的 SPL/响度四行和顶部平均值为 `--`；匹配 Enrichment 到达后，同一卡片实例自动显示完整数值。
2. Enrichment 后不发生 history revision、选择、卡片顺序、终态时间或卡片实例变化。
3. 未选中旧 run 在新录音活动期间收到 Enrichment，旧卡片立即刷新；选择旧 run 后摘要和图表均完整。
4. 人工故障类型先于 Enrichment：刷新测量值但保留人工 NG、故障类型、人工 revision 和人工总结果文本。
5. Enrichment 先于人工判定：人工操作后完整测量值保持。
6. Enrichment 后再执行人工判定、普通 history transition、refresh、选择切换或卡片重建，完整测量值均不回退为 `--`。
7. Enrichment 重放、身份不匹配、重新分析、淘汰和关闭路径不产生错误刷新。
8. SPL/响度单位和格式继续由现有 `ResultHistoryCard` 规则控制，包括 sone/phon。
9. 现有 Decision paint acknowledgement 只确认 Decision，不因摘要补齐重复发出。
10. 录音完整性、PLC 不重复写、历史选择、人工判定、图表补齐和 UI 布局测试继续通过。

使用一份接近生产结构的 payload，明确断言 `58.14 dB`、`53.71 dB`、`11.10 sone`、`8.08 sone` 等非空值，避免只断言“不等于 --”。

## 7. Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: 录音内容与生命周期、Decision/Enrichment 性能语义、PASS/FAIL、AI 与限值结论、人工故障类型与 revision、PLC、数据库 schema、artifact/payload 格式、历史/详情/导出、UI 布局与交互
- Allowed breakage: none
- Migration strategy: none

## 8. 非目标

- 不把未受限 SPL/响度重新移回 0.5 秒 Decision 关键路径。
- 不改变 DSP 计算、数值精度、单位或阈值语义。
- 不创建第二个终态历史事件来触发刷新。
- 不把 Enrichment payload 写回 `ResultHistoryStore` 作为新的业务事实。
- 不修改卡片布局、字号、表格结构或人工判定按钮。
- 不借机修复当前工作区内无关的数据库、录音后端或测试改动。
