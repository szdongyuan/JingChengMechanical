# AI 评分与原始标签导出修复

用户已批准修复已定位的数据来源错误。Excel 的 AI 评分和 AI 标签应来自实际生产分析的持久化结果，与历史分析显示所选的最近成功分析一致。零分必须输出为数字 0；缺失字段留空；保留原始标签（包括有意义的原文与空白）。

## 事实与方案

生产分析由 `production_executor.py` 写入 `analysis_attempt.result_payload_json`（可能引用 `run_results` 压缩文件），旧 `AiService` 导出查询却只访问 `jingchen.db.ai_execution_table`。现场旧表为空，而新结果及 `ai_runs/<attempt_id>/response.json` 已有评分/标签。

修复采用现有 attempt 仓储和结果附件读取能力。保留结果顶层固定字段，将新的小型 AI 快照写入 `result.review.ai_result`，包含 `status`、`score`、`ai_label`。Decision 就保存快照，Enrichment 保持不变。原始标签不得被判定用的 strip/upper 覆盖。

导出通过一个生产结果读取适配器实现 `get_current_effective_export_values`，替换容器内旧 AI 读取器接线。选择最近成功 attempt，沿用历史分析的选择语义；手动改故障类型不会改变 AI 值。不回写旧 AI 执行表，不依赖当前模型配置，不重新运行 AI。

旧记录没有新快照时：评分先取已保存的 `result.ai_score`；标签读取同一 attempt 的 `ai_runs/<attempt_id>/response.json` 的 `ai_result.ai_label`。使用所配置结果目录的同级 ai_runs，不扫描任意路径。不得从 OK/NG 推造原始标签；不得回退其他 attempt。新快照存在时，其缺失值就是缺失，不从旧诊断覆盖。没有成功记录、损坏/缺失结果附件、旧响应缺失或坏 JSON 时，无法恢复的 AI 字段留空，正常导出仍可进行。数据库查询失败仍应作为真实错误上报。纯 Excel 和 ZIP manifest 使用同一映射。

## Compatibility / Migration

- Backward compatibility: partial。
- Protected surfaces: 现有 Excel 十列、ZIP 布局、三张 run 数据库表、现有结果顶层字段、其他主目录改动。
- Allowed breakage: 导出不再依赖旧 ai_execution_table 提供生产结果。
- Migration strategy: 无数据库迁移；新记录追加嵌套字段，旧记录只读兼容恢复。旧响应缺失时不能保证恢复标签。

## 验证

回归重现旧 AI 表为空、真实 attempt 有评分标签时 Excel 为空的问题。覆盖新生产快照（原始 PASS/NG/自定义标签）、零分、无 AI/缺值、旧响应恢复/缺失、最近成功重分析、手动标签独立、结果附件格式。实际 Excel H/I 和 ZIP manifest 必须一致。用现场数据库只读验证并在临时位置输出，不更改真实记录。
