# 新增 PLC 默认字段偏移设计

## 背景

普通测试 PLC 的字段协议已经支持图片所示的新 PLC 数据块布局，但
`ui/modules/plc_settings.py` 中的 `add_plc()` 仍以
`PlcFieldOffsetTable.create(plc_id=row.id)` 创建全空偏移记录。因此，数据库中
已有 PLC 经人工补齐或迁移后可以使用，而通过界面新增的 PLC 仍显示全部空白，
不符合现场默认布局。

## 目标

- 通过 PLC 设置界面新增 PLC 时，立即为该 PLC 创建图片所示的默认字段偏移。
- 默认值由一处集中定义，避免 UI 新增逻辑重复协议常量。
- 保持两个现场不存在的返回字符串字段为空。
- 不修改、回填或覆盖任何已有 PLC 的偏移记录。

## 非目标

- 不自动迁移已有空白偏移记录。
- 不修改 PLC 连接参数默认值。
- 不改变字段类型、必填规则、读写方向、启动流程、配置绑定规则或结果回传规则。
- 不改变工艺 PLC 的连接或偏移配置。
- 不修改数据库表结构。

## 默认偏移

| 字段键 | 默认偏移 |
| --- | --- |
| `start_signal_offset` | `0.0` |
| `current_value_offset` | `2.0` |
| `rotation_speed_offset` | `6.0` |
| `res_testing_signal_offset` | `8.0` |
| `res_finish_signal_offset` | `8.1` |
| `res_error_code_offset` | `10.0` |
| `res_result_offset` | `12.0` |
| `res_score_offset` | `14.0` |
| `res_spl_offset` | `18.0` |
| `res_loudness_offset` | `22.0` |
| `res_vibrate_data_offset` | `26.0` |
| `res_ai_score_offset` | `30.0` |
| `res_spl_limit_up_offset` | `34.0` |
| `res_spl_limit_down_offset` | `38.0` |
| `res_loudness_limit_up_offset` | `42.0` |
| `res_loudness_limit_down_offset` | `46.0` |
| `res_vibrate_limit_up_offset` | `50.0` |
| `res_vibrate_limit_down_offset` | `54.0` |
| `res_ai_score_limit_down_offset` | `58.0` |
| `sn_code_offset` | `62.0` |
| `product_model_offset` | `80.0` |
| `res_sn_code_offset` | 空 |
| `res_product_model_offset` | 空 |

## 设计

### 集中的默认偏移定义

在普通 PLC 协议模块中新增不可变或只读使用的默认偏移映射。映射键必须来自
`PLC_FIELD_SPECS`，值使用数据库当前保存的字符串格式。

该映射表达“新建普通 PLC 的现场默认布局”，不改变 `PlcFieldSpec.required`：
仍然只有启动信号是必填字段，用户保存后仍可清空其他可选字段。

### 新增 PLC 数据流

`PlcSettingsModule.add_plc()` 保持现有数据库事务边界：

1. 创建 `PlcTable` 记录。
2. 使用新 PLC id 和集中默认偏移映射创建 `PlcFieldOffsetTable` 记录。
3. 任一步失败时回滚整个事务。
4. 创建成功后沿用现有刷新、选择和 `plc_changed` 通知流程。

只修改这一条新增路径。`open_field_offset_dialog()` 中为旧数据库补建缺失偏移
记录的兼容路径仍创建空白记录，避免将新默认值静默注入已有 PLC。

### 错误处理

沿用 `add_plc()` 当前异常处理：数据库写入失败时记录日志并显示“新增 PLC
配置失败”，不保留只有连接记录或只有偏移记录的半成品。

## 测试

新增或扩展 PLC 设置测试，至少覆盖：

1. 调用 `add_plc()` 后，新 PLC 的全部 23 个偏移字段与默认表完全一致。
2. `res_sn_code_offset` 和 `res_product_model_offset` 保持 `NULL`。
3. 新增 PLC 不修改此前已存在的 PLC 偏移值，包括空白值。
4. 新增事务与现有 UI 刷新、选择和变更通知行为继续通过现有测试。
5. 协议默认映射中的每个键都存在于 `PLC_FIELD_SPECS`，防止字段定义与默认表漂移。

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: [已有普通 PLC 偏移记录、普通 PLC 字段可选规则、PLC 与测试配置绑定、数据库表结构、工艺 PLC]
- Allowed breakage: [通过界面新增 PLC 时偏移不再为空，而是采用已确认的新现场默认布局]
- Migration strategy: none

已有 PLC 无论字段已填写还是为空，都不会被本改动回填或覆盖。数据库升级和应用
启动不会触发偏移迁移。
