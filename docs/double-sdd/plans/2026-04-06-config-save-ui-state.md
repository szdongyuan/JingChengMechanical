# Config Save UI-State Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the config editor’s selected channel tab and collapsible section state across a successful save/reload, and provide explicit visible success feedback after a config save succeeds.

**Architecture:** Keep the authoritative reload after save, but capture the current editor UI state before rebuilding and restore that state onto the rebuilt editor using stable `(channel_index, section_id)` keys. Add a small public state API to `CollapsibleGroupBox`, then use it from `ConfigModule._on_save()` and cover the success, fallback, zero-channel, and failure-feedback paths in the existing config test suite.

**Tech Stack:** Python 3.12, PyQt6, pytest

---

## File Structure

- Modify: `ui/widgets/collapsible_group_box.py`
  - Add a stable public state API for section id and expanded/collapsed state.
- Modify: `ui/modules/config.py`
  - Capture selected channel and collapsible section state before the successful reload path in `_on_save()`.
  - Restore that UI state after `_populate_detail(loaded)`.
  - Preserve the exact success bus message contract and add explicit success feedback with `QMessageBox.information(...)`.
- Modify: `tests/test_config_module.py`
  - Add focused regressions for:
    - `CollapsibleGroupBox` public state API
    - successful save preserving selected channel and section state
    - successful save fallback to first available channel when the previous channel disappears
    - successful save zero-channel path keeping the no-channel UI state
    - success dialog/status message only on successful save

### Task 1: Preserve Config Save UI State and Success Feedback

**Files:**
- Modify: `ui/widgets/collapsible_group_box.py`
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing public-API and save-path regressions**

Add focused tests in `tests/test_config_module.py`:

```python
def test_collapsible_group_box_exposes_public_section_state_api(qapp):
    box = CollapsibleGroupBox("FFT 频带上下限", section_id="fft")

    assert box.section_id() == "fft"
    assert box.is_expanded() is True

    box.set_expanded(False)

    assert box.is_expanded() is False


def test_on_save_preserves_selected_channel_and_collapsed_sections_and_shows_success(
    qapp, monkeypatch
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service, bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0), _channel_payload(1)]))
    first_container = module._channel_containers[0]
    module._on_channel_tab_clicked(1)

    selected_container = module.channel_stack.currentWidget()
    first_spl_box = next(
        box
        for box in first_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "spl"
    )
    fft_box = next(
        box
        for box in selected_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "fft"
    )
    spl_box = next(
        box
        for box in selected_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "spl"
    )
    first_spl_box.set_expanded(False)
    fft_box.set_expanded(False)
    spl_box.set_expanded(True)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    status_count_at_info = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: (status_count_at_info.append(len(bus.status.emissions)), infos.append(args)),
    )

    module._on_save()

    restored_container = module.channel_stack.currentWidget()
    restored_first_container = next(
        container
        for container in module._channel_containers
        if container.property("channel_index") == 0
    )
    restored_first_boxes = {
        box.section_id(): box
        for box in restored_first_container.findChildren(CollapsibleGroupBox)
    }
    restored_boxes = {
        box.section_id(): box
        for box in restored_container.findChildren(CollapsibleGroupBox)
    }

    assert module._current_config_id == 7
    assert module.config_list.currentItem().data(Qt.ItemDataRole.UserRole) == 7
    assert set(restored_boxes) == {
        "stft",
        "spl",
        "loudness",
        "golden_sample",
        "fft",
        "octave",
    }
    assert restored_container.property("channel_index") == 1
    assert restored_first_boxes["spl"].is_expanded() is False
    assert restored_boxes["fft"].is_expanded() is False
    assert restored_boxes["spl"].is_expanded() is True
    assert bus.status.emissions == [(("配置已保存",), {})]
    assert status_count_at_info == [1]
    assert infos[-1][-2:] == ("保存成功", "配置保存成功！")
```

Add focused save-path regressions for fallback/no-channel behavior:

```python
def test_on_save_falls_back_to_first_available_channel_when_selected_channel_disappears(
    qapp, monkeypatch
):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 1",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Mic Small",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 1,
                "max_output_channels": 0,
            },
            {
                "name": "Speaker 1",
                "hostapi": 1,
                "default_samplerate": 44100,
                "max_input_channels": 0,
                "max_output_channels": 2,
            },
        ]
    )
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(
        config_service=config_service,
        hardware_service=hardware_service,
        bus=bus,
    )
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0), _channel_payload(1)]))
    module._on_channel_tab_clicked(1)

    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda _config_id: {
            **_detail_payload([_channel_payload(0)]),
            "input_device_name": "Mic Small",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
        },
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    module._on_save()

    assert module.channel_stack.currentWidget().property("channel_index") == 0
```

```python
def test_on_save_keeps_no_channel_state_when_reloaded_editor_has_no_channels(
    qapp, monkeypatch
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service, bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda _config_id: {
            **_detail_payload([]),
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "selected_channels": [],
        },
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )

    module._on_save()

    assert module.channel_host.isVisible() is False
    assert module.channel_warning.isVisible() is True
    assert module.channel_stack.count() == 0
    assert infos
```

- [ ] **Step 2: Run the focused regressions to verify the current implementation fails**

Run:

```powershell
pytest tests/test_config_module.py -q -k "collapsible_group_box_exposes_public_section_state_api or on_save_preserves_selected_channel_and_collapsed_sections_and_shows_success or on_save_falls_back_to_first_available_channel_when_selected_channel_disappears or on_save_keeps_no_channel_state_when_reloaded_editor_has_no_channels"
```

Expected: FAIL because `CollapsibleGroupBox` does not yet expose the required public API, successful config saves currently rebuild the editor back to the default tab/expanded state, and config save does not yet show explicit success feedback.

- [ ] **Step 3: Add the success/failure feedback compatibility regressions**

Add a focused save-failure compatibility test in `tests/test_config_module.py`:

```python
def test_on_save_failure_does_not_show_success_feedback(qapp, monkeypatch):
    config_service = _ConfigServiceStub(save_error=ValueError("save exploded"))
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    module = _make_module(config_service=config_service, bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    assert infos == []
    assert warnings
    assert bus.status.emissions == []
```

Extend the existing post-commit failure regressions in `tests/test_config_module.py` so they also assert that no success dialog is shown and no success status message is emitted:

```python
def test_post_commit_reload_failure_does_not_rollback_committed_pending_save(
    qapp,
    monkeypatch,
):
    ...
    infos = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    ...

    assert infos == []
    assert module.services.bus.status.emissions == []
    assert config_service.begin_calls == 1
    assert config_service.commit_calls == 1
    assert config_service.rollback_calls == 0
    assert any("reload exploded" in str(args[-1]) for args in warnings)
```

```python
def test_post_commit_failure_reloads_authoritative_config_list_and_detail(
    qapp,
    monkeypatch,
):
    ...
    infos = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    ...

    assert infos == []
    assert bus.status.emissions == []
    assert any("post-commit exploded" in str(args[-1]) for args in warnings)
    assert config_service.load_config(config_id)["config_name"] == "Renamed"
    assert module.ed_name.text() == "Renamed"
    assert module.config_list.currentItem().text() == "Renamed"
```

- [ ] **Step 4: Run the feedback compatibility regression**

Run:

```powershell
pytest tests/test_config_module.py -q -k "on_save_failure_does_not_show_success_feedback or post_commit_reload_failure_does_not_rollback_committed_pending_save or post_commit_failure_reloads_authoritative_config_list_and_detail"
```

Expected: PASS or FAIL is acceptable before implementation, but record the actual result. The important contract is that all of these must pass after the production change.

- [ ] **Step 5: Implement the minimal public widget API and save-path restore logic**

Update `ui/widgets/collapsible_group_box.py`:

```python
class CollapsibleGroupBox(QWidget):
    def __init__(self, title: str, *, section_id: str, parent=None):
        ...

    def section_id(self) -> str:
        ...

    def is_expanded(self) -> bool:
        ...

    def set_expanded(self, expanded: bool) -> None:
        ...
```

Implementation notes:

- keep the existing default expanded behavior
- keep `_title` intact for existing tests
- require a non-empty `section_id` for the config-editor call sites in this pass
- `set_expanded(...)` must update content visibility and header arrow/text consistently even when setting the same value repeatedly

Update `ui/modules/config.py`:

- pass stable section ids when constructing each `CollapsibleGroupBox`
  - `stft`, `spl`, `loudness`, `golden_sample`, `fft`, `octave`
- add focused helpers to:
  - capture the selected `channel_index`
  - capture per-channel section state from `findChildren(CollapsibleGroupBox)`
  - restore the selected channel to the matching rebuilt container if present
  - otherwise select the first available tab
  - restore matching section states by `(channel_index, section_id)`
- preserve the existing authoritative reload flow in `_on_save()`
- emit `self.services.bus.status.emit("配置已保存")` only after the authoritative reload/UI rebuild succeeds
- show `QMessageBox.information(self, "保存成功", "配置保存成功！")` immediately after that success status emission
- do not show success feedback on any failing path

- [ ] **Step 6: Run the focused save-path regressions**

Run:

```powershell
pytest tests/test_config_module.py -q -k "collapsible_group_box_exposes_public_section_state_api or on_save_preserves_selected_channel_and_collapsed_sections_and_shows_success or on_save_falls_back_to_first_available_channel_when_selected_channel_disappears or on_save_keeps_no_channel_state_when_reloaded_editor_has_no_channels or on_save_failure_does_not_show_success_feedback"
pytest tests/test_config_module.py -q -k "post_commit_reload_failure_does_not_rollback_committed_pending_save or post_commit_failure_reloads_authoritative_config_list_and_detail"
```

Expected: PASS

- [ ] **Step 7: Run the directly affected config-module suite**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: PASS

- [ ] **Step 8: Commit the config save UI-state fix**

```bash
git add ui/widgets/collapsible_group_box.py ui/modules/config.py tests/test_config_module.py
git commit -m "fix(config): preserve save UI state and confirm success"
```
