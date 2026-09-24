# 重启按钮 PLC 关闭契约修复设计规格

## 背景与根因

主窗口的“重启软件”流程先调用控制模块关闭，再调用 `ServiceContainer.shutdown()`。只有收到精确类型的 `ApplicationShutdownReport` 且 `clean=True`，才会通过 `QProcess.startDetached` 启动新实例并退出当前应用。

应用级关闭编排把每个普通 PLC 服务包装为 `PlcServiceThreadShutdownOwner`，并按以下顺序在线程内调用两个槽：

1. `stop_ingress_polling`：停止新的 PLC 开始信号轮询，但暂时保留 PLC 连接，允许结果序列器完成关闭阶段需要的写入。
2. `disconnect_plc_service`：结果序列器关闭后断开 PLC、退出并等待 PLC 线程。

真实 `PlcService` 目前只提供合并式的 `stop_plc_service`，没有上述两个槽。`QMetaObject.invokeMethod` 因找不到 `stop_ingress_polling` 而失败，应用关闭报告被标记为非干净，重启流程据此显示“软件关闭失败，未启动新实例”。已有关闭顺序测试使用自带这两个方法的假服务，因此没有覆盖真实服务和关闭编排之间的接口不匹配。

## 目标

- 让正在连接、已连接或重连中的普通 PLC 服务能够参与应用的分阶段干净关闭。
- 恢复“重启软件”按钮在 PLC 服务存在时的完整流程：停止入口、完成有序关闭、启动一个新实例、退出旧实例。
- 保持 PLC 结果写入所要求的关闭顺序。
- 保留现有手动“断开 PLC”行为和调用入口。
- 增加针对真实 `PlcService` 关闭接口的回归测试，防止假对象再次掩盖契约漂移。

## 非目标

- 不改变应用级关闭报告的严格判定规则。
- 不在关闭失败时强制启动新实例。
- 不改变 PLC 连接、轮询、自动重连、读写或结果序列器业务语义。
- 不改变按钮文案、页面布局、角色权限或重启命令构造。
- 不重构独立的工艺参数 PLC 生命周期；本修复只处理当前导致关闭报告失败的普通 PLC 服务契约。

## 设计

### `PlcService.stop_ingress_polling`

- 声明为无参数 Qt 槽，确保可由 `BlockingQueuedConnection` 在线程归属正确的上下文中调用。
- 停止并释放开始信号轮询定时器和限值读取定时器。
- 清理与轮询入口相关的瞬态状态，包括上次开始信号值。
- 不断开 `plc_client`，以便 `PlcResultSequencer.shutdown()` 在后续阶段仍可完成必要的 PLC 写入。
- 重复调用安全；定时器已停止时不报错。

### `PlcService.disconnect_plc_service`

- 声明为无参数 Qt 槽，确保可由关闭所有者在线程内调用。
- 调用现有底层断开逻辑，释放 PLC 客户端。
- 将连接状态更新为“未连接”，并保持现有停止日志/总线通知语义。
- 重复调用安全；客户端已断开时不报错。

### `PlcService.stop_plc_service`

- 保留现有公开槽和外部调用兼容性。
- 改为按顺序组合调用 `stop_ingress_polling()` 与 `disconnect_plc_service()`，避免维护两套不同的清理逻辑。
- 手动断开仍在一次调用内停止轮询并断开连接，用户可见行为不变。

### 应用关闭与重启

- `PlcServiceThreadShutdownOwner`、`ServiceContainer.shutdown()` 和 `MainWindow._restart_application()` 的现有顺序及严格成功条件保持不变。
- 阶段一成功停止所有 PLC 新入口后，PLC 结果序列器获得关闭机会；阶段二再断开 PLC 并退出线程。
- 任一真正的关闭失败仍返回非干净报告并阻止新实例启动，避免旧资源未释放时产生双实例。

## 错误处理与边界情况

- PLC 从未连接成功：停止定时器后，断开阶段应作为幂等空操作成功完成。
- PLC 正在重连：阻塞式槽调用会在当前连接尝试返回后执行；执行后不得再次安排重连。
- PLC 已连接：阶段一保持连接，阶段二断开。
- 定时器未创建或已被停止：两个新槽均不得因缺少定时器而失败。
- 重复手动断开或重复调用槽：不得抛出异常或重新启动计时器。
- 其他资源关闭失败：继续沿用当前非干净报告和“不启动新实例”行为。

## 测试策略

- 为真实 `PlcService` 增加接口测试，验证 Qt 元对象能够发现 `stop_ingress_polling()` 和 `disconnect_plc_service()`。
- 验证阶段一停止两个定时器、清理轮询状态，但保留 PLC 客户端且不调用断开。
- 验证阶段二断开客户端并发布未连接状态。
- 验证 `stop_plc_service` 按阶段一、阶段二的顺序组合新接口，保持手动断开兼容性。
- 使用真实 `PlcService` 配合轻量线程/调用边界测试应用关闭包装器，证明不会再因缺少槽而产生非干净报告。
- 运行现有主窗口重启测试、PLC 服务测试、应用关闭顺序测试和服务容器关闭测试。

## 验收标准

- 普通 PLC 服务存在且其他资源可干净关闭时，`ServiceContainer.shutdown()` 返回 `clean=True`。
- 点击“重启软件”后，关闭流程不再因 PLC 缺少槽方法显示“软件关闭失败”。
- 新实例仅在完整干净关闭后启动，旧实例随后退出。
- 阶段一不会提前断开 PLC；阶段二才断开并等待 PLC 线程。
- 手动断开 PLC 的现有入口继续可用，表现与修复前一致。
- 新增回归测试在缺少任一分阶段槽时失败，并在修复后通过。

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: [`PlcService.stop_plc_service`, 手动 PLC 断开行为, PLC 轮询与重连行为, PLC 结果写入关闭顺序, `ApplicationShutdownReport` 严格语义, “重启软件”用户可见行为, 重启命令与参数]
- Allowed breakage: []
- Migration strategy: none

本修复不改变数据库、配置文件、命令行参数或持久化格式，不需要迁移。
