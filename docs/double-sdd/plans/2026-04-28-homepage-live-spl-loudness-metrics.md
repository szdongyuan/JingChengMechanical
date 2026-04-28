# Homepage Live SPL And Loudness Metrics Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two read-only homepage result-summary metrics that show live SPL/loudness during recording and selected-channel average SPL/loudness after analysis.

**Architecture:** Extend the existing live analysis stream with a throttled `LoudnessFrame`, route it through `TestSession` and `EventBus`, and let `ResultModule` render metrics using the same resolved display channel as the chart modules. `MainWindow` owns process-lifetime channel memory and validates it against the actually displayed config before wiring the selected channel to charts and metrics.

**Tech Stack:** PyQt6 signals/widgets, numpy/scipy/MoSQITo DSP helpers, pytest/pytest-qt-style offscreen widget tests.

---

## Spec

- `docs/double-sdd/specs/2026-04-28-homepage-live-spl-loudness-metrics-design.md`

## File Structure

- Modify `app/service/analysis/frames.py`
  - Add `LoudnessFrame(time: float, loudness_sone: float)`.
- Modify `app/services_container.py`
  - Add `EventBus.loudness_updated = pyqtSignal(int, str, int, object)` beside other live analysis signals.
- Modify `app/service/analysis/pipeline.py`
  - Add live loudness rolling buffer, throttle constants, and return key `loudness`.
- Modify `app/service/analysis/test_session.py`
  - Re-emit `LoudnessFrame` on `bus.loudness_updated`.
- Modify `app/main_window.py`
  - Add process-lifetime remembered channel.
  - Wire `ResultModule.selected_display_channel_provider`.
  - Refresh result metrics on toolbar selection.
- Modify `ui/modules/result.py`
  - Add two metric display widgets.
  - Track current live run ID from factory runtime signals.
  - Render live metrics during recording/analyzing and averages during review/stopped.
  - Clear metrics on reanalysis/reset/stale config or run.
- Modify `consts/style.py`
  - Add read-only metric badge styling.
- Modify tests:
  - `tests/test_service_container.py`
  - `tests/test_pipeline.py`
  - `tests/test_test_session.py`
  - `tests/test_main_window.py`
  - `tests/test_result_module.py`
  - Update any local bus stubs that instantiate modules needing `loudness_updated`.

## Task 1: Live Loudness Frame, Signal, And Pipeline Computation

**Files:**
- Modify: `app/service/analysis/frames.py`
- Modify: `app/services_container.py`
- Modify: `app/service/analysis/pipeline.py`
- Test: `tests/test_service_container.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing EventBus contract tests**

In `tests/test_service_container.py`, update the live analysis signal tests:

```python
def test_analysis_signals_exist():
    for name in (
        "stft_updated",
        "spl_updated",
        "loudness_updated",
        "octave_updated",
        "fft_updated",
        "recording_progress",
        "test_started",
        "test_finished",
    ):
        assert hasattr(EventBus, name), f"Missing signal: {name}"
```

Also assert the new signal has the same run-aware signature:

```python
assert _normalized_signal_argument_types(EventBus.loudness_updated) == expected_argument_types
```

- [ ] **Step 2: Write failing pipeline tests**

In `tests/test_pipeline.py`, import `LoudnessFrame` and add focused tests using monkeypatching so MoSQITo is not required:

```python
def test_process_chunk_returns_throttled_loudness_frame(monkeypatch):
    calls = []

    def fake_compute_loudness_trace(signal, sample_rate):
        calls.append(np.asarray(signal, dtype=np.float64).copy())
        return [0.0, 0.1], [1.25, 2.5], 2.5

    monkeypatch.setattr(
        "app.service.analysis.pipeline.dsp.compute_loudness_trace",
        fake_compute_loudness_trace,
    )

    p = AnalysisPipeline(
        channel_index=0,
        channel_config=_make_config(),
        sample_rate=100.0,
    )

    first = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.5)
    second = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=1.0)
    third = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=1.5)
    fourth = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=2.0)

    assert first["loudness"] is None
    assert isinstance(second["loudness"], LoudnessFrame)
    assert second["loudness"].time == pytest.approx(1.0)
    assert second["loudness"].loudness_sone == pytest.approx(2.5)
    assert third["loudness"] is None
    assert isinstance(fourth["loudness"], LoudnessFrame)
    assert len(calls) == 2
```

Add tests for non-fatal backend failure and reset:

```python
def test_process_chunk_suppresses_live_loudness_backend_errors(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.pipeline.dsp.compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("backend down")),
    )
    p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=100.0)
    result = p.process_chunk(np.ones(100, dtype=np.float32), elapsed_time=1.0)
    assert result["loudness"] is None

def test_reset_clears_live_loudness_buffer_and_throttle(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.pipeline.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0], [3.0], 3.0),
    )
    p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=100.0)
    p.process_chunk(np.ones(100, dtype=np.float32), elapsed_time=1.0)
    p.reset()
    assert p._loudness_buffer.size == 0
    assert p._last_live_loudness_emit_time == 0.0
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```powershell
pytest tests/test_service_container.py::test_analysis_signals_exist tests/test_service_container.py::test_analysis_frame_signals_include_run_id_contract tests/test_pipeline.py -q
```

Expected: FAIL because `LoudnessFrame`, `loudness_updated`, and `result["loudness"]` do not exist.

- [ ] **Step 4: Implement minimal live loudness contract**

In `app/service/analysis/frames.py`:

```python
@dataclass
class LoudnessFrame:
    """One live loudness measurement for a single channel."""
    time: float
    loudness_sone: float
```

In `app/services_container.py`, add:

```python
loudness_updated = pyqtSignal(int, str, int, object)
```

In `app/service/analysis/pipeline.py`:

- Import `LoudnessFrame`.
- Add module constants:

```python
_LIVE_LOUDNESS_THROTTLE_SEC = 1.0
_LIVE_LOUDNESS_WINDOW_SEC = 1.0
```

- Add instance state in `__init__`:

```python
self._loudness_buffer = np.array([], dtype=np.float64)
self._last_live_loudness_emit_time = 0.0
```

- After calibration, append the calibrated chunk to `_loudness_buffer`, cap to `round(sample_rate * _LIVE_LOUDNESS_WINDOW_SEC)`, and call a private helper:

```python
loudness_frame = self._maybe_compute_live_loudness(elapsed_time)
```

- Include `"loudness": loudness_frame` in the returned dict.
- Implement helper behavior:
  - If `elapsed_time - self._last_live_loudness_emit_time < _LIVE_LOUDNESS_THROTTLE_SEC`, return `None`.
  - If buffer is empty or `sample_rate <= 0`, return `None`.
  - Set `self._last_live_loudness_emit_time = elapsed_time` only when attempting computation.
  - Call `dsp.compute_loudness_trace`.
  - Use the last finite value from returned values as `loudness_sone`.
  - Return `LoudnessFrame(time=elapsed_time, loudness_sone=float(value))`.
  - On any exception or no finite value, return `None`.
- Update `reset()` to clear `_loudness_buffer` and reset throttle time to `0.0`.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
pytest tests/test_service_container.py::test_analysis_signals_exist tests/test_service_container.py::test_analysis_frame_signals_include_run_id_contract tests/test_pipeline.py -q
```

Expected: PASS.

## Task 2: TestSession Loudness Re-Emission

**Files:**
- Modify: `app/service/analysis/test_session.py`
- Modify: `tests/test_test_session.py`
- Modify if needed: `tests/test_test_manager/_support.py`

- [ ] **Step 1: Write failing session tests**

In `tests/test_test_session.py`:

- Import `LoudnessFrame`.
- Add `bus.loudness_updated.emit` to `_make_mock_bus()`.
- Add a test beside existing frame re-emission tests:

```python
def test_on_chunk_re_emits_loudness_frame_with_factory_run_id():
    bus = _make_mock_bus()
    ai = MagicMock()
    session = TestSession(
        config=_make_config(),
        bus=bus,
        ai_service=ai,
        mic_cal={},
        workorder={"factory_run_id": "factory-run-1"},
        audio_dir="/tmp/audio",
    )
    loudness_frame = LoudnessFrame(time=0.10, loudness_sone=2.25)
    fake_pipeline = MagicMock()
    fake_pipeline.channel_index = 0
    fake_pipeline.process_chunk.return_value = {
        "stft_frames": [],
        "stft": None,
        "spl": SplFrame(time=0.05, spl_db=60.0, weighting="Z"),
        "loudness": loudness_frame,
        "octave": None,
        "fft": None,
    }
    session._pipelines = [fake_pipeline]

    session._on_chunk(np.ones((2048, 1), dtype=np.float32), 0)

    assert bus.loudness_updated.emit.call_args.args == (
        1,
        "factory-run-1",
        0,
        loudness_frame,
    )
```

Update fake pipeline return dictionaries in existing tests to include `"loudness": None` where strict key access would otherwise fail.

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
pytest tests/test_test_session.py::TestTestSessionProcessing -q
```

Expected: FAIL until `TestSession` emits the new frame and test fakes include the new key.

- [ ] **Step 3: Implement session re-emission**

In `TestSession._process_chunk()`, after SPL emission and before/after octave emission:

```python
if result.get("loudness") is not None:
    self._bus.loudness_updated.emit(self.config_id, frame_run_id, ch_idx, result["loudness"])
```

Use `result.get("loudness")` for compatibility with simple fakes in older tests.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
pytest tests/test_test_session.py::TestTestSessionProcessing -q
```

Expected: PASS.

## Task 3: MainWindow Process-Lifetime Channel Memory And ResultModule Wiring

**Files:**
- Modify: `app/main_window.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write failing MainWindow tests**

Update `_BusStub` in `tests/test_main_window.py` to include:

```python
self.loudness_updated = _SignalStub()
```

Add tests that document the new process-lifetime channel preference:

```python
def test_main_window_uses_process_last_channel_when_valid_for_new_config(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_running_state(101))
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 2]},
        202: {"config_id": 202, "selected_channels": [2, 1]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 2
    assert window.current_display_channel_index() == 2
    assert window.result.selected_display_channel_provider() == 2
```

Add the invalid-channel fallback test:

```python
def test_main_window_falls_back_to_first_selected_channel_when_process_last_is_invalid(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_running_state(101))
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 2]},
        202: {"config_id": 202, "selected_channels": [1, 0]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.current_display_channel_index() == 1
    assert window.result.selected_display_channel_provider() == 1
```

Update or replace existing tests that assert old per-config channel restoration. New expected behavior is process-lifetime memory validated against the current config, not per-config restoration.

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
pytest tests/test_main_window.py -q
```

Expected: FAIL because `ResultModule` is not wired as a channel-aware display module and `MainWindow` still prefers per-config remembered channels.

- [ ] **Step 3: Implement channel memory and wiring**

In `MainWindow.__init__`, add:

```python
self._last_selected_display_channel: int | None = None
```

In `_attach_analysis_channel_providers()`:

- Include `self.result` in provider assignment.
- Include `self.result` in refresh module list if it exposes `refresh_selected_display_channel`.

Implementation shape:

```python
for module in (self.result, self.stft, self.loudness, self.spl, self.spectrum, self.octave):
    module.selected_display_channel_provider = self.current_display_channel_index
```

In `_sync_selected_display_channel()`:

- Store `self._last_selected_display_channel = channel_index` whenever a user/tool selection is applied.
- Continue storing `display_channel_by_config_id[config_id] = channel_index`.
- Refresh `ResultModule` with the other modules.

In `_resolve_current_display_channel()`:

- First use `self._last_selected_display_channel` if it is in `channels`.
- Otherwise return `channels[0]`.
- Do not use stale `display_channel_by_config_id[config_id]` as a fallback when process-lifetime remembered channel is invalid.
- Keep the existing review chart payload guard only to prevent choosing a channel that has no committed chart payload in review phases. If the process remembered channel is valid for config but missing from review chart payload, fallback to the first channel that is both configured and present in review chart payload.

In `_refresh_analysis_display_modules()`, include `self.result` if it provides `refresh_selected_display_channel()`.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
pytest tests/test_main_window.py -q
```

Expected: PASS after updating old per-config expectations to the new spec.

## Task 4: Result Summary Metric UI, Live Filtering, And Post-Analysis Averages

**Files:**
- Modify: `ui/modules/result.py`
- Modify: `consts/style.py`
- Modify: `tests/test_result_module.py`

- [ ] **Step 1: Write failing ResultModule tests**

Update `_BusStub` in `tests/test_result_module.py` to include:

```python
self.spl_updated = _SignalStub()
self.loudness_updated = _SignalStub()
self.factory_runtime_start_accepted = _SignalStub()
self.factory_runtime_analyzing_entered = _SignalStub()
```

Add helpers:

```python
def _metric_text(module: ResultModule, attr: str) -> str:
    return getattr(module, attr).text()

def _set_selected_channel(module: ResultModule, channel_index: int | None):
    module.selected_display_channel_provider = lambda: channel_index
```

Add tests:

```python
def test_result_module_shows_metric_placeholders_before_config(qapp):
    module = _make_module()
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

def test_result_module_updates_live_metrics_only_for_current_run_and_channel(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 1, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(_state("running", displayed_config_id=101, active_config_id=101))
    module.services.bus.factory_runtime_start_accepted.emit(SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual"))

    module.services.bus.spl_updated.emit(101, "old-run", 1, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=71.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace(spl_db=72.34))
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace(loudness_sone=2.345))
    module.services.bus.spl_updated.emit(101, "old-run", 1, SimpleNamespace(spl_db=80.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=9.0))

    assert module.lb_spl_metric_label.text() == "声压级"
    assert module.lb_spl_metric_value.text() == "72.3 dB"
    assert module.lb_loudness_metric_label.text() == "响度"
    assert module.lb_loudness_metric_value.text() == "2.35 sone"
```

Post-analysis average test:

```python
def test_result_module_renders_selected_channel_average_metrics_from_judgment_payload(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 1, "loudness_unit": "phon"}],
    })
    judgment_payload = {
        "channel_results": [
            {"channel_index": 0, "spl_average_db": 60.0, "loudness_average_sone": 1.0},
            {"channel_index": 1, "spl_average_db": 66.66, "loudness_average_sone": 2.0},
        ]
    }

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=judgment_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "66.7 dB"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "50.0 phon"
```

Reanalysis stale-clearing test:

```python
def test_result_module_clears_metric_values_during_reanalysis_until_replacement_review(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    first_payload = {"channel_results": [{"channel_index": 0, "spl_average_db": 60.0, "loudness_average_sone": 1.5}]}
    second_payload = {"channel_results": [{"channel_index": 0, "spl_average_db": 63.0, "loudness_average_sone": 2.5}]}

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=first_payload, summary_payload=_summary_payload(overall_status="pass"))
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    module.services.bus.factory_state_changed.emit(_state("reanalyzing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=None, summary_payload=None)
    )
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=second_payload, summary_payload=_summary_payload(overall_status="pass"))
    )
    assert module.lb_spl_metric_value.text() == "63.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.50 sone"
```

Add explicit live-run clearing coverage for displayed-config changes:

```python
def test_result_module_clears_live_run_when_displayed_config_changes(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda config_id: {
        "config_id": config_id,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=61.0))
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=202, active_config_id=202)
    )
    assert module.lb_spl_metric_value.text() == "--"

    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
```

Add explicit same-config new-run clearing coverage:

```python
def test_result_module_new_matching_start_clears_prior_live_metrics_and_rejects_old_run(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-1", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-1", 0, SimpleNamespace(spl_db=61.0))
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-2", trigger_source="manual")
    )
    assert module.lb_spl_metric_value.text() == "--"

    module.services.bus.spl_updated.emit(101, "run-1", 0, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-2", 0, SimpleNamespace(spl_db=62.0))
    assert module.lb_spl_metric_value.text() == "62.0 dB"
```

Add explicit analyzing preservation coverage:

```python
def test_result_module_preserves_latest_live_metrics_during_analyzing_until_review_arrives(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=61.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=1.25))

    module.services.bus.factory_state_changed.emit(
        _state("analyzing", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_analyzing_entered.emit(
        SimpleNamespace(config_id=101, run_id="run-101")
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={"channel_results": [{"channel_index": 0, "spl_average_db": 64.0, "loudness_average_sone": 2.0}]},
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    assert module.lb_spl_metric_value.text() == "64.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"
```

Also assert `factory_display_reset` clears both metric values and clears the tracked live run so later old-run frames are ignored.

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
pytest tests/test_result_module.py -q
```

Expected: FAIL because metric labels/values and live signal handling do not exist.

- [ ] **Step 3: Implement ResultModule UI and logic**

In `ResultModule.__init__`:

- Add public attributes used by tests:
  - `lb_spl_metric_label`
  - `lb_spl_metric_value`
  - `lb_loudness_metric_label`
  - `lb_loudness_metric_value`
- Add optional channel provider:

```python
self.selected_display_channel_provider = None
self._current_live_run_id: str | None = None
self._live_spl_by_key: dict[tuple[int, str, int], float] = {}
self._live_loudness_by_key: dict[tuple[int, str, int], float] = {}
```

- Place the metric row below the existing top result row and above `empty_state`.

Suggested widget shape:

```python
metrics = QWidget()
metrics_lay = QHBoxLayout(metrics)
metrics_lay.setContentsMargins(0, 0, 0, 0)
metrics_lay.setSpacing(8)
```

Each metric can be a `QWidget` with objectName `ResultMetricBadge`, containing small label and large value labels.

Connect signals in `_connect_bus()`:

```python
if hasattr(self.services.bus, "spl_updated"):
    self.services.bus.spl_updated.connect(self._on_spl_updated)
if hasattr(self.services.bus, "loudness_updated"):
    self.services.bus.loudness_updated.connect(self._on_loudness_updated)
if hasattr(self.services.bus, "factory_runtime_start_accepted"):
    self.services.bus.factory_runtime_start_accepted.connect(self._on_factory_runtime_start_accepted)
if hasattr(self.services.bus, "factory_runtime_analyzing_entered"):
    self.services.bus.factory_runtime_analyzing_entered.connect(self._on_factory_runtime_analyzing_entered)
```

Implement:

- `refresh_selected_display_channel()` calls `_refresh_metric_display()`.
- `_selected_channel_index()` calls provider and returns an `int | None`.
- `_displayed_config_id()` reads `self._factory_state.displayed_config_id`.
- `_current_metric_key()` returns `(config_id, run_id, channel_index)` only when all are present.
- `_on_factory_runtime_start_accepted(payload)` stores run ID when payload config matches displayed/active config and clears live caches for other runs.
- `_on_factory_runtime_analyzing_entered(payload)` keeps matching run ID.
- `_on_factory_state_changed(state)` must compare the previous displayed config ID with the new displayed config ID. When it changes to a different config, clear `_current_live_run_id` and live metric caches before refreshing display.
- `_on_factory_display_reset(_reason)` must clear `_current_live_run_id`, live metric caches, and metric labels/values.
- A new matching `factory_runtime_start_accepted` must replace `_current_live_run_id` with the new run ID and clear live metric caches from the previous run before accepting new frames.
- `_on_spl_updated(config_id, run_id, channel_idx, frame)` validates config/run/channel/phase and finite `frame.spl_db`.
- `_on_loudness_updated(config_id, run_id, channel_idx, frame)` validates config/run/channel/phase and finite `frame.loudness_sone`.
- `_refresh_metric_display()`:
  - If phase is `reviewing` or `stopped` and `judgment_payload` exists, display averages.
  - If phase is `running` or `analyzing`, display latest live values.
  - If phase is `reanalyzing`, display both as `--`.
  - Else display both as `--`.

Formatting helpers:

```python
@staticmethod
def _format_spl_db(value) -> str:
    # finite only; one decimal

@staticmethod
def _format_loudness(value_sone, unit: str) -> str:
    # sone: <10 two decimals, >=10 one decimal
    # phon: use dsp.sone_to_phon, one decimal
```

Config/unit helper:

- Load `services.config_service.load_config(config_id)` when available.
- Find the channel dict where `channel_index == selected_channel`.
- Loudness unit defaults to `sone`.
- Use `app.service.analysis.dsp.sone_to_phon`.

In existing `_show_empty()`, `_show_in_progress()`, `_render_summary()`, `_on_factory_display_reset()`, and state/review handlers, call `_refresh_metric_display()` after badge/table updates.

- [ ] **Step 4: Add styling**

In `consts/style.py`, add object styles:

```css
QWidget#ResultMetricBadge {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
}
QLabel#ResultMetricLabel {
  color: #64748b;
  font-weight: 700;
}
QLabel#ResultMetricValue {
  color: #0f172a;
  font-size: 20px;
  font-weight: 700;
}
```

Keep the controls read-only and non-clickable.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
pytest tests/test_result_module.py -q
pytest tests/test_main_window.py -q
```

Expected: PASS.

## Task 5: Integrated Verification

**Files:**
- No intended source changes unless focused verification finds a real issue.

- [ ] **Step 1: Run spec-focused suite**

Run:

```powershell
pytest tests/test_result_module.py tests/test_main_window.py tests/test_service_container.py tests/test_test_session.py tests/test_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 2: Run affected analysis and review suites**

Run:

```powershell
pytest tests/test_review_payloads.py tests/test_judgment.py tests/test_analysis_chart_modules -q
```

Expected: PASS.

- [ ] **Step 3: Run manager/session regression suite**

Run:

```powershell
pytest tests/test_test_manager -q
```

Expected: PASS.

- [ ] **Step 4: Commit implementation**

Only after all required tests pass:

```powershell
git add app/service/analysis/frames.py app/services_container.py app/service/analysis/pipeline.py app/service/analysis/test_session.py app/main_window.py ui/modules/result.py consts/style.py tests/test_service_container.py tests/test_pipeline.py tests/test_test_session.py tests/test_main_window.py tests/test_result_module.py
git commit -m "feat: add homepage live spl loudness metrics"
```
