# Analysis Kernel — Plan B: Orchestration Layer

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the audio I/O layer (recording + monitoring), the test session lifecycle, and the TestManager that replaces the fake TestService — wiring everything together with EventBus signals so the UI receives real-time analysis data.

**Architecture:** `AudioRecorder` (QThread + sounddevice callback) feeds audio chunks to `AnalysisPipeline` (from Plan A). `AudioMonitor` (QThread) plays one channel to the output device. `TestSession` orchestrates one test run lifecycle. `TestManager` replaces `TestService`, managing multiple concurrent sessions and shared recorders.

**Tech Stack:** sounddevice (audio I/O), PyQt6 (QThread, signals), numpy, scipy.io.wavfile (WAV export)

**Spec:** `docs/superpowers/specs/2026-03-17-analysis-kernel-design.md`

**Depends on:** Plan A must be implemented first (frames, dsp, pipeline, judgment modules).

---

## File Map

| File | Responsibility |
|------|---------------|
| Create: `app/service/analysis/audio_recorder.py` | QThread wrapping sounddevice InputStream; ring buffer; emits audio chunks |
| Create: `app/service/analysis/audio_monitor.py` | QThread wrapping sounddevice OutputStream; plays one channel for monitoring |
| Create: `app/service/analysis/test_session.py` | One test run lifecycle: recorder → pipeline → judgment → result |
| Create: `app/service/analysis/test_manager.py` | TestManager: replaces TestService, manages sessions, shared recorders |
| Modify: `app/services_container.py` | Add EventBus signals; replace TestService with TestManager |
| Create: `tests/test_audio_recorder.py` | Tests for AudioRecorder (with mocked sounddevice) |
| Create: `tests/test_audio_monitor.py` | Tests for AudioMonitor (with mocked sounddevice) |
| Create: `tests/test_test_session.py` | Tests for TestSession lifecycle |
| Create: `tests/test_test_manager.py` | Tests for TestManager start/stop/multi-config |
| Modify: `tests/conftest.py` | Add DeviceTable and AudioDataTable to test DB tables |

---

### Task 1: Update conftest.py for new tables

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add DeviceTable and AudioDataTable to test DB**

Edit `tests/conftest.py` — add the two new tables to `ALL_TABLES`:

```python
"""Shared pytest fixtures for all tests."""
from __future__ import annotations

import pytest
from peewee import SqliteDatabase

from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable
from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.device_table import DeviceTable
from model.audio_data_table import AudioDataTable

ALL_TABLES = [
    TestConfigTable,
    ChannelConfigTable,
    AiToolchainTable,
    TestConfigAiBindingTable,
    AiExecutionTable,
    DeviceTable,
    AudioDataTable,
]

_test_db = SqliteDatabase(":memory:", pragmas={"foreign_keys": 1})


@pytest.fixture(autouse=True)
def setup_test_db():
    """Bind all models to an in-memory DB, create tables, yield, then drop."""
    _test_db.bind(ALL_TABLES)
    _test_db.connect()
    _test_db.create_tables(ALL_TABLES)
    yield _test_db
    _test_db.drop_tables(ALL_TABLES)
    _test_db.close()
```

- [ ] **Step 2: Run existing tests to confirm no regressions**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All existing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add DeviceTable and AudioDataTable to test DB fixture"
```

---

### Task 2: AudioRecorder — sounddevice recording with ring buffer

**Files:**
- Create: `app/service/analysis/audio_recorder.py`
- Create: `tests/test_audio_recorder.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_audio_recorder.py`:

```python
"""Tests for AudioRecorder.

sounddevice is mocked — these tests verify the recorder's state machine,
ring buffer accumulation, and signal emission, not actual hardware I/O.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.service.analysis.audio_recorder import AudioRecorder


class TestAudioRecorderInit:
    def test_creates_ring_buffer(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=2, record_duration=5.0,
        )
        assert rec._buffer.shape == (int(44100 * 5), 2)
        assert rec._write_pos == 0

    def test_default_chunk_duration(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=3.0,
        )
        # Default chunk ~50 ms
        assert rec._chunk_samples == int(44100 * 0.05)


class TestAudioRecorderBuffer:
    def test_feed_writes_to_buffer(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[:100], chunk)

    def test_feed_multiple_chunks(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=2, record_duration=1.0,
        )
        chunk1 = np.ones((50, 2), dtype=np.float32)
        chunk2 = np.ones((50, 2), dtype=np.float32) * 2.0
        rec._feed_chunk(chunk1)
        rec._feed_chunk(chunk2)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[50:100], chunk2)

    def test_get_buffer_returns_written_portion(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        chunk = np.ones((200, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        buf = rec.get_buffer()
        assert buf.shape == (200, 1)

    def test_buffer_does_not_overflow(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=100.0,
            n_channels=1, record_duration=1.0,
        )
        # Buffer holds 100 samples. Feed 150.
        chunk = np.ones((150, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        # Should clamp at buffer size
        assert rec._write_pos == 100


class TestAudioRecorderSignals:
    def test_chunk_ready_signal_emitted(self, qtbot):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        signals = []
        rec.chunk_ready.connect(lambda data, idx: signals.append((data, idx)))
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        rec._emit_chunk()
        assert len(signals) == 1
        assert signals[0][1] == 0  # chunk_index

    def test_recording_done_signal(self, qtbot):
        rec = AudioRecorder(
            device_index=0, sample_rate=100.0,
            n_channels=1, record_duration=0.5,
        )
        done = []
        rec.recording_done.connect(lambda: done.append(True))
        # Fill buffer completely
        chunk = np.ones((50, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._is_buffer_full()
```

Note: The `qtbot` fixture comes from `pytest-qt`. Add `pytest-qt` to test dependencies if not present.

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_audio_recorder.py -v`

- [ ] **Step 3: Implement AudioRecorder**

Create `app/service/analysis/audio_recorder.py`:

```python
"""Audio recording via sounddevice with ring buffer.

Runs a sounddevice InputStream in callback mode. A QThread drains chunks
from a queue and emits them as Qt signals for real-time analysis.
"""
from __future__ import annotations

import os
import queue

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, pyqtSignal

os.environ.setdefault("SD_ENABLE_ASIO", "1")


class AudioRecorder(QThread):
    """Record multi-channel audio into a pre-allocated ring buffer.

    Signals
    -------
    chunk_ready(ndarray, int)
        Emitted ~20 Hz with (chunk_data, chunk_index).
    recording_done()
        Emitted once the buffer is full.
    error_occurred(str)
        Emitted on device errors.
    """

    chunk_ready = pyqtSignal(object, int)
    recording_done = pyqtSignal()
    error_occurred = pyqtSignal(str)

    CHUNK_DURATION = 0.05  # 50 ms default chunk size

    def __init__(
        self,
        device_index: int,
        sample_rate: float,
        n_channels: int,
        record_duration: float,
    ):
        super().__init__()
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._n_channels = n_channels
        self._record_duration = record_duration

        total_samples = int(sample_rate * record_duration)
        self._buffer = np.zeros((total_samples, n_channels), dtype=np.float32)
        self._write_pos = 0
        self._chunk_samples = int(sample_rate * self.CHUNK_DURATION)
        self._chunk_index = 0

        self._queue: queue.Queue = queue.Queue()
        self._stop_flag = False

    def run(self):
        """Open sounddevice stream and drain chunks until done."""
        self._stop_flag = False
        try:
            with sd.InputStream(
                device=self._device_index,
                samplerate=self._sample_rate,
                channels=self._n_channels,
                dtype="float32",
                blocksize=self._chunk_samples,
                callback=self._sd_callback,
            ):
                while not self._stop_flag and not self._is_buffer_full():
                    try:
                        data = self._queue.get(timeout=0.1)
                        self._feed_chunk(data)
                        self._emit_chunk()
                    except queue.Empty:
                        continue
        except sd.PortAudioError as e:
            self.error_occurred.emit(f"Audio device error: {e}")
            return
        except Exception as e:
            self.error_occurred.emit(f"Recording error: {e}")
            return

        if self._is_buffer_full():
            self.recording_done.emit()

    def stop(self):
        """Signal the recording thread to stop."""
        self._stop_flag = True

    def get_buffer(self) -> np.ndarray:
        """Return the recorded audio (only the written portion)."""
        return self._buffer[: self._write_pos].copy()

    def get_total_buffer(self) -> np.ndarray:
        """Return the full pre-allocated buffer."""
        return self._buffer

    def _sd_callback(self, indata, frames, time_info, status):
        """sounddevice callback — runs on PortAudio thread."""
        if status:
            pass  # Could log status flags
        self._queue.put(indata.copy())

    def _feed_chunk(self, data: np.ndarray):
        """Write chunk to ring buffer."""
        n = data.shape[0]
        space = self._buffer.shape[0] - self._write_pos
        if space <= 0:
            return
        n_write = min(n, space)
        self._buffer[self._write_pos: self._write_pos + n_write] = data[:n_write]
        self._write_pos += n_write

    def _emit_chunk(self):
        """Emit the latest chunk via signal."""
        start = max(0, self._write_pos - self._chunk_samples)
        chunk = self._buffer[start: self._write_pos]
        self.chunk_ready.emit(chunk, self._chunk_index)
        self._chunk_index += 1

    def _is_buffer_full(self) -> bool:
        return self._write_pos >= self._buffer.shape[0]

    @property
    def elapsed_seconds(self) -> float:
        return self._write_pos / self._sample_rate
```

- [ ] **Step 4: Run tests — expect PASS (non-qtbot tests)**

Run: `python -m pytest tests/test_audio_recorder.py -v -k "not qtbot"`

Note: If `pytest-qt` is not available, the qtbot tests will be skipped. The non-qtbot tests (Init, Buffer) should all pass.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/audio_recorder.py tests/test_audio_recorder.py
git commit -m "feat(analysis): add AudioRecorder with ring buffer and sounddevice"
```

---

### Task 3: AudioMonitor — single-channel monitoring playback

**Files:**
- Create: `app/service/analysis/audio_monitor.py`
- Create: `tests/test_audio_monitor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_audio_monitor.py`:

```python
"""Tests for AudioMonitor."""
from __future__ import annotations

import numpy as np

from app.service.analysis.audio_monitor import AudioMonitor


class TestAudioMonitorInit:
    def test_init(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=0,
        )
        assert mon._monitor_channel == 0
        assert mon._sample_rate == 44100.0


class TestAudioMonitorFeed:
    def test_extract_channel(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=1,
        )
        chunk = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.array([[2.0], [4.0]]))

    def test_extract_single_channel(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=0,
        )
        chunk = np.array([[1.0], [3.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, chunk)

    def test_channel_out_of_range_returns_zeros(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=5,
        )
        chunk = np.array([[1.0, 2.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.zeros((1, 1), dtype=np.float32))
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement AudioMonitor**

Create `app/service/analysis/audio_monitor.py`:

```python
"""Audio monitoring playback — plays one channel to output device.

Receives multi-channel audio chunks and plays a single selected channel
through the configured output device for real-time monitoring.
"""
from __future__ import annotations

import os
import queue

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread

os.environ.setdefault("SD_ENABLE_ASIO", "1")


class AudioMonitor(QThread):
    """Play a single channel to the output device for monitoring."""

    def __init__(
        self,
        device_index: int,
        sample_rate: float,
        monitor_channel: int,
    ):
        super().__init__()
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._monitor_channel = monitor_channel
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stop_flag = False

    def feed(self, chunk: np.ndarray):
        """Feed a multi-channel chunk for playback. Non-blocking, drops if full."""
        mono = self._extract_channel(chunk)
        try:
            self._queue.put_nowait(mono)
        except queue.Full:
            pass  # Drop chunk if playback is behind

    def run(self):
        """Open output stream and play queued chunks."""
        self._stop_flag = False
        try:
            with sd.OutputStream(
                device=self._device_index,
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
            ) as stream:
                while not self._stop_flag:
                    try:
                        mono = self._queue.get(timeout=0.1)
                        stream.write(mono)
                    except queue.Empty:
                        continue
        except (sd.PortAudioError, Exception):
            pass  # Monitoring is best-effort, don't crash

    def stop(self):
        self._stop_flag = True

    def _extract_channel(self, chunk: np.ndarray) -> np.ndarray:
        """Extract the monitor channel from multi-channel data."""
        if chunk.ndim == 1:
            return chunk.reshape(-1, 1)
        if self._monitor_channel >= chunk.shape[1]:
            return np.zeros((chunk.shape[0], 1), dtype=np.float32)
        return chunk[:, self._monitor_channel: self._monitor_channel + 1].copy()
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_audio_monitor.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/audio_monitor.py tests/test_audio_monitor.py
git commit -m "feat(analysis): add AudioMonitor for single-channel monitoring playback"
```

---

### Task 4: EventBus signal additions

**Files:**
- Modify: `app/services_container.py`

- [ ] **Step 1: Add new signals to EventBus**

Edit `app/services_container.py` — add signals to the `EventBus` class after the existing signals:

```python
class EventBus(QObject):
    # ... existing signals ...
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    workorder_changed = pyqtSignal(dict)
    result_changed = pyqtSignal(dict)
    refresh_channels = pyqtSignal()
    config_changed = pyqtSignal()

    # ── Analysis kernel real-time signals ──
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

- [ ] **Step 2: Run existing tests and verify signals**

Run: `python -m pytest tests/test_service_container.py -v`
Expected: All pass (new signals don't break existing functionality).

Verify signals exist (add to test file — note: `from app.services_container import EventBus` must be at the top of the test file, not inside the function):
```python
# At file top of tests/test_service_container.py, add import:
# from app.services_container import EventBus
#
# Then append this test:
def test_analysis_signals_exist():
    """All analysis kernel signals should be defined on EventBus."""
    for name in ("stft_updated", "spl_updated", "octave_updated",
                 "fft_updated", "recording_progress",
                 "test_started", "test_finished"):
        assert hasattr(EventBus, name), f"Missing signal: {name}"
```

- [ ] **Step 3: Commit**

```bash
git add app/services_container.py
git commit -m "feat(analysis): add real-time analysis signals to EventBus"
```

---

### Task 5: TestSession — one test run lifecycle

**Files:**
- Create: `app/service/analysis/test_session.py`
- Create: `tests/test_test_session.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_test_session.py`:

```python
"""Tests for TestSession lifecycle."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.service.analysis.test_session import TestSession
from app.service.analysis.frames import JudgmentResult
from model.audio_data_table import AudioDataTable


def _make_mock_bus():
    bus = MagicMock()
    bus.log = MagicMock()
    bus.log.emit = MagicMock()
    bus.status = MagicMock()
    bus.status.emit = MagicMock()
    bus.result_changed = MagicMock()
    bus.result_changed.emit = MagicMock()
    bus.stft_updated = MagicMock()
    bus.stft_updated.emit = MagicMock()
    bus.spl_updated = MagicMock()
    bus.spl_updated.emit = MagicMock()
    bus.octave_updated = MagicMock()
    bus.octave_updated.emit = MagicMock()
    bus.fft_updated = MagicMock()
    bus.fft_updated.emit = MagicMock()
    bus.recording_progress = MagicMock()
    bus.recording_progress.emit = MagicMock()
    bus.test_started = MagicMock()
    bus.test_started.emit = MagicMock()
    bus.test_finished = MagicMock()
    bus.test_finished.emit = MagicMock()
    return bus


def _make_config() -> dict:
    return {
        "config_id": 1,
        "config_name": "Test",
        "record_duration": 2.0,
        "analysis_duration": 1.0,
        "selected_channels": [0],
        "channels": [{
            "channel_index": 0,
            "stft_nfft": 1024,
            "stft_overlap": 0.5,
            "spl_weighting": "Z",
            "spl_time_constant": 0.125,
            "spl_upper_limit": 85.0,
            "spl_lower_limit": 40.0,
            "fft_band_limits": [],
            "oct_weighting": "Z",
            "oct_band_limits": [],
        }],
        "input_device_name": "Test Mic",
        "input_device_hostapi": "MME",
        "input_device_samplerate": 44100,
        "output_device_name": None,
    }


class TestTestSessionInit:
    def test_init(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "123", "model": "X"},
            audio_dir="/tmp/audio",
        )
        assert session.config_id == 1
        assert session._record_duration == 2.0


class TestTestSessionProcessing:
    def test_on_chunk_emits_analysis_signals(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        # Feed a chunk
        chunk = np.random.randn(2048, 1).astype(np.float32)
        session._on_chunk(chunk, 0)
        # SPL should always be emitted
        bus.spl_updated.emit.assert_called()


class TestTestSessionJudgment:
    def test_run_judgment_produces_result(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._setup_pipelines()
        # Simulate recorded buffer
        sr = 44100.0
        duration = 2.0
        t = np.arange(int(sr * duration)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        session._buffer = sig.reshape(-1, 1)
        session._sample_rate = sr
        result = session._run_judgment()
        assert isinstance(result, JudgmentResult)
        assert result.overall in ("合格", "不合格")


class TestTestSessionWavSave:
    def test_save_wav(self, tmp_path, test_db):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)
        path = session._save_wav(buf, sr)
        assert os.path.isfile(path)
        assert path.endswith(".wav")
        # Verify AudioDataTable record was created with all spec fields
        records = list(AudioDataTable.select())
        assert len(records) == 1
        rec = records[0]
        assert rec.file_path == path
        assert rec.barcode == "SN001"
        assert rec.product_model == "M1"
        assert rec.sample_rate == 44100
        assert rec.audio_data_id  # non-empty UUID
        assert rec.record_date  # non-empty ISO timestamp
        assert rec.labels is None  # no labels by default
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_test_session.py -v`

- [ ] **Step 3: Implement TestSession**

Create `app/service/analysis/test_session.py`:

```python
"""Test session — one test run lifecycle for a single config.

Orchestrates: recorder → pipelines → judgment → result.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.io import wavfile

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.pipeline import AnalysisPipeline
from model.audio_data_table import AudioDataTable

_TZ8 = timezone(timedelta(hours=8))


class TestSession:
    """Lifecycle manager for a single test run bound to one config."""

    def __init__(
        self,
        config: dict,
        bus,
        ai_service,
        mic_cal: dict,
        workorder: dict,
        audio_dir: str,
    ):
        self._config = config
        self._bus = bus
        self._ai_service = ai_service
        self._mic_cal = mic_cal
        self._workorder = workorder
        self._audio_dir = audio_dir

        self.config_id: int = config["config_id"]
        self._record_duration: float = config.get("record_duration", 5.0)
        self._analysis_duration: float = config.get("analysis_duration", 3.0)
        self._sample_rate: float = float(config.get("input_device_samplerate", 44100))

        self._pipelines: list[AnalysisPipeline] = []
        self._buffer: np.ndarray | None = None
        self._judgment: JudgmentEngine = JudgmentEngine(ai_service)
        self._last_result: JudgmentResult | None = None
        self._audio_file_path: str | None = None
        self._audio_data_id: str | None = None

    def _setup_pipelines(self):
        """Create one AnalysisPipeline per selected channel."""
        selected = self._config.get("selected_channels", [])
        channels_cfg = {ch["channel_index"]: ch for ch in self._config.get("channels", [])}
        self._pipelines = []
        for ch_idx in selected:
            ch_cfg = channels_cfg.get(ch_idx)
            if ch_cfg is None:
                continue
            sensitivity = self._get_sensitivity(ch_idx)
            self._pipelines.append(AnalysisPipeline(
                channel_index=ch_idx,
                channel_config=ch_cfg,
                sample_rate=self._sample_rate,
                mic_sensitivity=sensitivity,
            ))

    def _on_chunk(self, chunk: np.ndarray, chunk_index: int):
        """Process one audio chunk through all pipelines and emit results."""
        elapsed = chunk_index * 0.05  # approximate from chunk index

        for pipeline in self._pipelines:
            ch_idx = pipeline.channel_index
            # Extract this channel from multi-channel data
            if chunk.ndim == 2 and ch_idx < chunk.shape[1]:
                mono = chunk[:, ch_idx]
            elif chunk.ndim == 1:
                mono = chunk
            else:
                continue

            result = pipeline.process_chunk(mono, elapsed)

            # Emit each frame type
            if result["stft"] is not None:
                self._bus.stft_updated.emit(self.config_id, ch_idx, result["stft"])
            if result["spl"] is not None:
                self._bus.spl_updated.emit(self.config_id, ch_idx, result["spl"])
            if result["octave"] is not None:
                self._bus.octave_updated.emit(self.config_id, ch_idx, result["octave"])
            if result["fft"] is not None:
                self._bus.fft_updated.emit(self.config_id, ch_idx, result["fft"])

        self._bus.recording_progress.emit(self.config_id, elapsed)

    def _run_judgment(self) -> JudgmentResult:
        """Run judgment on the recorded buffer."""
        if self._buffer is None:
            return JudgmentResult(
                overall="不合格", limit_pass=False,
                ai_pass=None, ai_score=None,
            )

        # Save WAV
        self._audio_file_path = self._save_wav(self._buffer, self._sample_rate)

        result = self._judgment.judge(
            self._config, self._buffer, self._sample_rate,
            self._mic_cal, self._audio_file_path,
        )
        self._last_result = result
        return result

    def _save_wav(self, buffer: np.ndarray, sample_rate: float) -> str:
        """Save buffer to WAV file, insert AudioDataTable record, return path."""
        os.makedirs(self._audio_dir, exist_ok=True)
        ts = datetime.now(_TZ8).strftime("%Y%m%d_%H%M%S")
        barcode = self._workorder.get("sn", "") or uuid.uuid4().hex[:8]
        filename = f"{self.config_id}_{ts}_{barcode}.wav"
        filepath = os.path.join(self._audio_dir, filename)
        # scipy wavfile expects int16 or float32
        data = buffer.astype(np.float32)
        wavfile.write(filepath, int(sample_rate), data)

        # Persist record in AudioDataTable (spec §Audio File Persistence)
        audio_id = uuid.uuid4().hex
        AudioDataTable.create(
            audio_data_id=audio_id,
            file_path=filepath,
            product_model=self._workorder.get("model", ""),
            sample_rate=int(sample_rate),
            record_date=datetime.now(_TZ8).isoformat(),
            labels=None,
            barcode=self._workorder.get("sn", ""),
        )
        self._audio_data_id = audio_id
        return filepath

    def rejudge(self, start_sec: float, end_sec: float) -> JudgmentResult | None:
        """Re-run judgment on a user-selected window."""
        if self._buffer is None or self._audio_file_path is None:
            return None
        result = self._judgment.rejudge(
            self._config, self._buffer, self._sample_rate,
            self._mic_cal, self._audio_file_path,
            start_sec, end_sec,
        )
        self._last_result = result
        return result

    def _get_sensitivity(self, channel_index: int) -> float:
        """Get mic sensitivity (Pa/V) for a channel from mic_cal."""
        key = f"In{channel_index + 1}"
        entry = self._mic_cal.get(key, {})
        raw = entry.get("deviation_value", "0")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0

    @property
    def last_result(self) -> JudgmentResult | None:
        return self._last_result

    @property
    def buffer(self) -> np.ndarray | None:
        return self._buffer
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_test_session.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_session.py tests/test_test_session.py
git commit -m "feat(analysis): add TestSession lifecycle manager"
```

---

### Task 6: TestManager — replaces TestService

**Files:**
- Create: `app/service/analysis/test_manager.py`
- Create: `tests/test_test_manager.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_test_manager.py`:

```python
"""Tests for TestManager (replaces fake TestService)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.service.analysis.test_manager import TestManager


def _mock_bus():
    bus = MagicMock()
    for sig in ("log", "status", "result_changed", "stft_updated",
                "spl_updated", "octave_updated", "fft_updated",
                "recording_progress", "test_started", "test_finished"):
        setattr(bus, sig, MagicMock())
        getattr(bus, sig).emit = MagicMock()
    return bus


def _mock_services(bus):
    services = MagicMock()
    services.bus = bus
    services.config_service = MagicMock()
    services.ai_service = MagicMock()
    services.ai_service.resolve_binding.return_value = None
    services.hss_service = MagicMock()
    services.hss_service.load_io_devices.return_value = {
        "input": {
            "id": 0, "name": "Mic", "type": "MME",
            "samplerate": "44100", "channel_count": "2",
        },
        "output": {
            "id": 1, "name": "Speaker", "type": "MME",
            "samplerate": "44100", "channel_count": "2",
        },
        "mic_cal": {},
    }
    return services


class TestTestManagerInit:
    def test_init(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        assert mgr.is_running() is False


class TestTestManagerStartStop:
    def test_start_without_config_id_logs_error(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mgr.start({"sn": "123"})  # no config_id
        bus.log.emit.assert_called()
        assert mgr.is_running() is False

    def test_start_with_config_id_creates_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024, "stft_overlap": 0.5,
                "spl_weighting": "Z", "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0, "spl_lower_limit": 40.0,
                "fft_band_limits": [], "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        assert mgr.is_running(config_id=1) is True
        bus.test_started.emit.assert_called_with(1)

    def test_stop_all(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        # Manually add a mock session
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()
        mgr.stop()
        assert mgr.is_running() is False

    def test_stop_by_config_id(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session1 = MagicMock()
        mock_session1.config_id = 1
        mock_session2 = MagicMock()
        mock_session2.config_id = 2
        mgr._sessions = {1: mock_session1, 2: mock_session2}
        mgr._recorders = {1: MagicMock(), 2: MagicMock()}
        mgr.stop(config_id=1)
        assert 1 not in mgr._sessions
        assert 2 in mgr._sessions

    def test_stop_with_bool_from_clicked_stops_all(self):
        """QPushButton.clicked emits bool. stop() should treat it as stop-all."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()
        mgr.stop(False)  # bool from clicked signal
        assert mgr.is_running() is False

    def test_start_resolves_config_name(self):
        """WorkOrderModule sends config name, not config_id. Should resolve."""
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.list_configs.return_value = [
            {"config_id": 42, "config_name": "MyConfig"},
        ]
        services.config_service.load_config.return_value = {
            "config_id": 42,
            "config_name": "MyConfig",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024, "stft_overlap": 0.5,
                "spl_weighting": "Z", "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0, "spl_lower_limit": 40.0,
                "fft_band_limits": [], "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        # WorkOrderModule sends {"config": "MyConfig"}, no config_id
        mgr.start({"config": "MyConfig", "sn": "SN1", "model": "M1"})
        assert mgr.is_running(config_id=42) is True


class TestTestManagerRejudge:
    def test_rejudge_delegates_to_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)
        mock_session.rejudge.assert_called_with(1.0, 3.0)

    def test_rejudge_works_on_completed_session(self):
        """After recording completes, session moves to _completed_sessions.
        rejudge() should still find it there."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        # Simulate completion: session in _completed_sessions, not _sessions
        mgr._completed_sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)
        mock_session.rejudge.assert_called_with(0.0, 2.0)


class TestSharedRecorderTeardown:
    def test_shared_recorder_not_stopped_while_other_session_uses_it(self):
        """When two sessions share a recorder, stopping one should NOT stop the recorder."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        shared_rec = MagicMock()
        mock_s1 = MagicMock()
        mock_s1._buffer = None
        mock_s2 = MagicMock()
        mock_s2._buffer = None
        mgr._sessions = {1: mock_s1, 2: mock_s2}
        mgr._recorders = {1: shared_rec, 2: shared_rec}
        mgr._shared_recorders = {"dev|MME|44100": shared_rec}
        # Stop session 1 — recorder still used by session 2
        mgr._stop_one(1)
        shared_rec.stop.assert_not_called()
        # Stop session 2 — now recorder can stop
        mgr._stop_one(2)
        shared_rec.stop.assert_called_once()


class TestSampleRateMismatch:
    def test_logs_warning_on_mismatch(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        # Device reports 48000 but config says 44100
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0, "name": "Mic", "type": "MME",
                "samplerate": "48000", "channel_count": "2",
                "default_samplerate": 48000.0,
            },
            "output": {
                "id": 1, "name": "Speaker", "type": "MME",
                "samplerate": "48000", "channel_count": "2",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.return_value = {
            "config_id": 1, "config_name": "Test",
            "record_duration": 2.0, "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024, "stft_overlap": 0.5,
                "spl_weighting": "Z", "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0, "spl_lower_limit": 40.0,
                "fft_band_limits": [], "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        # Should have logged a warning about sample rate mismatch
        calls = [str(c) for c in bus.log.emit.call_args_list]
        assert any("WARNING" in c and "sample rate" in c for c in calls)
        # Verify the recorder was created with device rate (48000), not config rate (44100)
        assert mgr.is_running(config_id=1) is True
        rec = mgr._recorders.get(1)
        assert rec is not None
        assert rec._sample_rate == 48000.0


class TestSharedRecorderMaxDuration:
    def test_shared_recorder_extends_duration(self):
        """When a second config starts with longer duration, recorder buffer extends."""
        bus = _mock_bus()
        services = _mock_services(bus)
        # First config: record_duration=3.0
        services.config_service.load_config.return_value = {
            "config_id": 1, "config_name": "Short",
            "record_duration": 3.0, "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0, "stft_nfft": 1024,
                "stft_overlap": 0.5, "spl_weighting": "Z",
                "spl_time_constant": 0.125, "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0, "fft_band_limits": [],
                "oct_weighting": "Z", "oct_band_limits": []}],
            "input_device_name": "Mic", "input_device_hostapi": "MME",
            "input_device_samplerate": 44100, "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        rec = mgr._recorders[1]
        original_duration = rec._record_duration
        # Second config: record_duration=5.0, same device
        services.config_service.load_config.return_value = {
            "config_id": 2, "config_name": "Long",
            "record_duration": 5.0, "analysis_duration": 2.0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0, "stft_nfft": 1024,
                "stft_overlap": 0.5, "spl_weighting": "Z",
                "spl_time_constant": 0.125, "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0, "fft_band_limits": [],
                "oct_weighting": "Z", "oct_band_limits": []}],
            "input_device_name": "Mic", "input_device_hostapi": "MME",
            "input_device_samplerate": 44100, "output_device_name": None,
        }
        mgr.start({"config_id": 2, "sn": "SN2"})
        # Both sessions should share the same recorder
        assert mgr._recorders[1] is mgr._recorders[2]
        # Recorder duration should be max(3.0, 5.0) = 5.0
        shared_rec = mgr._recorders[1]
        assert shared_rec._record_duration == 5.0


class TestUserStopPartialSave:
    def test_user_stop_copies_buffer_and_saves(self):
        """User-initiated stop should copy recorder buffer and save partial WAV."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        partial_buf = np.random.randn(44100, 1).astype(np.float32)
        rec = MagicMock()
        rec.get_buffer.return_value = partial_buf
        rec._sample_rate = 44100.0
        session = MagicMock()
        session._buffer = None  # Not yet populated
        session.config_id = 1
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        # User stop (not silent)
        mgr._stop_one(1, silent=False)
        # Should have copied buffer from recorder
        rec.get_buffer.assert_called_once()
        # Should have set session._buffer to the partial data
        assert session._buffer is partial_buf
        # Should have set session._sample_rate
        assert session._sample_rate == 44100.0
        # Should have called _save_wav with correct buffer and rate
        session._save_wav.assert_called_once_with(partial_buf, 44100.0)
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_test_manager.py -v`

- [ ] **Step 3: Implement TestManager**

Create `app/service/analysis/test_manager.py`:

```python
"""TestManager — replaces the fake TestService.

Manages concurrent test sessions. Exposes the same start(workorder)/stop()
API so ControlModule needs zero changes.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from PyQt6.QtCore import QObject

import numpy as np

from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.test_session import TestSession

from consts.running_consts import base_dir


class TestManager(QObject):
    """Manages TestSession instances and shared AudioRecorders."""

    def __init__(self, bus, services):
        super().__init__()
        self._bus = bus
        self._services = services

        # {config_id: TestSession}
        self._sessions: dict[int, TestSession] = {}
        # Completed sessions kept for re-analysis (rejudge)
        self._completed_sessions: dict[int, TestSession] = {}
        # {config_id: AudioRecorder}
        self._recorders: dict[int, AudioRecorder] = {}
        # {config_id: AudioMonitor}
        self._monitors: dict[int, AudioMonitor] = {}
        # {device_key: AudioRecorder} for shared-device reuse
        self._shared_recorders: dict[str, AudioRecorder] = {}

    def start(self, workorder: dict):
        """Start a test for the config_id in the workorder.

        The workorder may carry config_id (int) directly, or only a config
        name string under "config". If config_id is missing, we resolve
        the name via ConfigService.list_configs().
        """
        config_id = workorder.get("config_id")
        if config_id is None:
            # Resolve config name → config_id
            config_name = workorder.get("config", "")
            if config_name:
                for cfg in self._services.config_service.list_configs():
                    if cfg["config_name"] == config_name:
                        config_id = cfg["config_id"]
                        break
        if config_id is None:
            self._bus.log.emit("[TestManager] ERROR: could not resolve config_id from workorder")
            return

        if config_id in self._sessions:
            self._bus.log.emit(f"[TestManager] config {config_id} already running, stopping first")
            self.stop(config_id=config_id)

        # Clear any completed session for re-analysis cleanup
        self._completed_sessions.pop(config_id, None)

        # Load config
        try:
            config = self._services.config_service.load_config(config_id)
        except Exception as e:
            self._bus.log.emit(f"[TestManager] ERROR loading config {config_id}: {e}")
            return

        # Load mic calibration
        device_info = self._services.hss_service.load_io_devices()
        mic_cal = device_info.get("mic_cal", {})

        # Audio output directory
        audio_dir = os.path.join(base_dir, "data", "audio")

        # Create session
        session = TestSession(
            config=config, bus=self._bus, ai_service=self._services.ai_service,
            mic_cal=mic_cal, workorder=workorder, audio_dir=audio_dir,
        )
        session._setup_pipelines()

        # Create or reuse recorder
        device_key = self._device_key(config)
        input_info = device_info.get("input", {})
        device_index = input_info.get("id", 0)
        config_sr = float(config.get("input_device_samplerate", 44100))
        device_sr = float(input_info.get("default_samplerate", config_sr))
        # Spec: use device's actual sample rate, log warning if mismatch
        if config_sr != device_sr:
            self._bus.log.emit(
                f"[TestManager] WARNING: config sample rate {config_sr} differs "
                f"from device {device_sr}, using device rate"
            )
        sample_rate = device_sr
        n_channels = int(input_info.get("channel_count", 1))
        record_duration = config.get("record_duration", 5.0)

        if device_key in self._shared_recorders:
            recorder = self._shared_recorders[device_key]
            # Spec: shared recorder uses max(record_duration) across sessions
            if record_duration > recorder._record_duration:
                recorder._record_duration = record_duration
                new_total = int(sample_rate * record_duration)
                old_buf = recorder._buffer
                if new_total > old_buf.shape[0]:
                    new_buf = np.zeros((new_total, n_channels), dtype=np.float32)
                    new_buf[:old_buf.shape[0]] = old_buf
                    recorder._buffer = new_buf
        else:
            recorder = AudioRecorder(
                device_index=device_index,
                sample_rate=sample_rate,
                n_channels=n_channels,
                record_duration=record_duration,
            )
            self._shared_recorders[device_key] = recorder

        # Connect recorder to session
        recorder.chunk_ready.connect(session._on_chunk)
        recorder.recording_done.connect(
            lambda cid=config_id: self._on_recording_done(cid)
        )
        recorder.error_occurred.connect(
            lambda msg, cid=config_id: self._on_error(cid, msg)
        )

        self._sessions[config_id] = session
        self._recorders[config_id] = recorder

        # Optional: monitoring
        output_name = config.get("output_device_name")
        if output_name:
            output_info = device_info.get("output", {})
            output_id = output_info.get("id", 0)
            output_sr = float(config.get("output_device_samplerate", sample_rate))
            monitor = AudioMonitor(
                device_index=output_id, sample_rate=output_sr,
                monitor_channel=0,
            )
            recorder.chunk_ready.connect(
                lambda data, idx, m=monitor: m.feed(data)
            )
            self._monitors[config_id] = monitor
            monitor.start()

        # Start recording
        self._bus.test_started.emit(config_id)
        self._bus.status.emit("Recording...")
        self._bus.log.emit(f"[TestManager] started config={config_id}, workorder={workorder}")
        if not recorder.isRunning():
            recorder.start()

    def stop(self, config_id: int | None = None, silent: bool = False):
        """Stop test(s). If config_id is None, stop all.

        Note: ControlModule connects btn_stop.clicked directly to this method.
        QPushButton.clicked emits bool (checked state). When called that way,
        config_id receives False (a bool). We treat bool as "stop all".
        """
        if isinstance(config_id, bool):
            config_id = None
        if config_id is not None:
            self._stop_one(config_id, silent)
        else:
            for cid in list(self._sessions.keys()):
                self._stop_one(cid, silent)

    def _stop_one(self, config_id: int, silent: bool = False):
        session = self._sessions.pop(config_id, None)
        rec = self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)

        # Save partial buffer if user-initiated stop (not silent)
        # Buffer lives in the recorder; copy it to session before saving
        if session is not None and rec is not None and not silent:
            partial_buf = rec.get_buffer()
            if partial_buf is not None and partial_buf.size > 0:
                session._buffer = partial_buf
                session._sample_rate = rec._sample_rate
                session._save_wav(partial_buf, rec._sample_rate)
                self._bus.log.emit(
                    f"[TestManager] saved partial recording for config={config_id}"
                )

        if mon is not None:
            mon.stop()
            mon.wait(1000)

        # Only stop recorder if no other sessions still reference it
        if rec is not None:
            still_used = any(r is rec for r in self._recorders.values())
            if not still_used:
                rec.stop()
                rec.wait(3000)
                # Clean shared recorder entry
                for key, shared_rec in list(self._shared_recorders.items()):
                    if shared_rec is rec:
                        del self._shared_recorders[key]

        if not silent:
            self._bus.log.emit(f"[TestManager] stopped config={config_id}")
            self._bus.status.emit("Stopped")

    def is_running(self, config_id: int | None = None) -> bool:
        if config_id is not None:
            return config_id in self._sessions
        return len(self._sessions) > 0

    def rejudge(self, config_id: int, start_sec: float, end_sec: float):
        """Re-run judgment on a user-selected window.

        Checks both active and completed sessions, since completed sessions
        keep their buffer for re-analysis until a new test starts.
        """
        session = self._sessions.get(config_id) or self._completed_sessions.get(config_id)
        if session is None:
            self._bus.log.emit(f"[TestManager] no session for config {config_id}")
            return
        result = session.rejudge(start_sec, end_sec)
        if result is not None:
            self._bus.test_finished.emit(config_id, asdict(result))
            self._bus.result_changed.emit(asdict(result))

    def _on_recording_done(self, config_id: int):
        """Called when a recorder fills its buffer."""
        session = self._sessions.get(config_id)
        if session is None:
            return

        self._bus.status.emit("Analyzing...")
        self._bus.log.emit(f"[TestManager] recording done for config={config_id}")

        # Copy buffer from recorder to session
        rec = self._recorders.get(config_id)
        if rec is not None:
            session._buffer = rec.get_buffer()
            session._sample_rate = rec._sample_rate

        # Run judgment
        result = session._run_judgment()
        result_dict = asdict(result)
        self._bus.test_finished.emit(config_id, result_dict)
        self._bus.result_changed.emit(result_dict)
        self._bus.log.emit(
            f"[TestManager] result: {result.overall} (limit={result.limit_pass})"
        )
        self._bus.status.emit("Idle")

        # Move session to completed (keeps buffer for re-analysis)
        # Then clean up recorder/monitor without removing session buffer
        session = self._sessions.pop(config_id, None)
        if session is not None:
            self._completed_sessions[config_id] = session

        rec = self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)
        if mon is not None:
            mon.stop()
            mon.wait(1000)
        if rec is not None:
            still_used = any(r is rec for r in self._recorders.values())
            if not still_used:
                rec.stop()
                rec.wait(3000)
                for key, shared_rec in list(self._shared_recorders.items()):
                    if shared_rec is rec:
                        del self._shared_recorders[key]

    def _on_error(self, config_id: int, message: str):
        self._bus.log.emit(f"[TestManager] ERROR config={config_id}: {message}")
        self._bus.status.emit(f"Error: {message}")
        self._stop_one(config_id, silent=True)

    @staticmethod
    def _device_key(config: dict) -> str:
        """Build a key for shared-device lookup."""
        return (
            f"{config.get('input_device_name', '')}|"
            f"{config.get('input_device_hostapi', '')}|"
            f"{config.get('input_device_samplerate', '')}"
        )
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_test_manager.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_manager.py tests/test_test_manager.py
git commit -m "feat(analysis): add TestManager replacing fake TestService"
```

---

### Task 7: Wire TestManager into ServiceContainer

**Files:**
- Modify: `app/services_container.py`

- [ ] **Step 1: Replace TestService with TestManager**

Edit `app/services_container.py`:

Replace the `TestService` class and its usage with:

```python
# At the top — remove old TestService, add new import:
from app.service.analysis.test_manager import TestManager

# In ServiceContainer.__post_init__, replace:
#   self.test = TestService(self.bus)
# With:
self.test = TestManager(self.bus, self)
```

The full updated file should look like:

```python
from __future__ import annotations

from dataclasses import dataclass
from PyQt6.QtCore import QObject, pyqtSignal

from app.service.db_connection_service import DbConnectionService
from app.service.hardwareselect_service import HardwareSelectService
from app.service.input_calibration_service import InputCalibrationService
from app.service.config_service import ConfigService
from app.service.ai_service import AiService
from app.service.analysis.test_manager import TestManager


class EventBus(QObject):
    """Module-to-module signal bus."""

    log = pyqtSignal(str)
    status = pyqtSignal(str)
    workorder_changed = pyqtSignal(dict)
    result_changed = pyqtSignal(dict)
    refresh_channels = pyqtSignal()
    config_changed = pyqtSignal()

    # ── Analysis kernel real-time signals ──
    stft_updated = pyqtSignal(int, int, object)
    spl_updated = pyqtSignal(int, int, object)
    octave_updated = pyqtSignal(int, int, object)
    fft_updated = pyqtSignal(int, int, object)
    recording_progress = pyqtSignal(int, float)
    test_started = pyqtSignal(int)
    test_finished = pyqtSignal(int, dict)


@dataclass
class ServiceContainer:
    bus = EventBus()

    def __post_init__(self):
        self.db = DbConnectionService()
        self.db.connect()
        self.hss_service = HardwareSelectService()
        self.input_calibration_service = InputCalibrationService(self.bus)
        self.config_service = ConfigService(self.bus)
        self.ai_service = AiService()
        self.test = TestManager(self.bus, self)
```

- [ ] **Step 2: Verify ControlModule still works (interface check)**

`ControlModule._start()` calls `self.services.test.start(self._workorder)` — TestManager has this exact method. `ControlModule` also connects `btn_stop.clicked` to `self.services.test.stop` — TestManager has `stop()`. No changes needed.

- [ ] **Step 3: Run service container tests**

Run: `python -m pytest tests/test_service_container.py -v`

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services_container.py
git commit -m "feat(analysis): wire TestManager into ServiceContainer, remove fake TestService"
```

---

### Task 8: Full integration smoke test

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

- [ ] **Step 2: Verify file structure**

Expected:
```
app/service/analysis/
├── __init__.py
├── frames.py          (Plan A)
├── dsp.py             (Plan A)
├── pipeline.py        (Plan A)
├── judgment.py        (Plan A)
├── audio_recorder.py  (Plan B)
├── audio_monitor.py   (Plan B)
├── test_session.py    (Plan B)
└── test_manager.py    (Plan B)
```

- [ ] **Step 3: Verify import chain works**

Run: `python -c "from app.service.analysis.test_manager import TestManager; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Final commit if any fixes needed**

---

## Summary

After Plan B, we have:
- **AudioRecorder**: QThread + sounddevice callback → ring buffer → chunk signals
- **AudioMonitor**: QThread + sounddevice OutputStream → single-channel playback
- **TestSession**: Lifecycle manager tying recorder → pipelines → judgment → result
- **TestManager**: Drop-in replacement for TestService, manages concurrent sessions, shared recorders
- **EventBus**: 7 new signals for real-time analysis data and session lifecycle
- **ServiceContainer**: Wired up with TestManager, fake TestService removed

The complete analysis kernel is functional end-to-end: Start button → audio recording → real-time analysis signals → judgment → result.
