# PLC Product Model Display and Persistence Restoration Design

## Status

Approved by the user on 2026-08-28. Independent specification review passed.

## Context

PLC field configuration already defines `product_model_offset` as a readable string
field and maps it to the workorder key `model`. The deployed Jingcheng database
configures `product_model_offset` as `80.0` for both PLC 1 and PLC 2.

The current `PlcService._emit_plc_workorder()` reads that field, but drops it while
building the returned workorder and the `plc_workorder_changed` display payload. The
start callback therefore receives no `model`, so downstream persistence stores an
empty product model.

The current `WorkOrderModule` also has no model display. Commit `8987ee4` introduced
per-PLC SN display and, in the same change, removed the earlier product-model field
and ignored `model` in `plc_workorder_changed`. The new behavior must restore the
model without reverting the coworker's per-PLC layout.

## Goals

1. Include a non-empty PLC product model in the workorder returned by
   `PlcService._emit_plc_workorder()` under the existing key `model`.
2. Pass the same workorder, unchanged, through the existing PLC start callback so
   current persistence paths receive and save the model.
3. Show an independent read-only product model for every configured PLC alongside
   that PLC's existing SN display.
4. Preserve the current per-PLC display, start routing, database schema, PLC field
   configuration, and downstream workorder contracts.
5. Use targeted logic only; do not add broad exception handling, fallback values,
   retries, or redundant validation.

## Non-Goals

- Do not restore the old global editable SN/model form.
- Do not merge multiple PLC models into one global value.
- Do not change PLC offsets, string codecs, database schema, or migrations.
- Do not change result-writing fields such as `res_product_model_offset`.
- Do not refactor the factory-mode or run-persistence architecture.
- Do not alter the PLC current-trace restoration in the same branch.

## Selected Approach

Retain the current per-PLC display and extend each PLC card with a model row. Restore
the missing `model` key at the PLC publication boundary and rely on the existing
downstream contracts, which already accept and persist it.

This approach was selected over restoring a single global model field because two
PLCs may report different models. It was selected over a persistence-only change
because the user explicitly requires both display and saving.

## Detailed Design

### 1. PLC workorder publication

`app/service/plc_service.py` continues to read `product_model_offset` through the
existing `read_plc_values()` call.

When `values["product_model_offset"]` is non-empty, `_emit_plc_workorder()` adds:

```python
payload["model"] = values["product_model_offset"]
```

The returned workorder contains the model, and the existing display event adds only
`plc_id` to a copy of that same payload. The source workorder remains free of the
display-only `plc_id` key.

Empty or failed model reads retain the existing optional-field behavior: omit
`model`. No default model is invented and no new exception boundary is added.

### 2. Start and persistence flow

The existing flow remains unchanged:

```text
product_model_offset
  -> PlcService workorder["model"]
  -> _on_start_requested(..., workorder, ...)
  -> ServiceContainer / FactoryModeController
  -> TestSession or analysis-envelope workorder
  -> AudioDataTable.product_model / exported run model
```

`FactoryPlcIngressPayload`, `FactoryModeController`, `TestSession._save_wav()`, and
the run-persistence export reader already support the `model` key. Production
changes in those components are not required unless a failing regression test proves
an actual additional drop point.

### 3. Per-PLC UI state

`ui/modules/workorder.py` keeps independent display state per PLC:

- SN values keyed by PLC id.
- Model values keyed by PLC id.
- Read-only SN editors keyed by PLC id.
- Read-only model editors keyed by PLC id.

The existing SN-only refresh/rebuild helpers may be renamed to workorder-row helpers
so their names match their expanded responsibility. This is a targeted rename only,
not a broader UI refactor.

For each PLC id, the existing two-column outer grid still contains one PLC card. The
card displays two read-only rows:

```text
PLC1 SN码   [SN-001]
PLC1 型号   [MODEL-A]
```

Both fields use the current `未读取` placeholder. Keep the existing SN highlighting
unchanged and do not add model-specific highlighting or other new styles.

### 4. UI event handling

`_on_plc_workorder_changed(payload)`:

1. Reuses the existing PLC-id parsing behavior.
2. Normalizes `sn` and `model` with the same current string/trim rule.
3. Updates both per-PLC value maps for that PLC id.
4. Refreshes the corresponding read-only displays.
5. Does not publish a second workorder or change start routing.

The model maps follow the existing PLC discovery and cache lifecycle used by the SN
maps. Do not introduce a new authoritative-prune path or change the current
last-PLC-removal fallback. An event with no model displays an empty model for that
PLC instead of retaining a stale value.

`WorkOrderModule.value()` remains compatible with the coworker's current design: it
does not turn per-PLC diagnostic fields back into one global editable workorder. PLC
starts receive their model directly from `PlcService`.

## Error Handling and Validation

- Reuse existing `read_plc_values()` behavior for PLC string read failures.
- Do not add `try/except`, retries, `getattr` fallbacks, synthetic model values, or
  duplicate PLC-id validation to the model path.
- Keep the existing optional-field rule: empty strings and `None` are omitted from
  the start workorder.
- Unexpected programming errors remain visible through existing boundaries.

## Testing Strategy

### PLC service regression

Add focused coverage proving that when PLC reads return SN, model, and rotation
speed:

- `_emit_plc_workorder()` returns `{"sn", "model", "rotation_speed"}`.
- `plc_workorder_changed` emits the same fields plus `plc_id`.
- A rising-edge start callback receives the workorder with `model`.
- Empty model input omits the key rather than substituting a value.

The test must fail on the current code because `model` is missing.

### UI regression

Extend the existing per-PLC workorder-module test to prove:

- Every configured PLC has independent SN and model editors.
- An event for PLC 2 changes only PLC 2's SN/model fields.
- A later event for PLC 1 does not overwrite PLC 2's stored text.
- Adding/removing PLC records rebuilds the model editor set alongside the existing
  SN editor set without changing the current last-PLC-removal fallback.
- The old global editable `ed_model` is not restored.

### Persistence regression

Use the restored PLC workorder in the closest existing start/persistence boundary and
assert the resulting workorder or stored record retains `model`. Reuse the existing
`TestTestSessionWavSave.test_save_wav` assertion that
`AudioDataTable.product_model == "M1"`; do not duplicate downstream persistence
logic when the source-boundary regression plus the existing persistence test covers
the chain.

### Verification

- Run the new PLC workorder tests and updated workorder UI tests red before the fix.
- Run them green after the minimal implementation.
- Run the existing factory-mode, service-container, TestSession persistence, and PLC
  current-trace focused regressions that can collect in the environment.
- Run `git diff --check` against the recorded task base.
- Attempt the full suite and report the existing QtCore DLL collection blocker
  separately if it persists.

## Compatibility / Migration

- **Backward compatibility:** required
- **Protected surfaces:** PLC offset configuration, `model` workorder key,
  `plc_workorder_changed` consumers, per-PLC SN display, start routing, persistence
  schema, current-trace behavior
- **Allowed breakage:** none
- **Migration strategy:** none

The change restores a previously supported optional field. Consumers that ignore
`model` remain compatible, and no database or configuration migration is required.

## Acceptance Criteria

1. With PLC 1 reporting `SN-001` and `MODEL-A`, its UI card shows both values.
2. With PLC 2 reporting different values, each PLC card retains its own SN and model.
3. The PLC start callback receives `workorder["model"] == "MODEL-A"` when the field
   is non-empty.
4. The persisted audio/run record receives that same model through existing
   downstream behavior.
5. Empty model reads do not create a fabricated model and do not retain stale UI
   text for that PLC.
6. No database, PLC configuration, current-trace, or unrelated UI production files
   change.
7. No redundant validation or broad exception handling is introduced.
