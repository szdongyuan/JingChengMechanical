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

from app.service.analysis import dsp
from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.review_payloads import build_chart_payload, build_summary_payload
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
        self._pending_factory_starts: dict[str, int] = {}
        self._canceled_factory_start_requests: set[str] = set()
        self._analyzing_sessions: set[int] = set()
        self._accepted_factory_stops: set[int] = set()

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

        request_id = self._get_factory_start_request_id(workorder)
        if request_id is not None:
            self._pending_factory_starts[request_id] = config_id

        if config_id in self._sessions:
            self._bus.log.emit(f"[TestManager] config {config_id} already running, stopping first")
            self.stop(config_id=config_id)

        try:
            config = self._services.config_service.load_config(config_id)
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR loading config {config_id}: {exc}")
            self._clear_pending_factory_start(request_id)
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
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR starting config {config_id}: {exc}")
            self._stop_one(config_id, silent=True)
            self._clear_pending_factory_start(request_id)
            return False

        if self._factory_start_canceled(request_id):
            self._stop_one(config_id, silent=True)
            self._clear_pending_factory_start(request_id)
            return False

        if callable(on_start_accepted):
            try:
                on_start_accepted(session_workorder["factory_run_id"])
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR accepting start for config {config_id}: {exc}"
                )
                self._stop_one(config_id, silent=True)
                self._clear_pending_factory_start(request_id)
                return False

        if self._factory_start_canceled(request_id):
            self._stop_one(config_id, silent=True)
            self._clear_pending_factory_start(request_id)
            return False

        prior_completed_session = self._completed_sessions.pop(config_id, None)

        try:
            if not recorder.isRunning():
                recorder.start()
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR starting config {config_id}: {exc}")
            self._emit_optional_signal(
                "factory_runtime_aborted",
                self._make_factory_runtime_abort_payload(
                    config_id,
                    session,
                    error_message=str(exc),
                ),
            )
            self._stop_one(config_id, silent=True)
            if prior_completed_session is not None and config_id not in self._completed_sessions:
                self._completed_sessions[config_id] = prior_completed_session
            self._clear_pending_factory_start(request_id)
            return False

        active_session = self._sessions.get(config_id)
        if active_session is not session:
            if prior_completed_session is not None and config_id not in self._completed_sessions:
                self._completed_sessions[config_id] = prior_completed_session
            self._clear_pending_factory_start(request_id)
            return True

        self._clear_pending_factory_start(request_id)
        self._bus.test_started.emit(config_id)
        self._bus.status.emit("Recording...")
        self._bus.log.emit(
            f"[TestManager] started config={config_id}, workorder={session_workorder}"
        )
        return True

    def cancel_pending_start(self, *, request_id: str, config_id: int | None = None) -> bool:
        if not request_id:
            return False

        pending_config_id = self._pending_factory_starts.get(request_id)
        if pending_config_id is not None and config_id is not None and pending_config_id != config_id:
            return False

        self._canceled_factory_start_requests.add(request_id)
        return True

    def stop(self, config_id: int | None = None, silent: bool = False):
        """Stop test(s). If config_id is None, stop all."""
        if isinstance(config_id, bool):
            config_id = None
        if config_id is not None:
            self._stop_one(config_id, silent, notify_factory_runtime_abort=True)
        else:
            for cid in list(self._sessions.keys()):
                self._stop_one(cid, silent, notify_factory_runtime_abort=True)

    def stop_active_run(self, config_id: int | None = None) -> bool:
        """Factory-mode hook for stopping an active session."""
        if config_id is None:
            if not self._sessions:
                return False
            for cid in list(self._sessions.keys()):
                self._stop_factory_run(cid)
            return True

        return self._stop_factory_run(config_id)

    def _stop_factory_run(self, config_id: int) -> bool:
        session = self._sessions.get(config_id)
        if session is None:
            return False
        if config_id in self._accepted_factory_stops:
            return True

        self._accepted_factory_stops.add(config_id)

        if self._session_is_analyzing(config_id):
            return self._stop_to_stopped(config_id, session)
        return self._stop_recording_with_optional_analysis(config_id, session)

    def _stop_one(
        self,
        config_id: int,
        silent: bool = False,
        *,
        notify_factory_runtime_abort: bool = False,
    ):
        session = self._sessions.pop(config_id, None)
        rec = self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)

        if session is not None and rec is not None:
            self._disconnect_session_signals(session, rec, mon)

        factory_abort_payload = None
        if (
            notify_factory_runtime_abort
            and session is not None
            and self._factory_run_id(session) is not None
        ):
            factory_abort_payload = self._make_factory_runtime_abort_payload(
                config_id,
                session,
                error_message=None,
            )

        try:
            if session is not None and rec is not None and not silent:
                partial_buf = rec.get_buffer_from(getattr(session, "_attach_snapshot", None))
                if isinstance(partial_buf, np.ndarray) and partial_buf.size > 0:
                    session._buffer = partial_buf
                    session._sample_rate = rec._sample_rate
                    try:
                        session._save_wav(partial_buf, rec._sample_rate)
                    except Exception as exc:
                        self._bus.log.emit(
                            f"[TestManager] ERROR saving partial recording for "
                            f"config={config_id}: {exc}"
                        )
                    else:
                        self._bus.log.emit(
                            f"[TestManager] saved partial recording for config={config_id}"
                        )
        finally:
            if mon is not None:
                mon.stop()
                mon.wait(1000)

            if rec is not None:
                self._stop_recorder_if_unused(rec)

            if factory_abort_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_aborted",
                    factory_abort_payload,
                )

            if not silent:
                self._bus.log.emit(f"[TestManager] stopped config={config_id}")
                self._bus.status.emit("Stopped")

    @staticmethod
    def _get_factory_start_request_id(workorder: dict) -> str | None:
        request_id = workorder.get("factory_start_request_id")
        if not request_id:
            return None
        return str(request_id)

    def _factory_start_canceled(self, request_id: str | None) -> bool:
        return bool(request_id) and request_id in self._canceled_factory_start_requests

    def _clear_pending_factory_start(self, request_id: str | None) -> None:
        if not request_id:
            return
        self._pending_factory_starts.pop(request_id, None)
        self._canceled_factory_start_requests.discard(request_id)

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
            summary_payload = build_summary_payload(
                config={},
                judgment_payload=None,
                ai_review=None,
                error_message=error_message,
            )
            self._emit_optional_signal(
                "factory_runtime_reanalysis_completed",
                self._make_factory_review_payload(
                    config_id,
                    None,
                    no_result=True,
                    error_message=error_message,
                    summary_payload=summary_payload,
                ),
            )
            return
        try:
            result = session.rejudge(start_sec, end_sec)
            result_dict = self._serialize_result(result)
        except Exception as exc:
            payload = self._emit_no_result(config_id, exc, context="rejudge")
            summary_payload = self._build_summary_payload_for_error(
                session,
                error_message=payload["error_summary"],
            )
            self._emit_optional_signal(
                "factory_runtime_reanalysis_completed",
                self._make_factory_review_payload(
                    config_id,
                    session,
                    no_result=True,
                    error_message=payload["error_summary"],
                    summary_payload=summary_payload,
                ),
            )
            return

        summary_payload = self._build_summary_payload(
            session,
            judgment_payload=result_dict,
        )
        chart_payload = self._build_chart_payload(
            session,
            analysis_window=result_dict.get("analysis_window"),
            judgment_payload=result_dict,
        )
        self._bus.test_finished.emit(config_id, result_dict)
        self._bus.result_changed.emit(result_dict)
        self._emit_optional_signal(
            "factory_runtime_reanalysis_completed",
            self._make_factory_review_payload(
                config_id,
                session,
                judgment_payload=result_dict,
                summary_payload=summary_payload,
                chart_payload=chart_payload,
            ),
        )

    def _on_recording_done(self, config_id: int):
        """Called when a recorder fills its buffer."""
        session = self._sessions.get(config_id)
        if (
            session is None
            or getattr(session, "_finished", False)
            or config_id in self._accepted_factory_stops
        ):
            return
        session._finished = True
        self._on_session_done(config_id)

    def _on_session_done(self, config_id: int):
        session = self._sessions.get(config_id)
        if session is None or config_id in self._accepted_factory_stops:
            return

        cleanup_settled_payload = self._make_cleanup_settled_payload(config_id, session)
        self._analyzing_sessions.add(config_id)
        try:
            self._emit_factory_analyzing_entered(
                config_id,
                session,
                log_message=f"[TestManager] recording done for config={config_id}",
            )

            rec = self._recorders.get(config_id)
            mon = self._monitors.get(config_id)
            if rec is not None:
                session._buffer = rec.get_buffer_from(getattr(session, "_attach_snapshot", None))
                session._sample_rate = rec._sample_rate
                self._disconnect_session_signals(session, rec, mon)

            if not self._emit_auto_analysis_review(config_id, session):
                return

            completed_session = self._sessions.pop(config_id, None)
            if completed_session is not None:
                self._completed_sessions[config_id] = completed_session

            self._recorders.pop(config_id, None)
            mon = self._monitors.pop(config_id, None)
            if mon is not None:
                mon.stop()
                mon.wait(1000)
            if rec is not None:
                self._stop_recorder_if_unused(rec)
            if cleanup_settled_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_auto_analysis_cleanup_settled",
                    cleanup_settled_payload,
                )
        finally:
            self._analyzing_sessions.discard(config_id)

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
        error_message: str | None,
    ):
        from app.services_container import FactoryRuntimeAbortPayload

        return FactoryRuntimeAbortPayload(
            config_id=config_id,
            run_id=self._factory_run_id(session),
            trigger_source=self._factory_trigger_source(session),
            error_message=error_message,
        )

    def _make_factory_runtime_stopped_payload(self, config_id: int, session):
        from app.services_container import FactoryRuntimeStoppedPayload

        run_id = self._factory_run_id(session)
        trigger_source = self._factory_trigger_source(session)
        if run_id is None or trigger_source is None:
            return None

        return FactoryRuntimeStoppedPayload(
            config_id=config_id,
            run_id=run_id,
            trigger_source=trigger_source,
        )

    def _make_cleanup_settled_payload(self, config_id: int, session):
        factory_run_id = self._factory_run_id(session)
        if factory_run_id is None:
            return None
        return self._make_factory_runtime_config_payload(
            config_id,
            factory_run_id,
        )

    def _emit_factory_analyzing_entered(
        self,
        config_id: int,
        session,
        *,
        log_message: str,
    ) -> None:
        self._bus.status.emit("Analyzing...")
        self._bus.log.emit(log_message)
        self._emit_optional_signal(
            "factory_runtime_analyzing_entered",
            self._make_factory_runtime_config_payload(
                config_id,
                self._factory_run_id(session),
            ),
        )

    def _auto_analysis_review_still_owned(
        self,
        config_id: int,
        session,
        *,
        allow_accepted_stop: bool = False,
    ) -> bool:
        return (
            self._sessions.get(config_id) is session
            and (allow_accepted_stop or config_id not in self._accepted_factory_stops)
        )

    def _emit_auto_analysis_review(
        self,
        config_id: int,
        session,
        *,
        allow_accepted_stop: bool = False,
    ) -> bool:
        analysis_error = None
        result = None
        result_dict = None
        try:
            result = session._run_judgment()
            result_dict = self._serialize_result(result)
        except Exception as exc:
            analysis_error = exc

        if not self._auto_analysis_review_still_owned(
            config_id,
            session,
            allow_accepted_stop=allow_accepted_stop,
        ):
            return False

        if analysis_error is not None:
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            payload = self._emit_no_result(config_id, analysis_error, context="analysis")
            signal_name = (
                "factory_runtime_auto_analysis_no_result"
                if self._is_factory_no_result_error(analysis_error)
                else "factory_runtime_auto_analysis_error"
            )
            summary_payload = self._build_summary_payload_for_error(
                session,
                error_message=payload["error_summary"],
            )
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            self._emit_optional_signal(
                signal_name,
                self._make_factory_review_payload(
                    config_id,
                    session,
                    no_result=True,
                    error_message=payload["error_summary"],
                    summary_payload=summary_payload,
                ),
            )
        else:
            summary_payload = self._build_summary_payload(
                session,
                judgment_payload=result_dict,
            )
            chart_payload = self._build_chart_payload(
                session,
                analysis_window=result_dict.get("analysis_window"),
                judgment_payload=result_dict,
            )
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            self._bus.test_finished.emit(config_id, result_dict)
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            self._bus.result_changed.emit(result_dict)
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            self._bus.log.emit(
                f"[TestManager] result: {result.overall} (limit={result.limit_pass})"
            )
            if not self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ):
                return False
            self._emit_optional_signal(
                "factory_runtime_auto_analysis_succeeded",
                self._make_factory_review_payload(
                    config_id,
                    session,
                    judgment_payload=result_dict,
                    summary_payload=summary_payload,
                    chart_payload=chart_payload,
                ),
            )

        if not self._auto_analysis_review_still_owned(
            config_id,
            session,
            allow_accepted_stop=allow_accepted_stop,
        ):
            return False
        self._bus.status.emit("Idle")
        return True

    def _stop_recording_with_optional_analysis(self, config_id: int, session) -> bool:
        rec = self._recorders.get(config_id)
        mon = self._monitors.get(config_id)
        if rec is not None:
            self._disconnect_session_signals(session, rec, mon)

        capture = self._capture_partial_stop_buffer(config_id, session, rec)
        try:
            self._stop_factory_runtime_io(config_id, rec=rec, mon=mon)
        except Exception as exc:
            return self._emit_factory_stop_abort(
                config_id,
                session,
                exc,
                rec=rec,
                mon=mon,
            )

        if self._stop_is_analysis_eligible(
            session,
            captured_duration_sec=capture["captured_duration_sec"],
            partial_wav_ok=capture["partial_wav_ok"],
        ):
            return self._run_stop_triggered_auto_analysis(
                config_id,
                session,
            )
        return self._emit_factory_runtime_stopped(config_id, session)

    def _stop_to_stopped(self, config_id: int, session) -> bool:
        rec = self._recorders.get(config_id)
        mon = self._monitors.get(config_id)
        if rec is not None:
            self._disconnect_session_signals(session, rec, mon)

        try:
            self._stop_factory_runtime_io(config_id, rec=rec, mon=mon)
        except Exception as exc:
            return self._emit_factory_stop_abort(
                config_id,
                session,
                exc,
                rec=rec,
                mon=mon,
            )
        return self._emit_factory_runtime_stopped(config_id, session)

    def _capture_partial_stop_buffer(self, config_id: int, session, rec) -> dict[str, object]:
        partial_buf = None
        partial_wav_ok = False

        if rec is None:
            return {
                "partial_buf": None,
                "partial_wav_ok": False,
                "captured_duration_sec": self._captured_duration_sec(session),
            }

        try:
            partial_buf = rec.get_buffer_from(getattr(session, "_attach_snapshot", None))
        except Exception as exc:
            self._bus.log.emit(
                f"[TestManager] ERROR capturing partial recording for config={config_id}: {exc}"
            )
            return {
                "partial_buf": None,
                "partial_wav_ok": False,
                "captured_duration_sec": None,
            }

        if isinstance(partial_buf, np.ndarray):
            session._buffer = partial_buf
            session._sample_rate = rec._sample_rate

        captured_duration_sec = self._captured_duration_sec(session)
        if isinstance(partial_buf, np.ndarray) and partial_buf.size > 0:
            try:
                saved_path = session._save_wav(partial_buf, rec._sample_rate)
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR saving partial recording for config={config_id}: {exc}"
                )
            else:
                if isinstance(saved_path, str) and saved_path:
                    session._audio_file_path = saved_path
                partial_wav_ok = bool(getattr(session, "_audio_file_path", None))
                self._bus.log.emit(
                    f"[TestManager] saved partial recording for config={config_id}"
                )

        if captured_duration_sec is None:
            self._bus.log.emit(
                f"[TestManager] WARNING captured duration unavailable for config={config_id}"
            )

        return {
            "partial_buf": partial_buf,
            "partial_wav_ok": partial_wav_ok,
            "captured_duration_sec": captured_duration_sec,
        }

    def _stop_is_analysis_eligible(
        self,
        session,
        *,
        captured_duration_sec: float | None,
        partial_wav_ok: bool,
    ) -> bool:
        config = self._session_config(session)
        if not bool(config.get("allow_incomplete_recording_analysis", False)):
            return False
        if captured_duration_sec is None or not partial_wav_ok:
            return False
        required_window_end_sec = self._configured_window_end_sec(config)
        return required_window_end_sec is not None and captured_duration_sec >= required_window_end_sec

    def _run_stop_triggered_auto_analysis(self, config_id: int, session) -> bool:
        cleanup_settled_payload = self._make_cleanup_settled_payload(config_id, session)
        self._analyzing_sessions.add(config_id)
        self._emit_factory_analyzing_entered(
            config_id,
            session,
            log_message=f"[TestManager] stop-triggered analysis for config={config_id}",
        )
        try:
            if not self._emit_auto_analysis_review(
                config_id,
                session,
                allow_accepted_stop=True,
            ):
                return True

            completed_session = self._sessions.pop(config_id, None)
            if completed_session is not None:
                self._completed_sessions[config_id] = completed_session

            if cleanup_settled_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_auto_analysis_cleanup_settled",
                    cleanup_settled_payload,
                )
            return True
        finally:
            self._analyzing_sessions.discard(config_id)
            self._accepted_factory_stops.discard(config_id)

    def _stop_factory_runtime_io(self, config_id: int, *, rec, mon) -> None:
        cleanup_error = None

        try:
            if mon is not None:
                mon.stop()
                mon.wait(1000)
        except Exception as exc:
            cleanup_error = exc

        try:
            if rec is not None:
                self._stop_recorder_if_unused(rec, excluding_config_id=config_id)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
        finally:
            self._recorders.pop(config_id, None)
            self._monitors.pop(config_id, None)

        if cleanup_error is not None:
            raise cleanup_error

    def _emit_factory_runtime_stopped(self, config_id: int, session) -> bool:
        self._analyzing_sessions.discard(config_id)
        self._accepted_factory_stops.discard(config_id)
        self._sessions.pop(config_id, None)
        stopped_payload = self._make_factory_runtime_stopped_payload(config_id, session)
        if stopped_payload is not None:
            self._emit_optional_signal(
                "factory_runtime_stopped",
                stopped_payload,
            )
        self._bus.log.emit(f"[TestManager] stopped config={config_id}")
        self._bus.status.emit("Stopped")
        return True

    def _emit_factory_stop_abort(self, config_id: int, session, error: Exception, *, rec, mon) -> bool:
        self._analyzing_sessions.discard(config_id)
        self._accepted_factory_stops.discard(config_id)
        self._sessions.pop(config_id, None)
        self._recorders.pop(config_id, None)
        self._monitors.pop(config_id, None)
        if rec is not None:
            self._discard_shared_recorder(rec)
        self._bus.log.emit(f"[TestManager] ERROR stopping config={config_id}: {error}")
        self._emit_optional_signal(
            "factory_runtime_aborted",
            self._make_factory_runtime_abort_payload(
                config_id,
                session,
                error_message=str(error),
            ),
        )
        self._bus.status.emit(f"Error: {error}")
        return False

    def _session_is_analyzing(self, config_id: int) -> bool:
        return config_id in self._analyzing_sessions

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
        summary_payload: dict | None = None,
        chart_payload: dict | None = None,
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
            summary_payload=summary_payload,
            chart_payload=chart_payload,
        )

    @staticmethod
    def _session_config(session) -> dict:
        config = getattr(session, "_config", None)
        if isinstance(config, dict):
            return config
        return {}

    def _build_summary_payload(self, session, *, judgment_payload: dict) -> dict:
        return build_summary_payload(
            config=self._session_config(session),
            judgment_payload=judgment_payload,
            ai_review=getattr(session, "last_ai_review", None),
            error_message=None,
        )

    def _build_summary_payload_for_error(self, session, *, error_message: str) -> dict:
        return build_summary_payload(
            config=self._session_config(session),
            judgment_payload=None,
            ai_review=None,
            error_message=error_message,
        )

    def _build_chart_payload(
        self,
        session,
        *,
        analysis_window,
        judgment_payload: dict | None = None,
    ) -> dict | None:
        mic_cal = getattr(session, "_mic_cal", None)
        if not isinstance(mic_cal, dict):
            mic_cal = {}
        chart_payload = build_chart_payload(
            buffer=getattr(session, "_buffer", None),
            config=TestManager._session_config(session),
            sample_rate=float(getattr(session, "_sample_rate", 0) or 0),
            mic_cal=mic_cal,
            analysis_window=analysis_window,
            judgment_payload=judgment_payload,
        )
        self._warn_loudness_chart_issues(
            session,
            judgment_payload=judgment_payload,
            chart_payload=chart_payload,
        )
        return chart_payload

    def _warn_loudness_chart_issues(
        self,
        session,
        *,
        judgment_payload: dict | None,
        chart_payload: dict | None,
    ) -> None:
        if not isinstance(judgment_payload, dict):
            return

        config = self._session_config(session)
        config_id = config.get("config_id", getattr(session, "config_id", None))
        for channel_result in judgment_payload.get("channel_results", []):
            if not isinstance(channel_result, dict):
                continue

            channel_index = channel_result.get("channel_index")
            if not isinstance(channel_index, int) or isinstance(channel_index, bool) or channel_index < 0:
                continue
            if channel_result.get("loudness_available") is not True:
                continue

            channel_payload = chart_payload.get(channel_index) if isinstance(chart_payload, dict) else None
            loudness_payload = (
                channel_payload.get("loudness")
                if isinstance(channel_payload, dict)
                else None
            )
            if not isinstance(loudness_payload, dict):
                self._bus.log.emit(
                    "[TestManager] WARNING: loudness chart recompute failed "
                    f"for config={config_id} channel=CH{channel_index + 1}"
                )
                continue

            serialized_peak = self._coerce_finite_float(channel_result.get("loudness_max_sone"))
            chart_peak = self._loudness_chart_peak_sone(loudness_payload)
            if serialized_peak is None or chart_peak is None:
                self._bus.log.emit(
                    "[TestManager] WARNING: loudness chart recompute failed "
                    f"for config={config_id} channel=CH{channel_index + 1}"
                )
                continue

            if not np.isclose(chart_peak, serialized_peak, atol=1e-4, rtol=1e-4):
                self._bus.log.emit(
                    "[TestManager] WARNING: loudness chart scalar mismatch "
                    f"for config={config_id} channel=CH{channel_index + 1} "
                    f"serialized={serialized_peak:g} recomputed={chart_peak:g}"
                )

    @staticmethod
    def _loudness_chart_peak_sone(loudness_payload: dict) -> float | None:
        values = loudness_payload.get("values")
        if not isinstance(values, list) or not values:
            return None

        unit = str(loudness_payload.get("unit") or "").strip().lower()
        normalized_values = []
        for value in values:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            if not np.isfinite(numeric):
                return None
            normalized_values.append(numeric)

        if unit == "phon":
            return max(dsp.phon_to_sone(value) for value in normalized_values)
        if unit == "sone":
            return max(max(value, 0.0) for value in normalized_values)
        return None

    @staticmethod
    def _coerce_finite_float(value) -> float | None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(numeric):
            return None
        return numeric

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
    def _configured_window_end_sec(config: dict) -> float | None:
        analysis_duration = TestManager._analysis_duration_sec(config)
        if analysis_duration <= 0:
            return None
        try:
            start_sec = float(config.get("analysis_window_offset_ms", 0) or 0) / 1000.0
        except (TypeError, ValueError, AttributeError):
            return None
        return start_sec + analysis_duration

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

    def _stop_recorder_if_unused(self, recorder, *, excluding_config_id: int | None = None):
        still_used = any(
            cid != excluding_config_id and r is recorder
            for cid, r in self._recorders.items()
        )
        if not still_used:
            recorder.stop()
            recorder.wait(3000)
            self._discard_shared_recorder(recorder)

    def _discard_shared_recorder(self, recorder) -> None:
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
