# Analysis Kernel Design

## Purpose

Replace the fake `TestService` with a real signal-processing pipeline that records audio, computes real-time analysis (STFT, SPL, octave bands, FFT spectrum), renders live results to the UI, and produces a final pass/fail judgment combining limit checks with AI scoring.

## Requirements (from brainstorming)

| # | Requirement |
|---|-------------|
| R1 | **Record + real-time display + final judgment** flow |
| R2 | Output device used for **monitoring playback only** (one channel) |
| R3 | Pass/fail = **SPL/FFT/octave band limit checks AND AI score** combined |
| R4 | Support **multiple configs running simultaneously** (different devices or same device with different channels) |
| R5 | AI runs **automatically after recording**; user can re-select a segment in the STFT figure for re-analysis |
| R6 | Triggers: **manual start** for now; architecture supports scan/PLC triggers later |
| R7 | Per-channel analysis config from `ChannelConfigTable` (nfft, overlap, weighting, time constant, limits, band limits) |
| R8 | `record_duration` defines total recording length; `analysis_duration` defines the window for final judgment |

## Architecture Overview

```
                         ┌─────────────┐
  trigger ──────────────▸│ TestManager  │ replaces fake TestService
                         └──────┬──────┘
                                │ creates per config_id
                         ┌──────▼──────┐
                         │ TestSession  │ one per running test
                         └──┬───┬───┬──┘
                            │   │   │
              ┌─────────────┘   │   └──────────────┐
              ▼                 ▼                   ▼
       ┌────────────┐  ┌──────────────┐   ┌──────────────┐
       │AudioRecorder│  │AnalysisPipe- │   │ AudioMonitor │
       │  (QThread)  │  │  line        │   │  (QThread)   │
       └──────┬─────┘  └──────┬───────┘   └──────────────┘
              │               │
     raw audio chunks    real-time results
     (numpy ndarray)     (STFT, SPL, octave, FFT)
              │               │
              ▼               ▼
         ring buffer      EventBus signals → UI
              │
              │ (after record_duration)
              ▼
       ┌──────────────┐
       │JudgmentEngine│
       │  • limits    │
       │  • verdict   │
       └──────┬───────┘
              │
              ▼
       ┌──────────────┐
       │ TestSession   │
       │  • AI call   │
       │  • combine   │
       └──────┬───────┘
              ▼
        result_changed → UI
```

## Components

### 1. TestManager (`app/service/analysis/test_manager.py`)

Replaces `TestService` in `services_container.py`. Exposes the same `start(workorder)` / `stop()` API so `ControlModule` needs zero changes.

**Workorder → config_id resolution:** The workorder dict may carry `config_id` (int) or `config` (name string). `WorkOrderModule` currently sends a config name via its free-text field. `TestManager` resolves the config name to `config_id` via `ConfigService.list_configs()`. If neither `config_id` nor a matching `config` name is found, `TestManager` logs an error and aborts.

**Responsibilities:**
- Manage a dict of `{config_id: TestSession}` for concurrent tests
- Route `start(workorder)` → extract `config_id` from workorder → create `TestSession`
- Route `stop()` → stop all sessions (or stop by config_id)
- Handle shared-device scenario: if two configs use the same input device, share one `AudioRecorder` between their `TestSession`s

**Interface:**
```python
class TestManager(QObject):
    def __init__(self, bus: EventBus, services: ServiceContainer): ...
    def start(self, workorder: dict): ...
    def stop(self, config_id: int | None = None): ...
    def is_running(self, config_id: int | None = None) -> bool: ...
```

### 2. TestSession (`app/service/analysis/test_session.py`)

One test run lifecycle for a single config.

**Responsibilities:**
- Load config via `ConfigService.load_config(config_id)`
- Load mic calibration from `DeviceTable.mic_cal`
- Create `AudioRecorder` (or receive shared one)
- Create `AnalysisPipeline` for each selected channel
- Create `AudioMonitor` if output device configured
- After `record_duration` seconds: stop recording, run `JudgmentEngine`
- Emit real-time + final results through EventBus
- Save audio to file for AI service and `AudioDataTable`

**Lifecycle:**
```
__init__ → _setup_pipelines() → [_on_chunk()...] → _run_judgment() → finished
```
Note: `TestManager` orchestrates the lifecycle — it calls `_setup_pipelines()` after construction, connects the recorder's `chunk_ready` signal to `_on_chunk()`, and calls `_run_judgment()` when recording completes.

**Signals emitted (via EventBus):**
- `status(str)` — "Recording...", "Analyzing...", "Idle"
- `log(str)` — detailed log lines
- `result_changed(dict)` — final verdict

**New signals needed on EventBus:**
```python
# Real-time analysis data (emitted per chunk, ~20 Hz)
stft_updated = pyqtSignal(int, int, object)    # config_id, channel_index, StftFrame
spl_updated = pyqtSignal(int, int, object)     # config_id, channel_index, SplFrame
octave_updated = pyqtSignal(int, int, object)  # config_id, channel_index, OctaveFrame
fft_updated = pyqtSignal(int, int, object)     # config_id, channel_index, FftFrame

# Recording progress
recording_progress = pyqtSignal(int, float)     # config_id, elapsed_seconds

# Test session lifecycle
test_started = pyqtSignal(int)                  # config_id
test_finished = pyqtSignal(int, dict)           # config_id, result_dict
```

### 3. AudioRecorder (`app/service/analysis/audio_recorder.py`)

Records multi-channel audio from sounddevice.

**Responsibilities:**
- Open `sd.InputStream` with the config's input device, sample rate, and channel count
- Accumulate audio into a pre-allocated numpy ring buffer (`record_duration * sample_rate` samples)
- Emit audio chunks (~50ms worth) via a Qt signal for real-time analysis
- After `record_duration` seconds, stop and signal completion
- Provide access to the full recorded buffer for judgment + file saving

**Key decisions:**
- **sounddevice callback mode** (not blocking read) — the callback fires on a PortAudio thread, puts data into a `queue.Queue`, and a QThread drains the queue to emit signals on the Qt thread
- Ring buffer is a simple numpy array with a write cursor; full buffer is available after recording ends
- For shared-device scenario: one recorder, multiple pipeline consumers

**Interface:**
```python
class AudioRecorder(QThread):
    chunk_ready = pyqtSignal(object, int)  # ndarray (n_samples, n_channels), chunk_index

    def __init__(self, device_name: str, hostapi: str, sample_rate: float,
                 n_channels: int, record_duration: float): ...
    def run(self): ...        # opens stream, runs event loop
    def stop(self): ...       # signals stop
    def get_buffer(self) -> np.ndarray: ...  # (total_samples, n_channels)
```

### 4. AudioMonitor (`app/service/analysis/audio_monitor.py`)

Plays back one channel of the recorded audio to the output device for monitoring.

**Responsibilities:**
- Open `sd.OutputStream` with the config's output device
- Receive audio chunks from `AudioRecorder.chunk_ready`
- Extract the selected monitoring channel and write to output stream

**Interface:**
```python
class AudioMonitor(QThread):
    def __init__(self, device_name: str, hostapi: str, sample_rate: float,
                 monitor_channel: int): ...
    def feed(self, chunk: np.ndarray): ...  # called from recorder's chunk_ready
    def run(self): ...
    def stop(self): ...
```

### 5. AnalysisPipeline (`app/service/analysis/pipeline.py`)

Real-time per-channel analysis. One instance per selected channel.

**Responsibilities:**
- Receive raw audio chunks from `AudioRecorder`
- Extract the relevant channel from the multi-channel chunk
- Maintain an overlap buffer for STFT windowing
- Compute: STFT frame, SPL value, octave bands, FFT spectrum
- Apply mic calibration deviation to raw data
- Emit results as dataclass objects via callback/signal

**Interface:**
```python
class AnalysisPipeline:
    def __init__(self, channel_index: int, channel_config: dict,
                 sample_rate: float, mic_deviation: float = 0.0): ...
    def process_chunk(self, chunk_mono: np.ndarray) -> AnalysisFrame: ...
    def reset(self): ...
```

**AnalysisFrame dataclass:**
```python
@dataclass
class StftFrame:
    time: float              # timestamp in seconds
    frequencies: np.ndarray  # freq axis (Hz)
    magnitudes: np.ndarray   # magnitude spectrum (dB)

@dataclass
class SplFrame:
    time: float
    spl_db: float            # weighted SPL value
    weighting: str           # "A", "C", or "Z"

@dataclass
class OctaveFrame:
    time: float
    center_freqs: np.ndarray  # octave band center frequencies
    levels: np.ndarray        # dB levels per band
    weighting: str

@dataclass
class FftFrame:
    time: float
    frequencies: np.ndarray
    magnitudes: np.ndarray    # dB
```

### 6. DSP Functions (`app/service/analysis/dsp.py`)

Pure stateless functions. Easily unit-testable.

**Functions:**

```python
# STFT
def compute_stft(signal: np.ndarray, sample_rate: float, nfft: int,
                 overlap: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (times, frequencies, Zxx_magnitude_db)."""

# Weighting filters
def a_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Returns A-weighting gains (dB) for given frequencies."""

def c_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Returns C-weighting gains (dB) for given frequencies."""

# SPL
def compute_spl(signal: np.ndarray, sample_rate: float,
                weighting: str, time_constant: float) -> float:
    """Returns weighted SPL in dB."""

# Octave bands (1/3 octave)
def compute_octave_bands(signal: np.ndarray, sample_rate: float,
                         weighting: str) -> tuple[np.ndarray, np.ndarray]:
    """Returns (center_frequencies, levels_db)."""

# FFT spectrum
def compute_fft_spectrum(signal: np.ndarray, sample_rate: float,
                         nfft: int) -> tuple[np.ndarray, np.ndarray]:
    """Returns (frequencies, magnitudes_db)."""

# Mic calibration
def apply_mic_calibration(signal: np.ndarray, sensitivity_pa_per_v: float) -> np.ndarray:
    """Apply microphone calibration.

    The existing mic_cal stores deviation_value as a string in Pa/V units
    (sensitivity, not dB). This function converts from voltage to pressure:
      pressure = signal * sensitivity_pa_per_v
    If sensitivity is 0 or not set, no calibration is applied (raw signal used).
    """
```

**Implementation notes:**
- STFT: `scipy.signal.stft` with Hann window
- A-weighting: IEC 61672-1 formula applied in frequency domain
- C-weighting: IEC 61672-1 formula
- Z-weighting: no filter (flat)
- SPL: RMS with exponential time-weighting (Fast=0.125s, Slow=1.0s) applied as IIR filter
- Octave bands: 1/3-octave filter bank, IEC 61260 center frequencies
- FFT: `numpy.fft.rfft`, convert to dB

### 7. JudgmentEngine (`app/service/analysis/judgment.py`)

Post-recording analysis and pass/fail determination.

**Responsibilities:**
- Extract `analysis_duration` window from the full recording buffer (default: last N seconds)
- Run full analysis on the window (using `dsp.py` functions)
- Check SPL limits per channel: `spl_lower_limit <= spl <= spl_upper_limit`
- Check FFT band limits per channel: each band's level within `[lower, upper]`
- Check octave band limits per channel: each band's level within `[lower, upper]`
- Validate AI binding existence (via `AiService.resolve_binding()`)
- Return `JudgmentResult` with `ai_pass=None` — actual `AiService.run_ai_analysis()` call is made by `TestSession`, which owns the required context (test_run_id, segments list, ai_runs_dir). TestSession patches `ai_pass`/`ai_score` into the result and re-derives `overall`.
- Combine: all limit checks must pass AND AI must pass (when available) → final verdict. When `ai_pass is None` (AI not configured or not yet run), verdict is based on limit checks alone.

**Interface:**
```python
class JudgmentEngine:
    def __init__(self, ai_service: AiService): ...

    def judge(self, config: dict, buffer: np.ndarray, sample_rate: float,
              mic_cal: dict, audio_file_path: str) -> JudgmentResult: ...

    def rejudge(self, config: dict, buffer: np.ndarray, sample_rate: float,
                mic_cal: dict, audio_file_path: str,
                start_sec: float, end_sec: float) -> JudgmentResult: ...
```

**JudgmentResult:**
```python
@dataclass
class JudgmentResult:
    overall: str                    # "合格" / "不合格"
    limit_pass: bool                # all limit checks passed
    ai_pass: bool | None            # AI passed (None if AI not configured)
    ai_score: float | None
    channel_results: list[ChannelJudgment]
    analysis_window: tuple[float, float]  # (start_sec, end_sec)

@dataclass
class ChannelJudgment:
    channel_index: int
    spl_db: float
    spl_pass: bool
    fft_band_results: list[BandResult]
    oct_band_results: list[BandResult]

@dataclass
class BandResult:
    freq: float
    level: float
    lower: float
    upper: float
    passed: bool
```

## EventBus Signal Additions

Add to `EventBus` in `services_container.py`:

```python
# Real-time analysis frames (~20 Hz per channel)
stft_updated = pyqtSignal(int, int, object)     # config_id, channel_idx, StftFrame
spl_updated = pyqtSignal(int, int, object)       # config_id, channel_idx, SplFrame
octave_updated = pyqtSignal(int, int, object)    # config_id, channel_idx, OctaveFrame
fft_updated = pyqtSignal(int, int, object)       # config_id, channel_idx, FftFrame

# Session lifecycle
recording_progress = pyqtSignal(int, float)      # config_id, elapsed_sec
test_started = pyqtSignal(int)                   # config_id
test_finished = pyqtSignal(int, dict)            # config_id, JudgmentResult as dict
```

## ServiceContainer Changes

```python
# Replace:
self.test = TestService(self.bus)

# With:
from app.service.analysis.test_manager import TestManager
self.test = TestManager(self.bus, self)
```

`TestManager` keeps the same `start(workorder)` / `stop()` API so `ControlModule` needs no changes.

## File Structure

```
app/service/analysis/
├── __init__.py
├── test_manager.py       # TestManager (replaces TestService)
├── test_session.py       # TestSession (one test run)
├── audio_recorder.py     # sounddevice recording + ring buffer
├── audio_monitor.py      # single-channel monitoring playback
├── pipeline.py           # AnalysisPipeline (per-channel real-time)
├── dsp.py                # Pure DSP functions (STFT, SPL, octave, FFT, weighting)
├── judgment.py           # JudgmentEngine logic
└── frames.py             # All dataclasses: StftFrame, SplFrame, OctaveFrame, FftFrame, BandResult, ChannelJudgment, JudgmentResult
```

## Multi-Config / Shared Device

When `TestManager.start()` is called for two configs that use the same `(input_device_name, input_device_hostapi, input_device_samplerate)`:

1. The first config creates an `AudioRecorder`
2. The second config reuses the same `AudioRecorder` (looked up by device key)
3. Each config has its own set of `AnalysisPipeline` instances for its selected channels
4. Each config has its own `TestSession` lifecycle and `JudgmentEngine`
5. The `AudioRecorder` keeps running until all sessions using it have finished

When configs use different devices, each gets its own `AudioRecorder`.

**Shared recorder with different `record_duration`:** The recorder uses the **maximum** `record_duration` across all attached sessions. Each session tracks its own elapsed time independently and triggers its own judgment when its `record_duration` expires. The recorder's ring buffer is sized for the maximum duration. When the last session finishes, the recorder stops.

**Channel count:** The shared recorder always opens the device with the **full device channel count** (`max_input_channels`), not just the channels selected by any one config. Each `AnalysisPipeline` extracts its channel from the multi-channel buffer by index.

## Audio File Persistence

After recording completes:
1. Save the full buffer as a WAV file to a configured directory (e.g. `data/audio/`)
2. Insert a record into `AudioDataTable` with metadata
3. Pass the file path to `AiService.run_ai_analysis()` as a segment

File naming: `{config_id}_{timestamp}_{barcode_or_uuid}.wav`

**AudioDataTable field mapping:**
- `audio_data_id`: UUID string (generated)
- `file_path`: absolute path to saved WAV
- `product_model`: from `workorder.get("model", "")`
- `sample_rate`: device's actual sample rate (int)
- `record_date`: ISO timestamp string
- `labels`: JSON string or null
- `barcode`: from `workorder.get("sn", "")`

**AI segment contract:** The segments list passed to `AiService.run_ai_analysis()`:
```python
segments = [{
    "segment_id": audio_data_id,
    "role": "device_1",
    "audio_path": wav_file_path,
    "channels": selected_channels,       # e.g. [0, 1]
    "duration_ms": int(analysis_duration * 1000),
    "sample_rate": int(sample_rate),
    "metadata": {},
}]
```

**test_run_id generation:** `f"run_{config_id}_{timestamp_ms}"` — unique per test invocation.

## Re-analysis Flow

1. User selects time range in STFT visualization (future UI feature)
2. UI calls `TestManager.rejudge(config_id, start_sec, end_sec)`
3. `TestSession` (or stored buffer) re-extracts the window
4. `JudgmentEngine.rejudge()` re-runs limit checks on the new window; `TestSession` re-runs AI if applicable
5. New result emitted via `test_finished` signal

For this to work, `TestSession` keeps the recording buffer in memory until the user starts a new test or explicitly discards it.

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Device not found / open fails | Log error, emit `status("Error: device not found")`, don't crash |
| Recording interrupted (user stop) | Save partial buffer, skip judgment, emit `status("Stopped")` |
| AI service fails | Limit-check result still valid, `ai_pass=None`, log warning |
| Channel config missing for a channel | Use defaults from `_CHANNEL_DEFAULTS` in config_service |
| Sample rate mismatch | Use device's actual sample rate, log warning if differs from config |

## Dependencies

All already in `requirements.txt`:
- `numpy` — array ops, FFT
- `scipy` — STFT (`scipy.signal.stft`), filter design
- `sounddevice` — audio I/O
- `PyQt6` — QThread, signals

## Testing Strategy

- **`dsp.py`**: Pure function unit tests with known signals (sine waves, white noise). Verify A-weighting curve, STFT output shape, SPL values against known references.
- **`judgment.py`**: Unit tests with mock AI service. Verify limit checks, combined verdict logic, AI deferral pattern (returns None for AI fields).
- **`pipeline.py`**: Feed known chunks, verify output frame shapes and values.
- **`audio_recorder.py`**: Integration test with loopback or mock sounddevice.
- **`test_manager.py`**: Integration test verifying start/stop lifecycle, signal emissions.

## Implementation Order

1. `frames.py` — dataclasses (no dependencies)
2. `dsp.py` — pure DSP functions + unit tests
3. `pipeline.py` — real-time analysis pipeline + tests
4. `audio_recorder.py` — sounddevice recording
5. `audio_monitor.py` — monitoring playback
6. `judgment.py` — limit checks + AI integration + tests
7. `test_session.py` — session lifecycle
8. `test_manager.py` — manager + ServiceContainer integration
9. EventBus signal additions
10. Wire up in `services_container.py`
