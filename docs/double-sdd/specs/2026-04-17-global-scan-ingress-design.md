# Global Scan Ingress Design

## Summary

The current app has factory-mode scan routing in `FactoryModeController`, but it has no UI-layer global scan ingress. As a result, a barcode scanner only behaves like normal keyboard input: scan text reaches `条码 SN` only when focus is already inside that input, and scan-triggered starts never occur unless some other caller emits `factory_scan_event`.

This pass adds a test-page global scan ingress that collects scanner keyboard input terminated by `Enter/Return`, writes accepted scans into `条码 SN`, refreshes the cached workorder snapshot, and emits the existing factory scan event when the current selected configs contain at least one scan-enabled config. If scan-triggered start occurs, that start must use the just-updated workorder snapshot so the run carries the scanned SN. The fix must preserve existing manual SN entry, manual start behavior, and current config semantics, and it must not modify hardware-management code.

## Scope

In scope:

- adding a test-page global scan ingress at the main-window/UI layer
- supporting scan completion only via `Enter/Return`
- replacing the current `条码 SN` value with the accepted scan text
- emitting `factory_scan_event` for accepted scans when the selected configs contain at least one `scan_triggers_test == True` config
- ignoring scans when factory-mode `start_ready` is not `True`, while logging that the scan was ignored
- preserving existing direct text-entry behavior when focus is already inside either workorder text input (`ed_sn` or `ed_model`)
- adding focused regression coverage for accepted scans, ignored scans, and scan-triggered start handoff

Out of scope:

- any time-window or heuristic scan completion detection
- any changes to hardware page or hardware-management module behavior
- any changes to config persistence or `scan_triggers_test` meaning
- changing controller-side config selection priority for scan starts
- changing manual workorder editing or manual start behavior

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces:
  - manual `条码 SN` editing
  - manual start behavior in the test page
  - existing `scan_triggers_test` config semantics
  - controller-side “first enabled scan config in selected order” routing
  - hardware-management page and hardware-management code
- Allowed breakage:
  - none beyond the addition of the new global scan entry path on the test page
- Migration strategy: none

## Approved Decisions

1. Only the test page gets global scan handling.
2. Only `Enter/Return` completes a scan; no timeout-based buffering is added in this pass.
3. A scan is accepted only when `FactoryControllerState.start_ready is True`.
4. If a scan is not accepted, it must not write into `条码 SN`; it should only log that the scan was ignored.
5. If a scan is accepted, it replaces the entire `条码 SN` value rather than appending.
6. If no selected config has `scan_triggers_test == True`, the accepted scan still writes into `条码 SN` but does not auto-start.
7. If at least one selected config has `scan_triggers_test == True`, the accepted scan emits the existing `factory_scan_event`; controller-side selection order remains authoritative.
8. Do not modify hardware-management code.
9. Existing text-entry behavior in `WorkOrderModule.ed_sn` and `WorkOrderModule.ed_model` stays unchanged; the new global ingress handles only the no-focus/no-click factory workflow path.
10. If an accepted scan emits `factory_scan_event`, the resulting scan-triggered start must consume the already refreshed `services.bus.current_workorder_payload`, including the replaced SN text.
11. Once the global scan buffer starts, any non-printable key other than `Enter/Return`, any mouse press, any focus transfer into `ed_sn`/`ed_model`, any test-page exit, or window deactivation cancels and clears the buffer without updating SN or emitting a start.

## Root Cause

Current behavior splits the scan flow across layers with one missing link:

- `WorkOrderModule` updates `条码 SN` only when the input field itself receives keyboard focus and text input
- `FactoryModeController` already listens for `factory_scan_event` and knows how to route scan-triggered starts
- no current UI or main-window code captures scanner keystrokes globally and bridges them into both workorder state and `factory_scan_event`

Because of that missing ingress layer, scanner input works only when focus is manually placed inside `条码 SN`, and scan-triggered start never occurs in the normal no-mouse/no-keyboard factory workflow.

## Design Summary

The new scan ingress should live in `MainWindow`, not in hardware-management code and not only inside `WorkOrderModule`.

Why `MainWindow`:

- scanner input must work without requiring focus on `条码 SN`, while still leaving focused workorder editors on their existing widget-local input path
- `MainWindow` already owns page activation context and the test-page module composition
- `WorkOrderModule` should continue to own workorder state publication, but not global keyboard capture for unrelated focused widgets

Implementation shape:

- install a UI-level event filter from `MainWindow`
- process key events only when:
  - the main window is active
  - the current main tab is the test page
- do not start the global scan buffer when the current focus widget is exactly `self.workorder.ed_sn` or `self.workorder.ed_model`; in that case, existing focused text-entry behavior remains authoritative
- accumulate printable characters into an internal scan buffer
- complete the buffer only on `Enter/Return`
- once the global scan buffer is active, consume its printable key events and the terminating `Enter/Return` so the scan text does not leak into unrelated focused controls
- cancel and clear the buffer, without side effects, on:
  - any non-printable key other than `Enter/Return`
  - any mouse press
  - focus transferring into `self.workorder.ed_sn` or `self.workorder.ed_model`
  - leaving the test page
  - window deactivation

Acceptance gate before applying a scan:

- consult the current `FactoryControllerState`
- accept the scan only if `start_ready is True`
- otherwise:
  - do not mutate `条码 SN`
  - do not emit `factory_scan_event`
  - emit a log line that the scan was ignored because start is not ready

Accepted-scan behavior:

- write the scan text into `WorkOrderModule` so that `ed_sn` is replaced in full
- rely on the existing workorder publication path to update `current_workorder_payload` and `workorder_changed`
- inspect the currently selected configs in the existing selected order
- if any selected config has `scan_triggers_test == True`, emit `FactoryScanIngressPayload(scan_text=<accepted text>)` only after the workorder cache is refreshed
- otherwise, stop after updating `条码 SN`

Scan-triggered start handoff:

- `FactoryModeController.handle_scan_event()` must source the scan start workorder from `services.bus.current_workorder_payload`
- that cached workorder snapshot must therefore already contain the replaced SN text before `factory_scan_event` is emitted
- no new config-priority rule is introduced; controller-side `_resolve_scan_config_id()` remains authoritative

This keeps config-selection priority in `FactoryModeController._resolve_scan_config_id()` unchanged and avoids creating a second scan-routing rule in the UI.

## Error Handling And Boundaries

- Empty-buffer `Enter/Return` is ignored.
- If a scan is accepted while no scan-trigger config is enabled, the SN update still occurs and no start is emitted.
- If the active page is not the test page, incoming key events must not be treated as scan ingress.
- If the main window is inactive, incoming key events must not be treated as scan ingress.
- If focus is inside `self.workorder.ed_sn` or `self.workorder.ed_model`, incoming keys continue to follow the existing widget-local text-entry path and must not be duplicated through the global scan ingress.
- If the global buffer is canceled, the partially captured text is discarded and must not update SN, emit `factory_scan_event`, or log a successful scan.
- The new scan buffer must not require modifications to hardware-management pages or services.

## Testing Strategy

Add focused regressions in `tests/test_main_window.py` for:

- accepted global scan on the test page with focus outside `条码 SN`, asserting:
  - the scan text replaces the SN field
  - workorder payload updates
  - `factory_scan_event` emits when a selected config is scan-enabled
- focused manual text entry inside `条码 SN`, asserting:
  - ordinary widget-local editing still updates the SN field
  - the global scan ingress does not emit duplicate `factory_scan_event` traffic for that focused text-entry path
- focused text entry inside `产品型号`, asserting:
  - the model field keeps its existing widget-local text-entry behavior
  - the global scan ingress does not replace SN or emit `factory_scan_event`
- accepted global scan on the test page when no selected config is scan-enabled, asserting:
  - the scan text replaces the SN field
  - no `factory_scan_event` is emitted
- ignored global scan when `start_ready` is not `True`, asserting:
  - `条码 SN` remains unchanged
  - no `factory_scan_event` is emitted
  - a log entry is emitted
- canceled global scan buffer, asserting:
  - partial scan text is discarded when a non-printable key, mouse press, focus transfer into `ed_sn`/`ed_model`, page exit, or window deactivation cancels capture
  - no SN mutation and no `factory_scan_event` occur
- ignored scan input when the active page is not the test page
- ignored scan input when the main window is inactive, if that condition can be expressed deterministically in tests

Add focused regressions in `tests/test_factory_mode_controller/test_trigger_routing.py` or the existing controller scan-routing test module for:

- scan-triggered start consuming the refreshed `services.bus.current_workorder_payload`, asserting the started workorder carries the replaced SN text and `trigger_source == "scan"`

Add focused regressions in `tests/test_workorder_module.py` only if a tiny public helper is introduced for replacing scanned SN text. If no helper is introduced, keep workorder coverage unchanged and verify the behavior from main-window tests.

Existing controller tests for scan-trigger routing remain authoritative for “first selected scan-enabled config wins” behavior and should not be rewritten to a new selection rule.

## Files Likely Affected

- `app/main_window.py`
- `app/service/factory_mode_controller.py`
- `ui/modules/workorder.py` (only if a minimal helper is needed)
- `tests/test_main_window.py`
- `tests/test_factory_mode_controller/test_trigger_routing.py`
- `tests/test_workorder_module.py` (only if a minimal helper is added)
