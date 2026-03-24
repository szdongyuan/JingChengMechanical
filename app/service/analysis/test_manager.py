"""TestManager - replaces the fake TestService.

Manages concurrent test sessions. Exposes the same start(workorder)/stop()
API so ControlModule needs zero changes.
"""
from __future__ import annotations

import os
import uuid
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

    def start(self, workorder: dict, *, on_start_accepted=None) -> bool:
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
            return False

        if config_id in self._sessions:
            self._bus.log.emit(f"[TestManager] config {config_id} already running, stopping first")
            self.stop(config_id=config_id)

        try:
            config = self._services.config_service.load_config(config_id)
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR loading config {config_id}: {exc}")
            return False

        device_info = self._services.hss_service.load_io_devices()
        mic_cal = device_info.get("mic_cal", {})
        audio_dir = os.path.join(base_dir, "data", "audio")

        device_key = self._device_key(config)
        input_info = device_info.get("input", {})
        config_sr = float(config.get("input_device_samplerate", 44100))
        device_sr = float(input_info.get("samplerate", config_sr))
        if config_sr != device_sr:
            self._bus.log.emit(
                f"[TestManager] WARNING: config sample rate {config_sr} differs "
                f"from device {device_sr}, using device rate"
            )
        sample_rate = device_sr
        n_channels = int(input_info.get("channel_count", 1))
        record_duration = config.get("record_duration", 5.0)
        session_workorder = dict(workorder)
        session_workorder["factory_run_id"] = self._new_factory_run_id()

        session = TestSession(
            config=config,
            bus=self._bus,
            ai_service=self._services.ai_service,
            mic_cal=mic_cal,
            workorder=session_workorder,
            audio_dir=audio_dir,
        )
        session._on_session_done_cb = self._on_session_done

        if device_key in self._shared_recorders:
            recorder = self._shared_recorders[device_key]
        else:
            recorder = AudioRecorder(
                device_name=config.get("input_device_name", ""),
                hostapi=config.get("input_device_hostapi", ""),
                sample_rate=sample_rate,
                n_channels=n_channels,
                record_duration=record_duration,
            )
            self._shared_recorders[device_key] = recorder

        session._sample_rate = recorder._sample_rate
        session._setup_pipelines()

        session._chunk_conn = session._on_chunk
        session._done_lambda = lambda cid=config_id: self._on_recording_done(cid)
        session._error_lambda = lambda msg, cid=config_id: self._on_error(cid, msg)
        prepare_for_attach = getattr(session, "prepare_for_attach", None)
        if callable(prepare_for_attach):
            prepare_for_attach()
        recorder.chunk_ready.connect(session._chunk_conn)
        attach_snapshot = recorder.attach_session(record_duration=record_duration)
        session.attach_to_recorder(
            chunk_index=attach_snapshot.chunk_index,
            snapshot=attach_snapshot,
        )
        recorder.recording_done.connect(session._done_lambda)
        recorder.error_occurred.connect(session._error_lambda)

        self._sessions[config_id] = session
        self._recorders[config_id] = recorder

        output_name = config.get("output_device_name")
        if output_name:
            output_sr = float(config.get("output_device_samplerate", sample_rate))
            monitor = AudioMonitor(
                device_name=config.get("output_device_name", ""),
                hostapi=config.get("output_device_hostapi", ""),
                sample_rate=output_sr,
                monitor_channel=config.get("selected_channels", [0])[0],
            )
            session._monitor_lambda = lambda data, idx, m=monitor: m.feed(data)
            session._monitor_error_lambda = lambda msg, cid=config_id: self._on_error(cid, msg)
            recorder.chunk_ready.connect(session._monitor_lambda)
            monitor.error_occurred.connect(session._monitor_error_lambda)
            self._monitors[config_id] = monitor

        try:
            if mon := self._monitors.get(config_id):
                mon.start()

            if not recorder.isRunning():
                recorder.start()
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR starting config {config_id}: {exc}")
            self._stop_one(config_id, silent=True)
            return False

        if callable(on_start_accepted):
            try:
                on_start_accepted(session_workorder["factory_run_id"])
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR accepting start for config {config_id}: {exc}"
                )
                self._stop_one(config_id, silent=True)
                return False

        self._completed_sessions.pop(config_id, None)

        self._bus.test_started.emit(config_id)
        self._bus.status.emit("Recording...")
        self._bus.log.emit(
            f"[TestManager] started config={config_id}, workorder={session_workorder}"
        )
        return True

    def stop(self, config_id: int | None = None, silent: bool = False):
        """Stop test(s). If config_id is None, stop all."""
        if isinstance(config_id, bool):
            config_id = None
        if config_id is not None:
            self._stop_one(config_id, silent)
        else:
            for cid in list(self._sessions.keys()):
                self._stop_one(cid, silent)

    def stop_active_run(self, config_id: int | None = None) -> bool:
        """Factory-mode hook for stopping an active session."""
        if config_id is None:
            if not self._sessions:
                return False
            self.stop()
            return True

        if config_id not in self._sessions:
            return False

        self.stop(config_id=config_id)
        return True

    def _stop_one(self, config_id: int, silent: bool = False):
        session = self._sessions.pop(config_id, None)
        rec = self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)

        if session is not None and rec is not None:
            self._disconnect_session_signals(session, rec, mon)

        if session is not None and rec is not None and not silent:
            partial_buf = rec.get_buffer_from(getattr(session, "_attach_snapshot", None))
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
            self._stop_recorder_if_unused(rec)

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
            error_message = f"no session for config {config_id}"
            self._bus.log.emit(f"[TestManager] {error_message}")
            self._emit_optional_signal(
                "factory_runtime_reanalysis_completed",
                self._make_factory_review_payload(
                    config_id,
                    None,
                    no_result=True,
                    error_message=error_message,
                ),
            )
            return
        try:
            result = session.rejudge(start_sec, end_sec)
            result_dict = self._serialize_result(result)
        except Exception as exc:
            payload = self._emit_no_result(config_id, exc, context="rejudge")
            self._emit_optional_signal(
                "factory_runtime_reanalysis_completed",
                self._make_factory_review_payload(
                    config_id,
                    session,
                    no_result=True,
                    error_message=payload["error_summary"],
                ),
            )
            return

        self._bus.test_finished.emit(config_id, result_dict)
        self._bus.result_changed.emit(result_dict)
        self._emit_optional_signal(
            "factory_runtime_reanalysis_completed",
            self._make_factory_review_payload(
                config_id,
                session,
                judgment_payload=result_dict,
            ),
        )

    def _on_recording_done(self, config_id: int):
        """Called when a recorder fills its buffer."""
        session = self._sessions.get(config_id)
        if session is None or getattr(session, "_finished", False):
            return
        session._finished = True
        self._on_session_done(config_id)

    def _on_session_done(self, config_id: int):
        session = self._sessions.get(config_id)
        if session is None:
            return

        self._bus.status.emit("Analyzing...")
        self._bus.log.emit(f"[TestManager] recording done for config={config_id}")
        self._emit_optional_signal(
            "factory_runtime_analyzing_entered",
            self._make_factory_runtime_config_payload(
                config_id,
                self._factory_run_id(session),
            ),
        )

        rec = self._recorders.get(config_id)
        mon = self._monitors.get(config_id)
        if rec is not None:
            session._buffer = rec.get_buffer_from(getattr(session, "_attach_snapshot", None))
            session._sample_rate = rec._sample_rate
            self._disconnect_session_signals(session, rec, mon)

        try:
            result = session._run_judgment()
            result_dict = self._serialize_result(result)
        except Exception as exc:
            payload = self._emit_no_result(config_id, exc, context="analysis")
            signal_name = (
                "factory_runtime_auto_analysis_no_result"
                if self._is_factory_no_result_error(exc)
                else "factory_runtime_auto_analysis_error"
            )
            self._emit_optional_signal(
                signal_name,
                self._make_factory_review_payload(
                    config_id,
                    session,
                    no_result=True,
                    error_message=payload["error_summary"],
                ),
            )
        else:
            self._bus.test_finished.emit(config_id, result_dict)
            self._bus.result_changed.emit(result_dict)
            self._bus.log.emit(
                f"[TestManager] result: {result.overall} (limit={result.limit_pass})"
            )
            self._emit_optional_signal(
                "factory_runtime_auto_analysis_succeeded",
                self._make_factory_review_payload(
                    config_id,
                    session,
                    judgment_payload=result_dict,
                ),
            )
        self._bus.status.emit("Idle")

        session = self._sessions.pop(config_id, None)
        if session is not None:
            self._completed_sessions[config_id] = session

        self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)
        if mon is not None:
            mon.stop()
            mon.wait(1000)
        if rec is not None:
            self._stop_recorder_if_unused(rec)

    def _on_error(self, config_id: int, message: str):
        session = self._sessions.get(config_id)
        self._bus.log.emit(f"[TestManager] ERROR config={config_id}: {message}")
        self._bus.status.emit(f"Error: {message}")
        if session is not None:
            self._emit_optional_signal(
                "factory_runtime_aborted",
                self._make_factory_runtime_abort_payload(
                    config_id,
                    session,
                    error_message=message,
                ),
            )
        self._stop_one(config_id, silent=True)

    @staticmethod
    def _new_factory_run_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _make_no_result_payload(error: Exception | str) -> dict:
        return {
            "no_result": True,
            "has_result": False,
            "display_status": "no_result",
            "ai_status": "no_result",
            "ai_label": None,
            "ai_score": None,
            "ai_summary": None,
            "error_summary": str(error),
        }

    @staticmethod
    def _serialize_result(result) -> dict:
        if result is None or not is_dataclass(result):
            raise RuntimeError("analysis result unavailable")
        return asdict(result)

    def _emit_no_result(self, config_id: int, error: Exception | str, *, context: str):
        payload = self._make_no_result_payload(error)
        self._bus.test_finished.emit(config_id, payload)
        self._bus.result_changed.emit(payload)
        self._bus.log.emit(
            f"[TestManager] no-result {context} for config={config_id}: {payload['error_summary']}"
        )
        return payload

    def _emit_optional_signal(self, signal_name: str, payload):
        signal = getattr(self._bus, signal_name, None)
        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit(payload)

    @staticmethod
    def _factory_run_id(session) -> str | None:
        workorder = getattr(session, "_workorder", None)
        if not isinstance(workorder, dict):
            return None
        run_id = workorder.get("factory_run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
        return None

    @staticmethod
    def _make_factory_runtime_config_payload(config_id: int, run_id: str):
        from app.services_container import FactoryRuntimeConfigPayload

        return FactoryRuntimeConfigPayload(config_id=config_id, run_id=run_id)

    @staticmethod
    def _factory_trigger_source(session) -> str | None:
        workorder = getattr(session, "_workorder", None)
        if not isinstance(workorder, dict):
            return None
        trigger_source = workorder.get("trigger_source")
        if isinstance(trigger_source, str) and trigger_source:
            return trigger_source
        return None

    def _make_factory_runtime_abort_payload(
        self,
        config_id: int,
        session,
        *,
        error_message: str,
    ):
        from app.services_container import FactoryRuntimeAbortPayload

        return FactoryRuntimeAbortPayload(
            config_id=config_id,
            run_id=self._factory_run_id(session),
            trigger_source=self._factory_trigger_source(session),
            error_message=error_message,
        )

    @staticmethod
    def _is_factory_no_result_error(error: Exception) -> bool:
        if isinstance(error, ValueError):
            return True
        return isinstance(error, RuntimeError) and str(error) == "analysis result unavailable"

    def _make_factory_review_payload(
        self,
        config_id: int,
        session,
        *,
        judgment_payload: dict | None = None,
        no_result: bool = False,
        error_message: str | None = None,
    ):
        from app.services_container import FactoryReviewPayload, FactoryReviewWindow

        config = getattr(session, "_config", {}) or {}
        analysis_duration = self._analysis_duration_sec(config)
        captured_duration_sec = self._captured_duration_sec(session)
        default_window = self._window_from_payload(
            judgment_payload,
            review_window_cls=FactoryReviewWindow,
        )
        if default_window is None:
            default_window = self._configured_review_window(
                config,
                review_window_cls=FactoryReviewWindow,
            )

        manual_reanalysis_allowed = (
            session is not None
            and captured_duration_sec is not None
            and analysis_duration > 0
            and captured_duration_sec >= analysis_duration
            and getattr(session, "_audio_file_path", None) is not None
        )
        fallback_window = None
        if manual_reanalysis_allowed and (
            default_window is None
            or default_window.start_sec < 0
            or default_window.end_sec > captured_duration_sec
        ):
            fallback_window = FactoryReviewWindow(
                start_sec=0.0,
                end_sec=analysis_duration,
            )

        return FactoryReviewPayload(
            displayed_config_id=config_id,
            run_id=self._factory_run_id(session),
            trigger_source=self._factory_trigger_source(session),
            captured_duration_sec=captured_duration_sec,
            default_window=default_window,
            fallback_window=fallback_window,
            manual_reanalysis_allowed=manual_reanalysis_allowed,
            no_result=no_result,
            error_message=error_message,
            judgment_payload=judgment_payload,
        )

    @staticmethod
    def _analysis_duration_sec(config: dict) -> float:
        try:
            return float(config.get("analysis_duration", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    @staticmethod
    def _captured_duration_sec(session) -> float | None:
        if session is None:
            return None
        buffer = getattr(session, "_buffer", None)
        try:
            sample_rate = float(getattr(session, "_sample_rate", 0) or 0)
        except (TypeError, ValueError):
            sample_rate = 0.0
        if not isinstance(buffer, np.ndarray) or buffer.size <= 0 or sample_rate <= 0:
            return None
        return float(buffer.shape[0]) / sample_rate

    @staticmethod
    def _window_from_payload(judgment_payload: dict | None, *, review_window_cls):
        if not isinstance(judgment_payload, dict):
            return None
        analysis_window = judgment_payload.get("analysis_window")
        if not isinstance(analysis_window, (list, tuple)) or len(analysis_window) != 2:
            return None
        return review_window_cls(
            start_sec=float(analysis_window[0]),
            end_sec=float(analysis_window[1]),
        )

    @staticmethod
    def _configured_review_window(config: dict, *, review_window_cls):
        analysis_duration = TestManager._analysis_duration_sec(config)
        if analysis_duration <= 0:
            return None
        try:
            start_sec = float(config.get("analysis_window_offset_ms", 0) or 0) / 1000.0
        except (TypeError, ValueError, AttributeError):
            start_sec = 0.0
        return review_window_cls(
            start_sec=start_sec,
            end_sec=start_sec + analysis_duration,
        )

    @staticmethod
    def _disconnect(signal, callback):
        if callback is None:
            return
        try:
            signal.disconnect(callback)
        except (TypeError, RuntimeError, ValueError):
            pass

    def _disconnect_session_signals(self, session, recorder, monitor=None):
        self._disconnect(recorder.chunk_ready, getattr(session, "_chunk_conn", None))
        self._disconnect(recorder.recording_done, getattr(session, "_done_lambda", None))
        self._disconnect(recorder.error_occurred, getattr(session, "_error_lambda", None))
        self._disconnect(recorder.chunk_ready, getattr(session, "_monitor_lambda", None))
        if monitor is not None:
            self._disconnect(monitor.error_occurred, getattr(session, "_monitor_error_lambda", None))

    def _stop_recorder_if_unused(self, recorder):
        still_used = any(r is recorder for r in self._recorders.values())
        if not still_used:
            recorder.stop()
            recorder.wait(3000)
            for key, shared_rec in list(self._shared_recorders.items()):
                if shared_rec is recorder:
                    del self._shared_recorders[key]

    @staticmethod
    def _device_key(config: dict) -> str:
        """Build a key for shared-device lookup."""
        return (
            f"{config.get('input_device_name', '')}|"
            f"{config.get('input_device_hostapi', '')}|"
            f"{config.get('input_device_samplerate', '')}"
        )
