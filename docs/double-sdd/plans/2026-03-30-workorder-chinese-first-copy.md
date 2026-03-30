# Workorder Chinese-First Copy Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render Chinese-first labels and runtime summaries in the workorder factory-config panel while keeping all factory selection behavior unchanged.

**Architecture:** Keep the change local to `WorkOrderModule`. Add small internal mapping helpers for displayed phase and trigger text, update the static panel labels, and extend the existing widget tests to cover the new exact copy plus fallback rendering for empty and unknown values.

**Tech Stack:** Python 3.12, PyQt6, pytest

**Execution Root:** `D:\Work\jingchengpython\JingChengMechanical\.worktrees\workorder-chinese-first-copy-20260330`

---

## File Structure

- Modify: `ui/modules/workorder.py`
  - Update the static panel labels.
  - Add render-only mapping helpers for `run_phase` and `trigger_source`.
  - Keep emitted payloads, controller state, and persistence unchanged.
- Modify: `tests/test_workorder_module.py`
  - Update exact text assertions for the new labels.
  - Add focused assertions for known mapped summary values.
  - Add focused assertions for empty trigger fallback and unknown-value passthrough.

### Task 1: Localize Workorder Panel Copy

**Files:**
- Modify: `ui/modules/workorder.py`
- Modify: `tests/test_workorder_module.py`
- Test: `tests/test_workorder_module.py`

- [ ] **Step 1: Write the failing tests for the approved Chinese-first copy**

Update `tests/test_workorder_module.py` so the existing layout assertion expects:

```python
assert module.lbl_factory_configs.text() == "配置选择"
assert module.form_layout.labelForField(module.ed_model).text() == "产品型号"
assert module.form_layout.labelForField(module.ed_sn).text() == "条码 SN"
```

Add focused summary-render tests that fully cover the approved mapping and fallback rules:

```python
@pytest.mark.parametrize(
    ("run_phase", "expected_text"),
    [
        ("not_ready", "当前阶段：未就绪"),
        ("armed", "当前阶段：待启动"),
        ("running", "当前阶段：录音中"),
        ("analyzing", "当前阶段：分析中"),
        ("reviewing", "当前阶段：等待复核"),
        ("reanalyzing", "当前阶段：重新分析中"),
    ],
)
def test_workorder_phase_summary_renders_all_known_chinese_labels(
    qapp, run_phase, expected_text
):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase=run_phase,
        )
    )

    assert module.lbl_phase_summary.text() == expected_text


@pytest.mark.parametrize(
    ("trigger_source", "expected_text"),
    [
        ("manual", "最近触发方式：手动"),
        ("plc", "最近触发方式：PLC"),
        ("scan", "最近触发方式：扫码"),
        (None, "最近触发方式：-"),
        ("", "最近触发方式：-"),
    ],
)
def test_workorder_trigger_summary_renders_all_known_chinese_labels_and_dash_fallback(
    qapp, trigger_source, expected_text
):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            trigger_source=trigger_source,
        )
    )

    assert module.lbl_trigger_summary.text() == expected_text


def test_workorder_summary_preserves_unknown_non_empty_values(qapp):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="future_phase",
            trigger_source="custom_trigger",
        )
    )

    assert module.lbl_phase_summary.text() == "当前阶段：future_phase"
    assert module.lbl_trigger_summary.text() == "最近触发方式：custom_trigger"
```

- [ ] **Step 2: Run the focused test file and verify it fails before implementation**

Run:

```powershell
pytest tests/test_workorder_module.py -q
```

Expected: FAIL because the widget still renders the old English labels and summary strings.

- [ ] **Step 3: Implement the minimal render-only copy update in the workorder module**

In `ui/modules/workorder.py`:

- change the static section label to `配置选择`
- change the model row label to `产品型号`
- add small internal mapping tables or helper methods for phase and trigger display
- render the summary labels as:

```python
self.lbl_phase_summary.setText(f"当前阶段：{display_phase}")
self.lbl_trigger_summary.setText(f"最近触发方式：{display_trigger}")
```

Use these exact mappings:

```python
PHASE_LABELS = {
    "not_ready": "未就绪",
    "armed": "待启动",
    "running": "录音中",
    "analyzing": "分析中",
    "reviewing": "等待复核",
    "reanalyzing": "重新分析中",
}

TRIGGER_LABELS = {
    "manual": "手动",
    "plc": "PLC",
    "scan": "扫码",
}
```

Rules:

- empty or `None` trigger renders as `-`
- unknown non-empty phase/trigger strings render verbatim
- do not change `value()`, `_publish_workorder()`, or any emitted payload field names or values

- [ ] **Step 4: Re-run the focused test file and verify it passes**

Run:

```powershell
pytest tests/test_workorder_module.py -q
```

Expected: PASS, including the full 6-phase mapping, the 3 trigger mappings, `None`/empty-string fallback to `-`, and unknown-value passthrough.

- [ ] **Step 5: Run one higher-level verification command**

Run:

```powershell
pytest tests/test_factory_config_list.py tests/test_workorder_module.py -q
```

Expected: PASS

- [ ] **Step 6: Commit the implementation task**

Run:

```powershell
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "feat: localize workorder panel copy"
```

Expected: commit created in the execution worktree branch.
