# Config Page Chinese UI Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace remaining customer-visible English text on the configuration page with Chinese while preserving stored values and behavior.

**Architecture:** This is a narrow presentation-layer change in the PyQt UI. Keep all internal object names, enum/data values, database fields, and package payload fields unchanged; only visible labels and tests that assert labels should change.

**Tech Stack:** Python, PyQt6, pytest.

---

## File Structure

- Modify: `ui/modules/config.py`
  - Configuration page visible labels, button text, file dialog title/filter,
    golden-sample mode display labels, channel display labels, and missing host
    API fallback display text.
- Modify: `ui/popups/golden_sample_import_dialog.py`
  - Golden-sample import dialog visible labels, plot axis labels, ready status,
    and standard OK/Cancel button text.
- Modify: `ui/popups/config_package_dialogs.py`
  - `.dy` file dialog filter, one user-facing toolchain warning sentence, and
    standard OK/Cancel button text.
- Modify: `tests/test_config_module.py`
  - Existing UI assertions for translated labels and mode display text.
- Modify: `tests/test_golden_sample_import_dialog.py`
  - Existing dialog tests or a new focused test for translated visible labels.

Do not modify service-layer package format code unless a test proves a visible
message from the config page still exposes avoidable English and the change does
not alter a protected data contract.

### Task 1: Translate Configuration Page Visible Text

**Files:**
- Modify: `ui/modules/config.py`
- Modify: `ui/popups/golden_sample_import_dialog.py`
- Modify: `ui/popups/config_package_dialogs.py`
- Test: `tests/test_config_module.py`
- Test: `tests/test_golden_sample_import_dialog.py`

- [ ] **Step 1: Update expected UI labels in tests first**

In `tests/test_config_module.py`, update the visible text assertions in
`test_populate_detail_renders_golden_summary_and_controls`:

```python
assert import_button.text() == "导入 WAV..."
assert replace_button.text() == "替换 WAV..."
assert clear_button.text() == "清除"
assert fft_mode.currentText() == "绝对值"
assert fft_mode.currentData() == "absolute"
assert oct_mode.currentText() == "差值"
assert oct_mode.currentData() == "delta"
```

Update tests that set or assert translated mode text:

```python
combo.setCurrentText("差值")
assert fft_mode.currentText() == "绝对值"
```

Update the `.dy` filter assertion:

```python
assert captured["filter"] == "DY 配置包 (*.dy)"
```

Add required assertions for channel labels:

```python
assert module._channel_tab_buttons[0].text() == "输入1（通道 1）"
assert module._channel_checkboxes[0].text() == "输入1"
```

Add or extend a test for unknown host API fallback behavior using a hardware
service stub whose host API list does not contain the device `hostapi` index:

```python
hardware_service = _HardwareServiceStub(
    devices=[
        {
            "name": "Mic Unknown",
            "hostapi": 99,
            "default_samplerate": 48000,
            "max_input_channels": 1,
            "max_output_channels": 0,
        }
    ],
    hostapis=[],
)
module = _make_module(hardware_service=hardware_service)
module._populate_detail(
    {
        **_detail_payload([_channel_payload(0)]),
        "input_device_name": "Mic Unknown",
        "input_device_hostapi": "N/A",
        "output_device_name": None,
        "output_device_hostapi": None,
        "selected_channels": [0],
    }
)

assert "未知" in module.combo_input_dev.currentText()
assert module._collect_data()["input_device_hostapi"] == "N/A"
```

Add a second required test for the saved-unavailable-device path, because
`_match_device_combo()` builds that visible label dynamically from stored data:

```python
hardware_service = _HardwareServiceStub(devices=[], hostapis=[])
module = _make_module(hardware_service=hardware_service)
module._populate_detail(
    {
        **_detail_payload([_channel_payload(0)]),
        "input_device_name": "Missing Mic",
        "input_device_hostapi": "N/A",
        "input_device_samplerate": 48000,
        "output_device_name": None,
        "output_device_hostapi": None,
        "selected_channels": [0],
    }
)

assert "未知" in module.combo_input_dev.currentText()
assert "N/A" not in module.combo_input_dev.currentText()
assert module._collect_data()["input_device_hostapi"] == "N/A"
```

In `tests/test_golden_sample_import_dialog.py`, add a focused translated-label
test or extend an existing valid-dialog test. Define a local helper equivalent
to the `_field_label_text` helper from `tests/test_config_module.py`, because
`tests/test_golden_sample_import_dialog.py` does not currently have that helper:

```python
assert dialog.windowTitle() == "导入黄金样本"
assert _field_label_text(dialog, "file_sample_rate_value") == "采样率"
assert _field_label_text(dialog, "file_channel_count_value") == "通道数"
assert _field_label_text(dialog, "file_total_duration_value") == "总时长"
assert _field_label_text(dialog, "required_golden_duration_value") == "所需时长"
assert _field_label_text(dialog, "source_channel_selector") == "源通道"
assert _field_label_text(dialog, "start_ms_spin") == "起点"
assert _field_label_text(dialog, "end_ms_display") == "终点"
assert dialog.findChild(QLabel, "validation_message").text() == "就绪"
button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
assert button_box.button(QDialogButtonBox.StandardButton.Ok).text() == "确定"
assert button_box.button(QDialogButtonBox.StandardButton.Cancel).text() == "取消"
```

In `tests/test_config_module.py`, extend
`test_dy_package_export_dialog_disables_ai_package_without_toolchain` or add a
focused config-package export dialog test:

```python
buttons = dialog.findChild(QDialogButtonBox)
assert buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "确定"
assert buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "取消"
```

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; pytest tests/test_config_module.py tests/test_golden_sample_import_dialog.py -q
```

Expected: tests fail because production UI still uses the old English labels.

- [ ] **Step 3: Update `ui/modules/config.py` visible strings**

Apply these exact presentation changes while leaving object names and data values
unchanged:

```python
self._on_import_golden_sample file dialog title: "选择黄金样本 WAV"
self._on_import_golden_sample file dialog filter: "WAV 文件 (*.wav)"
channel tab text: f"输入{ch_idx + 1}（通道 {ch_idx + 1}）"
channel checkbox text: f"输入{i + 1}"
SPL section title: "声压级参数"
SPL row label: "声压级"
average SPL row label: "平均声压级"
loudness section title: "响度参数"
golden section title: "黄金样本"
import button: "导入 WAV..."
replace button: "替换 WAV..."
clear button: "清除"
mode display labels: "关闭" / "绝对值" / "差值"
mode row label: "黄金样本模式"
overlay checkbox text: "显示黄金曲线"
overlay row label: "黄金曲线"
missing host API fallback display: "未知"
```

Mode data values must stay exactly:

```python
fft_mode.addItem("关闭", "off")
fft_mode.addItem("绝对值", "absolute")
fft_mode.addItem("差值", "delta")
oct_mode.addItem("关闭", "off")
oct_mode.addItem("绝对值", "absolute")
oct_mode.addItem("差值", "delta")
```

For missing host APIs, keep persisted combo data compatible while changing only
the visible label. For example:

```python
hostapi_value = hostapi_names.get(hostapi_idx, "N/A")
hostapi_display = "未知" if hostapi_value == "N/A" else hostapi_value
label = f"{dev.get('name', '?')} ({hostapi_display})"
dev_info = {
    "hostapi": hostapi_value,
    ...
}
```

Also update `_match_device_combo()` so saved unavailable devices display
`未知` when the stored `hostapi` value is `N/A`, but keep the combo item data
unchanged:

```python
hostapi_display = "未知" if hostapi == "N/A" else hostapi
label = f"{name} ({hostapi_display}) (设备不可用)"
dev_info = {
    "hostapi": hostapi,
    ...
}
```

- [ ] **Step 4: Update `ui/popups/golden_sample_import_dialog.py` visible strings**

Change visible labels only:

```python
self.setWindowTitle("导入黄金样本")
summary_form.addRow("采样率", self._sample_rate_value)
summary_form.addRow("通道数", self._channel_count_value)
summary_form.addRow("总时长", self._total_duration_value)
summary_form.addRow("所需时长", self._required_duration_value)
controls_form.addRow("源通道", self._source_channel_selector)
controls_form.addRow("起点", self._start_ms_spin)
controls_form.addRow("终点", self._end_ms_display)
self._plot_widget.setLabel("bottom", "时间", units="s")
self._plot_widget.setLabel("left", "幅值")
self._validation_message.setText(self._validation_error or "就绪")
```

After creating the `QDialogButtonBox`, set standard button text:

```python
self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
```

- [ ] **Step 5: Update `ui/popups/config_package_dialogs.py` visible strings**

Change:

```python
DY_PACKAGE_FILTER = "DY 配置包 (*.dy)"
```

Change the overwrite warning sentence to avoid exposing the raw field name:

```python
"\n覆盖 AI 工具链可能影响绑定到同一工具链 ID 的其他配置。"
```

After creating the export dialog `QDialogButtonBox`, set standard button text:

```python
buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; pytest tests/test_config_module.py tests/test_golden_sample_import_dialog.py -q
```

Expected: all tests in `tests/test_config_module.py` and
`tests/test_golden_sample_import_dialog.py` pass.

- [ ] **Step 7: Run a targeted string scan**

Run:

```powershell
rg -n "Golden Sample|Golden Sample Import|Import WAV|Replace WAV|Show golden curve|Golden curve|Golden mode|WAV Files|DY Packages|Sample rate|Channels|Total duration|Required duration|Source channel|\\bStart\\b|\\bEnd\\b|\\bReady\\b|\\bTime\\b|Amplitude|平均SPL|SPL 参数|\\bSPL\\b|响度 参数|\\bOff\\b|\\bAbsolute\\b|\\bDelta\\b|\\bClear\\b|\\bN/A\\b|\\bIn[0-9]+\\b|toolchain_id" ui/modules/config.py ui/popups/config_package_dialogs.py ui/popups/golden_sample_import_dialog.py tests/test_config_module.py tests/test_golden_sample_import_dialog.py
```

Expected: no production UI occurrences remain in `ui/modules/config.py` or
`ui/popups/config_package_dialogs.py` or
`ui/popups/golden_sample_import_dialog.py`.

Allowed production-code hits:

- `N/A` may remain only as an internal fallback value assigned to
  `hostapi_value` or stored in combo item data; it must not be the visible combo
  label when shown to the user.
- `toolchain_id` may remain only as internal field/key/identifier text; the
  user-facing overwrite warning must say `工具链 ID`, not raw `toolchain_id`.
- `FFT`, `STFT`, `AI`, `PLC`, `WAV`, and `DY` may remain as approved
  abbreviations where the spec permits them. Old visible `SPL` labels, including
  standalone `SPL`, `SPL 参数`, and `平均SPL`, must not remain.
- `Off`, `Absolute`, `Delta`, `Clear`, `Ready`, `Time`, `Amplitude`, `Sample rate`,
  `Channels`, `Total duration`, `Required duration`, `Source channel`, `Start`,
  and `End` must not remain as production visible strings.

Tests may contain internal data values such as `absolute`, `delta`, `N/A`,
`toolchain_id`, helper/test names, or historical strings only when they are not
asserting production English UI.

- [ ] **Step 8: Report changed files and verification**

Do not commit from the implementation agent unless explicitly instructed by the
orchestrator. Report changed files and verification output instead.
