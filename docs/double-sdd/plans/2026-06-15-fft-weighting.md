# FFT Weighting Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-channel FFT weighting and use it consistently for FFT display, judgment, live frames, and golden sample comparison.

**Architecture:** Add a `fft_weighting` channel setting and pass it into the existing FFT computation boundary. The weighted FFT output becomes the single source of truth for chart payloads, limit judgment, golden comparison, and live FFT frames; no runtime database migration code is added.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, peewee, SQLite, pytest

---

## Source Spec

- `docs/double-sdd/specs/2026-06-15-fft-weighting-design.md`

## File Structure

- Modify: `app/service/analysis/dsp.py`
  - extend `compute_fft_spectrum` with `weighting="Z"`
  - add a small FFT weighting helper that reuses `a_weighting` and `c_weighting`
- Modify: `app/service/analysis/frames.py`
  - add `weighting: str = "Z"` to `FftFrame`
- Modify: `app/service/analysis/pipeline.py`
  - read `fft_weighting` from channel config and pass it to live FFT computation
- Modify: `model/channel_config_table.py`
  - add `fft_weighting = TextField(default="Z")`
- Modify: `app/service/config_service.py`
  - add channel default, load/save/import normalization, and validation
- Modify: `app/service/db_connection_service.py`
  - require the prepared database column at startup
- Modify: `ui/modules/config.py`
  - add per-channel FFT `计权模式` combo and collect it
- Modify: `app/service/analysis/golden_reference.py`
  - cache FFT references by `(nfft, weighting)`
  - pass weighting to golden FFT derivation
- Modify: `app/service/analysis/judgment.py`
  - pass `fft_weighting` into measured and golden FFT paths
- Modify: `app/service/analysis/review_payloads.py`
  - pass `fft_weighting` into measured and golden FFT paths
  - filter measured/golden chart series together where needed
  - include optional `weighting` metadata in FFT payload
- Modify: `data/jingchen.db`
  - one-time prepared database schema update: add `channel_config_table.fft_weighting`
- Tests:
  - `tests/test_dsp.py`
  - `tests/test_pipeline.py`
  - `tests/test_config_service.py`
  - `tests/test_config_module.py`
  - `tests/test_db_connection_service.py`
  - `tests/test_golden_reference.py`
  - `tests/test_judgment.py`
  - `tests/test_review_payloads.py`

## Task 1: Add Weighted FFT DSP and Live Frame Support

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/frames.py`
- Modify: `app/service/analysis/pipeline.py`
- Test: `tests/test_dsp.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing DSP tests**

Add tests in `tests/test_dsp.py` near `TestComputeFftSpectrum`:

```python
def test_a_weighting_attenuates_low_frequency_fft_bin_relative_to_z():
    sample_rate = 48_000.0
    nfft = 4096
    bin_index = 10
    freq_hz = bin_index * sample_rate / nfft
    t = np.arange(nfft, dtype=np.float64) / sample_rate
    signal = np.sin(2.0 * np.pi * freq_hz * t)

    freqs_z, levels_z = compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z")
    freqs_a, levels_a = compute_fft_spectrum(signal, sample_rate, nfft, weighting="A")

    np.testing.assert_allclose(freqs_a, freqs_z)
    assert levels_a[bin_index] < levels_z[bin_index] - 10.0
```

```python
def test_c_weighting_is_flatter_than_a_weighting_at_low_frequency_fft_bin():
    sample_rate = 48_000.0
    nfft = 4096
    bin_index = 10
    freq_hz = bin_index * sample_rate / nfft
    t = np.arange(nfft, dtype=np.float64) / sample_rate
    signal = np.sin(2.0 * np.pi * freq_hz * t)

    _, levels_z = compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z")
    _, levels_a = compute_fft_spectrum(signal, sample_rate, nfft, weighting="A")
    _, levels_c = compute_fft_spectrum(signal, sample_rate, nfft, weighting="C")

    assert levels_a[bin_index] < levels_c[bin_index] < levels_z[bin_index]
```

- [ ] **Step 2: Run DSP tests and confirm failure**

Run:

```bash
pytest tests/test_dsp.py::TestComputeFftSpectrum::test_a_weighting_attenuates_low_frequency_fft_bin_relative_to_z tests/test_dsp.py::TestComputeFftSpectrum::test_c_weighting_is_flatter_than_a_weighting_at_low_frequency_fft_bin -q
```

Expected: FAIL because `compute_fft_spectrum` does not accept `weighting`.

- [ ] **Step 3: Implement weighted FFT output**

In `app/service/analysis/dsp.py`, change the signature:

```python
def compute_fft_spectrum(
    signal: np.ndarray,
    sample_rate: float,
    nfft: int,
    weighting: str = "Z",
) -> tuple[np.ndarray, np.ndarray]:
```

After computing `freqs` and `mag_db`, add:

```python
mag_db = mag_db + _fft_weighting_db(freqs, weighting)
```

Add helper:

```python
def _fft_weighting_db(frequencies: np.ndarray, weighting: str) -> np.ndarray:
    normalized = str(weighting or "Z").strip().upper()
    if normalized == "A":
        return a_weighting(frequencies)
    if normalized == "C":
        return c_weighting(frequencies)
    return np.zeros_like(np.asarray(frequencies, dtype=np.float64))
```

- [ ] **Step 4: Run DSP tests and confirm pass**

Run the same `pytest ... -q` command.

Expected: PASS.

- [ ] **Step 5: Write failing live pipeline test**

Add a test in `tests/test_pipeline.py`:

```python
def test_live_fft_uses_channel_fft_weighting(monkeypatch):
    calls = []

    def fake_fft(signal, sample_rate, nfft, weighting="Z"):
        calls.append(weighting)
        return np.array([100.0]), np.array([42.0])

    monkeypatch.setattr(
        "app.service.analysis.pipeline.dsp.compute_fft_spectrum",
        fake_fft,
    )
    pipeline = AnalysisPipeline(
        channel_index=0,
        channel_config=_make_config(fft_nfft=4, fft_weighting="A"),
        sample_rate=1000.0,
    )

    result = pipeline.process_chunk(np.ones(4, dtype=np.float32), elapsed_time=0.004)

    assert calls == ["A"]
    assert isinstance(result["fft"], FftFrame)
    assert result["fft"].weighting == "A"
```

- [ ] **Step 6: Run live pipeline test and confirm failure**

Run:

```bash
pytest tests/test_pipeline.py::TestAnalysisPipeline::test_live_fft_uses_channel_fft_weighting -q
```

Expected: FAIL because the pipeline does not pass `fft_weighting` and `FftFrame`
does not expose it.

- [ ] **Step 7: Implement live pipeline support**

In `app/service/analysis/frames.py`, update `FftFrame`:

```python
weighting: str = "Z"
```

In `app/service/analysis/pipeline.py`:

```python
self._fft_weighting = str(channel_config.get("fft_weighting", "Z") or "Z")
```

Pass it to `dsp.compute_fft_spectrum` and set it on `FftFrame`.

- [ ] **Step 8: Run Task 1 tests**

Run:

```bash
pytest tests/test_dsp.py::TestComputeFftSpectrum tests/test_pipeline.py::TestAnalysisPipeline::test_live_fft_uses_channel_fft_weighting -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add app/service/analysis/dsp.py app/service/analysis/frames.py app/service/analysis/pipeline.py tests/test_dsp.py tests/test_pipeline.py
git commit -m "feat: apply weighting to fft spectrum computation"
```

## Task 2: Add FFT Weighting Configuration and Prepared DB Schema

**Files:**
- Modify: `model/channel_config_table.py`
- Modify: `app/service/config_service.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `ui/modules/config.py`
- Modify: `data/jingchen.db`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_module.py`
- Test: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write failing config service tests**

Add tests near existing FFT resolution / x-axis tests in
`tests/test_config_service.py`:

```python
def test_save_config_persists_fft_weighting():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_weighting"] = "C"

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )

    assert loaded["channels"][0]["fft_weighting"] == "C"
    assert row.fft_weighting == "C"
```

```python
def test_validate_rejects_invalid_fft_weighting():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_weighting"] = "B"

    errors = svc.validate_config(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert "通道 1: FFT 计权模式无效" in errors
```

If the service validation method is named differently in the current file,
use the existing validation entrypoint used by adjacent tests.

- [ ] **Step 2: Write failing config UI tests**

Add tests in `tests/test_config_module.py` near
`test_populate_detail_restores_fft_x_axis_scale`:

```python
def test_populate_detail_restores_fft_weighting(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0, fft_weighting="C")])

    module._populate_detail(payload)
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_weighting")

    assert combo is not None
    assert combo.currentText() == "C"
```

```python
def test_collect_data_includes_fft_weighting(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_weighting")
    assert combo is not None
    combo.setCurrentIndex(combo.findText("A"))

    data = module._collect_data()

    assert data["channels"][0]["fft_weighting"] == "A"
```

- [ ] **Step 3: Write failing prepared DB startup test**

Update `tests/test_db_connection_service.py`:

```python
CHANNEL_CONFIG_COLUMN_SQL = {
    "fft_weighting": "TEXT NOT NULL DEFAULT 'Z'",
    ...
}
```

Add an assertion in
`test_connect_existing_prepared_database_accepts_required_channel_columns`:

```python
assert channel_columns["fft_weighting"][2] == "TEXT"
assert tmp_db.execute_sql(
    "SELECT fft_weighting FROM channel_config_table WHERE id = 1"
).fetchone() == ("Z",)
```

- [ ] **Step 4: Run Task 2 tests and confirm failure**

Run:

```bash
pytest tests/test_config_service.py::test_save_config_persists_fft_weighting tests/test_config_service.py::test_validate_rejects_invalid_fft_weighting tests/test_config_module.py::test_populate_detail_restores_fft_weighting tests/test_config_module.py::test_collect_data_includes_fft_weighting tests/test_db_connection_service.py::test_connect_existing_prepared_database_accepts_required_channel_columns -q
```

Expected: FAIL because config/database/UI do not know `fft_weighting`.

- [ ] **Step 5: Implement model and config service support**

In `model/channel_config_table.py` add:

```python
fft_weighting = TextField(default="Z")
```

In `app/service/config_service.py`:

- add `_CHANNEL_DEFAULTS["fft_weighting"] = "Z"`
- include `fft_weighting` in `load_config`
- include `fft_weighting` in `_normalize_import_channel`
- include `fft_weighting` in `_channel_db_values`
- validate with `_VALID_WEIGHTING`
- normalize missing/invalid persisted values to `Z` when loading/importing

Use this helper if no equivalent exists:

```python
@staticmethod
def _normalize_weighting(value) -> str:
    text = str(value or "").strip().upper()
    return text if text in _VALID_WEIGHTING else "Z"
```

- [ ] **Step 6: Implement DB startup schema requirement**

In `app/service/db_connection_service.py`, add `fft_weighting` to
`_REQUIRED_CHANNEL_CONFIG_COLUMNS`.

Do not add runtime migration code.

- [ ] **Step 7: Implement config UI combo**

In `ui/modules/config.py`, add a `_NoWheelComboBox` in the FFT parameter card:

```python
fft_weighting = _NoWheelComboBox()
fft_weighting.addItems(["A", "C", "Z"])
fft_weighting.setObjectName("fft_weighting")
fft_weighting_value = data.get("fft_weighting", "Z") if data else "Z"
fft_weighting_index = fft_weighting.findText(fft_weighting_value)
fft_weighting.setCurrentIndex(fft_weighting_index if fft_weighting_index >= 0 else 2)
fft_controls_form.addRow("计权模式", fft_weighting)
```

In `_collect_channel_data`, add:

```python
fft_weighting = container.findChild(QComboBox, "fft_weighting")
ch["fft_weighting"] = fft_weighting.currentText() if fft_weighting else "Z"
```

- [ ] **Step 8: Update canonical prepared database**

Run a one-time schema update against `data/jingchen.db`:

```powershell
@'
import sqlite3
from pathlib import Path

db_path = Path("data/jingchen.db")
con = sqlite3.connect(db_path)
try:
    columns = {row[1] for row in con.execute("PRAGMA table_info(channel_config_table)")}
    if "fft_weighting" not in columns:
        con.execute(
            "ALTER TABLE channel_config_table "
            "ADD COLUMN fft_weighting TEXT NOT NULL DEFAULT 'Z'"
        )
        con.commit()
finally:
    con.close()
'@ | python -
```

Verify:

```powershell
@'
import sqlite3
con = sqlite3.connect("data/jingchen.db")
try:
    print([row for row in con.execute("PRAGMA table_info(channel_config_table)") if row[1] == "fft_weighting"])
finally:
    con.close()
'@ | python -
```

Expected: one row for `fft_weighting`, type `TEXT`, default `'Z'`.

- [ ] **Step 9: Run Task 2 tests**

Run:

```bash
pytest tests/test_config_service.py::test_save_config_persists_fft_weighting tests/test_config_service.py::test_validate_rejects_invalid_fft_weighting tests/test_config_module.py::test_populate_detail_restores_fft_weighting tests/test_config_module.py::test_collect_data_includes_fft_weighting tests/test_db_connection_service.py::test_connect_existing_prepared_database_accepts_required_channel_columns -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 2**

```bash
git add model/channel_config_table.py app/service/config_service.py app/service/db_connection_service.py ui/modules/config.py data/jingchen.db tests/test_config_service.py tests/test_config_module.py tests/test_db_connection_service.py
git commit -m "feat: add fft weighting configuration"
```

## Task 3: Apply FFT Weighting to Judgment and Golden Reference

**Files:**
- Modify: `app/service/analysis/golden_reference.py`
- Modify: `app/service/analysis/judgment.py`
- Test: `tests/test_golden_reference.py`
- Test: `tests/test_judgment.py`

- [ ] **Step 1: Write failing golden cache test**

Update the existing golden reference cache test in `tests/test_golden_reference.py`
so fake FFT captures weighting:

```python
fft_calls: list[tuple[float, int, str]] = []

def fake_fft(signal, sample_rate, nfft, weighting="Z"):
    fft_calls.append((float(sample_rate), int(nfft), str(weighting)))
    axis = np.arange((int(nfft) // 2) + 1, dtype=np.float64)
    levels = np.full(axis.shape, 1.0, dtype=np.float64)
    return axis, levels
```

Then assert separate cache entries:

```python
fft_ref_z = cache.ensure_fft(bundle1, nfft=4096, weighting="Z")
fft_ref_a = cache.ensure_fft(bundle1, nfft=4096, weighting="A")
fft_ref_z_again = cache.ensure_fft(bundle1, nfft=4096, weighting="Z")

assert fft_ref_z[0] is fft_ref_z_again[0]
assert fft_ref_a[0] is not fft_ref_z[0]
assert fft_calls.count((48_000.0, 4096, "Z")) == 1
assert fft_calls.count((48_000.0, 4096, "A")) == 1
```

- [ ] **Step 2: Write failing judgment weighting test**

Add a focused test in `tests/test_judgment.py`:

```python
def test_fft_judgment_passes_weighting_to_measured_and_golden_fft(monkeypatch, tmp_path):
    engine = JudgmentEngine()
    calls = []

    def fake_fft(signal, sample_rate, nfft, weighting="Z"):
        calls.append(str(weighting))
        return np.array([100.0, 200.0]), np.array([50.0 + _REF_DB, 52.0 + _REF_DB])

    monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)
    golden_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))

    config = _make_config(
        channels=[
            _make_channel_config(
                0,
                fft_weighting="A",
                fft_golden_mode="absolute",
                fft_band_limits=[
                    {"freq": 100.0, "lower": -10.0, "upper": 10.0},
                    {"freq": 200.0, "lower": -10.0, "upper": 10.0},
                ],
                **_binding(golden_path),
            )
        ]
    )

    engine.judge(
        config,
        np.ones((int(48_000.0 * 3.0), 1)),
        48_000.0,
        mic_cal={},
        audio_file_path="/tmp/a.wav",
        golden_sample_service=service,
    )

    assert calls == ["A", "A"]
```

Adjust helper names to match the current test file; if `JudgmentEngine` receives
the golden sample service through constructor or fixture, follow the existing
golden reference tests in the same file.

- [ ] **Step 3: Run Task 3 tests and confirm failure**

Run:

```bash
pytest tests/test_golden_reference.py::test_cache_reuses_loaded_bundles_and_derived_metrics tests/test_judgment.py::TestJudgmentEngine::test_fft_judgment_passes_weighting_to_measured_and_golden_fft -q
```

Expected: FAIL because golden cache and judgment do not accept/pass weighting.

- [ ] **Step 4: Implement golden reference weighting cache**

In `app/service/analysis/golden_reference.py`:

- replace `fft_by_nfft` with a dict keyed by `(nfft, weighting)`
- normalize weighting to `A`, `C`, or `Z`
- update `ensure_fft(..., nfft, weighting="Z")`
- call `dsp.compute_fft_spectrum(bundle.signal, bundle.sample_rate, nfft, weighting)`
- keep octave caching unchanged

- [ ] **Step 5: Implement judgment weighting**

In `app/service/analysis/judgment.py`:

- read `weighting = str(channel_config.get("fft_weighting", "Z") or "Z")`
- pass weighting to `golden_cache.ensure_fft`
- add `weighting="Z"` to `_measure_fft`
- pass weighting to `dsp.compute_fft_spectrum`
- preserve existing absolute and delta limit semantics

- [ ] **Step 6: Run Task 3 tests**

Run:

```bash
pytest tests/test_golden_reference.py tests/test_judgment.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add app/service/analysis/golden_reference.py app/service/analysis/judgment.py tests/test_golden_reference.py tests/test_judgment.py
git commit -m "feat: apply fft weighting to judgment and golden references"
```

## Task 4: Apply FFT Weighting to Review Chart Payloads

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Test: `tests/test_review_payloads.py`

- [ ] **Step 1: Update review test stubs for weighting**

Update `_GoldenCacheStub.ensure_fft` in `tests/test_review_payloads.py`:

```python
def ensure_fft(self, bundle, *, nfft, weighting="Z"):
    self.ensure_fft_calls += 1
    self.ensure_fft_nffts.append(int(nfft))
    self.ensure_fft_weightings.append(str(weighting))
    ...
```

Initialize `self.ensure_fft_weightings = []`.

- [ ] **Step 2: Write failing review payload weighting test**

Add:

```python
def test_build_chart_payload_fft_passes_weighting_to_measured_and_golden(monkeypatch):
    calls = []
    ref_db = 20.0 * math.log10(20e-6)

    def fake_fft(signal, sample_rate, nfft, weighting="Z"):
        calls.append(str(weighting))
        return (
            np.array([1000.0, 2000.0], dtype=np.float64),
            np.array([64.0, 45.0], dtype=np.float64) + ref_db,
        )

    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        fake_fft,
    )
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_weighting="C",
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert calls == ["C"]
    assert cache.ensure_fft_weightings == ["C"]
    assert payload[0]["fft"]["weighting"] == "C"
```

- [ ] **Step 3: Write failing paired filtering test**

Add:

```python
def test_build_chart_payload_fft_filters_non_finite_measured_and_golden_pairs(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([0.0, 1000.0, 2000.0], dtype=np.float64),
            np.array([-np.inf, 64.0, 45.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([], dtype=np.float64),
            np.array([], dtype=np.float64),
        ),
    )
    cache = _GoldenCacheStub(
        fft_axis=(0.0, 1000.0, 2000.0),
        fft_levels=(-np.inf, 60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    fft = payload[0]["fft"]
    assert fft["frequencies_hz"] == [1000.0, 2000.0]
    assert fft["levels_db"] == [64.0, 45.0]
    assert fft["golden_overlay_levels_db"] == [60.0, 41.0]
```

- [ ] **Step 4: Run Task 4 tests and confirm failure**

Run:

```bash
pytest tests/test_review_payloads.py::test_build_chart_payload_fft_passes_weighting_to_measured_and_golden tests/test_review_payloads.py::test_build_chart_payload_fft_filters_non_finite_measured_and_golden_pairs -q
```

Expected: FAIL.

- [ ] **Step 5: Implement review payload support**

In `app/service/analysis/review_payloads.py`:

- read `weighting = str(channel_config.get("fft_weighting", "Z") or "Z")`
- pass weighting into `_measure_fft_levels`
- pass weighting into `golden_cache.ensure_fft`
- include `"weighting": normalized_weighting` in FFT chart payload
- add pair/triple filtering helpers for FFT chart series
- ensure delta and absolute modes use frequency, measured, and golden arrays of
  matching filtered length before subtracting or serializing overlay
- preserve existing `limit_bands` and `limit_envelope` semantics

- [ ] **Step 6: Run review payload tests**

Run:

```bash
pytest tests/test_review_payloads.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add app/service/analysis/review_payloads.py tests/test_review_payloads.py
git commit -m "feat: apply fft weighting to review chart payloads"
```

## Task 5: Final Verification and Integration Check

**Files:**
- Verify all files changed by Tasks 1-4
- No new production files expected

- [ ] **Step 1: Verify prepared database schema**

Run:

```powershell
@'
import sqlite3
con = sqlite3.connect("data/jingchen.db")
try:
    cols = {row[1]: row for row in con.execute("PRAGMA table_info(channel_config_table)")}
    print(cols.get("fft_weighting"))
finally:
    con.close()
'@ | python -
```

Expected: output contains a `fft_weighting` row with `TEXT` and default `'Z'`.

- [ ] **Step 2: Run focused test suite**

Run:

```bash
pytest tests/test_dsp.py tests/test_pipeline.py tests/test_config_service.py tests/test_config_module.py tests/test_db_connection_service.py tests/test_golden_reference.py tests/test_judgment.py tests/test_review_payloads.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite if time allows**

Run:

```bash
pytest -q
```

Expected: PASS. If full suite is too slow or blocked by environment constraints,
record the exact command, failure/timeout, and why focused verification is the
available evidence.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git diff --stat HEAD~4..HEAD
git diff --check
git status --short
```

Expected:

- no whitespace errors
- only intended implementation files and tests changed
- unrelated pre-existing workspace changes are not staged or reverted

- [ ] **Step 5: Commit any final verification-only adjustments**

Only if Task 5 required code/test corrections:

```bash
git add <exact files changed>
git commit -m "test: verify fft weighting integration"
```

Otherwise do not create an empty commit.
