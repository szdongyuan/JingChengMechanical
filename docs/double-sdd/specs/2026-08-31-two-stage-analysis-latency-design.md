# 两阶段分析延迟优化设计

## 1. 文档状态与范围

- 日期：2026-08-31
- 状态：设计方向与“快速判定为正式业务终态”均已获用户批准，等待规格复审和用户书面复核
- 适用范围：生产录音完成后的自动分析、AI 判定、结果投影、图表生成及结果 artifact 持久化
- 基准环境：当前 Windows/Python 生产技术栈，8.85 秒单通道 44.1 kHz 录音；并覆盖现有最多六条并发录音语义

本文只优化性能。录音、判定、历史记录、详情、导出、数据库 schema、PLC 协议和最终结果 payload 的
外部语义不变。

## 2. 已确认的基线与根因

当前生产路径在完整音频冻结后，按顺序执行：加载音频、Legacy DSP 判定、Resident AI、完整图表构造、
百万级结果树冻结和校验、JSON/zlib 传输、artifact 落盘，然后才发布终态结果。

对仓库现有数据和基准脚本的调查结果如下：

- 8 秒双通道基准：进程内约 3.29 秒；隔离分析进程热运行约 3.69 秒。
- 一条 8.85 秒单通道生产录音：录音文件完成至 AI 请求约 1.48 秒，Resident AI 热请求约 0.06 秒，
  AI 响应至完整结果 artifact 完成约 1.37 秒，总计约 2.91 秒。
- Resident AI 热路径通常为 50–65 ms；约 1.3–1.45 秒的启动开销只出现在每个分析进程首次按需创建
  对应 AI 工具链子进程时。
- 当前默认两个分析进程各自拥有一个 `ResidentToolchainRuntime`，所以同一工具链最多经历两次按需冷启动。
- 单条结果 JSON 约 5.13 MB，压缩 artifact 约 2.39 MB。
- profile 的主要热点是响度轨迹、STFT 评分、倍频程计算、图表生成、结果树递归冻结/验证和传输；AI 热推理
  不是稳态主要瓶颈。
- `ProcessIsolatedAnalysisExecutor` 在执行及接收大型结果前均等待 capture 全局安静，并额外要求 250 ms
  quiet fence。这个保护录音的门必须保留给 Enrichment、大型 transport、artifact fsync 和图表投影；
  它从 `capture_quiet_at` 起计入 1.5 秒图表预算，但不阻塞隔离、低优先级且无大型传输的 Decision。

因此，只把图表调用移到另一个线程不足以实现 0.5 秒判定目标；必须拆分关键路径、消除重复 DSP 和大型
结果构造，并把 AI 工具链在录音前预热为真正的稳态 Resident。

## 3. 目标

### 3.1 延迟目标

定义：

- `audio_frozen_at`：录音权威确认音频 immutable、目标帧数和连续性校验通过且 WAV 已完成安全落盘的时刻。
- `capture_quiet_at`：系统中最后一条活动录音完成上述冻结的时刻。
- `decision_committed_at`：带幂等 identity 的正式 Decision 已成功提交到现有 durable terminal
  database/history 权威的时刻；这是不可撤回的唯一业务 commit point。
- `decision_published_at`：同一 Decision 已由 UI 投影接收，并由现有 PLC result sequencer 成功完成正常路径
  写入或取得其既有成功确认的时刻。
- `decision_visible_at`：结果组件消费上述提交、更新模型，并在 Qt 事件循环完成至少一次包含新 badge 和摘要的
  `paintEvent` 后发出 test-visible render acknowledgement 的时刻。
- `charts_visible_at`：当前结果需要的完整图表已投影，且兼容的完整结果 payload 已可供历史、详情和导出读取
  的时刻。

硬性目标：

1. `max(decision_visible_at, decision_published_at) - audio_frozen_at <= 0.5 seconds`，且
   `decision_committed_at <= decision_published_at`。
2. 同一条件下，`charts_visible_at - audio_frozen_at <= 1.5 seconds`。
3. 某条录音冻结后，即使其他录音仍活动，也允许其 Decision stage 在低优先级隔离分析进程中运行；
   0.5 秒判定目标仍适用，但录音正确性是更高 gate。若目标硬件不能同时满足二者，验收失败，不能通过
   抢占 callback、Resident drain 或 WAV 落盘来换取延迟。活动录音期间不得运行 Enrichment stage，也不对
   图表执行 1.5 秒约束。
4. `capture_quiet_at` 的 eligible backlog 定义为：此前已经 `audio_frozen` 且尚未 `charts_visible` 的全部
   attempt，包括 Decision 尚未完成或尚未建立 enrichment 队列项的 attempt。现有最大六条录音容量的
   生产形状验收中，这个集合的剩余 Decision、全部 Enrichment、最终 payload/持久化和图表投影必须在
   `capture_quiet_at + 1.5 seconds` 前完成。
5. 延迟验收使用已完成应用初始化和工具链预热的正常稳态运行。启动预热必须在允许开始生产录音前完成，
   不能把首次 AI 冷启动隐藏到第一条录音的 0.5 秒窗口中。工具链崩溃、显式 revision 切换和操作系统 I/O
   故障属于异常恢复窗口，不计入正常稳态 SLA，但不得产生错误录音或错误判定。

### 3.2 正确性和兼容目标

1. 录音的采样率、通道数、通道映射、dtype、目标帧数、样本顺序、连续性、内容 revision、WAV 命名和
   安全落盘语义不变。
2. PASS/FAIL、AI 结果、所有限值结论及通道归属与当前实现一致。
3. 数值计算允许第 11.2 节定义的有限浮点误差；不得因优化导致阈值两侧的业务结论改变。
4. 最终 `result_payload` 的兼容 JSON 结构、历史记录、结果详情、导出文件和既有数据库数据读取保持不变。
5. 不新增用户配置、迁移脚本或“快速/兼容”模式开关。

## 4. 非目标

- 不修改采集 callback、Resident Ring 的音频所有权或录音窗口定义。
- 不把重型 DSP 移进 PortAudio callback 或录音落盘临界区。
- 不降低图表分辨率、删除当前图表、改变判定算法含义或跳过已配置的检测项。
- 不改变默认分析并发数、最多六条 capture、PLC 寄存器或人工复判语义。
- 不为理论上的图表阶段独立失败新增用户可见状态；Decision 提交前沿用现有整体分析失败语义，提交后按
  第 9 节的不可撤回终态处理。
- 不借机重构无关 UI、数据库 schema、历史统计或设备管理。

## 5. 核心不变量

### 5.1 录音优先

1. 只有现有录音权威发布 immutable audio reference 后，分析才可读取样本。
2. 分析只能读取已验证的 `(frame_count, channel_count)` 数据，不得截短、填充或重新映射录音来追求性能。
3. 任意 capture active 时，不启动大型图表序列化、artifact 压缩或主进程反序列化；已经在运行的图表工作
   必须可在安全边界降级、让出或延迟其投影。
4. callback 和 Resident drain 不等待分析 future、图表 future、artifact I/O 或 UI。
5. 录音完整性测试和 callback 隔离测试是性能实现的前置 gate；性能通过不能覆盖录音回归失败。

### 5.2 判定一致

1. 快速阶段使用与最终 payload 相同的 frozen config、calibration、workorder、analysis window、channel map、
   golden artifact 和 AI toolchain revision。
2. 快速阶段产生的 PASS/FAIL 是本次运行的唯一业务判定；图表阶段只能丰富显示数据，不能重新决定结论。
3. 共享 DSP 中间结果必须带 run id、analysis attempt id、输入 content revision、算法 revision 和配置
   identity；任何身份不匹配都禁止复用。
4. 对靠近阈值的数值不能使用会改变结论的近似或降采样。允许的浮点差必须通过结论一致性回归证明。

### 5.3 最终兼容

1. 完成图表补齐后，生产消费者看到的 canonical payload 与当前字段拓扑兼容。
2. 既有 artifact reference schema 和旧 artifact 解码路径不变；内部 transport 可替换为更高效表示。
3. 人工复判和导入音频分析仍能生成同一兼容结果，但本规格的 0.5/1.5 秒硬 SLA 只覆盖生产录音自动分析；
   人工复判和导入分析只做功能与兼容回归，不扩大本次性能验收范围。

## 6. 选定方案

采用“录音冻结后的两阶段分析 + 身份绑定的 DSP 中间结果 + Resident 预热 + 延迟兼容物化”。

未采用的方案：

- 只延后 UI 绘图：Legacy DSP 和大型结果冻结仍在终态之前，不能达到 0.5 秒。
- 把最终分析完全替换为实时录音图表：会把判定正确性绑定到显示节流、丢帧和 callback 调度，增加录音风险。
- 只增加进程或 BLAS 线程：单通道热点和 5 MB Python 对象递归无法靠并行稳定消除，并可能抢占录音。

## 7. 架构与数据流

```text
audio authority: exact immutable WAV/reference
    -> decision admission (allowed during other capture; isolated/low priority)
        -> decision stage (small, bounded result)
            -> authoritative UI/PLC/database terminal <= 0.5 s
            -> identity-bound DSP intermediate bundle
                -> capture quiet gate
                    -> enrichment stage
                        -> charts + compatible canonical payload
                        -> artifact/history detail/export <= 1.5 s
```

### 7.1 预热阶段

- 分析进程池仍在应用初始化期间启动并预载响度 backend。
- 生产 prerequisite 冻结当前可用 AI binding 后，把需要的 toolchain identity/entrypoint/package 发送给每个
  分析 slot；每个 slot 创建并缓存对应 Resident AI 进程，并执行无业务副作用的协议级 readiness handshake。
- 只有所有必要分析 slot 报告 DSP 与 AI ready 后，生产分析入口才标记 ready。预热失败沿用现有启动/分析
  失败语义，不在第一次录音后临时支付冷启动成本。
- 工具链 revision 或绑定改变时，正在运行的 attempt 继续使用其旧 frozen snapshot 和旧 Resident instance；
  新 revision 在每个 slot 完成预热并原子发布 `READY(revision)` 前不能成为新录音的 frozen binding。选择了
  尚未 ready 的配置时，生产开始入口保持现有 prerequisite-not-ready/不可开始语义，不允许把冷启动移到
  录音后。
- Resident AI 进程意外退出后，该 slot 立即撤销对应 revision 的 ready 标记并后台重启、handshake；重启
  完成前不准入使用该 revision 的新录音。已经冻结的 attempt 若尚未提交 Decision，则沿用现有分析失败
  语义，不能静默换 revision 重试。

### 7.2 Decision stage

Decision stage 只产出有界的小结果：

- PASS/FAIL、AI score/label/pass；
- 每通道限值结论和结果摘要所需的标量；
- 后续图表可复用的紧凑 numpy/typed-array 中间结果引用；
- 所有身份和时间窗口元数据。

实现必须：

1. 将当前 `JudgmentEngine.rejudge()` 中“判定必需”和“仅展示需要”的工作拆开。
2. 每个通道的校准切片、SPL/响度特征、FFT/STFT 基础变换只计算一次；判定和 enrichment 共享结果。
3. 优先把逐窗 Python 调用和重复 percentile/filter 调用改为等价的批量 numpy/scipy 计算；不降低判定输入
   分辨率。
4. AI 继续使用 frozen request 和 Resident stdio 协议，但不在关键路径写入带缩进的 request/response 副本；
   诊断副本移到 enrichment/I/O 队列，最终文件内容语义保持。
5. 不创建完整 `chart_payload`，不递归 `_frozen_scalar` 百万级节点，不执行 5 MB JSON/zlib 传输。
6. 通过小型阶段结果立即驱动现有结论消费者；图表区域维持当前录音/live 图或 loading 状态，随后由同一
   attempt 的 enrichment 更新。
7. Decision 提交是本次 attempt 的唯一业务终态提交：UI、PLC、数据库结论与历史摘要在一个协调器动作中
   消费相同 immutable decision envelope。任何后续 enrichment 都不能改写 judgment、AI/limit conclusion、
   terminal owner 或 PLC 值。

### 7.3 Decision commit 与部分发布

每个 Decision 使用稳定幂等键
`(run_id, analysis_attempt_id, terminal_revision, "decision/v1")`。提交和投影顺序固定如下：

1. Decision stage 在发送前完成 envelope、judgment、summary 和轻量兼容 payload 的全部校验；校验失败不得
   产生任何终态副作用。
2. coordinator 使用现有 exactly-once terminal owner，把 Decision 提交到 durable database/history 权威；
   成功事务返回的时刻是 `decision_committed_at`。相同幂等键重放必须读回同一结论，冲突内容必须拒绝。
3. durable commit 成功后，coordinator 才向 UI/history adapter 和现有 `PlcResultSequencer` 发布同一 envelope。
   UI 以幂等键忽略重复事件；PLC sequencer 以 attempt/revision 身份沿用现有有序、去重和重试语义。
4. 正常路径的 PLC 写入成功确认和 UI paint acknowledgement 都进入 0.5 秒计时。durable commit 成功但某个
   投影暂时失败时，绝不回滚或改写 judgment：UI 可从 durable fact 重建，PLC action 保持 pending 并按现有
   有界重试；重复执行只能使用原幂等键。
5. PLC 永久不可达、UI 窗口销毁或数据库 I/O 失败是异常路径，不计入正常稳态 SLA，继续使用现有 PLC/UI/
   persistence 诊断和失败处理；已经 durable commit 的 Decision 仍是权威结果。
6. Enrichment 不创建新的 terminal fact 或 PLC action，只用同一幂等 identity 更新图表 materialization。

这里的“一个 coordinator 动作”表示一个有序、幂等的状态转换，不宣称数据库、Qt 和 PLC 是分布式原子事务。

### 7.4 DSP intermediate bundle 契约

- bundle 是分析子进程内的 immutable、slot-owned 私有对象，不跨进程传递裸 Python/numpy 引用。
- bundle 至少绑定 `(run_id, attempt_id, content_revision, algorithm_revision, config identity, calibration
  identity, channel mapping identity, analysis window)`；全部匹配才可消费。
- numpy 数组在发布 bundle 前设为 read-only；Decision 和 Enrichment 不得原地修改共享数组。
- 同一 attempt 的 Decision 与 Enrichment 固定由同一个分析 slot 执行。主进程只持有 opaque bundle token
  和小型 decision envelope。
- 令 `audio_bytes = frame_count * channel_count * dtype_size`。单 attempt bundle 的硬上界为
  `4 * audio_bytes + 32 MiB`，六 attempt 总上界为各自上界之和；不得存储多份完整音频或无界 Python
  节点。超界在 Decision 提交前按现有分析失败，不能无界增长。实现计划必须列出每个数组的预算，并用六
  attempt 压力测试验证实际峰值。
- 生命周期从 Decision 开始到 Enrichment 成功、attempt retirement/cancel、一次允许的 enrichment retry
  耗尽或 shutdown，以先发生者为准；所有退出路径 exactly-once 释放数组和 token。
- token 缺失、损坏、过期或身份不匹配时不得复用其他 attempt 数据。Decision 尚未提交时按现有分析失败；
  Decision 已提交时只令 enrichment 失败并进入第 9 节的非撤回处理。

### 7.5 Enrichment stage

- 仅在全局 `capture_active == false` 且 quiet gate 已满足时运行重型图表和 artifact 工作。
- 直接消费 Decision stage 的身份绑定中间结果，避免重新读取、重新校准或重新计算相同 DSP。
- STFT score、完整 STFT 图、FFT、SPL、响度和倍频程图继续使用当前配置与输出字段。
- 将 numpy 数组直接编码为 canonical JSON/压缩 artifact，或在子进程内一次性物化最终 JSON；禁止先构造
  百万个 `FrozenResultObject` 节点、再验证、再转回普通对象。
- 子进程 transport 传递压缩 canonical bytes 和小型 judgment envelope；主进程只验证边界、hash、大小、
  schema 和 judgment，不递归重建整个数值树。
- artifact store 保持现有原子临时文件、flush/fsync、replace 和 reference schema；最终读取路径不变。
- UI enrichment 必须以 `(run_id, attempt_id, content_revision)` 匹配当前卡片；迟到的旧 attempt 不能覆盖
  新结果。
- Decision 提交时在现有数据库列和 payload 拓扑内持久化兼容的轻量结果：保留 judgment、summary、
  channel conclusions 和现有必需元数据，`chart_payload` 使用现有消费者已支持的空对象；不新增 schema
  或必须字段。Enrichment 完成后以现有完整 canonical payload 原子替换同一 attempt 的轻量 payload；
  history/detail/export 只在完整 artifact 发布后展示图表，但 judgment 始终来自已提交 Decision。
- 待补齐期间，结果卡片和详情页显示已提交 summary/结论，图表区域沿用现有空图/加载占位，不伪造历史图；
  用户触发详情读取不会阻塞或再次启动 DSP。
- 待补齐期间触发导出时，现有导出操作只登记/等待同一 attempt 的 Enrichment future，在其现有操作超时
  预算内完成后导出当前兼容完整格式；超时则使用现有“结果暂不可用/导出失败”路径，不生成部分格式。
- Enrichment 重试耗尽且 artifact 永久缺失时，历史和详情继续显示 durable summary 与空图；导出继续走
  现有 artifact unavailable/导出失败路径，不新增“图表失败”产品状态，也不改变任何已生成文件格式。

### 7.6 多录音调度

- Decision stage 继续使用有界分析 slot，但不得在任何录音 active 时进行大型结果接收和物化。
- Enrichment 任务按已完成时间排队；capture 从 0 变为 1 时停止准入新的 enrichment。
- capture 从 1 变为 0 时记录唯一 `capture_quiet_at`，优先处理所有已完成运行的待补齐图表。
- quiet epoch 创建时扫描全部 `audio_frozen && !charts_visible` attempt，而不是只扫描已经 enqueue enrichment
  的 attempt；未完成 Decision 的 attempt 也属于该 epoch 的 eligible backlog。
- 实现可以在现有两个分析 slot 上并行 enrichment，但必须证明不会反向阻塞新的 capture、保存或
  Decision stage；不得增加无界 worker。
- 如果新录音在 enrichment 中途开始，已经在纯子进程计算的任务可以到安全检查点完成，但其大型传输、
  解压、UI 投影和 artifact fsync 必须服从 capture quiet gate。若无法证明隔离，则任务在阶段边界暂停并
  在下一 quiet epoch 继续。

## 8. 性能诊断与预算

实现必须为下列边界记录 `perf_counter_ns`，并使用 attempt id 关联：

- audio frozen / quiet gate released；
- audio transport ready；
- legacy decision DSP；
- Resident AI acquire/send/response；
- decision projection；
- enrichment DSP；
- canonical encoding/compression；
- process transport；
- artifact publication；
- chart projection；
- result widget render acknowledgement（在包含新内容的 paint event 后）。

日志只记录时长、大小、身份和状态，不记录原始音频或大型 payload。生产日志需能直接解释一次超预算发生在
等待录音、DSP、AI、序列化、I/O 还是 UI。

初始预算分配如下，计划阶段可依据微基准收紧，但总预算不可放宽：

- Decision 调度/小型音频交接：50 ms；
- 判定 DSP：250 ms；
- Resident AI：100 ms；
- durable commit、PLC 正常写入和 UI paint：100 ms；
- Enrichment 的既有 quiet fence：从 `capture_quiet_at` 起连续 250 ms，明确计入而不是平移外部时钟；
- quiet fence 结束后的 enrichment DSP、编码、artifact 和全部 eligible backlog 图表投影合计预算为剩余
  1.25 秒，从而仍在 `capture_quiet_at + 1.5 seconds` 前完成。

硬 SLA 只在本次命名的基线机器验收：Windows 11、AMD Ryzen 5 5600GT
（6 核 12 线程，最高 3.6 GHz）、31 GB RAM、NVMe SSD；应用使用 release-equivalent Python/依赖、默认
两个分析 slot、默认线程限制和生产配置。验收时记录 CPU、内存、磁盘、Python/依赖版本和电源模式。
机器存在其他非本应用重型负载时不作为硬 SLA 样本，但“其他录音活动”必须纳入指定并发测试。其他部署
主机可以运行同一 benchmark 并报告结果，但在另行批准可比阈值前，不能替代这台命名机器的硬验收。

## 9. 错误与取消语义

- 用户已批准 Decision 是正式、不可撤回的业务终态，Enrichment 是非业务终态的同 attempt 补充阶段。
- Decision 提交前失败时继续按现有分析失败/取消语义结算。
- Decision 提交后 Enrichment 失败时绝不撤回 UI/PLC/数据库 judgment，不创建第二个 terminal owner，也不
  把已判定产品改成分析失败。系统在 capture quiet 时对同一 bundle 最多自动重试一次；仍失败则记录内部
  enrichment diagnostic 并释放资源。正常路径必须生成兼容完整 artifact；异常路径允许该 attempt 暂无
  图表 artifact，且本次不新增用户可见错误状态。
- 快速显示和后续图表是同一 analysis attempt 的两个投影，不是两个可独立提交的业务结果。
- stop、reanalysis、generation retirement 和 shutdown 必须取消/忽略同 attempt 的迟到 enrichment，
  保持 exactly-once terminal 和现有取消优先级。

## 10. Compatibility / Migration

- `Backward compatibility: required`
- `Protected surfaces: [audio samples and WAV files, sample rate/channel mapping/frame count/content revision, PASS/FAIL and AI/limit conclusions, PLC/manual workflow, current database schema and data, result payload JSON topology, analysis-result-artifact/v1 references, history/detail/export behavior, reanalysis behavior, existing configuration]`
- `Allowed breakage: [private analysis executor interfaces, private process transport, internal DSP intermediate representations, internal scheduling and performance diagnostic fields, timing of chart appearance]`
- `Migration strategy: none`

新版本可直接读取既有数据库和 artifact。新生成结果在 enrichment 完成后仍使用当前兼容格式，无配置或数据
迁移步骤。

## 11. 测试策略

### 11.1 录音正确性 gate

1. 对相同输入验证优化前后 WAV 字节、sample rate、channel count、frame count、dtype 和 channel map 不变。
2. 在 Decision/enrichment 双 slot 满载下连续执行现有最大并发 capture；验证无 callback gap、Ring overflow、
   截短、补零、跨 generation 污染或错绑。
3. 慢 enrichment、artifact fsync 和 UI 投影不能出现在 callback/Resident drain 调用栈，也不能延长录音
   freeze 或 WAV 落盘。
4. 保留并通过现有 resident audio、managed input、multi-run capture/save 和 audio persistence 回归测试。

### 11.2 判定一致性

- 使用真实生产录音、合成边界录音和每项限值附近样本，对比优化前后 PASS/FAIL、AI pass/label、每通道
  limit pass 和所有 required availability flags。
- judgment、AI pass/label、每通道 limit pass、required availability flags、离散 channel/frequency bin identity
  必须完全一致。
- 时间轴和频率轴：`rtol=1e-10, atol=1e-9`。
- SPL、FFT/STFT magnitude、倍频程等 dB 输出：`rtol=1e-6, atol=1e-4 dB`。
- 响度 sone 输出：`rtol=1e-5, atol=1e-7 sone`。
- AI score、归一化 score 和其他无量纲连续标量：`rtol=1e-7, atol=1e-9`；外部 AI toolchain 自身输出按
  原始响应逐值相等，不由本地优化重新近似。
- 非上述类别的 float 输出使用 `rtol=1e-7, atol=1e-9`。任何数值即使在容差内，只要改变阈值结论也判为
  不兼容。
- 修改后的共享 DSP 必须有 red/green 单元测试证明不重复计算且身份不匹配时拒绝复用。

### 11.3 兼容性

- 旧 artifact 继续可读；新 artifact 由现有 reader、历史详情、导出和人工复判消费。
- 新旧最终 canonical payload 的必需字段和类型一致；图表分辨率及配置字段不丢失。
- PLC、UI 结果卡片、历史卡片和 reanalysis 不出现重复终态或旧 enrichment 覆盖新 attempt。

### 11.4 性能验收

提供可重复的 production-shaped benchmark，不依赖手工计时：

1. 8.85 秒、44.1 kHz、单通道生产配置，连续至少 30 次预热运行：每次 decision 不超过 0.5 秒，每次完整
   charts 不超过 1.5 秒。Decision 计时终点必须是 Qt 结果组件完成包含新 badge/摘要的 paint acknowledgement，
   不能只用 service signal 发出时间代替可见时间。硬 SLA 运行时主窗口和结果组件必须实际 shown、非最小化、
   paint-enabled，并由正常 Qt 事件循环驱动；hidden/offscreen/minimized 测试只用于功能回归，不能替代该终点。
2. 8 秒、48 kHz、双通道配置：报告 p50/p95/max 和各阶段预算，且不得比单通道通过掩盖。
3. 两个分析 slot 都验证 AI 已预热，任何一次稳态请求不得创建新 AI 进程。
4. 最多六条录音有重叠时，每条已冻结录音的 Decision 仍在 0.5 秒内完成且录音完整；active-capture 期间
   不对图表计时。最后一条结束时快照全部 `audio_frozen && !charts_visible` attempt（包含未完成 Decision），
   该 eligible backlog 在 1.5 秒内全部完成。
5. benchmark 输出 decision、AI、enrichment DSP、JSON bytes、compressed bytes、transport、fsync、UI
   projection 各阶段数据，禁止只输出总耗时。
6. 相关测试和 benchmark 必须在最终实现上重新执行；旧基准结果不能作为完成证据。
7. 同样的真实/合成输入用于导入分析和人工复判功能兼容测试，但不对这两个交互路径声明 0.5/1.5 秒 SLA。

## 12. 实现边界

预计涉及：

- `app/service/analysis/production_executor.py`：Decision/Enrichment 拆分和共享中间结果；
- `app/service/analysis/process_executor.py`：阶段协议、预热、capture-aware 调度和压缩 transport；
- `app/service/analysis/judgment.py`、`dsp.py`、`review_payloads.py`、`stft_score.py`：只做经过 profile 证明的
  单次计算、批量化和中间结果复用；
- `app/service/ai_resident_runtime.py`：无业务副作用的预热/readiness 接口；
- `app/service/run_persistence/` 和 `app/service/multi_run_integration/`：同一 attempt 的阶段投影、最终 artifact
  物化和迟到结果 fencing；
- `ui/modules/`：仅支持“摘要先显示、图表随后补齐”，不改变最终展示和历史读取；
- `scripts/benchmark_analysis_isolation.py` 或新的专用基准及对应测试。

不得修改音频 callback 的样本处理语义，也不得以本任务名义重构无关模块。

## 13. 验收标准

1. 稳态单条生产录音的判定满足 0.5 秒硬目标；存在其他活动录音时也必须满足，并以录音完整性 gate 为先。
2. 无其他录音时完整图表满足 1.5 秒；有其他录音时暂停图表时限，最后一条录音结束后 eligible backlog
   （含未完成 Decision）在 1.5 秒内全部补齐。
3. AI 在每个分析 slot 中于生产录音前 Resident ready，热分析不发生进程创建。
4. 录音字节、帧数、格式、通道映射、连续性和落盘语义无回归；压力下无 callback/Ring 数据丢失。
5. PASS/FAIL、AI 结果和所有限值结论与当前实现一致；数值仅存在批准的有限浮点误差。
6. 最终结果 schema、旧数据、历史、详情、导出、人工复判和 PLC/手动工作流兼容。
7. 重复 DSP 和百万级中间结果树从关键路径移除，日志可定位每个阶段的实际耗时和大小。
8. 单元、集成、并发、兼容、性能和录音压力测试均以新鲜输出通过。
