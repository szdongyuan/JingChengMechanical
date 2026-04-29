# Configurable FFT Resolution Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated configurable FFT resolution that caps FFT cost while keeping committed FFT analysis based on the full selected analysis window.

**Architecture:** Split the change into four slices. First add the persisted `fft_nfft` setting and expose it in the config UI. Then update the FFT DSP and golden-reference cache to use one bounded committed FFT contract. After that, update the live pipeline to use a rolling FFT buffer. Finally, route the new setting through judgment and committed review payloads with soft-failure behavior for measured FFT errors.

**Tech Stack:** Python 3.12, Peewee, PyQt6, NumPy, SciPy, pytest

---

**Execution Context:** Commands below assume the worker is already running from the dedicated worktree root created by the `subagent-driven-development` setup step.

## File Structure

- Modify: `model/channel_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/config_service.py`
- Modify: `ui/modules/config.py`
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/pipeline.py`
- Modify: `app/service/analysis/golden_reference.py`
- Modify: `app/service/analysis/judgment.py`
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `tests/test_db_connection_service.py`
- Modify: `tests/test_config_service.py`
- Modify: `tests/test_config_module.py`
- Modify: `tests/test_dsp.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_golden_reference.py`
- Modify: `tests/test_judgment.py`
- Modify: `tests/test_review_payloads.py`
- Modify as needed for required `fft_nfft` defaults:
  - `tests/test_test_session.py`
  - `tests/test_test_manager/_support.py`
  - `tests/test_test_manager/test_sample_rate_mismatch.py`
  - `tests/test_test_manager/test_start_inputs_and_resolution.py`
  - `tests/test_test_manager/test_start_acceptance_and_failures.py`
  - `tests/test_test_manager/test_max_duration.py`
  - `tests/test_test_manager/test_late_attach_buffer_slice.py`
  - `tests/test_test_manager/test_signal_disconnection.py`

### Task 1: Persist `fft_nfft` And Expose It In Config UI

**Files:**
- Modify: `model/channel_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/config_service.py`
- Modify: `ui/modules/config.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing persistence and UI tests**

Add/extend tests so they assert:

```python
assert "fft_nfft" in _table_columns(tmp_db, "channel_config_table")
assert loaded["channels"][0]["fft_nfft"] == 4096
assert saved_payload["channels"][0]["fft_nfft"] == 2048
assert container.findChild(QComboBox, "fft_nfft").currentData() == 4096
assert _field_label_text(container, "fft_nfft") == "FFT 分辨率"
assert _ancestor_group_title(container.findChild(QComboBox, "fft_nfft")) == "FFT 频带上下限"
assert legacy_loaded["channels"][0]["stft_nfft"] == 1024
assert legacy_loaded["channels"][0]["fft_nfft"] == 4096
```

Cover:

- startup DB migration/backfill adds `fft_nfft INTEGER NOT NULL DEFAULT 4096`
- startup DB migration preserves legacy channel rows while backfilling `fft_nfft` to `4096`
- `ConfigService.load_config()` returns `fft_nfft`
- `ConfigService.save_config()` persists `fft_nfft`
- `ConfigService.validate()` rejects values outside the allowed list
- `ConfigModule` populates and collects `fft_nfft` from the FFT section
- `ConfigModule` gives the control an explicit operator-facing label text of `FFT 分辨率`

- [ ] **Step 2: Run the failing config-facing tests**

Run:

```powershell
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py -q
```

Expected: FAIL because the schema, service payloads, and UI do not yet know about `fft_nfft`.

- [ ] **Step 3: Implement schema, service, and UI wiring**

Add the new field and allowed values:

```python
# model/channel_config_table.py
fft_nfft = IntegerField(default=4096)

# app/service/config_service.py
_CHANNEL_DEFAULTS = {
    ...
    "fft_nfft": 4096,
}
_VALID_FFT_NFFT = [256, 512, 1024, 2048, 4096, 8192]
```

Update startup migration ownership in `DbConnectionService`:

```python
self._ensure_columns(
    "channel_config_table",
    [
        ("fft_nfft", "INTEGER NOT NULL DEFAULT 4096"),
        ...
    ],
)
```

Update `ConfigService.load_config()`, `validate()`, and `_channel_db_values()` to round-trip `fft_nfft`.

Move the FFT point-count selector into the FFT section in `ui/modules/config.py`:

```python
fft_resolution = _NoWheelComboBox()
for value in [256, 512, 1024, 2048, 4096, 8192]:
    fft_resolution.addItem(str(value), value)
fft_resolution.setObjectName("fft_nfft")
fft_controls_form.addRow("FFT 分辨率", fft_resolution)
```

Implementation notes:

- keep `stft_nfft` in the STFT section only
- use `4096` as the new default everywhere
- keep the label text explicit enough that an operator can distinguish FFT resolution from the STFT `FFT 点数` control

- [ ] **Step 4: Update shared test helpers and default payload builders that now require `fft_nfft`**

Touch the exact helper/default surfaces listed in the file structure where channel-config payloads are constructed, and add `fft_nfft: 4096` unless a test intentionally overrides it.

- [ ] **Step 5: Run the helper/default fallout tests**

Run:

```powershell
pytest tests/test_test_session.py tests/test_test_manager/test_sample_rate_mismatch.py tests/test_test_manager/test_start_inputs_and_resolution.py tests/test_test_manager/test_start_acceptance_and_failures.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_signal_disconnection.py -q
```

Expected: PASS

- [ ] **Step 6: Re-run the config-facing tests**

Run:

```powershell
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py -q
```

Expected: PASS

- [ ] **Step 7: Commit Task 1**

```bash
git add model/channel_config_table.py app/service/db_connection_service.py app/service/config_service.py ui/modules/config.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_test_session.py tests/test_test_manager/_support.py tests/test_test_manager/test_sample_rate_mismatch.py tests/test_test_manager/test_start_inputs_and_resolution.py tests/test_test_manager/test_start_acceptance_and_failures.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_signal_disconnection.py
git commit -m "feat(config): add configurable fft resolution"
```

### Task 2: Implement The Bounded FFT Aggregation And `nfft`-Aware Golden Cache

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/golden_reference.py`
- Modify: `app/service/analysis/judgment.py`
- Modify: `app/service/analysis/review_payloads.py`
- Test: `tests/test_dsp.py`
- Test: `tests/test_golden_reference.py`
- Test: `tests/test_judgment.py`
- Test: `tests/test_review_payloads.py`

- [ ] **Step 1: Write the failing DSP and golden-cache tests**

Add/extend tests for:

```python
freqs, levels = compute_fft_spectrum(long_signal, 48000.0, 4096)
assert len(freqs) == 2049

lead = np.sin(2 * np.pi * 200.0 * t_short)
tail = np.sin(2 * np.pi * 2000.0 * t_short)

fft_ref_1 = cache.ensure_fft(bundle, nfft=4096)
fft_ref_2 = cache.ensure_fft(bundle, nfft=2048)
assert len(fft_ref_1[0]) == 2049
assert len(fft_ref_2[0]) == 1025

fft_ref_3 = cache.ensure_fft(bundle, nfft=4096)
assert fft_ref_1[0] is fft_ref_3[0]

bundle_b = cache.bundle_for_channel(...)
fft_ref_b = cache.ensure_fft(bundle_b, nfft=4096)
assert fft_ref_b[0] is not fft_ref_1[0]

captured_windows = []
monkeypatch.setattr(dsp_module.np, "hanning", lambda n: np.ones(n, dtype=np.float64))
def fake_rfft(values):
    captured_windows.append(values.copy())
    if float(values[0]) == 0.0:
        return np.array([1.0 + 0j, 2.0 + 0j, 3.0 + 0j], dtype=np.complex128)
    return np.array([5.0 + 0j, 7.0 + 0j, 11.0 + 0j], dtype=np.complex128)

monkeypatch.setattr(dsp_module.np.fft, "rfft", fake_rfft)
_, mag_db = compute_fft_spectrum(np.arange(5, dtype=np.float64), 1.0, 4)
assert [window.tolist() for window in captured_windows] == [
    [0.0, 1.0, 2.0, 3.0],
    [1.0, 2.0, 3.0, 4.0],
]
np.testing.assert_allclose(
    10 ** (mag_db / 20.0),
    np.array([1.5, 2.25, 3.5], dtype=np.float64),
)

captured_windows.clear()
compute_fft_spectrum(np.arange(6, dtype=np.float64), 1.0, 4)
assert [window.tolist() for window in captured_windows] == [
    [0.0, 1.0, 2.0, 3.0],
    [2.0, 3.0, 4.0, 5.0],
]
```

Cover:

- short signals still zero-pad to `nfft`
- long signals use a bounded aggregated FFT and return `nfft // 2 + 1` bins
- long-signal aggregation uses the exact normative window-start sequence from the spec, including both the tail-append and tail-dedup cases
- `GoldenReferenceCache.ensure_fft(..., nfft=...)` caches by `nfft`
- repeated same-`nfft` calls reuse the cached FFT result object for that bundle/`nfft`
- different bundles with the same `nfft` do not share the same cached FFT object

- [ ] **Step 2: Run the failing DSP/golden tests**

Run:

```powershell
pytest tests/test_dsp.py tests/test_golden_reference.py -k "fft" -q
```

Expected: FAIL because the helper still truncates/uses native length and the golden cache has no `nfft`-aware boundary.

- [ ] **Step 3: Implement the new committed FFT helper and `nfft`-aware golden cache**

Update `compute_fft_spectrum()` so it uses the committed bounded contract:

```python
def compute_fft_spectrum(signal: np.ndarray, sample_rate: float, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    ...
    if sig.size <= nfft:
        ...
    hop = max(1, nfft // 2)
    starts = list(range(0, sig.size - nfft + 1, hop))
    tail_start = sig.size - nfft
    if not starts or starts[-1] != tail_start:
        starts.append(tail_start)
    linear_magnitudes = []
    for start in starts:
        windowed = sig[start:start + nfft] * np.hanning(nfft)
        spectrum = np.fft.rfft(windowed)
        linear_magnitudes.append(np.abs(spectrum) * 2.0 / nfft)
    mag = np.mean(np.vstack(linear_magnitudes), axis=0)
    ...
```

Average linear magnitudes across windows before converting to dB.

Update `GoldenReferenceCache.ensure_fft()`:

```python
def ensure_fft(
    self,
    bundle: GoldenReferenceBundle,
    *,
    nfft: int,
) -> tuple[np.ndarray, np.ndarray]:
    resolved_nfft = _validated_positive_nfft(nfft)
    cache_key = resolved_nfft
    ...
```

Replace the current single cached FFT slot with an `nfft`-keyed cache on the bundle, for example:

```python
@dataclass
class GoldenReferenceBundle:
    ...
    fft_by_nfft: dict[int, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
```

Update the existing judgment/review runtime call sites in this task so no unresolved
`ensure_fft(bundle)` call remains between Task 2 and Task 4:

- in `JudgmentEngine._check_fft_bands()`, pass the current measured raw-length `nfft`
- in `review_payloads._build_fft_chart()`, pass the current selected-window raw-length `nfft`
- update any affected FFT test doubles in `tests/test_judgment.py` and `tests/test_review_payloads.py` to accept the explicit `nfft=` keyword while preserving the current raw-length behavior until Task 4 switches them to `channel_config["fft_nfft"]`

Implementation notes:

- keep the one-sided frequency axis spanning `0 .. sample_rate / 2`
- do not change octave behavior
- keep measured/golden FFT axis construction identical for the same `sample_rate` and `nfft`
- for long-signal aggregation, apply the same `2.0 / nfft` linear-magnitude scaling used by the current helper before averaging and dB conversion
- keep the `ensure_fft(..., nfft=...)` boundary explicit in this task by passing the current raw-length `nfft` at the existing judgment/review call sites; Task 4 will change those call sites from raw length to `channel_config["fft_nfft"]`
- do not introduce the final `compute_fft_spectrum(...)` `ValueError` contract in this task; Task 4 will add it together with the required judgment/review soft-failure handling

- [ ] **Step 4: Re-run the DSP/golden tests and caller smoke tests**

Run:

```powershell
pytest tests/test_dsp.py tests/test_golden_reference.py tests/test_judgment.py tests/test_review_payloads.py -k "fft" -q
```

Expected: PASS because Task 2 must preserve existing runtime FFT callers while Task 4 is still pending.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/service/analysis/dsp.py app/service/analysis/golden_reference.py app/service/analysis/judgment.py app/service/analysis/review_payloads.py tests/test_dsp.py tests/test_golden_reference.py tests/test_judgment.py tests/test_review_payloads.py
git commit -m "feat(fft): add bounded fft aggregation and golden cache"
```

### Task 3: Move Live FFT To `fft_nfft` With A Rolling Buffer

**Files:**
- Modify: `app/service/analysis/pipeline.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_test_session.py`

- [ ] **Step 1: Write the failing live-pipeline tests**

Add/extend tests proving:

```python
cfg = _make_config(stft_nfft=512, fft_nfft=4096)
pipeline = AnalysisPipeline(channel_index=0, channel_config=cfg, sample_rate=48_000.0)
first_result = pipeline.process_chunk(np.ones(2048), elapsed_time=0.05)
assert first_result["fft"] is None

second = pipeline.process_chunk(np.ones(2048), elapsed_time=0.10)
assert second["fft"] is not None
assert second["fft"].frequencies.shape[0] == 4096 // 2 + 1

pipeline.reset()
assert pipeline._fft_buffer.size == 0

fft_frame = FftFrame(
    time=0.10,
    frequencies=np.array([0.0, 100.0], dtype=np.float64),
    magnitudes=np.array([1.0, 2.0], dtype=np.float64),
)
fake_pipeline.process_chunk.return_value = {
    "stft_frames": [],
    "stft": None,
    "spl": None,
    "octave": None,
    "fft": fft_frame,
}
session._on_chunk(np.ones((2048, 1), dtype=np.float32), 0)
assert bus.fft_updated.emit.call_args.args == (1, "factory-run-1", 0, fft_frame)
```

Cover:

- live FFT uses `fft_nfft`, not `stft_nfft`
- no live FFT frame is emitted until the rolling buffer reaches `fft_nfft`
- after that threshold, each subsequent chunk emits exactly one FFT frame for that channel
- `reset()` clears the rolling FFT buffer
- `TestSession` still re-emits live FFT frames through `fft_updated.emit(config_id, run_id, channel_index, frame)` without changing the signal shape

- [ ] **Step 2: Run the failing live-pipeline tests**

Run:

```powershell
pytest tests/test_pipeline.py tests/test_test_session.py -k "fft" -q
```

Expected: FAIL because the live pipeline still uses `stft_nfft` directly on the current chunk.

- [ ] **Step 3: Implement rolling live FFT buffering**

Update `AnalysisPipeline`:

```python
self._fft_nfft = int(channel_config["fft_nfft"])
self._fft_buffer = np.array([], dtype=np.float64)
...
self._fft_buffer = np.concatenate([self._fft_buffer, sig])
if self._fft_buffer.size > self._fft_nfft:
    self._fft_buffer = self._fft_buffer[-self._fft_nfft:]
if self._fft_buffer.size >= self._fft_nfft:
    freqs, mag_db = dsp.compute_fft_spectrum(self._fft_buffer, self.sample_rate, self._fft_nfft)
```

Implementation notes:

- keep STFT buffering unchanged
- live FFT uses the rolling window of most recent `fft_nfft` samples, not the full run history
- `reset()` must clear both STFT and FFT rolling state
- do not change the `fft_updated` EventBus signature or the `TestSession._on_chunk()` emission order/shape

- [ ] **Step 4: Re-run the live-pipeline tests**

Run:

```powershell
pytest tests/test_pipeline.py tests/test_test_session.py -k "fft" -q
```

Expected: PASS

- [ ] **Step 5: Commit Task 3**

```bash
git add app/service/analysis/pipeline.py tests/test_pipeline.py tests/test_test_session.py
git commit -m "feat(pipeline): use rolling fft resolution buffer"
```

### Task 4: Route `fft_nfft` Through Judgment And Committed Review Payloads

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/judgment.py`
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `app/service/analysis/golden_reference.py`
- Test: `tests/test_dsp.py`
- Test: `tests/test_judgment.py`
- Test: `tests/test_review_payloads.py`
- Test: `tests/test_test_manager/test_review_payload_emission.py`

- [ ] **Step 1: Write the failing judgment/review tests**

Add/extend tests so they assert:

```python
with pytest.raises(ValueError):
    compute_fft_spectrum(np.array([]), 44100.0, 4096)

with pytest.raises(ValueError):
    compute_fft_spectrum(np.ones(8), 0.0, 4096)

with pytest.raises(ValueError):
    compute_fft_spectrum(np.ones(8), float("nan"), 4096)

with pytest.raises(ValueError):
    compute_fft_spectrum(np.ones(8), 44100.0, 1024.5)

with pytest.raises(ValueError):
    compute_fft_spectrum(np.ones(8), 44100.0, 0)

assert fft_calls == [4096]
assert channel_result.fft_available is False
assert "empty" in (channel_result.fft_error_message or "").lower()
assert "fft" not in payload[0]
assert "FFT chart payload omitted" in caplog.text
```

Cover:

- judgment `_measure_fft(..., nfft=...)` receives `channel_config["fft_nfft"]`
- review `_measure_fft_levels(..., nfft=...)` receives `channel_config["fft_nfft"]`
- `compute_fft_spectrum(...)` now raises `ValueError` for empty input, non-finite/non-positive `sample_rate`, and invalid `nfft`
- measured-side FFT errors set `fft_available=False` and preserve soft failure in judgment
- measured-side FFT chart recompute errors omit only the affected `fft` payload item and log a warning
- golden FFT lookups pass `nfft=channel_config["fft_nfft"]`
- measured and golden FFT axes align under the same `sample_rate` and `fft_nfft`
- axis mismatches still surface through the existing unavailable/omitted payload behavior when a golden stub returns a different axis
- old tests that asserted native/full-window point counts are updated to the new bounded contract
- downstream factory-runtime review payload emission still preserves the summary payload when only the FFT chart item is omitted

- [ ] **Step 2: Run the failing judgment/review tests**

Run:

```powershell
pytest tests/test_dsp.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py -k "fft" -q
```

Expected: FAIL because committed judgment/review paths still derive FFT length from the raw signal length, the helper does not yet enforce the final invalid-input contract, and downstream review payload emission is not yet verified against the new omission behavior.

- [ ] **Step 3: Implement committed consumer wiring**

Make the `nfft` boundary explicit:

```python
def _measure_fft(self, sig: np.ndarray, sample_rate: float, *, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    freqs, mag_db = dsp.compute_fft_spectrum(sig, sample_rate, nfft)
    return np.asarray(freqs, dtype=np.float64), self._absolute_levels_db(mag_db)
```

And in `review_payloads.py`:

```python
def _measure_fft_levels(signal: np.ndarray, sample_rate: float, *, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    frequencies_hz, magnitudes_db = dsp.compute_fft_spectrum(signal, sample_rate, nfft)
    return np.asarray(frequencies_hz, dtype=np.float64), _absolute_levels_db(magnitudes_db)
```

Route `channel_config["fft_nfft"]` through:

- `JudgmentEngine._check_fft_bands()`
- `_build_fft_chart()`
- `GoldenReferenceCache.ensure_fft(bundle, nfft=...)`

Implementation notes:

- now add the final `compute_fft_spectrum(...)` `ValueError` contract for empty input, invalid `sample_rate`, and invalid `nfft`
- judgment measured-side FFT errors must soft-fail by setting `fft_available=False`
- review measured-side FFT errors must omit the `fft` chart payload item instead of aborting the full payload
- update the explicit `ensure_fft(..., nfft=...)` call sites from Task 2 so they now pass `channel_config["fft_nfft"]` instead of raw signal length
- keep payload keys and signal signatures unchanged

- [ ] **Step 4: Re-run the judgment/review tests**

Run:

```powershell
pytest tests/test_dsp.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py -k "fft" -q
```

Expected: PASS

- [ ] **Step 5: Run the focused FFT regression suite**

Run:

```powershell
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_dsp.py tests/test_pipeline.py tests/test_golden_reference.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py -q
```

Expected: PASS

- [ ] **Step 6: Run the full suite**

Run:

```powershell
pytest tests -q
```

Expected: PASS

- [ ] **Step 7: Commit Task 4**

```bash
git add app/service/analysis/dsp.py app/service/analysis/judgment.py app/service/analysis/review_payloads.py app/service/analysis/golden_reference.py tests/test_dsp.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py
git commit -m "feat(fft): route configurable resolution through analysis consumers"
```
