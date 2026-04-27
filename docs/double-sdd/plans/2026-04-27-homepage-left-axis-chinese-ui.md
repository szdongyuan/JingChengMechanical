# Homepage Left Axis And Chinese UI Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide only the test-homepage chart left y-axes, move y quantity/unit text into concise Chinese card titles, widen the left sidebar for video preview, and normalize bounded customer-facing English UI text to Chinese while preserving result/status contracts.

**Architecture:** Add an explicit opt-in left-axis hiding capability to the shared chart widgets, then enable it only from homepage analysis modules. Keep title/unit and channel/result label translation in UI-layer components so analysis data, payloads, database fields, config formats, and judgment logic remain unchanged.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, pytest

---

## Source Spec

- `docs/double-sdd/specs/2026-04-27-axisless-charts-chinese-ui-design.md`

## File Structure

- Modify: `ui/widgets/analysis_charts.py`
  - add an explicit opt-in constructor flag or helper for hiding only the left y-axis while preserving bottom x-axis visibility and viewbox range behavior
- Modify: `ui/modules/stft.py`
  - create the homepage STFT chart with left-axis hiding enabled
- Modify: `ui/modules/spectrum.py`
  - create the homepage FFT chart with left-axis hiding enabled
- Modify: `ui/modules/spl.py`
  - create the homepage SPL chart with left-axis hiding enabled and stop relying on visible left-axis label text
- Modify: `ui/modules/loudness.py`
  - create the homepage loudness chart with left-axis hiding enabled
  - emit or expose the current title text when the rendered loudness unit changes
- Modify: `ui/modules/octave.py`
  - create the homepage octave chart with left-axis hiding enabled
- Modify: `ui/widgets/cards.py`
  - expose a small card title update API if dynamic loudness titles need it
- Modify: `app/main_window.py`
  - widen the test-page left sidebar
  - replace homepage chart card titles with approved Chinese titles
  - connect dynamic loudness title updates
  - change homepage toolbar context channel text to `通道N`
- Modify: `ui/widgets/analysis_channel_toolbar.py`
  - display channel options as `通道N` while keeping item data as zero-based channel indexes
- Modify: `ui/modules/result.py`
  - translate result-table display labels and channel labels at the UI boundary only
- Modify: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
  - cover default axis compatibility and opt-in left-axis hiding
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - cover homepage chart module instances using hidden left axes while preserving render behavior
- Modify: `tests/test_main_window.py`
  - cover homepage titles, left sidebar width, toolbar/context Chinese channel text, loudness title updates, and preserved status labels
- Modify: `tests/test_result_module.py`
  - cover result table display translation and preserved `PASS`/`FAIL`/`OK`/`NG`

## Task 1: Add Opt-In Left-Axis Hiding To Chart Widgets

**Files:**
- Modify: `ui/widgets/analysis_charts.py`
- Test: `tests/test_analysis_chart_modules/test_zero_based_axes.py`

- [ ] **Step 1: Write failing chart-widget tests**

Add helper assertions near the existing range helpers:

```python
def _assert_left_axis_hidden_bottom_visible(widget) -> None:
    assert widget.plot_item.getAxis("left").isVisible() is False
    assert widget.plot_item.getAxis("bottom").isVisible() is True


def _assert_left_axis_visible(widget) -> None:
    assert widget.plot_item.getAxis("left").isVisible() is True
```

Add a default compatibility test:

```python
@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_chart_widgets_keep_left_axis_visible_by_default(qapp, widget_cls):
    widget = widget_cls()
    try:
        _show_widget(widget, qapp)
        _assert_left_axis_visible(widget)
        assert widget.plot_item.getAxis("bottom").isVisible() is True
    finally:
        widget.close()
```

Add an opt-in hiding/range preservation test:

```python
@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_chart_widgets_can_hide_only_left_axis_when_opted_in(qapp, widget_cls):
    widget = widget_cls(hide_left_axis=True)
    try:
        _show_widget(widget, qapp)
        _assert_left_axis_hidden_bottom_visible(widget)
    finally:
        widget.close()
```

Extend an existing range test or add a focused one so the hidden-axis flag does
not change viewbox behavior:

```python
def test_left_axis_hiding_preserves_zero_based_ranges(qapp):
    line = LineChartWidget(hide_left_axis=True)
    fft = FftLineChartWidget(hide_left_axis=True)
    octave = OctaveBarChartWidget(hide_left_axis=True)
    stft = StftImageChartWidget(hide_left_axis=True)

    try:
        for widget in (line, fft, octave, stft):
            _show_widget(widget, qapp)

        line.set_series([1.0, 2.0], [70.0, 71.0])
        fft.set_spectrum(frequencies_hz=[125.0, 250.0], levels_db=[42.0, 45.0], limit_bands=[])
        octave.set_bands(center_freqs_hz=[125.0, 250.0], levels_db=[32.0, 34.0])
        stft.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
        )
        qapp.processEvents()

        _assert_x_range(line, (0.0, 2.0))
        _assert_x_range(fft, (0.0, 250.0))
        _assert_x_range(octave, (-0.5, 1.5))
        _assert_xy_range(stft, (0.0, 1.0), (0.0, 200.0))
        for widget in (line, fft, octave, stft):
            _assert_left_axis_hidden_bottom_visible(widget)
    finally:
        line.close()
        fft.close()
        octave.close()
        stft.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py -q -k "left_axis"
```

Expected: FAIL because chart constructors do not accept `hide_left_axis`.

- [ ] **Step 3: Implement minimal opt-in chart support**

Update `_BasePlotChart` and subclasses in `ui/widgets/analysis_charts.py`.
Prefer a constructor flag because it is explicit and keeps defaults compatible:

```python
class _BasePlotChart(QWidget):
    def __init__(self, parent: QWidget | None = None, *, hide_left_axis: bool = False):
        super().__init__(parent)
        self._hide_left_axis = bool(hide_left_axis)
        ...
        if self._hide_left_axis:
            self.apply_left_axis_hidden_style()

    def apply_left_axis_hidden_style(self) -> None:
        self.plot_item.hideAxis("left")
```

Thread the keyword through public chart constructors:

```python
class LineChartWidget(_BasePlotChart):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent, hide_left_axis=hide_left_axis)
        ...
```

Do the same for:

- `StftImageChartWidget`
- `FftLineChartWidget`
- `OctaveBarChartWidget`

Implementation requirements:

- Do not hide the bottom axis.
- Do not change `_apply_x_axis_policy`, `_apply_stft_y_axis_policy`, `_apply_auto_y_axis_policy`, or `_apply_bar_x_axis_policy` range behavior.
- Keep default constructor behavior unchanged.
- If `plot_item.setLabel("left", ...)` is called after hiding, the left axis must remain hidden. If needed, call `apply_left_axis_hidden_style()` after label setup in affected constructors only.

- [ ] **Step 4: Run focused chart tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py -q -k "left_axis or zero_based"
```

Expected: PASS

- [ ] **Step 5: Commit Task 1**

```powershell
git add ui/widgets/analysis_charts.py tests/test_analysis_chart_modules/test_zero_based_axes.py
git commit -m "feat(ui): add opt-in chart left axis hiding"
```

## Task 2: Apply Homepage Axis Hiding, Titles, Dynamic Loudness Title, And Sidebar Width

**Files:**
- Modify: `ui/modules/stft.py`
- Modify: `ui/modules/spectrum.py`
- Modify: `ui/modules/spl.py`
- Modify: `ui/modules/loudness.py`
- Modify: `ui/modules/octave.py`
- Modify: `ui/widgets/cards.py`
- Modify: `app/main_window.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing module and homepage tests**

In `tests/test_analysis_chart_modules/test_committed_chart_modules.py`, add:

```python
@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
def test_committed_chart_modules_hide_left_axis_but_keep_bottom_axis(qapp, module_cls):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    if module_cls is LoudnessModule:
        review_payload = _with_loudness_payload(review_payload)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.plot.plot_item.getAxis("left").isVisible() is False
    assert module.plot.plot_item.getAxis("bottom").isVisible() is True
```

Add focused STFT coverage in the same file, or in an existing STFT module test
file if that is a better local fit:

```python
from ui.modules.stft import StftModule


def test_stft_module_hides_left_axis_but_keeps_bottom_axis(qapp):
    services = _make_services(initial_state=_state(101, "running"))
    module = StftModule(services)

    try:
        module.show()
        qapp.processEvents()

        assert module.canvas.heatmap.plot_item.getAxis("left").isVisible() is False
        assert module.canvas.heatmap.plot_item.getAxis("bottom").isVisible() is True
    finally:
        module.close()
```

Update the existing SPL/Loudness axis label assertions so they assert visibility
instead of visible left-axis label text. For example:

```python
assert module.plot.plot_item.getAxis("left").isVisible() is False
assert module.plot.plot_item.getAxis("bottom").isVisible() is True
```

In `tests/test_main_window.py`, add or update focused tests:

```python
def test_test_page_uses_chinese_chart_titles_with_units(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert _card_title(_test_page_grid_card(window, 1, 2)) == "FFT频谱（声压级dB）"
    assert _card_title(_test_page_grid_card(window, 1, 1)) == "STFT时频图（频率Hz，幅值dB）"
    assert _card_title(_test_page_grid_card(window, 2, 0)) == "响度"
    assert _card_title(_test_page_grid_card(window, 2, 1)) == "声压级（dB）"
    assert _card_title(_test_page_grid_card(window, 2, 2)) == "倍频程（声压级dB）"
```

Add the sidebar width regression:

```python
def test_test_page_left_sidebar_is_wider_for_video_preview(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    page = _test_page(window)
    left_column = page.layout().itemAt(0).widget()
    assert left_column.width() >= 480
    assert left_column.minimumWidth() >= 480 or left_column.maximumWidth() >= 480
```

Add dynamic loudness title coverage:

```python
def test_loudness_card_title_updates_to_rendered_unit(qapp, monkeypatch):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0,)),
    )
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(_review_payload_with_chart_payload(101, channels=(0,)))
    qapp.processEvents()

    assert _card_title(_test_page_grid_card(window, 2, 0)) == "响度（sone）"

    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["loudness"]["unit"] = "phon"
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert _card_title(_test_page_grid_card(window, 2, 0)) == "响度（phon）"
```

Add startup coverage for an initial review payload without re-emitting review
signals. This catches a missed initial sync when `LoudnessModule` emits during
construction before `MainWindow` connects the title signal:

```python
def test_loudness_card_title_initializes_from_existing_review_payload(qapp, monkeypatch):
    window, _services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0,)),
    )
    _show(window, qapp)

    assert _card_title(_test_page_grid_card(window, 2, 0)) == "响度（sone）"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "hide_left_axis or spl_module_renders_committed_chart_for_selected_channel or loudness_module_renders_committed_chart_for_selected_channel"
pytest tests/test_main_window.py -q -k "chinese_chart_titles or left_sidebar_is_wider or loudness_card_title"
```

Expected: FAIL because homepage modules, including STFT, do not opt into
left-axis hiding, titles are still mixed English, the left sidebar is still
fixed at `420`, and `Card` does not expose dynamic title updates.

- [ ] **Step 3: Implement homepage module opt-in**

Update module chart construction:

```python
# ui/modules/spectrum.py
self.plot = FftLineChartWidget(hide_left_axis=True)

# ui/modules/spl.py
self.plot = LineChartWidget(hide_left_axis=True)

# ui/modules/loudness.py
self.plot = LineChartWidget(hide_left_axis=True)

# ui/modules/octave.py
self.plot = OctaveBarChartWidget(hide_left_axis=True)
```

Update `ui/modules/stft.py`:

```python
self.heatmap = StftImageChartWidget(self, hide_left_axis=True)
```

Remove or stop reapplying visible left-axis label assertions in `SplModule` and
`LoudnessModule`. Keeping hidden internal label text is acceptable, but do not
depend on it for visible UI units.

- [ ] **Step 4: Add card title update API**

Update `ui/widgets/cards.py`:

```python
class Card(QFrame):
    def __init__(self, title: str, body: QWidget):
        ...
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        lay.addWidget(self.title_label)
        lay.addWidget(body, 1)

    def set_title(self, title: str) -> None:
        self.title_label.setText(str(title))

    def title(self) -> str:
        return self.title_label.text()
```

Do not change card layout margins or styles.

- [ ] **Step 5: Implement dynamic loudness title emission**

Update `ui/modules/loudness.py`:

```python
from PyQt6.QtCore import pyqtSignal


class LoudnessModule(QWidget):
    module_id = "loudness"
    title_changed = pyqtSignal(str)
    ...
    def __init__(self, services):
        ...
        self._current_title = "响度"

    @staticmethod
    def _title_for_unit(unit: str | None) -> str:
        normalized = str(unit or "").strip().lower()
        if normalized in {"sone", "phon"}:
            return f"响度（{normalized}）"
        return "响度"

    def current_title(self) -> str:
        return self._current_title

    def _emit_title_for_unit(self, unit: str | None) -> None:
        self._current_title = self._title_for_unit(unit)
        self.title_changed.emit(self._current_title)
```

Emit after a valid render:

```python
self._emit_title_for_unit(unit)
```

Emit fallback in `_clear_plot()`:

```python
self._emit_title_for_unit(None)
```

Keep `current_plot_payload()` and payload storage unchanged.

- [ ] **Step 6: Update homepage layout and titles**

Update `app/main_window.py`:

```python
left.setFixedWidth(480)
```

Replace chart card construction:

```python
self.spectrum = SpectrumModule(self.services)
grid.addWidget(Card("FFT频谱（声压级dB）", self.spectrum), 1, 2)

self.stft = StftModule(self.services)
grid.addWidget(Card("STFT时频图（频率Hz，幅值dB）", self.stft), 1, 1)

self.loudness = LoudnessModule(self.services)
self.loudness_card = Card("响度", self.loudness)
self.loudness.title_changed.connect(self.loudness_card.set_title)
self.loudness_card.set_title(self.loudness.current_title())
grid.addWidget(self.loudness_card, 2, 0)

self.spl = SplModule(self.services)
grid.addWidget(Card("声压级（dB）", self.spl), 2, 1)

self.octave = OctaveModule(self.services)
grid.addWidget(Card("倍频程（声压级dB）", self.octave), 2, 2)
```

Implementation notes:

- Keep grid row/column placement unchanged.
- Do not rename `self.spectrum`, `self.stft`, `self.loudness`, `self.spl`, or `self.octave`.
- Keep `PASS`, `FAIL`, `OK`, and `NG` untouched.
- If `LoudnessModule` already rendered an initial review payload during
  construction, `current_title()` must return that rendered-unit title when
  `MainWindow` connects the card.
- Later review payloads that change loudness units must still emit
  `title_changed` and update the same card.
- Clearing loudness rendering or receiving an invalid/missing unit must restore
  the card title to `响度`.

- [ ] **Step 7: Run focused homepage/module tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "hide_left_axis or spl_module_renders_committed_chart_for_selected_channel or loudness_module_renders_committed_chart_for_selected_channel"
pytest tests/test_main_window.py -q -k "chinese_chart_titles or left_sidebar_is_wider or loudness_card_title"
```

Expected: PASS

- [ ] **Step 8: Commit Task 2**

```powershell
git add ui/modules/stft.py ui/modules/spectrum.py ui/modules/spl.py ui/modules/loudness.py ui/modules/octave.py ui/widgets/cards.py app/main_window.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py
git commit -m "feat(ui): apply homepage left axis and title updates"
```

## Task 3: Normalize Result And Channel UI Text To Chinese

**Files:**
- Modify: `ui/widgets/analysis_channel_toolbar.py`
- Modify: `app/main_window.py`
- Modify: `ui/modules/result.py`
- Test: `tests/test_main_window.py`
- Test: `tests/test_result_module.py`

- [ ] **Step 1: Write failing result and toolbar tests**

In `tests/test_result_module.py`, update placeholder/default expectations:

```python
assert _table_value(module, 0, 0) == "声压级"
assert _table_value(module, 1, 0) == "平均声压级"
assert _table_value(module, 2, 0) == "响度"
```

Update completed/no-result summary expectations:

```python
assert _table_value(module, 0, 0) == "声压级"
assert _table_value(module, 0, 2) == "通道2"
assert _table_value(module, 0, 3) == "NG"
assert _table_value(module, 1, 0) == "平均声压级"
assert _table_value(module, 2, 0) == "响度"
assert _table_value(module, 3, 0) == "平均响度"
assert _table_value(module, 4, 0) == "FFT频谱"
assert _table_value(module, 6, 0) == "AI分析"
assert _table_value(module, 6, 3) == "OK"
```

Add a focused preservation test:

```python
def test_result_module_preserves_approved_status_labels(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=_summary_payload(overall_status="fail"))
    )

    assert module.lb_overall.text() == "FAIL"
    assert _table_value(module, 0, 3) == "OK"
```

Add explicit English `Octave` payload coverage:

```python
def test_result_module_translates_english_octave_row_label(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    payload = _summary_payload(overall_status="pass")
    payload["rows"][5]["label"] = "Octave"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=payload)
    )

    assert _table_value(module, 5, 0) == "倍频程"
```

In `tests/test_main_window.py`, update partial no-result expectations:

```python
assert _result_table_value(window, 0, 0) == "声压级"
assert _result_table_value(window, 0, 2) == "通道2"
assert _result_table_value(window, 0, 3) == "OK"
assert _result_table_value(window, 1, 0) == "响度"
assert _result_table_value(window, 2, 0) == "FFT频谱"
assert _result_table_value(window, 2, 2) == "通道1"
assert _result_table_value(window, 2, 3) == "NG"
```

Add toolbar display coverage:

```python
def test_main_window_analysis_toolbar_uses_chinese_channel_text(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_running_state(101))
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    combo = window.analysis_toolbar.channel_combo
    assert combo.itemText(0) == "通道1"
    assert combo.itemText(1) == "通道2"
    assert window.analysis_toolbar.context_label.text() == "配置 101 / 通道1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_result_module.py -q -k "result_module"
pytest tests/test_main_window.py -q -k "partial_no_result or analysis_toolbar_uses_chinese_channel_text"
```

Expected: FAIL because result rows and channel labels still show `SPL`,
`Loudness`, `FFT`, `Octave`, and `CHN`.

- [ ] **Step 3: Implement channel toolbar display mapping**

Update `ui/widgets/analysis_channel_toolbar.py`:

```python
def _channel_label(channel_index: int) -> str:
    return f"通道{channel_index + 1}"
```

Keep combo item data unchanged:

```python
self.channel_combo.addItem(_channel_label(channel_index), channel_index)
```

Update `app/main_window.py`:

```python
@staticmethod
def _build_analysis_toolbar_context(config_id: int, channel_index: int) -> str:
    return f"配置 {config_id} / 通道{channel_index + 1}"
```

- [ ] **Step 4: Implement result display normalization**

Update `ui/modules/result.py` with UI-only helpers:

```python
import re


_DISPLAY_LABEL_MAP = {
    "SPL": "声压级",
    "平均SPL": "平均声压级",
    "Loudness": "响度",
    "平均Loudness": "平均响度",
    "FFT": "FFT频谱",
    "Octave": "倍频程",
}


def _display_label(value, default: str | None) -> str | None:
    text = ResultModule._text_or_default(value, default)
    if text is None:
        return None
    return _DISPLAY_LABEL_MAP.get(text, text)


def _display_channel(value, default: str | None) -> str | None:
    text = ResultModule._text_or_default(value, default)
    if text is None:
        return None
    match = re.fullmatch(r"CH(\d+)", text.strip(), flags=re.IGNORECASE)
    if match:
        return f"通道{match.group(1)}"
    return text
```

If module-level helpers referencing `ResultModule._text_or_default` feel awkward,
make them `@staticmethod`s on `ResultModule` instead. Keep behavior equivalent.

Update `_ROW_LABELS`:

```python
_ROW_LABELS = (
    "声压级",
    "平均声压级",
    "响度",
    "平均响度",
    "FFT频谱",
    "倍频程",
    "AI分析",
)
```

Update `_render_summary()`:

```python
self._set_row(
    row_index,
    self._display_label(row_payload.get("label"), default_label),
    self._text_or_default(row_payload.get("deviation_display"), "-"),
    self._display_channel(row_payload.get("channel_label"), "-"),
    self._text_or_default(row_payload.get("status"), "-"),
)
```

Implementation requirements:

- Do not mutate `summary_payload`.
- Do not translate `PASS`, `FAIL`, `OK`, or `NG`.
- Preserve `--`, `-`, `无结果`, and already-Chinese labels.
- Do not change table column headers.

- [ ] **Step 5: Run focused UI text tests**

Run:

```powershell
pytest tests/test_result_module.py -q
pytest tests/test_main_window.py -q -k "partial_no_result or analysis_toolbar_uses_chinese_channel_text or toolbar"
```

Expected: PASS

- [ ] **Step 6: Commit Task 3**

```powershell
git add ui/widgets/analysis_channel_toolbar.py app/main_window.py ui/modules/result.py tests/test_main_window.py tests/test_result_module.py
git commit -m "feat(ui): localize homepage result text"
```

## Task 4: Run Targeted Regression Suite And Clean Up Assertions

**Files:**
- Modify only if needed:
  - `tests/test_main_window.py`
  - `tests/test_result_module.py`
  - `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - `tests/test_analysis_chart_modules/test_zero_based_axes.py`
  - touched UI files from Tasks 1-3

- [ ] **Step 1: Run the affected targeted suite**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_result_module.py -q
```

Expected: PASS

- [ ] **Step 2: If failures are only stale UI expectations, update them**

Acceptable stale expectation updates:

- `SPL` visible table row -> `声压级`
- `平均SPL` visible table row -> `平均声压级`
- `Loudness` visible table row/card title -> `响度`
- `平均Loudness` visible table row -> `平均响度`
- `FFT` visible result row -> `FFT频谱`
- `CH1`, `CH2`, etc. visible channel text -> `通道1`, `通道2`, etc.
- SPL/Loudness visible left-axis label assertions -> left-axis hidden and bottom-axis visible assertions

Do not update tests that indicate behavior drift in:

- payload contents
- chart data/ranges
- selected channel routing
- STFT selection geometry
- status labels `PASS`, `FAIL`, `OK`, `NG`

- [ ] **Step 3: Run a broader nearby UI smoke suite**

Run:

```powershell
pytest tests/test_stft_module tests/test_analysis_chart_modules tests/test_main_window.py tests/test_result_module.py -q
```

Expected: PASS

- [ ] **Step 4: Inspect final diff for scope control**

Run:

```powershell
git diff --stat HEAD
git diff -- ui/widgets/analysis_charts.py ui/modules/stft.py ui/modules/spectrum.py ui/modules/spl.py ui/modules/loudness.py ui/modules/octave.py ui/widgets/cards.py app/main_window.py ui/widgets/analysis_channel_toolbar.py ui/modules/result.py
```

Expected:

- Changes are limited to UI presentation, chart axis visibility opt-in, and tests.
- No service payload producers, database models, config schemas, AI protocol fields, or analysis calculations changed.

- [ ] **Step 5: Commit any final cleanup**

If Step 2 required additional fixes:

```powershell
git add tests/test_main_window.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_zero_based_axes.py ui/widgets/analysis_charts.py ui/modules/stft.py ui/modules/spectrum.py ui/modules/spl.py ui/modules/loudness.py ui/modules/octave.py ui/widgets/cards.py app/main_window.py ui/widgets/analysis_channel_toolbar.py ui/modules/result.py
git commit -m "test(ui): cover homepage axis and Chinese labels"
```

If no additional fixes were needed after Tasks 1-3, do not create an empty commit.

## Final Verification

Run before handing back:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_stft_module tests/test_main_window.py tests/test_result_module.py -q
```

Expected: PASS

Also verify:

```powershell
git status --short
```

Expected: no new unstaged changes from this implementation slice, except unrelated pre-existing worktree changes that were present before execution.
