# PLC Product Model Restoration Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore PLC product-model publication, independent per-PLC model display, and delivery of the model to the existing persistence path without reverting the current multi-PLC UI.

**Architecture:** Add the missing `model` key at `PlcService`'s existing workorder boundary, then extend the current per-PLC SN cards with a second read-only model field. Keep all downstream start and persistence contracts unchanged because they already accept `workorder["model"]`; protect the source-to-callback boundary and existing persistence behavior with focused tests.

**Tech Stack:** Python 3.12, PyQt6, python-snap7, pytest, Peewee/SQLite

---

## Fixed Context

- Worktree: `C:\Users\DY\Documents\ChatGPT\New project\.worktrees\plc-current-trace-restoration`
- Feature branch: `codex/plc-current-trace-restoration`
- The implementation task base must be recorded from `HEAD` after this approved plan is committed.
- Approved spec: `docs/double-sdd/specs/2026-08-28-plc-product-model-restoration-design.md`
- Existing PLC current restoration commit must remain intact.
- The source Jingcheng database already has `product_model_offset = "80.0"` for PLC 1 and PLC 2; no database work is required.
- The full suite has a pre-existing Windows `0xc0000139` / QtCore DLL collection blocker. Focused tests must still pass, and the full suite must still be attempted and reported.

## Scope and File Structure

### Production files

- Modify `app/service/plc_service.py`
  - Restore only the missing `product_model_offset -> workorder["model"]` mapping.
- Modify `ui/modules/workorder.py`
  - Preserve the current per-PLC card/grid architecture.
  - Add independent model value/editor maps and a read-only model row per PLC.
  - Do not restore the old global editable `ed_model` field.

### Test files

- Create `tests/test_plc_service_workorder.py`
  - Cover returned/displayed workorder model, optional empty model behavior, and rising-edge callback delivery.
- Modify `tests/test_workorder_module.py`
  - Extend the existing per-PLC display test to cover independent model display and pruning.

### Files explicitly out of scope

- Database models, migrations, and copied database files
- PLC settings/configuration and `app/service/plc_protocol.py`
- `FactoryPlcIngressPayload`, `FactoryModeController`, and `ServiceContainer` production code
- `TestSession` and run-persistence production code
- STFT/current production code
- Unrelated UI modules

Do not add fallback models, retries, broad catches, duplicate PLC-id parsing, or other defensive checks.

---

### Task 1: Restore the PLC Workorder Model Boundary

**Files:**

- Create: `tests/test_plc_service_workorder.py`
- Modify: `app/service/plc_service.py:456-515`

- [ ] **Step 1: Record the task review base**

Run:

```powershell
$taskBase = git rev-parse HEAD
$taskBase
```

Keep this exact SHA for the Task 1 review range.

- [ ] **Step 2: Write a focused failing test for returned and displayed payloads**

Create a small test-local signal stub and construct a `PlcService` instance without
connecting to hardware. Stub `read_plc_values()` to return all three workorder
values:

```python
def test_emit_plc_workorder_includes_product_model_in_return_and_display():
    service, emitted = _service_with_values(
        {
            "sn_code_offset": "SN-7",
            "product_model_offset": "MODEL-X",
            "rotation_speed_offset": 1500,
        }
    )

    workorder = service._emit_plc_workorder()

    assert workorder == {
        "sn": "SN-7",
        "model": "MODEL-X",
        "rotation_speed": 1500,
    }
    assert emitted == [
        {
            "plc_id": 7,
            "sn": "SN-7",
            "model": "MODEL-X",
            "rotation_speed": 1500,
        }
    ]
```

The helper must provide only the attributes actually used by
`_emit_plc_workorder()` (`_plc_id`, `bus.plc_workorder_changed`, `logger`, and
`read_plc_values`). Do not add production guards to make the helper work.

- [ ] **Step 3: Add the optional empty-model regression**

```python
@pytest.mark.parametrize("model", [None, ""])
def test_emit_plc_workorder_omits_empty_product_model(model):
    service, emitted = _service_with_values(
        {
            "sn_code_offset": "SN-7",
            "product_model_offset": model,
            "rotation_speed_offset": 1500,
        }
    )

    workorder = service._emit_plc_workorder()

    assert "model" not in workorder
    assert "model" not in emitted[0]
```

Use the existing optional-field rule exactly. Do not trim, normalize, or invent a
default in `PlcService`; string decoding and empty handling already exist.

- [ ] **Step 4: Add a rising-edge callback regression**

Use the actual `poll_once()` and `_emit_plc_workorder()` with a false baseline poll
followed by a true rising-edge poll. Stub only the hardware/current/result-writing
boundaries needed to isolate the test.

```python
def test_rising_edge_start_callback_receives_plc_product_model():
    callbacks = []
    service = _polling_service(
        start_values=[False, True],
        workorder_values={
            "sn_code_offset": "SN-7",
            "product_model_offset": "MODEL-X",
            "rotation_speed_offset": 1500,
        },
        on_start=lambda plc_id, workorder, trigger_ns: callbacks.append(
            (plc_id, workorder, trigger_ns)
        ),
    )

    service.poll_once()
    service.poll_once()

    assert len(callbacks) == 1
    assert callbacks[0][0] == 7
    assert callbacks[0][1] == {
        "sn": "SN-7",
        "model": "MODEL-X",
        "rotation_speed": 1500,
    }
    assert type(callbacks[0][2]) is int
```

Reuse the current `poll_once()` sequence. Do not change current polling, baseline,
start-bit reset, or exception behavior for this task.

- [ ] **Step 5: Run the new tests and verify the expected red state**

Run:

```powershell
python -m pytest tests/test_plc_service_workorder.py -q
```

Expected: the non-empty model assertions fail because the current returned and
display payloads omit `model`. Empty-model cases may already pass.

- [ ] **Step 6: Implement the minimal source-boundary fix**

In `PlcService._emit_plc_workorder()`, immediately after the existing SN mapping,
add only:

```python
if values.get("product_model_offset"):
    payload["model"] = values["product_model_offset"]
```

Do not alter the `read_plc_values()` field list, logging, display copy, callback,
downstream services, or error handling.

- [ ] **Step 7: Run Task 1 tests green**

Run:

```powershell
python -m pytest tests/test_plc_service_workorder.py tests/test_plc_service_current.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Check scope and create a temporary checkpoint**

Run:

```powershell
git diff --check
git diff -- app/service/plc_service.py tests/test_plc_service_workorder.py
git add app/service/plc_service.py tests/test_plc_service_workorder.py
git commit -m "tmp/sdd: restore PLC product model workorder"
```

Dispatch parallel `spec-code-reviewer` and `quality-code-reviewer` reviews for
`$taskBase..HEAD`. Both must pass before Task 2.

---

### Task 2: Add Independent Per-PLC Model Display

**Files:**

- Modify: `ui/modules/workorder.py:27-100,143-154,287-363`
- Modify: `tests/test_workorder_module.py:594-632`

- [ ] **Step 1: Record the Task 2 review base**

Run:

```powershell
$taskBase = git rev-parse HEAD
$taskBase
```

- [ ] **Step 2: Extend the existing per-PLC display test and verify red**

Update `test_plc_sn_display_follows_plc_add_delete_and_updates_by_plc_id` or rename
it to reflect both fields. Keep the existing SN assertions and add model assertions:

```python
services.bus.plc_workorder_changed.emit(
    {
        "plc_id": second.id,
        "sn": "SN-PLC-B",
        "model": "MODEL-B",
    }
)

assert module._plc_sn_edits[first.id].text() == ""
assert module._plc_model_edits[first.id].text() == ""
assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
assert module._plc_model_edits[second.id].text() == "MODEL-B"

services.bus.plc_workorder_changed.emit(
    {
        "plc_id": first.id,
        "sn": "SN-PLC-A",
        "model": "MODEL-A",
    }
)

assert module._plc_model_edits[first.id].text() == "MODEL-A"
assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
assert module._plc_model_edits[second.id].text() == "MODEL-B"
```

After adding a third PLC and then deleting it, assert both editor maps follow the
same ids:

```python
assert tuple(module._plc_sn_edits) == (first.id, second.id, third.id)
assert tuple(module._plc_model_edits) == (first.id, second.id, third.id)
# ... delete third and refresh ...
assert tuple(module._plc_sn_edits) == (first.id, second.id)
assert tuple(module._plc_model_edits) == (first.id, second.id)
```

Verify the model editor is mounted in the same visible PLC card and preserves the
specified read-only presentation:

```python
first_card = module.plc_sn_layout.itemAtPosition(0, 0).widget()
first_labels = [label.text() for label in first_card.findChildren(QLabel)]
assert first_labels == [f"PLC{first.id} SN码", f"PLC{first.id} 型号"]
assert first_card.isAncestorOf(module._plc_model_edits[first.id])
assert module._plc_model_edits[first.id].isReadOnly() is True
assert module._plc_model_edits[first.id].placeholderText() == "未读取"
```

Verify that a later snapshot without `model` clears the previous model for that PLC
instead of displaying a stale unit value:

```python
services.bus.plc_workorder_changed.emit(
    {"plc_id": first.id, "sn": "SN-PLC-A-2"}
)
assert module._plc_model_edits[first.id].text() == ""
```

Finally, delete the remaining configured PLC records and emit `plc_changed`. This is
a compatibility assertion for the existing empty-known-id cache fallback, not a new
cleanup requirement:

```python
first.delete_instance()
second.delete_instance()
services.bus.plc_changed.emit()

assert tuple(module._plc_sn_edits) == (first.id, second.id)
assert tuple(module._plc_model_edits) == (first.id, second.id)
assert module._plc_sn_edits[first.id].text() == "SN-PLC-A-2"
assert module._plc_model_edits[first.id].text() == ""
assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
assert module._plc_model_edits[second.id].text() == "MODEL-B"
```

Keep the assertion that the old global `ed_model` does not exist.

Run:

```powershell
python -m pytest tests/test_workorder_module.py -q
```

Expected: fail because `_plc_model_edits` and per-PLC model rendering do not exist.

- [ ] **Step 3: Add per-PLC model state**

In `WorkOrderModule.__init__`, add model maps beside the existing SN maps:

```python
self._plc_model_values: dict[int, str] = {}
self._plc_model_edits: dict[int, QLineEdit] = {}
```

Do not add a global `ed_model` and do not alter `value()`'s current global blank
workorder behavior.

- [ ] **Step 4: Update the existing event handler**

Use the current PLC-id parser once, then update both maps:

```python
sn = str(payload.get("sn") or "").strip()
model = str(payload.get("model") or "").strip()
plc_id = self._payload_plc_id(payload)
if plc_id is None:
    return
self._plc_sn_values[plc_id] = sn
self._plc_model_values[plc_id] = model
```

Refresh once after both assignments. Keep existing SN highlighting unchanged and do
not add highlighting or other new styles to the model editor.

- [ ] **Step 5: Refresh and prune model state with SN state**

In the existing refresh path, set each model editor from
`self._plc_model_values`. In `_plc_ids_for_display()`, prune `_plc_model_values` with
the exact same known-id set used for SN values.

Extend only the existing truthy-known-id branch:

```python
if plc_ids:
    # keep the existing SN pruning unchanged
    self._plc_model_values = {
        plc_id: value
        for plc_id, value in self._plc_model_values.items()
        if plc_id in plc_ids
    }
    return tuple(sorted(plc_ids))
```

Preserve the current empty-known-id cache fallback and existing `plc_changed`
connection exactly. Do not add a control flag, a new handler, a second PLC-id
discovery path, or extra exception handling.

- [ ] **Step 6: Render a read-only model row in each existing PLC card**

When rebuilding cards:

1. Reset both editor maps.
2. Keep the outer `plc_sn_widget`/`plc_sn_layout` grid and its two-column placement.
3. Replace the card's one-line layout with a compact two-row grid.
4. Add read-only SN and model editors with the existing `未读取` placeholder.
5. Store editors in the matching maps.

The visible rows must be equivalent to:

```text
PLC1 SN码   [SN-001]
PLC1 型号   [MODEL-A]
```

Remove `QHBoxLayout` from the import only if it is no longer used after the focused
card change. Do not redesign the surrounding module.

- [ ] **Step 7: Run Task 2 tests green**

Run:

```powershell
python -m pytest tests/test_workorder_module.py tests/test_plc_service_workorder.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Check scope and create a temporary checkpoint**

Run:

```powershell
git diff --check
git diff -- ui/modules/workorder.py tests/test_workorder_module.py
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "tmp/sdd: show PLC product model per PLC"
```

Dispatch parallel `spec-code-reviewer` and `quality-code-reviewer` reviews for
`$taskBase..HEAD`. Both must pass before final verification.

---

### Task 3: Verify Display, Start Delivery, Persistence Support, and Scope

**Files:**

- No planned production changes
- Any verification-driven fix must receive its own temporary checkpoint and a new
  parallel spec/quality review pass before completion.

- [ ] **Step 1: Resolve and validate the complete implementation base**

Use the commit containing this approved plan as the fixed implementation base. Do
not use `HEAD~1` because Task 1 and Task 2 create temporary commits. Resolve it from
the plan file's own last commit so the command remains executable after temporary
implementation commits:

```powershell
$planPath = 'docs/double-sdd/plans/2026-08-28-plc-product-model-restoration.md'
$implementationBase = git log -1 --format=%H -- $planPath
if (-not $implementationBase) {
    throw "Unable to resolve approved plan commit"
}
git merge-base --is-ancestor $implementationBase HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Approved plan commit is not an ancestor of HEAD"
}
$implementationBase
```

- [ ] **Step 2: Run the complete focused regression set**

Run:

```powershell
python -m pytest tests/test_plc_service_workorder.py tests/test_plc_service_current.py tests/test_current_meter_service.py tests/multi_run_integration/test_production_composition.py tests/test_stft_module/test_live_current_trace.py tests/test_stft_module/test_plc_current_live_path.py tests/test_workorder_module.py tests/test_factory_mode_controller tests/test_service_container.py -q
```

Expected: all collected tests pass. This protects the restored model boundary,
current trace, per-PLC UI, factory routing, and service-container workorder forwarding.

- [ ] **Step 3: Verify the existing persistence contract**

Run:

```powershell
python -m pytest tests/test_test_session.py::TestTestSessionWavSave::test_save_wav -q
```

Expected functional assertion: a workorder containing `model="M1"` creates an
`AudioDataTable` row with `product_model == "M1"`.

If collection is blocked by the known Windows `0xc0000139` / QtCore DLL issue,
record it as an environment blocker. Do not modify production code, install packages,
or add test-only fallbacks to hide it.

- [ ] **Step 4: Attempt the full suite**

Run:

```powershell
python -m pytest -q
```

Record exact pass/failure/collection counts. The known Qt DLL blocker must be
reported separately from business assertion failures.

- [ ] **Step 5: Verify the fixed implementation scope**

Run:

```powershell
git diff --check "$implementationBase..HEAD"
git diff --stat "$implementationBase..HEAD"
git diff --name-status "$implementationBase..HEAD"
```

Expected implementation files only:

```text
M app/service/plc_service.py
M ui/modules/workorder.py
A tests/test_plc_service_workorder.py
M tests/test_workorder_module.py
```

No database, configuration, protocol, persistence, STFT, or unrelated UI production
file may appear.

- [ ] **Step 6: Run the full implementation quality review**

Dispatch a fresh `quality-code-reviewer` for
`$implementationBase..HEAD`, including the spec and plan paths. Resolve only
verified, current-scope findings. Any code change requires the paired spec/quality
review again.

- [ ] **Step 7: Record final evidence**

Record:

- Task 1 and Task 2 red/green results.
- Focused regression pass count.
- Persistence-test result or exact Qt collection blocker.
- Full-suite result or exact Qt collection blocker.
- Both task-level dual-review outcomes and the final full-quality review outcome.
- `git diff --check` result.
- Final changed-file list, branch, clean-worktree state, and HEAD.

Do not claim the full suite passed unless the command actually exits successfully.
