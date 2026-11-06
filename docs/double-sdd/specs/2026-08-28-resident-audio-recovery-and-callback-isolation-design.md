# Resident 音频恢复、回调隔离与校验收敛设计

## 1. 文档状态与权威范围

- 日期：2026-08-28
- 状态：规格审查与用户书面复核均已通过
- 适用范围：当前生产组合使用的 `resident_audio_mvp`、受管 Resident 捕获端口、
  SoundDevice/PortAudio 输入适配和实时显示音频扇出
- 上游设计：
  - `2026-07-25-resident-audio-capture-design.md`
  - `2026-07-25-multi-run-audio-testing-core-design.md`

本文只修订 Resident 运行期的软件流健康判断和恢复语义。它取代 Core MVP 中“流失败后必须重启应用、
不做自动恢复”的限制，但不放宽精确硬件身份、配置重启生效、禁止自动换绑设备等规则。本文没有把
Phase 2 的大型 `resident_audio` 子系统设为生产依赖。

## 2. 背景与已确认问题

PLC 启动和界面按钮启动最终都进入同一个 captured-start 路径，并在创建运行前向
`ManagedResidentCapturePort.reserve()` 申请 Resident 捕获。生产组合为该端口注入
`_LiveDisplayAudioBackend.capture_ready()` 作为健康探针。

当前实现存在以下相互关联的问题：

1. 探针把“最后一次成功 callback 距今超过 1 秒”直接视为不健康，并把关闭旧句柄、创建新句柄、
   启动以及等待首个 callback 全部压进同一个 1 秒预算。
2. 心跳只在整个 Resident callback 返回 `CONTINUE` 后更新。callback 内的 Ring、实时显示、锁竞争
   或其他软件工作失败或变慢，都会被误报成“硬件未返回采样”。
3. `_LiveDisplayAudioBackend` 和 Resident Pool 分别维护 generation、心跳和故障状态，形成两套状态
   权威。外层所谓重建只替换原始 SoundDevice 句柄，没有重建 Resident slot、callback closure 和 Ring。
4. Resident slot 一旦进入 `FAULTED`，会同时撤销 generation 并停止接受 callback；外层却继续复用
   这个旧 callback。因此新句柄即使打开，也会永久收到 `STOP`。
5. 当旧句柄关闭返回 `NOT_CONFIRMED` 时，外层会清空 registration generation，却保留同一个已污染
   句柄。后续每次启动重复得到 `False`，不会再创建真正的新 generation。
6. 生产包装层没有把底层 `on_fault` 交还 Resident Pool，导致底层与 Pool 对同一物理流的状态可能分叉。
7. `False` 和所有异常最终都折叠成相同的 `resident_unavailable / 音频输入设备未返回采样`，无法区分
   callback 暂时迟到、连续性丢失、关闭失败、打开失败、启动失败或首采样超时。
8. 当前 PortAudio callback 路径同步执行了超出实时边界的工作，包括持锁调用 Resident callback、
   Ring 处理以及实时显示扇出。分析、显示、回传或数据库负载因此可能放大 callback 饥饿概率。

已通过确定性故障替身复现粘滞分支：旧句柄关闭持续返回 `NOT_CONFIRMED` 后，连续两次 readiness probe
均失败，底层创建次数仍为一次，registration generation 为 `None`，且注册对象仍持有同一个旧句柄。
这解释了“一次错误后，后续启动始终报错，直至重启程序”。

## 3. 目标

1. 硬件持续正常采样时，分析、实时显示、结果回传、保存、数据库增长或普通线程调度抖动不得造成
   虚假的 `resident_unavailable`。
2. Resident 软件对象或 PortAudio 流发生故障后，不重启应用也能建立完整的新 generation，使后续
   启动恢复正常。
3. 启动遇到可疑流时最多使用同一个 5 秒单调时钟预算完成判定和恢复。
4. 保持原触发时刻的音频语义；不得为了让本次运行看起来成功而把录音起点平移到恢复完成时刻。
5. 一条运行处于分析、显示、保存或 PLC 回传阶段时，另一条运行的启动和录制不得因此明显延迟。
6. 同一硬件继续支持重叠捕获；不同硬件的健康、恢复和录制互不阻塞。
7. 故障必须报告准确阶段和稳定原因，不能再用宽泛捕获把所有问题折叠成布尔值。
8. 删除或合并可信内部边界上的重复校验，同时保留防止音频损坏、越界、错绑和跨 generation 污染的
   安全校验。
9. 保持所有用户可见的操作、功能、PLC 语义、配置、数据库数据、录音文件和分析结果格式不变。

## 4. 非目标

- 不自动选择“名称相似”的设备，也不在故障时换绑到另一个硬件端点。
- 不新增硬件热插拔配置流程；恢复只重新打开已经精确解析并受管的同一端点。
- 不改变分析算法、判断阈值、结果含义、WAV 命名和格式、历史卡片或 PLC 寄存器协议。
- 不改变默认分析并发数 2、可配置范围 1–6、最多六条 concurrent capture 等既有容量语义。
- 不把生产代码整体迁移到仓库中另一套体量较大的 `app.service.analysis.resident_audio`。
- 不在本规格内修改 Today Statistics 的数据库查询或数据库 schema。数据库扫描优化可另立独立性能任务，
  但 Resident 正确性不能依赖该优化。
- 不承诺修复物理断线、独占设备被外部程序占用或驱动永久失效；这些情况必须准确失败并持续有界重试。

## 5. 核心不变量

### 5.1 时间与连续性

1. 每次 PLC 或按钮入口首先记录不可变的单调触发时刻 `trigger_monotonic_ns`。
2. `CapturedStartRequest` 和 `FrozenCapturedRun` 已经携带该时刻；coordinator 在 Resident reserve 阶段必须
   把 `frozen.trigger_monotonic_ns` 继续传入端口，不能等到后续 `start()` 才第一次交给 Resident。
3. “一块音频”的未来时钟容差按 `start_capture()` 调用时的可信 `monotonic_now` 计算，与 Ring tail
   无关；条件保持为 `trigger <= monotonic_now + one_block_duration`。
4. 若合法 trigger 位于健康 generation 的 Ring tail 之后，无论 tail 落后 trigger 几个 block，Ring 都
   返回 `NOT_YET_AVAILABLE` 并等待后续块；只要第 3 项的当前时钟检查通过，它就不是 invalid request。
5. 若触发时刻已早于 Ring 的两秒逻辑窗口，按现有 `EXPIRED` 语义在 post-accept `start_capture()` 阶段
   形成 terminal run，不能提前改成 preflight 拒绝，也不能以重建流规避过期。
6. 本次运行只有在它所绑定的同一 generation 能证明覆盖原触发时刻，并且从所需起点到当前没有
   overflow、callback gap 或 generation change 时，才能产出成功音频。
7. 恢复等待不能改变 `trigger_monotonic_ns`。
8. 新 generation 永远不能为旧 generation 的触发时刻提供伪造音频。

### 5.2 generation

1. generation 同时拥有 callback、原始输入句柄、入口缓冲、Ring、心跳和故障证据。
2. generation 失效后，其 callback 即使迟到也只能丢弃数据，不能写入新 generation。
3. 完整恢复必须创建新的 generation、callback closure、入口缓冲、Ring 和原始输入句柄。
4. 旧句柄关闭未确认时，旧 generation 仍必须从活动索引中永久摘除；不得作为后续恢复的候选句柄复用。
5. 未确认关闭的资源进入独立 orphan-cleanup 记录，不能占用活动 generation 的状态槽。

### 5.3 并发隔离

1. 同一 `hardware_id` 只有一个活动物理输入 generation，但可扇出给多个重叠 capture owner。
2. 每个 `hardware_id` 只有一个在途恢复任务；同硬件的并发探针共享该任务及结果，不能重复 stop/close/open。
3. 恢复不得在等待 callback、stop、close、open 或 start 时持有 Pool 全局锁。
4. Managed port 不得在自身锁内调用 readiness、等待恢复或执行 Pool/Backend 外部调用。
5. 一条硬件的恢复不得等待另一条硬件的分析、显示、回传、保存或恢复。
6. 5 秒等待使用可挂起的 captured-start continuation；等待期间仍占用原有启动准入容量，但不占用两个
   captured-start worker。已经准入的另一硬件启动仍可运行。
7. 已完成采集并进入保存、分析、显示或 PLC 回传的运行不再持有 Resident capture 资源。

### 5.4 容量与功能

- 保留最多六条受管物理流、最多六条并发 capture、captured-start 双 worker、分析默认双 worker及其既有
  独立容量；任何容量都不得互相借位。
- 正常健康路径不能执行 stop、close、open、start，也不能等待 5 秒恢复预算。

## 6. 选定方案

采用“Resident Pool 单一状态权威 + 完整 generation 恢复 + callback 通道隔离”。

未采用的路线：

- 只把 1 秒超时延长到 5 秒并修补外层句柄重建：仍保留双重状态权威，无法系统性消除粘滞故障。
- 整体迁移到大型 `resident_audio`：涉及过多当前未使用的 artifact、settlement、shutdown 和严格值对象，
  改动面及校验数量均与本次目标不相称。

## 7. 组件职责

### 7.1 ResidentAudioPool：唯一状态权威

Pool 负责：

- 受管硬件到活动 generation 的唯一映射；
- generation 状态、callback 心跳、Ring 连续性和显式底层故障；
- capture owner 的注册、独立区间和终态；
- 每硬件恢复门、恢复结果和后台重试；
- 旧 generation 摘除和孤儿资源清理。

Pool 对外提供结构化、与 trigger 无关的 lifecycle 判定，不再让调用者读取外层包装器的布尔心跳。
概念结果如下：

- `GENERATION_CONTINUOUS`：原 generation 健康，或可疑后恢复且其自身 timeline 没有新增 gap；
- `GENERATION_REPLACED`：旧 generation 已退役，完整新 generation 已发布；
- `RECOVERY_PENDING`：前台 deadline 内尚未恢复，后台继续重试；
- `RECOVERY_FAILED`：本次前台尝试已得到可重试失败，后台会按矩阵继续；
- `LIFECYCLE_CANCELLED`：Pool closing、desired revision 更新或 candidate superseded，本 lifecycle owner
  不再发布业务失败，由 coordinator/新 revision 的既有所有者处理。

具体类型和名称可在计划阶段按现有模块风格确定，但不得退回无诊断的 `bool`。

共享恢复任务只发布与 trigger 无关的 lifecycle outcome：旧 generation 是否仍连续、是否已退役、新
generation 是否发布、失败阶段和下一次重试时间。它不得发布“某次启动可继续”这种 trigger-specific
结论。每个 reserve claim 在收到同一个 lifecycle outcome 后，必须使用自己的 trigger、run id 和所见
generation revision 独立执行 Ring 映射与过期判定。并发 trigger 即使乱序到达，也不能共享映射结果。

### 7.2 原始音频 Backend：被动硬件适配器

Backend 只负责：

- 按已经解析的精确设备 index 和格式创建输入句柄；
- start、abort、stop、close 等有界硬件调用；
- 把 PortAudio status 和异常原样转换为结构化底层故障；
- 把采样交给创建该句柄时绑定的 generation callback。

Backend 不再拥有业务 generation、Resident readiness、capture owner 或第二套恢复状态机。
`_LiveDisplayAudioBackend.capture_ready()` 及其 registration generation 不再作为生产健康权威。

### 7.3 Callback ingress 与 Resident drain

PortAudio callback 只允许执行：

1. 检查预先绑定的 generation 是否仍可接收；
2. 读取已规范化的帧数和 PortAudio status；
3. 把数据复制进预分配、非等待的单生产者入口缓冲；
4. 在复制成功后立即推进 callback 序号和心跳；
5. 在入口缓冲满或 status 异常时记录有界故障并立即返回。

callback 禁止执行：

- 分析、保存、数据库、日志 I/O、PLC 回传或 Qt 信号派发；
- 实时显示处理；
- 等待 Condition、Future、线程、设备生命周期或可阻塞锁；
- 动态创建大数组或按 capture owner 重复复制完整音频；
- 调用可能抛出宽泛业务异常的下游流水线。

每条活动 generation 配置独立 Resident drain。drain 先把入口块写入 canonical Ring 并推进 capture owner，
然后通过有界、可丢帧的显示队列扇出实时显示。显示队列拥塞只丢显示帧并产生诊断，不能反压 Resident
采集或使硬件心跳变旧。分析、保存和结果回传继续使用现有独立调度器。

### 7.4 ManagedResidentCapturePort

软件入口与用户可见契约保持不变；内部端口契约由 `reserve(run_id, hardware_id)` 收敛为带触发时刻和
deadline 的异步两阶段 reserve。概念接口为：

- `begin_reserve(run_id, hardware_id, trigger_monotonic_ns, deadline_monotonic_ns)`；
- 立即返回 `RESERVED`、lifecycle/capacity failure，或返回带认证 claim token 的 `PENDING`；
- coordinator 只可用原 claim token 恢复同一次调用，不能用同一个 run id 再调用 `begin_reserve()`。

coordinator 从 `FrozenCapturedRun` 传入 trigger，并消费结构化结果。

- `GENERATION_CONTINUOUS`：端口创建钉在该 generation revision 的 binding，但不提前消费 trigger
  mapping。durable ACTIVE、completion registration 和现有 active history 提交后，仍由 `start_capture()`
  执行 AVAILABLE/NOT_YET_AVAILABLE/EXPIRED/TIMELINE_GAP；后两者维持现有 terminal-run 行为。
- `GENERATION_REPLACED`：返回兼容类别 `resident_unavailable`，明确说明本次触发跨 generation，当前流
  已经恢复，下一次触发可用。
- `RECOVERY_PENDING` 或 `RECOVERY_FAILED`：返回兼容类别 `resident_unavailable` 和精确阶段/原因。
- `LIFECYCLE_CANCELLED`：返回内部 closing/cancelled 结果，让 coordinator 的现有 cancel、shutdown 或
  newer-revision 路径获胜，不能改写成 Resident 设备错误。

端口不得自行重建句柄、维护心跳或吞掉 Pool 的结构化错误。

端口的线性化顺序固定如下：

1. 在端口锁内检查 lifecycle、route、重复 run id 和六项 capture 容量；
2. 容量可用后创建唯一 `_ReserveClaim`，该 claim 立即占用一项 capture 容量；
3. 释放端口锁，再调用 Pool readiness/reserve；
4. 若需要等待，则 claim 进入 `WAITING`，worker 挂起；若得到连续 generation，则 Pool 创建绑定并把
   binding 钉在该 generation id 上；
5. 回到端口锁内发布 reservation，或在端口已关闭/claim 已取消时在锁外取消刚创建的 binding；
6. claim、reservation 或 terminal cleanup 只能有一个路径释放该容量，必须 exactly-once。

公开的重复 run id 行为保持现状：第二次独立调用明确拒绝。continuation 不是第二次公开调用，必须用原
claim token 鉴权。端口关闭或运行取消时，等待 claim 先从 lifecycle future 退订，再释放容量；共享的
硬件后台恢复不因某个 claim 取消而取消。

### 7.5 Captured-start continuation

`CapturedStartDispatcher` 保留容量 2 和 worker 数 2，但内部工作结果增加 `COMPLETE` 与 `PARKED`：

- 提交时仍立即取得一个现有容量 permit，因而第三条 concurrent captured-start 的拒绝语义不变；
- Resident 返回 `PENDING` 时，dispatcher 把 permit 转交给不可伪造的 parked ticket，并立即释放当前
  worker 去处理另一条已经准入的启动；
- lifecycle future 完成或 deadline 到达时，只把原 continuation 重新放回原 lane，不重新取得 permit；
- continuation 在 captured-start worker 上恢复 coordinator，不允许 recovery 线程直接调用 coordinator；
- 完成、取消、异常或 dispatcher close 只能释放 permit 一次；close 会取消 parked ticket 并唤醒
  coordinator 走现有关闭路径。

这样两个异常启动仍受现有容量 2 限制，但不会把两个 worker 都阻塞在 Event.wait() 上。对 coordinator
的修改只限 Resident reserve 阶段的 park/resume，不重写后续持久化、录制、保存或分析状态机。

## 8. 正常数据流与隔离边界

```text
PortAudio callback
    -> generation ingress + heartbeat
        -> per-generation Resident drain
            -> canonical timestamped Ring
            -> overlapping capture owners
            -> best-effort live-display queue

capture terminal
    -> existing save dispatcher
        -> existing analysis scheduler (default workers = 2)
            -> existing UI / PLC result publication
```

箭头只能向下游传播数据。保存、分析、显示和回传不得反向取得 callback 锁或 Pool lifecycle 锁。

## 9. 启动判定与 5 秒恢复语义

### 9.1 统一 deadline

- captured-start 进入 Resident reserve 时创建 `now + 5.0 seconds` 的单一单调 deadline。
- 所有等待和硬件步骤使用剩余预算，不能每阶段重新获得 5 秒。
- 5 秒是异常路径的最大等待时间，不是正常启动延迟。

### 9.2 判定顺序

1. 若 generation callback 心跳新鲜且无显式故障，把 claim/binding 钉在该 generation revision 上并
   继续现有 durable ACTIVE 流程；此阶段不把 trigger mapping 结果发布为 preflight failure。
2. 若心跳变旧但 generation 尚未显式故障，先进入 `SUSPECT`，最多给原 generation 1 秒且不超过总
   deadline 的恢复观察窗口。
3. 若 callback 序号恢复推进，共享 lifecycle outcome 返回“原 generation 仍可用”；每个 claim 创建
   钉在原 revision 的 binding。其 trigger 仍在 post-accept `start_capture()` 独立映射。
4. 若出现显式故障、连续性缺口、入口 overflow，或观察窗口结束仍无 callback，则原 generation 不再
   能服务本次触发，进入完整 replacement。
5. replacement 在总 deadline 剩余时间内成功，则返回 `GENERATION_REPLACED`；本次触发失败，
   但活动映射已指向新 generation。
6. replacement 未在 deadline 内成功，则返回 `RECOVERY_PENDING`，并由后台重试接管。

任何在新 generation 发布前已经创建的 claim 都以 `generation_replaced_for_trigger` 失败，即使该 claim
的 trigger 恰好晚于新 generation 首块。新 generation 发布之后才进入的全新启动可按正常路径使用它。
这使“本次触发不因重建而伪成功”的边界具有单一线性化点。

“心跳新鲜”基于 callback 已成功进入 generation ingress，而不是整个显示或业务 callback 完成。
初始阈值沿用当前 1 秒，但实现必须使该阈值只反映 PortAudio/ingress，不再反映下游工作耗时。

Trigger mapping 的接受边界保持现状：

- `EXPIRED`、`TIMELINE_GAP` 和映射出的 stream fault 由已接受运行的 `CaptureOutcome` 终态处理，保留
  `TerminalErrorCategory.CAPTURE_FAILED`、数据库 terminal row、结果卡片和 PLC terminal publication；
- trigger 超出 `start_capture()` 当前单调时钟一块容差时，仍走现有 post-accept
  `CAPTURE_START_FAILED`；
- 这些 trigger-specific 错误不启动 lifecycle recovery；
- 只有 lifecycle 在 reserve 前已经换成新 generation、或 5 秒内仍不可用时，才维持 preflight
  `resident_unavailable`。

### 9.3 完整 replacement

一次 replacement 必须按以下所有权顺序执行：

1. 在 Pool 的短临界区内把旧 generation 标为不可接收并从活动映射摘除；
2. 释放 Pool 锁；
3. 对旧句柄执行有界 abort/stop/close；
4. 若关闭未确认，将旧资源登记为 orphan，不把旧句柄放回活动 registration；
5. 为同一精确硬件端点创建全新的 generation、callback、ingress、Ring 和原始句柄；
6. 启动句柄并等待新 generation 的首个成功 ingress；
7. 在短临界区内确认 desired hardware/revision 未改变后发布为活动 generation；
8. 若发布前条件已过期，关闭该候选 generation，不能覆盖较新的状态。

不能因为旧 close 未确认就把活动状态永久留在 `generation=None + old_handle`。也不能用全局
PortAudio terminate 作为普通单硬件恢复步骤，因为它会污染其他健康输入流。

### 9.4 后台重试

- 每硬件独立使用 1、2、5、10、30 秒退避，之后保持 30 秒上限。
- 每次尝试本身使用有界硬件调用，不能形成永久阻塞线程。
- 只要该精确硬件仍在 desired set、应用仍运行且没有更新的 generation，重试持续进行。
- 新启动若遇到同一个在途重试，只能共享结果或在自己的 5 秒 deadline 到期后失败，不能另建恢复任务。
- 成功收到首个 ingress 后清零退避。
- 配置改变、应用关闭或 generation 被更新版本替代时取消旧重试。

### 9.5 重试性矩阵

| 条件 | 本次 claim | 启动/加入恢复 | 后台重试 |
|---|---|---|---|
| trigger 过期、历史 timeline gap | post-accept `CAPTURE_FAILED` terminal run | 否 | 否 |
| trigger 超出当前单调时钟一块容差 | post-accept `CAPTURE_START_FAILED` | 否 | 否 |
| 重复 run id、capture 容量满 | 按现有类别拒绝 | 否 | 否 |
| heartbeat stale、显式 backend fault、ingress overflow | 等待或失败 | 是，同硬件合并 | 是 |
| old close 未确认 | 在原 5 秒 deadline 内等待 replacement；新 generation 发布后仍以跨 generation 失败 | 是 | 是，但受 orphan 上限约束 |
| endpoint 扫描暂时失败、缺失、歧义或身份不匹配 | 失败关闭 | 是 | 是，绝不换绑 |
| candidate open/start/first-sample 失败或超时 | 失败 | 是 | 是 |
| candidate 被新 desired revision 或 shutdown 取代 | `LIFECYCLE_CANCELLED`，由现有关闭/取消所有者结算 | 否，由新 revision 所有者决定 | 否 |
| orphan 上限已满 | 失败并报告资源阻塞 | 只加入 cleanup 观察 | 不创建新句柄；资源释放后恢复重试 |
| Pool/应用正在关闭 | 走现有 closing 类别 | 否 | 否 |

后台尝试次数不设“耗尽即永久停止”；只有退避上限 30 秒。因此诊断使用 `retry_scheduled`、
`retry_wait` 或 `orphan_limit_reached`，不产生虚假的 `retry_exhausted`。

### 9.6 每次重开的精确端点解析

恢复不能直接复用启动时冻结的 `device_index`。每次 candidate open 前必须执行一次新的完整输入枚举，
并使用新的 scan id 重新验证：

- 有 stable backend UID 时，要求 UID 唯一匹配，且 host API、设备描述、采样率和输入声道仍与冻结配置
  兼容；
- 没有 stable UID 时，要求 `(normalized device name, host API name, default sample rate, input channels)`
  完整元组恰好匹配一个观察值；不能只按名称匹配；
- 零个或多个匹配均失败关闭，记录 missing/ambiguous，不使用“第一个设备”或相似名称；
- 只有验证成功后，才用该次枚举得到的新 `device_index` 构造 candidate spec；不回写用户配置和数据库。

### 9.7 有界 native lifecycle 与 orphan 上限

SoundDevice/PortAudio 调用无法安全强杀 Python 线程，因此有界语义定义为“调用者有界返回、卡住的 native
worker 被隔离且数量有上限”，而不是声称能够取消任意 C 调用。

1. 每个候选句柄拥有一个 daemon control worker；InputStream/open、start、stop、close 只在该 worker
   上串行执行，控制面只等待总 deadline 的剩余预算。
2. orphan 有四个明确状态：
   - `CALL_RUNNING`：native lifecycle call 正常在途；
   - `CALL_TIMED_OUT`：控制面 deadline 已过，但 worker 仍停留在该 native call，只允许轮询 completion；
   - `CLOSE_RETRYABLE`：worker 已返回，但 abort/stop/close 给出异常或 `NOT_CONFIRMED`；该 worker 和
     native handle 只接受 cleanup command，不能再成为活动流；
   - `CLOSED`：取得下面定义的关闭证据，可以释放 orphan slot。
3. deadline 到达后 generation 和句柄立即从活动映射摘除。`CALL_TIMED_OUT` 不能收到第二个 lifecycle
   command；它之后若返回，则按返回证据转为 `CLOSED` 或 `CLOSE_RETRYABLE`。
4. 失效 generation 的 callback 返回 `STOP`，SoundDevice 适配器必须转换成 native `CallbackStop`，
   同时 control owner 设置 stop/close 请求，不能仅无限丢弃迟到 callback。
5. `CLOSE_RETRYABLE` 的 control worker 必须保持可用，并按 1、2、5、10、30 秒、之后 30 秒上限执行
   cleanup：优先 native abort（若可用），然后 stop/close；一次 cleanup 仍只使用该 worker，控制面有界
   等待。cleanup call 卡住时转为 `CALL_TIMED_OUT`，返回异常时仍为 `CLOSE_RETRYABLE`。
6. 释放 orphan slot 只接受三类证据：worker 证明 native handle 从未构造成功；native `close()` 正常
   返回；或在同一 control worker 上读取到 backend 明确的 `closed is True` 且 `active is False`。
   CallbackStop、worker 结束、对象析构、经过一段时间或设备重新枚举本身都不是关闭证据。
7. 每个硬件最多保留 2 个 unresolved orphan，全局最多 12 个；活动 candidate/handle 不计入 orphan，
   但每硬件仍只有一个候选 lifecycle 在途。
8. 达到任一上限后不得创建更多线程或句柄；`CALL_TIMED_OUT` 继续轮询 completion，
   `CLOSE_RETRYABLE` 继续上述 cleanup backoff。任一记录获得关闭证据并结算后，才允许下一次 candidate
   open。若 native 调用永久不返回或永远不能给出关闭证据，后台 cleanup 仍保持有界运行，但未来启动
   会持续得到精确 `orphan_limit_reached`；Python 进程内不能安全承诺强杀该 C 调用。
9. orphan metadata 保留到取得关闭证据或进程关闭，单硬件最多两条，不做无界历史累积；状态转换
   另以有界/限频诊断写出。
10. shutdown 对非 `CALL_TIMED_OUT` orphan 发出 stop/close 请求，并对所有 orphan 在统一 shutdown
   deadline 内等待；未取得关闭证据的项目进入现有
   non-clean shutdown report。普通单硬件恢复禁止调用全局 PortAudio terminate。

## 10. 运行中故障语义

- 原 generation 只是短暂可疑且连续性仍可证明时，现有 capture owners 继续。
- generation 明确故障、overflow 或产生不可证明的采样缺口时，所有依赖该 generation 且覆盖缺口的
  capture owners 必须各自得到明确失败；不能跨 generation 拼接音频。
- 某一 capture owner 失败不改变其他硬件 generation，也不覆盖其他运行结果。
- 已经冻结音频并进入保存/分析/显示/回传的运行不受之后的 Resident replacement 影响。
- replacement 成功不会自动重放导致恢复的 PLC 上升沿或按钮点击。

Pool lock 只用于 owner/generation 的短状态提交，并定义 freeze-versus-fault 的线性化：

1. capture start 先提交到 generation：owner 钉在旧 generation；之后 fault 会使该 owner 得到一次明确
   失败，不能迁移到新 generation。
2. generation fault 先提交：尚未提交的 reserve/start 不能进入旧 generation，并加入同硬件恢复结果。
3. capture 已经复制完目标帧并提交 immutable audio：该成功终态先赢，之后 fault 不得改写它。
4. fault 与 capture terminal 同时竞争时，现有 exactly-once terminal publisher 决定唯一胜者。

底层 `on_fault` 到达 Pool 后，由 Pool 在锁内完成一次 fault latch 和受影响 owner 终态候选，然后在锁外
立即请求每硬件 recovery controller；不需要等待下一次用户启动。readiness 发现同一 fault 时只加入现有
恢复，不再启动第二次 replacement。

## 11. 错误与诊断模型

### 11.1 兼容类别

对现有 coordinator、UI 和 PLC 失败映射继续使用 `resident_unavailable`，避免改变外部契约。
类别之外增加内部稳定的 `stage`、`reason_code` 和有界 `detail`。

`stage` 只描述生命周期位置，`reason_code` 只描述原因。稳定组合至少如下：

| stage | reason_code | 含义/行为 |
|---|---|---|
| `admission` | `duplicate_run_id` | 保持现有重复拒绝，不恢复 |
| `admission` | `capture_capacity` | 保持现有容量拒绝，不恢复 |
| `admission` | `trigger_future_invalid` | 超出一块时钟容差，不恢复 |
| `map_trigger` | `trigger_expired` | 早于 Ring 逻辑窗口，不恢复 |
| `map_trigger` | `timeline_gap` | 历史 trigger 不连续；只失败该 claim |
| `liveness` | `heartbeat_stale` | 进入观察窗/合并恢复 |
| `liveness` | `ingress_overflow` | fault generation 并恢复 |
| `liveness` | `portaudio_input_overflow` | PortAudio 明确报告输入 overflow，fault 并恢复 |
| `liveness` | `callback_continuity_lost` | callback timeline 新增不可证明区间，fault 并恢复 |
| `liveness` | `backend_fault` | fault generation 并恢复 |
| `retire_old` | `abort_failed` / `abort_timeout` | 记录并继续有界 stop/close；进入 orphan 状态机 |
| `retire_old` | `stop_failed` / `stop_timeout` | 记录并继续有界 close；进入 orphan 状态机 |
| `retire_old` | `close_failed` / `close_timeout` / `close_unconfirmed` | 旧句柄进入对应 orphan 状态 |
| `resolve_endpoint` | `endpoint_scan_failed` | 失败关闭并重试 |
| `resolve_endpoint` | `endpoint_missing` | 失败关闭并重试 |
| `resolve_endpoint` | `endpoint_ambiguous` | 失败关闭并重试 |
| `resolve_endpoint` | `endpoint_identity_mismatch` | 失败关闭并重试，绝不换绑 |
| `validate_candidate` | `format_mismatch` | 实际 sample rate/channel/dtype 不兼容，候选关闭并重试 |
| `validate_candidate` | `capability_mismatch` | 当前 endpoint 能力不足，候选关闭并重试 |
| `open_candidate` | `open_failed` / `open_timeout` | 候选失败并重试 |
| `start_candidate` | `start_failed` / `start_timeout` | 候选失败并重试 |
| `await_first_sample` | `first_sample_timeout` | 候选失败并重试 |
| `publish_candidate` | `candidate_superseded` | 候选关闭，由新 revision/关闭流程接管 |
| `publish_candidate` | `generation_replaced_for_trigger` | 恢复成功，但恢复前 claim 必须失败 |
| `background_retry` | `retry_scheduled` | 含 attempt 和 next delay |
| `background_retry` | `orphan_limit_reached` | 不再创建资源，等待 cleanup |
| `shutdown_cleanup` | `close_unconfirmed` | 进入 non-clean shutdown report |
| `internal` | `invariant_violation` | 保留类型/安全消息；按发生时的 generation/claim fail closed |

“音频输入设备未返回采样”只允许用于 `first_sample` 阶段确实超时的情况，不能用于 close/open/start 等失败。

### 11.2 对外结果映射

| 内部结果/原因 | Port/Coordinator 结果 | 接受边界 |
|---|---|---|
| coordinator ingress 已关闭、context 已取消或 shutdown contender 已存在 | 保留现有 `INGRESS_CLOSED`、preflight cancel 或 `SHUTDOWN_CANCELLED` 所有者 | 不由 Resident 覆盖 |
| 独立 duplicate run id | 现有 `DUPLICATE_RUN_ID` | preaccept |
| capture capacity | `CaptureReserveStatus.CAPACITY` -> `CAPTURE_CAPACITY` | preaccept |
| `GENERATION_REPLACED` | `RESIDENT_UNAVAILABLE`，detail=`generation_replaced_for_trigger` | preaccept |
| `RECOVERY_PENDING`、`RECOVERY_FAILED`、endpoint/candidate/orphan failure | `RESIDENT_UNAVAILABLE`，保留 stage/reason | preaccept |
| `LIFECYCLE_CANCELLED` | continuation 先让 coordinator 的 cancel/shutdown/new revision 所有者结算；若端口已独立 closing，则保留现有 `CLOSING` port status | 不创建新的设备错误 |
| port/pool 独立 closing 且没有更早 cancel owner | 保留当前非-reserved 路径，coordinator 映射 `RESIDENT_UNAVAILABLE` | preaccept |
| trigger expired/timeline gap/stream mapping fault | `CaptureOutcomeStatus.FAILED` / `CAPTURE_FAILED` | durable ACTIVE 后 terminal run |
| trigger 超出当前时钟一块容差 | `CAPTURE_START_FAILED` | durable ACTIVE 后 terminal run |
| runtime generation fault/overflow | 受影响 owner 的 `CaptureOutcomeStatus.FAILED` / `CAPTURE_FAILED` | 已接受运行 terminal |

continuation 恢复时必须先执行 coordinator action reconciliation，再消费 Resident 结果，因此 shutdown、
manual cancel 和已有 terminal contender 能按现有优先级获胜。

### 11.3 异常处理

- OS/PortAudio 边界可以捕获 `Exception`，但必须立即转换成带阶段、异常类型和安全消息的结构化故障。
- 禁止 `except Exception: return False`、`except Exception: pass` 或只记录无阶段字符串。
- 程序员不变量错误与设备运行错误必须区分；前者标记 generation 内部故障并保留诊断，不能冒充硬件无采样。
- 所有恢复状态转换记录 hardware id、旧/新 generation id、尝试序号、耗时和最终阶段。
- 重复后台失败需要限频，避免日志风暴，但每次原因变化必须记录。

## 12. 校验收敛策略

本次不采用“删除所有校验”。校验按所有权分为三类。

### 12.1 必须保留的边界校验

- 配置和数据库读出的受管硬件身份、唯一绑定与精确 endpoint；
- SoundDevice 返回的实际 sample rate、channel count、dtype 和设备能力；
- callback 帧形状、最大帧数和 PortAudio status；
- Ring 容量、绝对游标、单调时间、内存与录音大小上限；
- generation token、capture binding 所有权和跨运行身份；
- 保存文件边界及 shutdown 资源所有权。

### 12.2 合并或删除的重复校验

- 值在公开构造或 OS 边界规范化后，可信内部方法不再重复 exact-type 检查；
- callback ingress 已验证数组形状和帧数后，Ring append 不再重复同一组类型/形状检查；
- 不再手工重复调用已经构造成功对象的 `__post_init__()`；
- 同一状态转换只由 Pool 校验一次，Backend 和外层包装器不再复制 generation/readiness 规则；
- 能由类型、私有构造器或单一所有权保证的不变量，不在每个热路径调用重复验证。

### 12.3 处理原则

- 校验必须放在拥有该边界的最外层一次完成，并输出具体字段和原因。
- callback 中只保留防止内存越界、跨 generation 写入和错误音频格式的常数时间检查。
- 删除校验之前必须有测试证明它已由更早的边界保证；不能仅因代码冗长而删除。
- 不允许用宽泛 fallback、默认值或静默丢弃代替被删除的校验。

## 13. 数据库增长的关系

当前 Today Statistics 会周期性读取和物化全部 durable run，并按未建索引的时间字段排序。随着数据库
增长，这会增加 CPU、SQLite 和 Python 对象分配负载，可能与音频 callback 时间窗重叠，因此是故障概率
的放大器。

它不是本次粘滞 `resident_unavailable` 的直接根因：Resident 探针的 1 秒判定、callback 下游耦合、
伪句柄重建和永久 faulted generation 才能解释错误及其不可恢复性。

本设计通过 callback 隔离使正确性不再依赖数据库规模。数据库聚合查询和索引优化属于可独立验证的性能
工作，不与 Resident 恢复共同提交，以免用降低系统负载掩盖音频状态机缺陷。

## 14. Compatibility / Migration

- `Backward compatibility: required`
- `Protected surfaces: [PLC/manual start semantics, UI operations and workflow, managed hardware/config files, existing database data and schema, WAV naming and format, analysis algorithm and result meaning, history/result presentation, public coordinator categories, capacity settings and defaults]`
- `Allowed breakage: [private ResidentAudioPool and backend implementation types, private readiness/recovery interfaces, internal generation/heartbeat registrations, test-only seams, internal diagnostic detail fields]`
- `Migration strategy: none`

本规格不执行数据库、配置或录音文件迁移，不新增必须由用户设置的开关。应用启动仍按当前配置打开同一受管
端点。内部诊断可以更精确，但用户操作和业务结果不改变。

## 15. 实现边界

预计只修改或新增与下列职责直接相关的模块及测试：

- `app/service/analysis/resident_audio_mvp/`：generation 所有权、callback ingress、Ring/owner drain、
  Pool recovery 和结构化诊断；
- `app/service/multi_run_integration/production_composition.py`：移除外层双重健康/重建权威，把实时显示改为
  有界下游消费者；
- `app/service/multi_run_coordinator_mvp/managed_resident_capture_port.py`：使用 Pool 结构化 readiness；
- `app/service/multi_run_coordinator_mvp/coordinator.py` 与
  `app/service/multi_run_integration/captured_start_dispatcher.py`：只为 Resident reserve 增加 authenticated
  park/resume continuation，保留现有容量、lane、后续状态机和用户入口；
- `app/service/managed_input_resident_adapter.py` 和底层 SoundDevice 适配：只在接口收敛确有需要时调整；
- `app/service/managed_input_sounddevice_backend.py`/现有 managed-input identity 边界：为每次 candidate open
  提供完整重新枚举与精确身份验证；
- 对应的 Resident、production composition、coordinator、PLC/manual ingress 和压力测试。

不借机重写 coordinator、分析调度器、UI、PLC 协议或数据库统计模块。

## 16. 测试策略

### 16.1 确定性单元测试

使用 fake clock、可控制 callback 的 fake handle 和可编程 close/open/start 结果覆盖：

1. 健康心跳和连续 Ring 立即返回，不调用任何 recovery 生命周期方法。
2. callback 暂停后在观察窗内恢复，Ring 连续覆盖原触发时刻，本次启动继续。
3. callback 恢复但 Ring 存在 gap 或 overflow，本次启动不能成功。
4. 原 generation faulted 后创建全新 callback、Ring、handle 和 generation。
5. replacement 在 5 秒内成功，本次触发失败但下一次 reserve 成功。
6. replacement 超时后返回具体阶段，后台成功后下一次 reserve 成功。
7. 旧 close 返回 `NOT_CONFIRMED` 时，后续尝试仍会创建新候选句柄，绝不复用 poisoned handle。
8. 底层 `on_fault` 必须到达 Pool，并终止对应 generation；不得只更新外层 heartbeat。
9. 同一硬件十个并发 readiness 只触发一次 replacement。
10. 一条硬件 replacement 时，另一条硬件的 reserve 和 callback 正常推进。
11. 旧 callback 在新 generation 发布后迟到，其数据被拒绝且不污染新 Ring。
12. 每个故障阶段映射到稳定 reason code；禁止被统一折叠成“未返回采样”。
13. 健康流的 trigger 通过“当前单调时钟 + 一块”检查但位于落后的 Ring tail 之后时创建正常 pending
    mapping；旧于两秒窗口时在 durable ACTIVE 后返回 `trigger_expired`，二者都不误触发 recovery。
14. 两个 trigger 共享同一个 lifecycle future，但在各自 post-accept `start_capture()` 中执行 Ring
    mapping；一个过期不能污染另一个。
15. reserve claim 在成功、失败、取消、端口关闭和 continuation 异常下均 exactly-once 释放容量；独立
    duplicate run id 维持现有拒绝。
16. 每次 candidate open 重新枚举：device index 改变但完整身份唯一匹配时使用新 index；缺失、完整
    身份不符或多个同描述设备时失败关闭。
17. 表 9.5 的每一行均验证本次 claim、前台恢复和后台重试三个结果。
18. native open/start/close 永不返回时，前台在 deadline 内返回，orphan 数量不超过单硬件 2/全局 12，
    stale callback 请求 native `CallbackStop`；另覆盖 timed-out call 后来返回、close 返回
    `NOT_CONFIRMED` 后 cleanup 重试、取得/未取得关闭证据三条路径。

### 16.2 Callback 实时边界测试

- 慢速/阻塞实时显示消费者不能减慢 callback heartbeat 或 Resident Ring 推进。
- 饱和分析工作池、保存 dispatcher、PLC 回传和数据库读取不能在 callback 线程出现调用栈。
- callback 不等待 Pool lock、Condition、Future 或设备生命周期。
- 显示队列饱和只增加显示丢帧诊断，录音样本仍连续。
- 每 generation 的 ingress 预分配容量至少覆盖 2.0 秒，即生产 50 ms block 下至少 40 blocks；
  live-display 队列固定最多 4 blocks，满时丢最旧显示帧并计数。
- 确定性 callback benchmark 使用六条并行 50 ms block、每条 20 Hz、连续 60 秒；callback 墙钟耗时
  p99 不高于 5 ms、单次不高于 10 ms，Resident ingress 不发生 overflow。
- 同一压力测试同时令两个 analysis worker 各持续占用 2 秒、显示消费者每秒阻塞 500 ms、PLC sink
  阻塞 3 秒，并并发执行等价于 100,000 条历史记录物化的 CPU/SQLite 负载；上述 callback 指标和
  ingress 零 overflow 仍成立。
- 另设故意阻塞 Resident drain 超过 2 秒的故障测试：必须产生 `ingress_overflow`、fault generation 并
  启动恢复，不能静默丢录音数据。

### 16.3 多运行回归测试

1. 第一条运行长时间分析时，第二条运行仍按原触发时刻捕获。
2. 第一条运行正在显示或 PLC 回传时，第二条启动和录音不增加串行等待。
3. 同一硬件两条重叠 capture 分别得到正确、互不覆盖的区间。
4. 两个硬件同时录音，其中一个故障并恢复时，另一个录音不失败、不缺帧。
5. captured-start 两 worker、分析默认两 worker和最多六 capture 的现有容量测试保持通过。
6. PLC 与按钮入口对同一 Resident 状态得到一致结果。
7. 在另一条运行执行上述分析/显示/PLC/历史负载时，对健康硬件提交 1,000 次交替启动；从 dispatcher
   worker 开始处理到 Resident reserve 成功的 p99 不高于 100 ms、单次不高于 250 ms，且所有捕获的
   start cursor 仍映射原 trigger。
8. 两个不同硬件的启动均已准入，其中一个 park 5 秒时，另一个在空闲 captured-start worker 上仍满足
   健康 reserve 的 100/250 ms 指标。
9. 同硬件两个乱序 trigger 共享一次恢复 lifecycle，但分别得到独立 expired/generation-replaced 结果。
10. 达到两个 orphan 后，重复 100 次 probe 不新增 worker、句柄或 orphan 记录；释放一个 orphan 后只
    允许一个 candidate 尝试。
11. 令 readiness/Backend fake 在端口锁外阻塞 5 秒；期间端口的 cancel、close 和另一硬件 reserve
    仍能进入并离开各自短临界区，证明外部调用没有持有端口锁。
12. 覆盖 fault 与 owner freeze、candidate publish 与 desired revision 更新、parked continuation 与
    dispatcher close 三组双向竞态，证明 exactly-once terminal、permit 和资源结算。

上述延迟指标只测 Resident reserve 健康路径，不把用户配置读取、PLC 网络注册或持久化耗时混入；它用于
防止本次重构把分析/显示/回传重新串到采集路径，不改变现有外部超时语义。

### 16.4 兼容和集成测试

- 原有配置和现有数据库可直接启动，无迁移步骤。
- 既有 WAV 文件名、声道、采样率、dtype 和结果 payload 不变。
- 既有 `resident_unavailable` 类别仍可被 UI/coordinator 消费，同时日志包含新阶段和原因。
- 程序关闭时取消 recovery、join drain，并对 orphan close 给出有界报告。
- PyQt 可用的目标环境执行 production composition 集成测试；当前开发机若因 Qt DLL 缺失无法导入，
  必须记录环境阻塞并在可运行目标环境补齐，不能据此宣称集成通过。

### 16.5 现场验证

- 在实际受管输入设备上连续运行 PLC 与按钮交替启动，并同时制造分析、实时显示、回传和历史读取负载。
- 主动注入一次软件句柄 fault，验证不重启应用即可使后续启动恢复。
- 保留恢复前后 generation、阶段耗时、callback 序号和 continuity 证据，不能只凭“没有弹窗”判断成功。

## 17. 验收标准

1. 正常健康流的 reserve 不进入恢复、不调用硬件生命周期，并保持现有触发时间边界。
2. 分析、显示、保存、PLC 回传和数据库负载不能造成虚假的硬件无采样诊断。
3. 一次 fault 后无需重启应用，完整新 generation 可建立，且下一次启动成功。
4. close 未确认、open 失败、start 失败和 first-sample 超时均不会留下可复用的 poisoned registration；
   未达到 orphan 上限时继续 candidate recovery，达到上限时继续有界 cleanup 并明确拒绝新 candidate，
   获得关闭证据后自动恢复 candidate retry。
5. 只有原 generation 连续覆盖原触发时刻时，本次启动才可继续；新 generation 恢复不能伪造本次录音。
6. 一条运行分析/显示/回传期间，另一条运行启动和录制保持原有并发行为。
7. 同硬件重叠 capture 和不同硬件并行 capture 均保持隔离。
8. 所有失败都有稳定 stage/reason；`resident_unavailable` 外部兼容类别仍保留。
9. 重复内部校验得到收敛，安全边界校验和现有音频/结果兼容性保持。
10. 相关单元、集成、并发、压力和关闭测试通过；任何环境未运行项明确列出，不能以未执行代替通过。
11. callback 六流压力满足 p99 5 ms、最大 10 ms、正常 workload 零 ingress overflow；健康 Resident
    reserve 在隔离负载下满足 p99 100 ms、最大 250 ms。
12. Resident 异常等待不会占用 captured-start worker；parked continuation、capture capacity 和
    dispatcher permit 均保持原有上限并 exactly-once 结算。
13. 每次重开都重新验证完整设备身份，native lifecycle 卡死时 orphan 资源保持单硬件 2/全局 12 上限。
