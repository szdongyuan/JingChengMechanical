# 常驻音频采集池、时间戳环形缓冲与六路并发录音设计

## 1. 文档地位

本文是
`docs/double-sdd/specs/2026-07-25-multi-run-audio-testing-core-design.md`
定义的第一份子规格，负责常驻音频采集层。

Core 规格是本功能的长期权威边界。本文可以补充接口、状态机、并发和存储细节，
但不得降低 Core 的保证。若实施过程中发现本文与 Core 冲突，必须先修改并重新
审查规格，不能在实现中自行选择较弱语义。

本文完成前不修改结果 UI。多 `run_id` 运行协调、分析工作池、PLC FIFO 和最终
录音数据库落库属于后续子规格。

## 2. 背景与现状

当前 `AudioRecorder` 已具有一些可复用的并发基础：

- PortAudio 回调为每个音频块分配绝对半开采样区间；
- 同一临时 recorder 可以存在多个 capture owner；
- `SessionCaptureWindow` 可以按 owner 的独立区间裁剪、去重和检测采样缺口；
- request、callback、停止和完成之间已有锁保护和竞态测试；
- 同设备 recorder 的创建、启动和退役已有 start gate、pending claim 和
  identity guard 等保护思想。

但是当前 recorder 仍然是按测试创建和关闭的一次性对象：

- 启动测试后才枚举设备、打开 PortAudio 流并等待首块；
- 最后一位 owner 完成后 recorder 停止接受 capture 并关闭输入流；
- 内部所谓缓冲是持续扩张的线性 NumPy 数组，不是两秒滚动环形缓冲；
- capture request 使用注册当下的时间，没有接收入口记录的事件时间；
- 每次 capture 固定丢弃约 50 ms warmup；
- recorder 和活动 session 仍主要按 `config_id` 管理；
- 共享键来自配置中的设备名称、Host API 和采样率字符串，而不是稳定
  `hardware_id`；
- callback 到 drain、attachment 前 pending block 都存在无界队列或列表；
- 完整录音长期保存在内存数组中；
- PortAudio status 和流失败仍是字符串错误，缺少结构化故障和 generation
  隔离；
- 应用退出没有统一关闭 recorder、PortAudio 流和相关线程。

硬件管理现状还存在数据源分裂：运行前可用性检查从受管硬件记录读取真实格式，
但实际 recorder 的共享键和部分打开参数仍来自配置中冗余保存的设备字段。
新采集层必须以一次硬件解析结果作为唯一数据源。

## 3. 目标

1. 以稳定 `hardware_id` 为键管理最多六条常驻输入流。
2. 同一实际硬件只打开一条流，并允许多个配置、同一配置的多次触发及多个
   `run_id` 共享该流。
3. 在当前启用配置需要硬件期间保持流打开，使测试触发不再等待设备枚举、
   PortAudio 打开和首块到达。
4. 每条流取得可靠时间基准和首个有效音频块后才标记为可用。
5. 维护单调时钟与绝对采样游标的连续映射，以及至少 2.0 秒的滚动音频缓冲。
6. capture 注册晚于触发时，仍能从环形缓冲取得触发之后的音频。
7. 捕获起点选择最接近且不早于事件时间的采样帧，取消固定 50 ms warmup。
8. 允许最多六条录音同时存在；第七条由采集池原子拒绝且不排队。
9. 每条 capture 独立写入磁盘后备 artifact，录音完成后不把完整数组留在
   等待保存或等待分析队列。
10. 音频回调不执行磁盘、数据库、分析或 UI 工作；慢 capture 不能阻塞同流的
    其他 capture。
11. 一条硬件流的打开、断开、时间基准或输入缺口故障只影响该硬件及其 capture。
12. 流重连使用新 generation，旧回调不能污染新运行。
13. 提供确定的停止和退出行为，应用退出后不残留 PortAudio 流或采集线程。

## 4. 非目标

- 不在本子规格中实现 `RunContext`、多运行卡片、锁定/解锁结果选择或图表分页。
- 不在本子规格中实现分析工作池、重新分析、人工判定或最终结果历史。
- 不在本子规格中实现 PLC 轮询、测试中计数、结果 FIFO 或 PLC 错误码数值映射。
- 不在本子规格中决定哪些配置当前启用；只提供按已解析硬件集合协调流的接口。
- 不在本子规格中实现硬件添加、删除、刷新、重新绑定和校准的完整 UI 门禁；
  这些属于第四份子规格。
- 不改变现有最终分析算法、麦克风校准、通道含义或结果判定。
- 不支持超过六条同时录音，也不支持一个当前所需集合中超过六个不同
  `hardware_id`。
- 不限制数据库中保存的硬件记录或测试配置总数。
- 不保证应用崩溃后恢复尚未完成的临时 capture artifact。
- 不把实时 STFT 或其他图表处理放入音频回调。

## 5. 方案选择

### 5.1 采用：独立 `ResidentAudioPool` 与 `ResidentStream`

新增独立采集层：

- `ResidentAudioPool` 管理常驻流集合、全局 capture lease、流协调和关闭；
- `ResidentStream` 管理一台硬件的一条 PortAudio 输入流；
- `StreamIngestWorker` 在 PortAudio callback 之外推进游标、时间轴、ring 和
  owner 扇出；
- `StreamTimeline` 管理单调时间与绝对采样游标映射；
- `TimestampedBlockRing` 管理至少两秒的共享滚动音频块；
- `CaptureOwner` 管理一次录音的独立范围、drain 和 artifact；
- `CaptureArtifactStore` 管理磁盘后备临时文件及其冻结、转交和清理。

复用现有绝对半开采样区间、向上取整帧数、窗口裁剪、重叠去重和部分并发保护
思想，但不继续扩展一次性 `AudioRecorder` 的生命周期和线性数组。

### 5.2 不采用：直接把 `AudioRecorder` 改造成常驻录音器

该方案表面改动较少，但会继续耦合：

- 一次测试的目标时长和一条硬件流的生命周期；
- `config_id`、字符串设备键和硬件身份；
- recorder QThread、完整录音数组和多个 session；
- 老的完成即关闭语义与新的持续接受 capture 语义。

它还会进一步扩大已有 7188 行的 `TestManager`，不利于后续多运行改造。

### 5.3 不采用：单一全局采集引擎线程

把六条 PortAudio 流、全部时间轴和写盘集中到一个执行线程会产生跨硬件单点
阻塞，并削弱流失败隔离。池负责集中控制，但每条硬件流和每位 capture owner
保留独立状态与 drain。

## 6. 核心组件与数据契约

### 6.1 `ResolvedInputDevice`

硬件解析层向采集池提供不可变的 `ResolvedInputDevice`，至少包含：

- `hardware_id`；
- 本次枚举得到的 PortAudio `device_index`；
- 设备名称和 Host API；
- 实际采样率；
- 实际输入声道数；
- `float32` 输入格式；
- 解析发生时间或版本信息，供日志诊断。

以下规则强制成立：

- 常驻流身份只使用 `hardware_id`；
- `device_index`、名称、Host API、采样率和声道数必须来自同一次受管硬件解析；
- 配置中冗余保存的设备名称、Host API 和采样率不得参与流去重或打开参数；
- 两个配置绑定同一 `hardware_id` 时，即使其冗余字段不同，也不能打开第二条流；
- 同一 `hardware_id` 若被提交为两套不同格式，协调失败并报告硬件解析冲突，
  不能选择其中一套或创建两条流。

`hardware_id` 是当前数据库内的稳定受管硬件身份，不在本子规格中扩展为跨机器
GUID。

### 6.2 `StreamReadyToken`

采集池只为已经可用的流发布不可变 `StreamReadyToken`，至少包含：

- `hardware_id`；
- `pool_epoch`；
- `stream_generation`；
- 实际采样率；
- 实际声道数；
- 当前时间轴最早及最新可映射范围的只读摘要。

运行协调器在冻结配置快照时保存该 token。capture 提交必须带回相同 generation。
如果事件之后流已经失败并重连，旧 token 不能自动改投新流，因为新流的环形
缓冲不包含旧 generation 的事件时间。

### 6.3 `CaptureRequest`

一次 capture request 至少包含：

- 唯一 `run_id`；
- 接收入口记录的 `trigger_monotonic`；
- 录音时长；
- 预期 `StreamReadyToken`；
- artifact 建立所需的轻量元数据。

pool 在 `RESERVED → COMMITTING` 之前负责完整校验。以下任一条件失败都把 lease
直接转为 `FAILED`、释放容量并返回 `INVALID_CAPTURE_REQUEST`，不建立 artifact
或 owner：

- `trigger_monotonic` 是有限正数；
- trigger 不晚于 commit 时读取的 monotonic 加一采样帧容差；
- 录音时长是有限正数且不超过现有配置 UI 上限 3600 秒；
- token 中采样率是有限正数，声道数是非布尔正整数；
- 下述帧数和字节数使用 checked integer arithmetic，不能溢出平台整数；
- `target_frames >= 1`；
- 单 capture 预分配字节数不超过 8 GiB 硬上限；现场配置只能降低，不能提高该
  产品安全边界。

目标帧数和预分配字节数使用：

`target_frames = ceil(record_duration * actual_sample_rate)`

`artifact_bytes = target_frames * actual_channels * sizeof(float32)`

`CaptureArtifactStore` 还必须在建立 partial 前原子预留 staging quota，并检查实际
可用磁盘空间。生产默认 pool quota 为 48 GiB，即六条单项硬上限之和；现场配置
可以降低。建立文件后必须至少保留 1 GiB 实际可用空间。quota 和磁盘不足返回
`CAPTURE_STORAGE_CAPACITY`，回滚
`COMMITTING`，不把磁盘不足伪装为输入流故障。每个 terminal 路径最多释放一次
quota reservation 的所有权，但 terminal 本身不释放仍有物理文件占用的 quota。
生产 staging quota 和最低剩余空间作为应用配置提供，测试必须注入小额度验证
六条并发预留不会超卖磁盘。

capture 保存该硬件的全部输入声道，保持原声道顺序和 `float32` 样本。逻辑通道
选择与麦克风校准仍由后续 session/分析层处理。

### 6.4 `RawCallbackBlock` 与 stream ingress

PortAudio callback 只建立 `RawCallbackBlock`，至少包含：

- callback 闭包固定的 `pool_epoch`、`hardware_id` 和 generation；
- callback sequence；
- `callback_monotonic`；
- 原始 PortAudio `inputBufferAdcTime`、`currentTime` 和 status；
- `frames`；
- 一份连续 `float32[frames, channels]` 数据副本。

每条 stream 使用容量固定为四块的单生产者/单消费者 ingress。callback 只调用
不等待的 `try_push`。四块均未被 ingest worker 取走时，第五块不能覆盖旧块或
扩张队列，而是原子锁存 `INPUT_INGRESS_BACKLOG`，使当前 generation 失败。

`StreamIngestWorker` 是该 ingress 的唯一消费者，也是绝对采样游标、
`StreamTimeline`、`TimestampedBlockRing` 和 stream owner 集合的唯一写者。
这样 PortAudio callback 不需要取得 stream lock，也不直接调用 owner queue。

### 6.5 `CapturedAudioBlock`

共享音频块至少包含：

- `hardware_id`；
- `stream_generation`；
- 绝对 `sample_start` 和 `sample_end`；
- 连续 `float32[frames, channels]` 数据；
- 可用的块起止单调时间或时间质量状态；
- PortAudio status 摘要。

区间统一使用 `[sample_start, sample_end)`。ingest worker 使用
`RawCallbackBlock` 已有的唯一数据副本建立共享 `CapturedAudioBlock`，不再次
复制完整输入。环形缓冲和所有 capture queue 保存同一数据对象的引用，不为
每位 owner 重复制整块。

### 6.6 `FrozenCaptureArtifact`

capture 成功冻结后返回轻量 artifact 引用，至少包含：

- `run_id`；
- artifact 路径和格式版本；
- `dtype`、采样率、声道数；
- 已捕获帧数和目标帧数；
- 起止绝对采样游标；
- `stream_generation`；
- artifact ID 和当前所有权状态；
- 触发时间及映射后的捕获起点；
- 时间质量摘要；
- 正常完整 capture 的终止语义。

等待保存和等待分析的队列只能保存该引用与轻量元数据，不能保存完整音频数组。

## 7. `ResidentAudioPool`

### 7.1 两个独立上限

池内存在两个不同但都固定为六的产品上限：

1. 当前所需的不同 `hardware_id` 最多六个，即最多六条常驻流；
2. 全局已预留加已提交的 capture lease 最多六条。

六条 capture 可以全部位于同一流，也可以分布在最多六条流上。已保存配置和
硬件记录数量不计入任一上限。

### 7.2 所需硬件集合协调

池提供按完整 desired set 协调流的接口：

- 先按 `hardware_id` 去重并验证格式一致；
- desired set 超过六个时整次请求失败，不任意打开前六个；
- 失败的非法 desired set 不修改当前已发布的流集合；
- desired set 变化时，为新增硬件打开流，为不再需要的硬件关闭流；
- 能在六条上限内并存时，先使新增流就绪再关闭旧流；
- 六换六等无法临时并存的替换，先关闭明确移除且无 owner 的旧流，再打开新流；
- 单条新增流打开失败只使相关硬件未就绪，不把其他成功流回滚；
- 返回逐硬件的 ready、opening、failed 和原因报告。

Core 禁止活动运行期间改变流集合。池本身防御性拒绝在存在 reserved、
committing 或 committed lease 时修改 desired set；第四份子规格还必须在更
上层把等待保存、等待分析、分析和重新分析纳入维护门禁。

### 7.3 capture lease 是唯一容量裁决

池拥有全局 lease 表并提供：

- `reserve(run_id)`；
- `commit(run_id, stream_token, request)`；
- `release(run_id, owner_capability, artifact_id)`；
- `cancel(run_id, reason)`。

状态机为：

```text
不存在 ─reserve→ RESERVED ─commit→ COMMITTING ─publish→ COMMITTED
                     │             │                     │
                     ├invalid─────→ FAILED               │
                     │             ├abort/cancel────────→ CANCELED
                     │             ├failure─────────────→ FAILED
                     └cancel────────────────────────────→ CANCELED

COMMITTED ─owner release→ RELEASED
COMMITTED ─failure──────→ FAILED
COMMITTED ─cancel───────→ CANCELED
```

约束：

- `reserve` 在一个线性化点内检查 reserved、committing 与 committed 总数并
  预占名额；
- 已有六条时第七条立即返回结构化容量拒绝，不排队；
- 相同 `run_id` 不能重复 reserve 或 commit；
- request validation 失败使 `RESERVED → FAILED`，不经过 `COMMITTING`；
- `commit` 验证流仍属于 token 指定的 `pool_epoch` 和 generation；ready 流可
  立即映射，`TIMING_RECOVERY` 或正常 callback 间隙可建立等待映射 owner；
- 客户端主动放弃一个仍有效的 reserved lease 使用 `cancel`；非法 request 使用
  `FAILED`，两者不能互换；
- 正常 capture 冻结后，由持有不可伪造 `owner_capability` 的 owner terminal
  控制事件调用 `release`；第二份子规格的客户端不能提前释放录音名额；
- capture 内部失败进入 `FAILED` 并自动回收；
- 每条路径最多回收一次；重复 release/cancel 只返回既有终态，不重复递减；
- 对已经 `FAILED` 的 run 调用 cancel 或重试 commit，只幂等返回原失败；相同
  `run_id` 不能重试或重新 reserve，新的尝试必须分配新 `run_id`；
- terminal lease 不计入容量，但保留必要诊断信息直到协调器确认或池按界限清理；
- 池外不得维护可用于接受/拒绝录音的第二份容量计数。

池可以提供只读快照供卡片显示，但该快照不构成新的裁决来源。

### 7.4 无嵌套锁的两阶段 commit

`commit` 必须使用唯一 `commit_attempt_id`、owner capability 和可原子取消的
commit token，不得为了“原子”同时持有 pool lock 与 stream lock。

协议为：

1. 在 pool lock 内验证 lease、pool 状态、`pool_epoch` 和当前 stream identity，
   将 `RESERVED` CAS 为 `COMMITTING`，保存 `commit_attempt_id`、预期 stream
   object 和 generation，然后释放 pool lock。这是 commit 尝试的线性化点，
   但尚未向调用者报告成功。
2. 在锁外预分配 partial artifact，并建立 `PREPARED` owner。任何异常进入统一
   rollback。
3. 把 prepare 命令送给目标 stream ingest worker。ingest worker 串行验证
   identity、generation、流状态和 trigger 是否已过期，将 owner 放入 dormant
   registry。`PREPARED` owner 不接受 callback 数据；ring 继续保留这段音频。
4. 回到 pool lock，以 `run_id + commit_attempt_id` CAS。只有 lease 仍为
   `COMMITTING`、没有 cancel/shutdown 请求且 prepared owner 未失败时，才转为
   `COMMITTED` 并原子打开 owner commit token。
5. ingest worker 看到 commit token 已打开后，再把 owner 激活为
   `WAITING_FOR_MAPPING`、`CATCHING_UP` 或 `LIVE`。commit 返回前必须收到该
   activation 的成功或明确失败。
6. 第 2 至 5 步任何失败都以 object identity、generation 和
   `commit_attempt_id` 从 stream 移除对应 owner、关闭并删除 partial
   artifact，再把 lease CAS 为 `FAILED` 或 `CANCELED`。

cancel 或 shutdown 在 `COMMITTING` 期间只原子设置 `cancel_requested`，不能先
把 lease 变成 terminal 后遗留 prepared/live owner。commit 执行者必须在
`finally` 中完成 identity-guarded rollback，随后才发布 terminal 状态。

如果 stream 在 prepare 后、activation 前失败，prepared owner 先锁存失败。
第 4 步或第 5 步观察到该状态并 rollback。callback/ingest 只向 commit token
已经打开且 owner 仍 active 的对象扇出，因此 terminal lease 永远不能保留
仍接收音频的 owner。

## 8. `ResidentStream` 生命周期

### 8.1 状态机

每条流使用以下状态：

```text
CLOSED → OPENING → READY ↔ TIMING_RECOVERY
             │        │             │
             └failure─┴failure──────→ FAILED → RETRY_WAIT → OPENING

任意非终止状态 → CLOSING → CLOSED
```

- `OPENING`：设备已解析，正在打开输入流并等待可靠首块；
- `READY`：有可靠时间基准、有效首块和可用环形缓冲，可以接受并映射 commit；
- `TIMING_RECOVERY`：首个基准已存在，但最新 callback 缺少有效时间戳；不发布
  新 token，但相同 generation 的既有 token 可以 prepare owner；涉及未确认尾部
  的 owner 进入 `WAITING_FOR_MAPPING`，等待下一块恢复；
- `FAILED`：本 generation 不再接收或完成 capture；
- `RETRY_WAIT`：硬件仍在 desired set 中，等待受控退避重连；
- `CLOSING/CLOSED`：不再接受新回调或 capture。

### 8.2 打开与 readiness

打开流时：

1. 使用 `ResolvedInputDevice.device_index` 和同一对象中的格式参数；
2. 不重新按名称寻找“第一个匹配设备”；
3. 请求生产录音使用的固定输入块节奏，实际以 callback 的 `frames` 为准；
4. 为本次打开分配新的、单调递增的 generation；
5. 读取并规范化 PortAudio 实际 input latency，非有限或负值按零处理并记录；
6. callback 到达前保持 `OPENING`；
7. 只有首个带可靠 PortAudio ADC 时间信息的有效音频块才能建立时间轴；
8. 建立时间轴并放入环形缓冲后转为 `READY` 并发布 token。

如果持续收到音频但没有任何可用 ADC 时间基准，流不能降级为 ready。首块等待
沿用当前 2 秒输入超时；超时后结构化失败并进入重连。

### 8.3 generation 分配边界

- 每次进程启动建立随机 `pool_epoch`；
- `ResidentAudioPool` 按 `hardware_id` 保存单调递增的 generation counter；
- counter 在 pool 生命周期内不因 stream object 删除、desired set 移除或重新
  添加而删除；
- 每次实际 PortAudio 流打开前，都从 pool allocator 取得新的
  `(pool_epoch, hardware_id, generation)`；
- callback 闭包携带其创建时的 generation；
- callback 进入采集层的第一步是检查闭包内的 generation-alive 原子标志；
- 旧 generation callback 只能被丢弃和计数，不能写环、推进游标、完成 owner、
  恢复 readiness 或发布错误给新流；
- token、raw block、音频块、capture owner 和 artifact 都保存 pool epoch 与
  generation；
- generation 变化后，旧事件必须失败为流已更换，不能在新流上重新映射。

进程重启后旧 token 和 callback 不可能继续存在，因此 counter 不要求数据库
持久化；`pool_epoch` 防止测试替身、迟到任务或对象复用把前一 pool 实例的 token
误认为当前 token。

### 8.4 重解析协作者

池通过可注入的 `InputDeviceResolver` 重新解析设备：

```text
resolve(hardware_id, desired_revision, resolution_attempt_id, cancel_token)
    -> ResolvedInputDevice | structured failure
```

- resolver 必须重新枚举并执行现有输入格式检查；
- 调用发生在 pool 控制执行器，不在 callback 或 ingest worker；
- 每次 desired set 变化增加 per-hardware `desired_revision`；
- 每次初次打开或重连增加 `resolution_attempt_id`；
- 从 desired set 移除、被新 attempt 替代或 shutdown 时设置 cancel token；
- 若底层枚举不能中断，迟到结果只能在 pool lock 内通过
  `hardware_id + desired_revision + resolution_attempt_id` identity check 后
  接受；
- 失配的迟到解析或迟到 open 结果立即关闭其临时资源，不能发布 stream；
- generation 只在解析结果仍匹配且即将实际打开时分配，失败 attempt 不复用
  已分配 generation。

## 9. 单调时间轴

### 9.1 时间源

触发入口和采集层统一使用 `time.monotonic()` 同一时基，不使用墙钟。

PortAudio callback 入口立即记录 `callback_monotonic`。时间信息有效时，块起点
映射为：

```text
block_start_monotonic =
    callback_monotonic
    + (inputBufferAdcTime - currentTime)
```

对 callback 的实际 `frames` 定义：

```text
block_duration = frames / actual_sample_rate
max_adc_age = max(
    0.5 seconds,
    normalized_input_latency + 4 * block_duration,
)
future_tolerance = 1 / actual_sample_rate
```

时间字段仅在以下条件全部成立时有效：

- `inputBufferAdcTime` 和 `currentTime` 均为有限正数；
- `0 <= currentTime - inputBufferAdcTime <= max_adc_age`；
- 映射所得 `block_start_monotonic` 不晚于
  `callback_monotonic + future_tolerance`；
- `frames > 0` 且采样率为有限正数。

全零、NaN、无穷、关系无效或超过上述数值边界均视为无效。0.5 秒是对未知或极
小 latency 的固定容差下限；已报告的较大 input latency 通过公式显式纳入，
实现不得再使用“合理时间”等未定义判断。

### 9.2 绝对采样游标与时间锚点

- 每个 generation 的绝对采样游标从零开始，只随 callback `frames` 前进；
- 每个有效时间 callback 为其 `sample_start` 增加时间锚点；
- `StreamTimeline` 在当前环形覆盖范围内维护有序锚点；
- 相邻有效锚点之间按采样游标连续插值；
- 最新有效锚点之后只允许对一个缺失时间块暂时外推；
- 时间锚点必须保持游标递增和单调时间递增。

相邻锚点连续性容差为：

```text
continuity_tolerance =
    max(previous_callback_frames, current_callback_frames)
    / actual_sample_rate
```

新锚点与上一有效锚点按采样率推算的偏差不得超过该数值。超过范围、时间倒退或
与 PortAudio overflow/gap 同时发生时，时间轴不可恢复。“一个输入块”在
variable callback frame count 下均使用涉及比较的相邻两块中较大者，不使用
未定义的 nominal block。

### 9.3 严格首块和偶发缺失策略

采用用户确认的严格策略：

- 建立首个有效 ADC 时间锚点前，流不可用；
- 首锚点之前收到但无法定位的块不进入可触发的环形时间范围；
- ready 后允许最多一个连续 callback 缺少有效时间信息；
- 该块进入 `TIMING_RECOVERY`，触及尚未确认时间区间的新 capture 映射暂缓，
  不悄悄改为下一块开始；
- 下一 callback 必须提供与连续采样游标一致、偏差不超过一个块的有效锚点；
- 恢复后完成缺失区间的插值并回到 `READY`；
- 连续第二块仍无效、恢复锚点不连续或时间倒退时，本 generation 失败。

已有 active capture 在单块恢复窗口内继续按绝对游标接收数据；如果随后恢复
失败，这些 capture 全部失败，不把时间质量未知的录音标记为正常完成。

### 9.4 事件时间到采样帧

映射规则：

- 找到当前可确认时间线中最接近且不早于 `trigger_monotonic` 的采样帧；
- 事件落在块内时使用向上取整：

  `ceil((trigger - block_start) * sample_rate)`；

- 结果夹在该块的 `[sample_start, sample_end]` 边界内；
- 事件恰好位于块末端时映射到 `sample_end`，由下一块提供第一个样本；
- 不把事件前的样本加入 capture；
- 不添加固定 warmup，也不把旧 50 ms warmup 改名保留；
- 映射起点与真实事件的验收误差不超过一个实际输入块。

若事件晚于最新已确认范围或落在 `TIMING_RECOVERY` 尚未确认的尾部，commit
可以发布 `WAITING_FOR_MAPPING` owner，但不能计算一个更晚的替代起点：

- lease 保持 `COMMITTED` 并计入六条容量；
- owner 保存原始 trigger，不保存猜测游标，也不接收 live queue；
- ingest worker 每次时间轴扩展后重新尝试精确映射；
- 正常 callback 间隙在下一有效块到达后映射；
- 单块 timing recovery 在下一有效锚点确认后映射；
- 映射成功后原子记录 start/end cursor，并进入 `CATCHING_UP`；
- generation 变化、stream 失败、cancel 或 shutdown 使 owner 使用对应原因终止；
- 从 `max(commit_monotonic, trigger_monotonic)` 起 2 秒仍没有可确认映射时失败为
  `TIMING_UNAVAILABLE`；
- 等待期间 ring 最早可映射时间越过 trigger 时失败为
  `TRIGGER_AUDIO_EXPIRED`。

若 commit 当下事件已经早于 ring 最早可映射时间，直接 rollback 并失败为
`TRIGGER_AUDIO_EXPIRED`。上述等待只等待原事件的精确映射，绝不从“下一块现在
开始”。

## 10. 两秒环形缓冲

### 10.1 保存范围

每条 ready 流维护 `TimestampedBlockRing`：

- 最低覆盖帧数为 `ceil(actual_sample_rate * 2.0)`；
- 按绝对采样游标连续保存共享不可变块；
- 允许为完整保留边界块多保存最多一个观察到的 callback 块；
- 淘汰以完整旧块为单位；
- 时间锚点和音频块使用相同 generation 和淘汰边界；
- 环形缓冲不随应用运行时间线性增长。

环形缓冲保存硬件全部声道。它不是任一 capture 的完整结果，也不因没有 active
capture 而停止更新。

### 10.2 串行 ingest 注册与未来扇出

pool 的两阶段 commit 把 prepare/activate 命令发送给本 generation 唯一的
ingest worker。所有 raw block、时间轴扩展、ring 追加、owner 状态转换和 live
扇出都在同一 ingest 顺序中发生，不与 PortAudio callback 共享 stream lock。

owner activation 时：

1. 验证 identity、generation、流状态和 trigger 是否已过期；
2. trigger 可映射时计算独立 start/end cursor 并进入 `CATCHING_UP`；
3. trigger 尚未被确认时进入 `WAITING_FOR_MAPPING`；
4. 保存 activation 顺序点的 stream cursor；
5. 后续 raw block 必须先追加 ring 和扩展时间轴，再推进 pending mapping、
   catch-up/live 状态。

drain 在 ingest 顺序之外按历史游标追赶。切换到 live 使用发送给 ingest worker
的命令完成：ingest worker 先记录切换 cursor，确认 owner 没有未写历史引用，
再使之后处理的块进入 live queue。该单写者顺序必须证明每个目标样本恰好写入
一次，不产生注册窗口缺口或重复。

### 10.3 历史追赶不绕过八块上限

不能在 commit 时把两秒历史的全部 block 引用一次塞入 owner queue，因为这会
使正常晚注册天然超过八块上限。

采用游标追赶：

- owner 只保存下一个待写绝对游标，不保存整份历史 block 列表；
- drain 受统一 credit 限制从 ring 取得有界引用，写入后推进游标；
- owner 尚未追上 live cursor 时，ingest worker 不为它追加重复历史引用；
- ring 继续保存新块，drain 反复以有界批次追赶；
- 只有当历史 batch 和 in-flight 引用均为零时，才由 ingest worker 从
  catching-up 原子切换到 live queue；
- trigger 在 commit/`WAITING_FOR_MAPPING` 期间已经早于最早可映射时间，失败为
  `TRIGGER_AUDIO_EXPIRED`；
- owner 已经取得确定 start cursor 并进入 `CATCHING_UP` 后，下一个待写游标被
  ring 淘汰，只能失败为 `CAPTURE_PERSISTENCE_BACKLOG`，因为此时丢失来自 drain
  未追上输入，而不是触发本身无法映射；
- 两种路径都只失败对应 owner，不能阻塞 ring 或扩大队列。

## 11. `CaptureOwner`、drain 与 artifact

### 11.1 owner 状态

```text
PREPARED → WAITING_FOR_MAPPING → CATCHING_UP → LIVE → FREEZING → COMPLETED
    │               │                 │         │        │
    └───────────────┴─────────────────┴─────────┴failure─→ FAILED
    └───────────────┴─────────────────┴─────────┴cancel──→ CANCELED
```

每位 owner 独立保存：

- `run_id`、commit attempt、owner capability、pool epoch 和 generation；
- 原始 trigger、mapping deadline 以及可空起止游标；
- 下一个待写游标；
- 统一的八个 unwritten-reference credits 和 live drain queue；
- artifact writer；
- 完成、失败或取消原因；
- 一次性 terminal 通知。

同一配置重复触发没有特殊共享状态；只要 `run_id` 不同，就是两个独立 owner。

### 11.2 磁盘后备格式

每位 owner 在 commit 时建立预分配、可内存映射的临时 `float32` artifact：

- 形状为 `[target_frames, actual_channels]`；
- 文件在采集临时目录中以随机进程命名空间和 `run_id` 标识；
- 建立时使用 partial 状态；
- drain 按绝对游标裁剪后写入目标相对位置；
- 正常完成时 flush、关闭写句柄并原子冻结为 ready artifact；
- 冻结对象的逻辑有效帧数必须等于 `target_frames`；
- 后续最终 WAV 保存只读取逻辑有效范围；
- capture 失败或取消时删除 partial artifact；
- 删除失败记录路径并交由启动/退出时的孤儿清理处理。

临时 artifact 格式是采集层内部契约，不替代现有最终录音文件。第二份子规格
负责把冻结 artifact 保存为现有录音文件、写数据库并把轻量引用交给分析队列。
本切片不提供“提前冻结半段录音”API。录音阶段手动停止等同 cancel 并删除
partial；如果运行在完整 capture 已冻结后才停止，后续子规格按其保存/分析阶段
处理既有完整 artifact。

artifact 所有权状态为：

```text
PARTIAL ─freeze→ READY_UNCLAIMED ─claim→ CLAIMED ─accept→ TRANSFERRED ─release→ RELEASED
PARTIAL ─failure/cancel→ DELETED | DELETE_PENDING
READY_UNCLAIMED ─terminal cancel/shutdown/decline→ DELETED | DELETE_PENDING
CLAIMED ─abandon while pool open→ READY_UNCLAIMED
CLAIMED ─abandon while pool closing→ DELETED | DELETE_PENDING
```

`CaptureArtifactStore` 提供：

- `claim_ready(artifact_id, publication_token, consumer_id) -> claim_token`；
- `decline_publication(artifact_id, publication_token, reason)`；
- `accept_transfer(claim_token)`；
- `abandon_claim(claim_token, disposition)`；
- `release_transferred(claim_token, disposition)`。

所有状态变化在 artifact store lock 内按 artifact ID 和 claim token CAS：

- freeze 完成后转为带唯一 `publication_token` 和
  `publication_pending=True` 的 `READY_UNCLAIMED`；
- publication pending 时 shutdown 不直接删除，而是先让 pool terminal arbitration
  决定发布成功还是取消；
- 第二份子规格的 completion handler 必须先 `claim_ready`，再把保存任务视为已接收；
- completion handler 不接受时必须调用 `decline_publication`；该调用只在
  artifact 仍为 `READY_UNCLAIMED`、publication pending 且 token 匹配时清除
  pending 并删除文件；
- `decline_publication`、重复 claim、重复 abandon 和重复 release 均按 token
  幂等；失配 token 不得改变文件或 quota；
- `CLAIMED` 表示 store 仍持有文件但已为唯一消费者保留；
- 消费者建立自己的持久化任务后调用 `accept_transfer`，从此由消费者负责最终
  调用 `release_transferred`；
- 最终 WAV 与数据库成功，或后续保存明确失败后，消费者均必须 release；
- claim 后、transfer 前取消时使用 `abandon_claim`；pool 仍运行时可回到
  `READY_UNCLAIMED`，pool 已关闭时删除；
- pool shutdown 只删除 `PARTIAL` 和 terminal arbitration 已结束、没有
  publication pending、锁内仍为 `READY_UNCLAIMED` 的文件；
- 如果 claim 与 shutdown 竞态，artifact store lock 的先后决定结果：shutdown
  先删除则 claim 明确失败，claim 先成功则 shutdown 不得删除；
- `CLAIMED` 或 `TRANSFERRED` 文件不能被 pool shutdown 删除。消费者未按时释放
  时记录 claim token 和路径，留作孤儿清理，不能制造 use-after-delete。

staging quota reservation 绑定 artifact ID，而不是 lease：

- 从 partial 文件建立前预留开始，贯穿 `PARTIAL`、`READY_UNCLAIMED`、
  `CLAIMED`、`TRANSFERRED` 和 `DELETE_PENDING`；
- lease 成功、失败、取消或容量名额释放都不能提前释放仍有物理 staging 文件的
  quota；
- 只有文件成功删除，或消费者把文件原子移动到 staging 根目录之外且 store
  验证原路径已不存在后，才把 artifact 转为 `RELEASED/DELETED` 并释放 quota；
- 删除失败进入 `DELETE_PENDING`，保留原字节 accounting 并由有界清理重试；
- claimed/transferred 文件长期存在会继续占 quota，新 capture 必须按剩余额度
  拒绝，不能因为录音名额已释放而超卖磁盘。

### 11.3 drain 执行模型

最多六位 active owner 各自具有独立、按需创建的 drain worker。选择该模型是
为了避免一位 owner 的磁盘写入阻塞其他 owner；最大线程数由六条 lease 上限
天然约束。

drain worker 可以执行：

- 从 ring 有界取得历史块；
- 从自己的最大八块 live queue 取块；
- 按 owner 起止边界裁剪；
- 写、flush 和冻结自身 artifact；
- 发布轻量完成或失败事件。

它不能执行分析、数据库写入、PLC 写入或 UI 绘制。

### 11.4 八块统一 credit

每位 owner 建立八个 `unwritten_reference_credit`。owner 在 ring 本身之外保留
任何尚未完成写入的 `CapturedAudioBlock` 引用前必须先取得一个 credit，包括：

- catch-up batch 中已取得但尚未写的引用；
- live queue 中排队的引用；
- drain 已 dequeue、正在裁剪或写入的 in-flight 引用；
- 仍持有原 block 的切片 view。

引用写入完成且 worker 丢弃 block/view 后才归还 credit。因此“一个 in-flight
加八个 queued”的九引用状态不可能出现。

- `CATCHING_UP` 时 drain 一次最多取得当前可用 credit 数量的 ring 引用；
- catch-up 期间 ingest worker 不建立 live queue 引用；
- 只有 catch-up cursor 已追上切换点，且 batch、queue、in-flight 引用和已占
  credits 全部为零时，ingest worker 才切到 `LIVE`；
- `LIVE` 时 ingest worker 先非阻塞取得 credit，再把共享引用 `try_push` 到
  owner queue；
- 无 credit 表示第九个未写引用到达，原子把该 owner 标记为
  `CAPTURE_PERSISTENCE_BACKLOG` 并从未来扇出集合移除；
- queue push 意外失败时立即归还刚取得的 credit，再以相同错误终止 owner；
- 该失败不阻塞 ingest，不扩大队列，也不终止同流其他 owner；
- owner drain 完成必要清理后发布一次 terminal 失败并回收 lease；
- 环形缓冲自身持有的共享块不计入 owner credit；owner 在未取得 credit 时只保存
  游标，不得 pin ring entry 或用额外历史列表绕过上限。

### 11.5 完成与名额释放

当写入达到 `target_frames`：

1. owner 原子停止接受超过 `capture_end_cursor` 的样本；
2. owner 从 ingest registry 脱离并进入 `FREEZING`；从此已经拥有全部目标音频，
   后来的 stream failure 不再失败该 owner；
3. drain 写完最后裁剪块；
4. flush 并把 artifact 冻结为带 publication pending 的
   `READY_UNCLAIMED`；
5. 使用 owner capability、artifact ID 和唯一 `terminal_attempt_id` 向 pool
   发布 success release 控制事件；
6. pool lock 内由 success、cancel、owner failure 或 shutdown 对
   `COMMITTED` lease 执行一次 CAS，谁先成功就是 terminal 线性化点：
   - success CAS 为 `RELEASED`：后来的 cancel/failure 幂等返回该成功终态；
     pool 记录待发布动作；
   - cancel/failure/shutdown CAS 为 terminal：success release 被拒绝，不发布
     completion；pool 记录待 decline 动作；
7. pool 必须释放 pool lock 后才执行 artifact store 调用：
   - success 动作发布 completion，handler 使用 publication token 同步
     `claim_ready` 或 `decline_publication`；
   - cancel/failure/shutdown 动作调用 token-guarded `decline_publication`；
   - 任一路径都不得同时持有 pool lock 和 artifact store lock；
8. pool 只有在 artifact publication 已 claim/decline 或取消清理已确认后，才把
   对应 terminal transaction 视为 settled。

名额释放不等待最终 WAV、数据库、分析或 PLC。artifact 冻结失败时返回结构化
失败并以 `FAILED` 回收名额，不能发布看似完整的 artifact。

如果 success 已线性化但 shutdown 在 completion handler claim 前开始，
publication pending 保护文件；shutdown 控制路径必须投递该 completion。内部
handler 若接受就原子 claim/transfer，若因 shutdown 拒绝就 decline 并删除。
不能出现“已发布成功但路径在消费者 claim 前被 shutdown 删除”。

## 12. 音频回调实时性

### 12.1 callback 允许执行

callback 只允许：

1. 检查 callback closure 的 generation-alive 原子标志；
2. 读取 PortAudio status 和时间信息；
3. 记录 callback sequence 和 `callback_monotonic`；
4. 对 `indata` 建立一份连续 `float32` 共享副本；
5. 建立 `RawCallbackBlock`；
6. 对四块 bounded SPSC ingress 调用不等待的 `try_push`；
7. push 失败或 status 致命时设置 generation failure latch 并立即返回。

绝对采样游标、时间轴、ring、owner 集合和 owner queue 全部由 ingest worker
处理，不在 PortAudio callback 中修改。

### 12.2 callback 禁止执行

- 文件建立、写入、flush、rename 或删除；
- 数据库、PLC、网络或日志文件 I/O；
- 分析、STFT、校准或图表构建；
- Qt UI 信号链上的同步重工作；
- 等待队列空位、线程 join、sleep 或阻塞锁；
- 为每位 owner 复制整块音频；
- 取得 pool、stream、owner 或 artifact store lock；
- 设备枚举、流重连或完整错误清理。

callback 只能设置预先存在的 generation-alive/failure latch 和 ingress wakeup。
资源清理、状态转换和日志由 ingest/control 执行器完成。

### 12.3 锁与死锁约束

- pool registry/lease 使用 pool lock；
- 每条 stream 的 ingest worker 通过单写者事件顺序拥有 timeline、ring、owner
  registry 和 stream generation 状态；
- owner queue/artifact 状态使用 owner 自身同步；
- callback 不取得任何上述锁，只使用不等待的 bounded SPSC `try_push`；
- pool 在自己的锁内取得 stream identity 快照后释放，再向 ingest command
  queue 发送带 attempt identity 的命令；
- ingest 不回调仍持有 pool lock 的同步函数；
- artifact store lock 不与 pool lock 或 owner state lock 嵌套；
- stream/owner terminal 通过轻量控制队列通知 pool 回收 lease；
- 所有 identity-guarded 删除都比较 pool epoch、对象、generation、run ID 和
  attempt/capability，旧实例不能删除 replacement。

SPSC ingress 的实现必须提供“容量不足立即返回”的语义，不能在内部等待 consumer
持有的 stream lock。实现计划可以选择经过验证的 CPython SPSC deque 或等价
结构，但必须用 contention 测试证明 producer 不因 consumer 暂停而等待。

## 13. 流故障、恢复与隔离

### 13.1 结构化故障

采集层至少区分：

- `TOO_MANY_REQUIRED_HARDWARE`；
- `CAPTURE_CAPACITY_FULL`；
- `INVALID_CAPTURE_REQUEST`；
- `CAPTURE_STORAGE_CAPACITY`；
- `HARDWARE_RESOLUTION_CONFLICT`；
- `STREAM_OPEN_FAILED`；
- `STREAM_NOT_READY`；
- `STREAM_GENERATION_CHANGED`；
- `FIRST_VALID_BLOCK_TIMEOUT`；
- `TIMING_UNAVAILABLE`；
- `TRIGGER_AUDIO_EXPIRED`；
- `INPUT_OVERFLOW_OR_GAP`；
- `INPUT_INGRESS_BACKLOG`；
- `INPUT_DATA_TIMEOUT`；
- `CAPTURE_PERSISTENCE_BACKLOG`；
- `CAPTURE_ARTIFACT_IO_FAILED`；
- `CAPTURE_CANCELED`；
- `POOL_SHUTTING_DOWN`；
- `SHUTDOWN_INCOMPLETE`。

第二份子规格把这些稳定类别映射到运行卡片和 PLC `Int16` 数值。采集层不定义
PLC 数值。

### 13.2 需要终止整个 generation 的故障

以下情况使当前 stream generation 失败：

- PortAudio 打开异常；
- 首个有效时间块超时；
- ready 后连续时间戳缺失或恢复不连续；
- PortAudio 输入 overflow、明确丢帧或不可恢复采样缺口；
- 四块 stream ingress 已满；
- 已取得首块后连续 2 秒没有新输入；
- callback 或流线程不可恢复异常；
- 设备断开。

失败流程：

1. 原子清除 generation-alive 标志，停止接受 callback ingress；
2. 终止并失败所有引用它的 prepared、pending、catching-up 或 live capture；
   已取得全部目标样本并从 stream 脱离进入 `FREEZING` 的 owner 不再因后续
   stream failure 失败；
3. 不影响其他 `hardware_id` 的流和 capture；
4. 关闭旧 PortAudio stream；
5. 若硬件仍在 desired set 且池未关闭，进入受控重连。

单个 artifact I/O 或八块积压只失败对应 owner，不失败整条硬件流。

### 13.3 重连

同一硬件使用确定的受控退避，例如：

`0.5 s → 1 s → 2 s → 5 s → 10 s（封顶）`

- 每次实际重开取得新 generation；
- 从 desired set 移除或 pool shutdown 时取消 pending retry；
- 重连成功后仍必须等待首个有效时间块才能 ready；
- 成功 ready 后重置退避；
- 失败的旧 capture 不自动恢复或重录；
- 只恢复该硬件后续接受新触发的能力。

设备重新枚举与 stable ID 到设备 index 的再次解析由硬件解析协作者提供。池不能
使用过期 index 冒充重连成功。

## 14. 停止、关闭与临时文件清理

### 14.1 停止单个 capture

`cancel(run_id)`：

- 对 reserved lease 直接取消并回收；
- 对 committing lease 设置 cancel request，由 commit `finally` rollback 后转为
  terminal；
- 对 committed owner 原子停止未来扇出；
- 唤醒 drain，停止写入并清理 partial artifact；
- 只终止目标 `run_id`；
- terminal 事件最多发布一次；
- 不关闭常驻流。

用户可见的“停止测试”停止全部运行语义由第二份子规格把多个 `run_id` 映射到
这些单项取消接口。

### 14.2 pool shutdown

采集池关闭顺序：

1. 标记 shutting down，拒绝新的 reserve、commit、reconcile 和 retry；
2. 取消所有 reserved lease，并为 committing lease 设置 shutdown cancel；
3. 等待 committing transaction 执行 identity rollback；
4. 取消 committed owner，停止 retry timer；
5. 清除全部 generation-alive 标志，唤醒 ingest/drain；
6. 在控制路径调用所有 PortAudio stream 的 abort/stop/close；
7. 等待 stream、ingest、drain 和 commit worker 退出；
8. 完成 success/cancel terminal arbitration 及所有 publication pending 的
   claim/decline，再在 artifact store lock 内删除 partial 和可删除的
   `READY_UNCLAIMED` 文件；
9. 保留并报告 `CLAIMED/TRANSFERRED` 文件，不能在消费者仍可能读取时删除；
10. 发布关闭报告。

关闭使用以下固定期限：

- 从步骤 1 起给 cooperative cancel、close 和 join 总计 5 秒；
- 5 秒后仍存在 PortAudio handle 或 ingest thread 时，因为此时整个 pool 都已
  shutdown 且不存在合法维护并发，允许调用一次 backend-wide force terminate；
- force terminate 后再给 1 秒 join grace；
- 6 秒后仍有线程或 handle 时返回 `SHUTDOWN_INCOMPLETE`，列出具体
  `hardware_id`、generation、thread 和 handle；pool 状态保持 failed-closed，
  不得报告 `CLOSED`、重新使用 PortAudio 或继续数据库/业务操作；
- 应用顶层记录该故障后可以结束进程，依赖进程资源回收，但不能把该路径计为
  clean shutdown。

调用方必须在数据库关闭、Qt 服务销毁和进程退出之前调用该流程。正常关闭和
backend force-terminate 降级路径都必须实现无残留 worker/handle；
`SHUTDOWN_INCOMPLETE` 只用于无法协作甚至无法强制终止的异常 backend，并需要
单独的故障测试，不能让应用无限等待。

### 14.3 staging 目录

- 生产环境使用可配置的应用 staging 根目录；
- 每个进程使用独立随机 session 子目录；
- 测试使用独立临时目录；
- clean shutdown 删除本 session 已释放、partial 和 ready-unclaimed 文件；
- clean shutdown 不删除 claimed/transferred 文件；
- 下次启动确认旧 session 不活动后，可尽力删除其 partial/ready/claimed 孤儿；
- 清理失败只记录明确路径，不删除现有最终 WAV 或数据库记录；
- staging 文件不构成崩溃恢复承诺。

## 15. 与后续子规格的边界

### 15.1 第二份子规格：多运行协调

第二份子规格负责：

- 在触发入口立即记录 `trigger_monotonic` 和分配 `run_id`；
- 解析配置、冻结快照并取得 `StreamReadyToken`；
- 按本规格调用 reserve/commit/cancel；正常 release 由 owner capability 控制
  事件在采集池内部完成，协调器不得提前释放；
- 原子 claim/transfer 冻结 artifact，并在最终 WAV/数据库成功或失败后 release
  artifact claim；
- 把 capture terminal 转成运行状态、最终录音文件、数据库记录和分析任务；
- 保证池外不存在第二份录音容量计数；
- 正式从旧 `AudioRecorder`/`config_id` 单运行路径切换到新采集层。

本子规格的实施可以先增加并独立验证新组件；在第二份子规格完成原子切换前，
现有生产触发路径保持旧实现，不能同时由新旧 recorder 打开同一实际硬件。

### 15.2 第四份子规格：硬件生命周期

第四份子规格负责：

- 从当前启用配置生成 desired hardware set；
- 启动恢复、选择变化和硬件解析；
- 添加、刷新、删除、重新绑定和校准的独占门禁；
- 在维护前协调停止受影响流，维护后重建；
- 真实设备重连协作者；
- 应用退出时把 pool shutdown 接入总关闭顺序；
- 六路真实或可替代硬件的端到端压力验收。

`ResidentAudioPool` 必须提供完成这些流程所需的 reconcile、状态快照和 shutdown
接口，但不自行读取 UI 选择或数据库配置。

### 15.3 模块化约束

`TestManager` 当前约 7188 行，属于明确的长期维护风险。实现不得把
`ResidentAudioPool`、时间轴、环形缓冲、artifact 或 lease 状态机继续堆入
`test_manager.py`。

新组件应放在职责单一、可独立测试的模块中。实现计划必须在任务边界中明确各
模块所有权，并避免建立另一个同规模“采集总文件”。

## 16. 可观测性

状态事件和日志至少包含适用的：

- `run_id`；
- `hardware_id`；
- `stream_generation`；
- stream/owner/lease 前后状态；
- 实际采样率、声道数和 callback frames；
- trigger、映射起点及最早/最新 ring 时间；
- 队列深度和 artifact 路径；
- 结构化错误类别与原始异常；
- retry 次数和下一次间隔。

不得在每个正常 callback 写普通日志。正常流量使用计数器或采样调试信息，错误
状态转换才写结构化日志。不得在日志中输出整块音频数据。

建议暴露只读诊断快照：

- desired/ready/failed stream 数量；
- 每条流状态、generation、ring 覆盖时长和最后块时间；
- reserved/committing/committed lease 数；
- 每位 owner 状态、已占 credits、待写块数和已写帧数；
- 每条 stream ingress 深度和 failure latch；
- artifact ownership state 和 consumer ID；
- callback 丢弃的旧 generation 数；
- overflow、时间恢复和 backlog 计数。

## 17. 测试策略

### 17.1 纯单元测试

`StreamTimeline`：

- 有效 ADC 时间公式；
- 块内、块边界和块后触发的向上取整；
- 事件前样本不被选择；
- 单调游标与多锚点插值；
- 首块无效时不 ready；
- ADC 时间非有限、非正、倒序、超过 `max_adc_age` 和未来超过一帧时无效；
- variable callback frames 使用相邻较大块计算 continuity tolerance；
- 单个无效时间块后成功恢复；
- 连续第二个无效块失败；
- 锚点倒退或偏差超过一个块失败；
- 墙钟变化不影响映射。

`TimestampedBlockRing`：

- 至少两秒覆盖；
- 完整块淘汰及最多一个边界块额外保留；
- 环回后按绝对游标读取；
- 过期游标明确失败；
- generation 不混合；
- 运行时间增加时内存不线性增长。

capture lease：

- 非有限/非正/future trigger、非有限/非正/超过 3600 秒时长、帧数或字节溢出
  在 `COMMITTING` 前失败并回收；
- invalid request 走 `RESERVED → FAILED`；后续 cancel/commit/reserve 同一 run
  只返回原失败；
- 单 capture 8 GiB 上限、staging quota 和磁盘空间不足均不建立 partial；
- 六条并发 quota reservation 不会超卖；terminal 不提前释放，artifact 删除或
  移出 staging 后最多释放一次 quota；
- reserve/commit/release/cancel 正常状态；
- 重复调用幂等；
- 相同 `run_id` 拒绝重复；
- 六条成功、第七条原子失败；
- 多线程 reserve 竞态始终最多六条；
- commit generation 变化失败并正确回收；
- prepared 后 cancel、stream failure 和 shutdown 均完成 identity rollback；
- activation 前后 cancel 竞态不会让 terminal lease 留下 active owner；
- terminal 回调与 cancel 竞态最多释放一次；
- 正常 release 只有正确 owner capability 可以调用，客户端不能提前释放。

`CaptureOwner`：

- 不同起点和时长独立结束；
- 同流重叠 owner 获得各自精确区间；
- 同配置不同 `run_id` 不覆盖；
- 晚注册从 ring 回填并继续 live；
- 正常 callback 间隙和 timing recovery 中进入 `WAITING_FOR_MAPPING`，恢复后从原
  trigger 映射；
- pending mapping 在 generation change、2 秒超时、cancel 和 shutdown 时使用
  正确原因终止；
- mapping 前 ring 越过 trigger 为 `TRIGGER_AUDIO_EXPIRED`；
- 映射后 catch-up cursor 被 ring 淘汰为 `CAPTURE_PERSISTENCE_BACKLOG`；
- 游标追赶受统一八 credits 限制；
- 一个 in-flight 与七个 queued 合计八；第九引用失败且从未出现九引用瞬间；
- catch-up batch 与 live queue 不共存；
- catch-up 到 live 无缺口、无重复；
- live queue 第九块只失败该 owner；
- artifact I/O 失败只失败该 owner；
- 正常完成冻结准确帧数和元数据；
- 录音中 cancel 删除 partial 且不产生提前冻结 artifact；
- freeze/release 与 cancel 竞态：success CAS 胜出则 completion 可 claim，cancel
  CAS 胜出则 ready-unclaimed 删除且没有 completion；
- freeze/release 与 owner failure、shutdown 竞态遵守相同 terminal CAS；
- success 已线性化后 shutdown 等待 publication claim/decline，不提前删除；
- cancel 删除 partial 且不关闭流。

### 17.2 stream 与 callback 测试

使用可注入的 sounddevice/clock/artifact fake 覆盖：

- 相同 `hardware_id` 多次 reconcile 只打开一条流；
- 不同硬件最多打开六条；
- 七硬件 desired set 整体拒绝且不修改已有集合；
- 使用解析所得 device index 和格式，不重新按名称选择；
- desired set remove/re-add 后 generation 仍增加；
- 被 remove、新 attempt 或 shutdown 取代的迟到 resolver/open 结果被关闭且不发布；
- 首个有效块后才 ready；
- 首块超时、运行中无数据超时、overflow、四块 ingress 满和断开；
- 失败只终止同硬件 owner；
- 受控退避和成功后重置；
- 每次重连 generation 增加；
- 旧 generation callback 全部丢弃；
- shutdown 后无新 callback 改变状态。

callback 实时性必须同时做结构断言和压力断言：

- mock 证明 callback 不调用任何文件、数据库、分析、Qt UI、sleep、wait 或
  阻塞 queue API；
- 每个 callback 只复制一次输入块并只产生一个 `RawCallbackBlock`；ingest
  扇出六 owner 后所有 queue 引用同一音频数据对象；
- consumer 暂停时 callback 的 SPSC `try_push` 不等待；第五块锁存 generation
  failure 而不覆盖或扩张；
- 任一满 owner queue 不阻塞 ingest 对其他 owner 的扇出；
- 用生产块大小连续执行至少 10,000 次 synthetic callback + ingest，游标无
  丢失且内存有界；
- 性能标记测试要求 callback 的 p99 明显低于实际块时长，
  实施计划应结合 CI 基线设定不掩盖回归且不过度易抖的阈值；
- 六条 fake stream 并行 callback 时无全局 callback 锁串行化。

### 17.3 artifact 与关闭测试

- 预分配文件形状、dtype、声道顺序和逻辑帧数；
- partial 到 ready 的原子冻结；
- ready-unclaimed、claim、transfer、release 全状态和 token 校验；
- token-guarded `decline_publication` 删除未接受 completion，重复调用幂等；
- publication pending 只在 completion claim/decline 后清除；
- claim 与 shutdown 竞态由 artifact store lock 决定，claim 成功后文件不删除；
- transfer 后 pool shutdown 不删除，consumer release 后才删除；
- claimed/transferred 文件仍占 staging quota；在其释放前新 capture 按剩余额度
  拒绝；
- 删除失败进入 `DELETE_PENDING` 并继续占 quota，清理重试成功后才释放；
- 等待消费者只持有 `FrozenCaptureArtifact`，不持完整数组；
- 失败和取消清理 partial；
- clean shutdown 清理本 session；
- 孤儿清理不触碰现有最终 WAV；
- cooperative shutdown 在 5 秒内 join 全部资源；
- close 卡住时第 5 秒触发 backend force terminate，并在额外 1 秒内 join；
- force terminate 后仍不退出时返回 `SHUTDOWN_INCOMPLETE` 且不报告 closed；
- commit/cancel/shutdown 与 blocked close 竞态均不遗留 active owner；
- 所有 fake stream/ingest/drain/commit worker 正常及 force 路径均被 join；
- 退出后迟到 callback 不复活资源。

### 17.4 兼容回归

- `CapturedAudioBlock` 和窗口半开区间行为保持；
- 采样率、声道顺序、`float32` 数值与旧录音输入等价；
- 相同录音时长仍使用向上取整目标帧数；
- 新 capture 不再包含固定 50 ms warmup；
- 对固定既有音频 artifact，交给后续分析得到的输入数组与预期完全一致；
- 本切片不改变结果 UI、PLC 字段或数据库 schema。

## 18. 验收标准

1. 当前所需集合内相同 `hardware_id` 只打开一条常驻输入流；不同硬件最多六条。
2. 流只有在取得有效首块和可靠单调时间基准后才 ready。
3. 触发不需要等待设备枚举或 PortAudio 开流；晚注册可以从至少两秒 ring 回填。
4. capture 起点是不早于事件时间的最近采样帧，误差不超过一个输入块，且没有
   固定 50 ms warmup。
5. 时间基准从未有效时不接受 capture；单块偶发时间缺失可恢复，无法恢复时
   明确失败相关 generation。
6. 同一流上的多个重叠 capture 按 `run_id` 获得各自完整、无重复、无缺口的
   样本区间。
7. 全局最多六条 reserved/committing/committed capture，第七条立即结构化
   拒绝且不排队。
8. 录音完成、失败或取消后名额最多释放一次；正常完成不等待分析即可释放。
9. 每位 capture 在 ring 之外最多持有八个未写块引用，queued、catch-up 和
   in-flight 共用同一 credit；第九块只失败该 capture，callback 和其他
   capture 继续。
10. capture 使用磁盘后备 artifact；录音结束后的等待队列不保存完整 NumPy
    音频数组。
11. frozen artifact 只有经过 claim/transfer 才由后续保存层拥有；shutdown
    不删除 claimed/transferred 文件；物理 staging 文件删除或移出前始终占用
    quota。
12. PortAudio callback 不执行游标、ring、owner、磁盘、数据库、分析、PLC
    或 UI 工作，并通过 bounded ingress 与六 fake stream 压力测试。
13. 一条流失败只终止引用该硬件的 capture；其他硬件流继续。
14. 重连使用新 generation，旧 callback 和旧 token 均不能更新新流或新运行。
15. cooperative 和 backend force-terminate shutdown 后无残留 PortAudio 流、
    ingest/drain/commit worker 或仍可写入的 callback；无法强制终止的 backend
    返回 `SHUTDOWN_INCOMPLETE` 而不谎报 clean close。
16. 现有硬件记录、稳定 `hardware_id`、配置绑定、校准数据、数据库记录和最终
    WAV 不被迁移、覆盖或删除。
17. 本子规格实施不修改结果 UI；UI 人工检查不适用于本切片。

## 19. Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces: [现有受管硬件记录、当前数据库内稳定 `hardware_id`、配置与硬件绑定、麦克风校准、实际采样率与声道顺序、`float32` 录音样本、现有最终录音文件、数据库记录、最终分析输入含义、结果 UI、普通 PLC 协议]
- Allowed breakage: [内部 `AudioRecorder`/shared-recorder/capture attachment 接口、按配置设备字符串共享 recorder 的行为、单次录音完成即关闭输入流、固定约 50 ms session warmup、按 `config_id` 唯一保存 recorder owner 的内部假设]
- Migration strategy: none

本子规格不需要数据库迁移，不移动、覆盖或重命名现有最终录音。新 staging
artifact 是进程内采集基础设施，不是历史数据格式。正式生产触发路径在第二份
子规格完成原子切换前继续使用旧 recorder；切换后不保留新旧路径并行打开同一
硬件的兼容模式。
