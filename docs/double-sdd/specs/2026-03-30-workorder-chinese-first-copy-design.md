# Workorder Chinese-First Copy Design

## Summary

The factory workorder panel should use Chinese-first operator-facing copy for the labels shown in the panel body while keeping `SN` unchanged. This pass is intentionally scoped to the workorder / factory-config area shown in the screenshot and does not introduce broader localization infrastructure.

## Problem

The current panel mixes Chinese and English in places that are primarily used by Chinese operators:

- `产品型号 Model`
- `工厂配置 Factory Configs`
- `Current phase: ...`
- `Current/last trigger: ...`

This creates inconsistent operator-facing copy and makes the panel feel unfinished for the primary audience.

## Scope

This change applies only to the workorder / factory-config panel in the workorder module.

This spec intentionally supersedes the earlier exact section-label requirement from `2026-03-29-workorder-layout-and-ai-binding-legacy-save-design.md` for this panel. The section should now render `配置选择` instead of `工厂配置 Factory Configs`.

Included:

- the static form label for product model
- the factory-config section label
- the phase summary label
- the trigger summary label
- Chinese rendering of the existing `run_phase` and `trigger_source` values in those summary labels

Excluded:

- app-wide localization
- translation of unrelated panels
- changes to selection logic, persistence, controller behavior, or signal contracts
- translation of the `Primary` badge in the selector

## Approved Copy

Static labels:

- `产品型号 Model` -> `产品型号`
- `工厂配置 Factory Configs` -> `配置选择`
- `条码 SN` remains unchanged

Dynamic summary labels:

- `Current phase: <value>` -> `当前阶段：<value>`
- `Current/last trigger: <value>` -> `最近触发方式：<value>`

Runtime value mapping:

### Phase Mapping

- `not_ready` -> `未就绪`
- `armed` -> `待启动`
- `running` -> `录音中`
- `analyzing` -> `分析中`
- `reviewing` -> `等待复核`
- `reanalyzing` -> `重新分析中`

### Trigger Mapping

- `manual` -> `手动`
- `plc` -> `PLC`
- `scan` -> `扫码`
- `None` or empty -> `-`

## Design

`WorkOrderModule` remains the single owner of the rendered summary text. The module should add a small internal mapping helper for phase and trigger values and use that helper when refreshing the summary labels.

The change should be render-only:

- do not alter the stored workorder payload
- do not alter controller state values
- do not alter persistence format
- do not add a general translation framework

Unknown future values should still render safely. If a phase or trigger value is a non-empty string that is not in the mapping table, the module must display the original value verbatim rather than crashing, blanking the value, or substituting `-`.

## Testing

Update the workorder module tests to cover:

- exact static label text for `配置选择` and `产品型号`
- Chinese summary labels for known phase and trigger values
- unchanged `条码 SN` label
- empty or `None` trigger rendering as `-`
- unknown non-empty phase or trigger values rendering verbatim

No new integration surface is required beyond the existing workorder widget tests.

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces: workorder payload structure, factory controller state fields, persistence behavior, selection behavior, and signal flow
- Allowed breakage: tests that assert the previous English strings in this panel
- Migration strategy: none
