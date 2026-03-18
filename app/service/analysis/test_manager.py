"""TestManager - replaces the fake TestService.

Manages concurrent test sessions. Exposes the same start(workorder)/stop()
API so ControlModule needs zero changes.
"""
from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass

import numpy as np
from PyQt6.QtCore import QObject

from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.test_session import TestSession
from consts.running_consts import base_dir


class TestManager(QObject):
    """Manages TestSession instances and shared AudioRecorders."""

    __test__ = False

    def __init__(self, bus, services):
        super().__init__()
        self._bus = bus
        self._services = services

        self._sessions: dict[int, TestSession] = {}
        self._completed_sessions: dict[int, TestSession] = {}
        self._recorders: dict[int, AudioRecorder] = {}
        self._monitors: dict[int, AudioMonitor] = {}
        self._shared_recorders: dict[str, AudioRecorder] = {}

    def start(self, workorder: dict):
        """Start a test for the config_id in the workorder."""
        config_id = workorder.get("config_id")
        if config_id is None:
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

        self._completed_sessions.pop(config_id, None)

        try:
            config = self._services.config_service.load_config(config_id)
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR loading config {config_id}: {exc}")
            return

        device_info = self._services.hss_service.load_io_devices()
        mic_cal = device_info.get("mic_cal", {})
        audio_dir = os.path.join(base_dir, "data", "audio")

        session = TestSession(
            config=config,
            bus=self._bus,
            ai_service=self._services.ai_service,
            mic_cal=mic_cal,
            workorder=workorder,
            audio_dir=audio_dir,
        )
        session._setup_pipelines()

        device_key = self._device_key(config)
        input_info = device_info.get("input", {})
        config_sr = float(config.get("input_device_samplerate", 44100))
        device_sr = float(input_info.get("default_samplerate", config_sr))
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
            if record_duration > recorder._record_duration:
                recorder._record_duration = record_duration
                new_total = int(sample_rate * record_duration)
                old_buf = recorder._buffer
                if new_total > old_buf.shape[0]:
                    new_buf = np.zeros((new_total, n_channels), dtype=np.float32)
                    new_buf[: old_buf.shape[0]] = old_buf
                    recorder._buffer = new_buf
        else:
            recorder = AudioRecorder(
                device_name=config.get("input_device_name", ""),
                hostapi=config.get("input_device_hostapi", ""),
                sample_rate=sample_rate,
                n_channels=n_channels,
                record_duration=record_duration,
            )
            self._shared_recorders[device_key] = recorder

        recorder.chunk_ready.connect(session._on_chunk)
        recorder.recording_done.connect(lambda cid=config_id: self._on_recording_done(cid))
        recorder.error_occurred.connect(lambda msg, cid=config_id: self._on_error(cid, msg))

        self._sessions[config_id] = session
        self._recorders[config_id] = recorder

        output_name = config.get("output_device_name")
        if output_name:
            output_sr = float(config.get("output_device_samplerate", sample_rate))
            monitor = AudioMonitor(
                device_name=config.get("output_device_name", ""),
                hostapi=config.get("output_device_hostapi", ""),
                sample_rate=output_sr,
                monitor_channel=0,
            )
            recorder.chunk_ready.connect(lambda data, idx, m=monitor: m.feed(data))
            monitor.error_occurred.connect(lambda msg, cid=config_id: self._on_error(cid, msg))
            self._monitors[config_id] = monitor
            monitor.start()

        self._bus.test_started.emit(config_id)
        self._bus.status.emit("Recording...")
        self._bus.log.emit(f"[TestManager] started config={config_id}, workorder={workorder}")
        if not recorder.isRunning():
            recorder.start()

    def stop(self, config_id: int | None = None, silent: bool = False):
        """Stop test(s). If config_id is None, stop all."""
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

        if session is not None and rec is not None and not silent:
            partial_buf = rec.get_buffer()
            if isinstance(partial_buf, np.ndarray) and partial_buf.size > 0:
                session._buffer = partial_buf
                session._sample_rate = rec._sample_rate
                session._save_wav(partial_buf, rec._sample_rate)
                self._bus.log.emit(
                    f"[TestManager] saved partial recording for config={config_id}"
                )

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

        if not silent:
            self._bus.log.emit(f"[TestManager] stopped config={config_id}")
            self._bus.status.emit("Stopped")

    def is_running(self, config_id: int | None = None) -> bool:
        if config_id is not None:
            return config_id in self._sessions
        return len(self._sessions) > 0

    def rejudge(self, config_id: int, start_sec: float, end_sec: float):
        """Re-run judgment on a user-selected window."""
        session = self._sessions.get(config_id) or self._completed_sessions.get(config_id)
        if session is None:
            self._bus.log.emit(f"[TestManager] no session for config {config_id}")
            return
        result = session.rejudge(start_sec, end_sec)
        if result is not None and is_dataclass(result):
            result_dict = asdict(result)
            self._bus.test_finished.emit(config_id, result_dict)
            self._bus.result_changed.emit(result_dict)

    def _on_recording_done(self, config_id: int):
        """Called when a recorder fills its buffer."""
        session = self._sessions.get(config_id)
        if session is None:
            return

        self._bus.status.emit("Analyzing...")
        self._bus.log.emit(f"[TestManager] recording done for config={config_id}")

        rec = self._recorders.get(config_id)
        if rec is not None:
            session._buffer = rec.get_buffer()
            session._sample_rate = rec._sample_rate

        result = session._run_judgment()
        result_dict = asdict(result)
        self._bus.test_finished.emit(config_id, result_dict)
        self._bus.result_changed.emit(result_dict)
        self._bus.log.emit(
            f"[TestManager] result: {result.overall} (limit={result.limit_pass})"
        )
        self._bus.status.emit("Idle")

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
