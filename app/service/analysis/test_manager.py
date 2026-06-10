"""TestManager - replaces the fake TestService.

Manages concurrent test sessions. Exposes the same start(workorder)/stop()
API so ControlModule needs zero changes.
"""
from __future__ import annotations

import copy
import inspect
import os
import uuid
from dataclasses import asdict, is_dataclass
from numbers import Integral

import numpy as np
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal

from app.service.analysis import dsp
from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.monitor_playback import (
    MONITOR_GAIN_MAX_DB,
    MONITOR_GAIN_MIN_DB,
    OneShotReplayPlayer,
)
from app.service.analysis.review_payloads import (
    attach_stft_score_summary,
    build_chart_payload,
    build_summary_payload,
)
from app.service.analysis.selected_channels import normalize_selected_channels
from app.service.analysis.test_session import ManualLabelUpdateResult, TestSession
from app.service.current_meter_service import (
    CurrentMeterCollector,
    build_current_trace_payload,
    normalize_current_meter_config,
)
from consts.running_consts import base_dir


class TestManager(QObject):
    """Manages TestSession instances and shared AudioRecorders."""

    __test__ = False
    _MIN_MANUAL_REANALYSIS_DURATION_SEC = 0.1

    _deferred_monitor_error_occurred = pyqtSignal(int, str)

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
        self._last_start_error: str | None = None
        self._analyzing_sessions: set[int] = set()
        self._accepted_factory_stops: set[int] = set()
        self._live_loudness_channel_by_config_id: dict[int, int] = {}
        self._current_collectors: dict[int, CurrentMeterCollector] = {}
        self._current_samples_by_config_id: dict[int, list] = {}
        self._replay_player = OneShotReplayPlayer()
        self._replay_config_id: int | None = None

        display_channel_signal = getattr(self._bus, "factory_display_channel_changed", None)
        connect = getattr(display_channel_signal, "connect", None)
        if callable(connect):
            connect(self._on_factory_display_channel_changed)
        self._deferred_monitor_error_occurred.connect(
            self._on_monitor_error,
            Qt.ConnectionType.QueuedConnection,
        )

    def last_start_error(self) -> str | None:
        return self._last_start_error

    def analyze_imported_wav(self, request):
        from app.service.analysis import imported_audio

        config = self._services.config_service.load_config(request.config_id)
        device_info = self._services.hss_service.load_io_devices()
        mic_cal = device_info.get("mic_cal", {}) if isinstance(device_info, dict) else {}
        if not isinstance(mic_cal, dict):
            mic_cal = {}
        return imported_audio.analyze_imported_wav(
            request=request,
            config=config,
            mic_cal=mic_cal,
            judgment_engine=self._create_judgment_engine(),
        )

    def _create_judgment_engine(self) -> JudgmentEngine:
        config_service = getattr(self._services, "config_service", None)
        golden_sample_service = getattr(config_service, "golden_sample_service", None)
        return JudgmentEngine(
            self._services.ai_service,
            golden_sample_service=golden_sample_service,
        )

    def start(self, workorder: dict, *, on_start_accepted=None) -> bool:
        """Start a test for the config_id in the workorder."""
        self._clear_last_start_error()
        request_id = self._get_factory_start_request_id(workorder)
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
            return self._fail_start_before_acceptance(
                request_id,
                "无法解析启动配置，请重新选择配置。",
            )

        if request_id is not None:
            self._pending_factory_starts[request_id] = config_id

        if config_id in self._sessions:
            self._bus.log.emit(f"[TestManager] config {config_id} already running, stopping first")
            self.stop(config_id=config_id)

        try:
            config = self._services.config_service.load_config(config_id)
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR loading config {config_id}: {exc}")
            return self._fail_start_before_acceptance(
                request_id,
                f"配置加载失败：{exc}",
            )

        try:
            device_info = self._services.hss_service.load_io_devices()
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR loading input hardware: {exc}")
            return self._fail_start_before_acceptance(
                request_id,
                f"读取硬件配置失败：{exc}",
            )

        hardware_error = self._saved_input_hardware_error(device_info)
        if hardware_error is not None:
            self._bus.log.emit(f"[TestManager] ERROR {hardware_error}")
            return self._fail_start_before_acceptance(request_id, hardware_error)

        if self._config_input_device_fields_missing(config):
            self._bus.log.emit(
                f"[TestManager] ERROR config {config_id} missing input device fields"
            )
            return self._fail_start_before_acceptance(
                request_id,
                "配置未选择输入设备，请先在「配置」页面选择输入设备并保存。",
            )

        mic_cal = device_info.get("mic_cal", {})
        if not isinstance(mic_cal, dict):
            mic_cal = {}
        audio_dir = os.path.join(base_dir, "data", "audio")

        device_key = self._device_key(config)
        input_info = device_info["input"]
        config_sr = float(config.get("input_device_samplerate", 44100))
        device_sr = float(input_info.get("samplerate", config_sr))
        if config_sr != device_sr:
            self._bus.log.emit(
                f"[TestManager] WARNING: config sample rate {config_sr} differs "
                f"from device {device_sr}, using device rate"
            )
        sample_rate = device_sr
        n_channels = self._positive_int_value(input_info.get("channel_count"))
        if n_channels is None:
            return self._fail_start_before_acceptance(
                request_id,
                "输入硬件配置无效，请重新保存硬件设备。",
            )
        try:
            normalized_selected_channels = normalize_selected_channels(
                config.get("selected_channels", []),
                hardware_channel_count=n_channels,
            )
        except ValueError as exc:
            self._bus.log.emit(
                f"[TestManager] ERROR config {config_id} invalid selected_channels: {exc}"
            )
            return self._fail_start_before_acceptance(
                request_id,
                f"选择的输入通道无效：{exc}",
            )
        config = dict(config)
        config["selected_channels"] = list(normalized_selected_channels)
        try:
            self._resolve_vibration_view_channel(
                config,
                hardware_channel_count=n_channels,
            )
        except ValueError as exc:
            self._bus.log.emit(
                f"[TestManager] ERROR config {config_id} invalid vibration view channel: {exc}"
            )
            return self._fail_start_before_acceptance(request_id, str(exc))
        try:
            resolved_monitor_channel = self._resolve_monitor_channel(
                config,
                hardware_channel_count=n_channels,
            )
            resolved_monitor_gain_db = self._resolve_monitor_gain_db(config)
        except ValueError as exc:
            self._bus.log.emit(
                f"[TestManager] ERROR config {config_id} invalid monitor controls: {exc}"
            )
            return self._fail_start_before_acceptance(request_id, str(exc))

        record_duration = config.get("record_duration", 5.0)
        session_workorder = dict(workorder)
        session_workorder["factory_run_id"] = self._new_factory_run_id()

        session = None
        recorder = None
        monitor = None

        try:
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
            self._apply_stored_live_loudness_channel(session, config)

            output_name = config.get("output_device_name")
            if output_name:
                output_sr = float(config.get("output_device_samplerate", sample_rate))
                monitor = self._create_audio_monitor(
                    device_name=config.get("output_device_name", ""),
                    hostapi=config.get("output_device_hostapi", ""),
                    sample_rate=output_sr,
                    monitor_channel=resolved_monitor_channel,
                    gain_db=resolved_monitor_gain_db,
                )
                session._monitor_lambda = lambda data, idx, m=monitor: m.feed(data)
                session._monitor_error_lambda = (
                    lambda msg, cid=config_id: self._deferred_monitor_error_occurred.emit(
                        cid,
                        msg,
                    )
                )

            session._chunk_conn = session._on_chunk
            session._done_lambda = lambda cid=config_id: self._on_recording_done(cid)
            session._error_lambda = lambda msg, cid=config_id: self._on_error(cid, msg)
            prepare_for_attach = getattr(session, "prepare_for_attach", None)
            if callable(prepare_for_attach):
                prepare_for_attach()
            if monitor is not None:
                recorder.chunk_ready.connect(session._monitor_lambda)
                monitor.error_occurred.connect(session._monitor_error_lambda)
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
            if monitor is not None:
                self._monitors[config_id] = monitor
            self._start_current_collection(config_id, session)

            if mon := self._monitors.get(config_id):
                mon.start()

            if self._factory_start_canceled(request_id):
                self._cleanup_failed_start_setup(config_id, session, recorder, monitor)
                self._clear_pending_factory_start(request_id)
                return False

            if callable(on_start_accepted):
                on_start_accepted(session_workorder["factory_run_id"])
        except Exception as exc:
            self._bus.log.emit(f"[TestManager] ERROR starting config {config_id}: {exc}")
            self._cleanup_failed_start_setup(config_id, session, recorder, monitor)
            return self._fail_start_before_acceptance(
                request_id,
                f"启动失败：{exc}",
            )

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
            self._clear_last_start_error()
            return True

        self._clear_pending_factory_start(request_id)
        self._clear_last_start_error()
        self._bus.test_started.emit(config_id)
        self._bus.status.emit("Recording...")
        self._bus.log.emit(
            f"[TestManager] started config={config_id}, workorder={session_workorder}"
        )
        return True

    def _on_factory_display_channel_changed(self, config_id, channel_index) -> None:
        normalized_config_id = self._as_non_bool_int(config_id)
        normalized_channel = self._as_non_bool_int(channel_index)
        if normalized_config_id is None or normalized_channel is None:
            return

        self._live_loudness_channel_by_config_id[normalized_config_id] = normalized_channel
        session = self._sessions.get(normalized_config_id)
        if session is None:
            return
        if not self._channel_is_selected_for_session(session, normalized_channel):
            return
        setter = getattr(session, "set_live_loudness_channel_index", None)
        if callable(setter):
            setter(normalized_channel)

    def _apply_stored_live_loudness_channel(self, session, config: dict) -> None:
        stored_channel = self._live_loudness_channel_by_config_id.get(
            self._as_non_bool_int(config.get("config_id"))
        )
        if stored_channel is None:
            return
        if stored_channel not in self._selected_channels(config):
            return
        setter = getattr(session, "set_live_loudness_channel_index", None)
        if callable(setter):
            setter(stored_channel)

    @classmethod
    def _resolve_monitor_channel(
        cls,
        config: dict,
        *,
        hardware_channel_count: int,
    ) -> int:
        raw_channel = config.get("monitor_channel_index")
        if raw_channel is None:
            resolved = None
            for selected_channel in config.get("selected_channels", []) or ():
                if isinstance(selected_channel, Integral) and not isinstance(
                    selected_channel,
                    (bool, np.bool_),
                ):
                    resolved = int(selected_channel)
                    break
            if resolved is None and hardware_channel_count > 0:
                resolved = 0
        elif isinstance(raw_channel, Integral) and not isinstance(
            raw_channel,
            (bool, np.bool_),
        ):
            resolved = int(raw_channel)
        else:
            raise ValueError("监听通道必须为不小于 0 的整数")

        if resolved is None or resolved < 0:
            raise ValueError("监听通道必须为不小于 0 的整数")
        if resolved >= hardware_channel_count:
            raise ValueError(
                f"监听通道必须在输入通道范围 0..{hardware_channel_count - 1} 内"
            )
        return resolved

    @classmethod
    def _resolve_vibration_view_channel(
        cls,
        config: dict,
        *,
        hardware_channel_count: int,
    ) -> int | None:
        raw_channel = config.get("vibration_view_channel_index")
        if raw_channel is None:
            return None
        if not isinstance(raw_channel, Integral) or isinstance(
            raw_channel,
            (bool, np.bool_),
        ):
            raise ValueError("振动视图通道必须为不小于 0 的整数")
        channel = int(raw_channel)
        if channel < 0:
            raise ValueError("振动视图通道必须为不小于 0 的整数")
        if channel >= hardware_channel_count:
            raise ValueError(
                f"振动视图通道必须在输入通道范围 0..{hardware_channel_count - 1} 内"
            )
        if channel not in cls._selected_channels(config):
            raise ValueError("振动视图通道必须来自已选择通道")
        return channel

    @staticmethod
    def _resolve_monitor_gain_db(config: dict) -> float:
        raw_gain = config.get("monitor_gain_db", 0.0)
        if raw_gain is None:
            return 0.0
        if isinstance(raw_gain, (bool, np.bool_, str, bytes)):
            raise ValueError("监听增益必须为有限数值")
        try:
            gain_db = float(raw_gain)
        except (TypeError, ValueError):
            raise ValueError("监听增益必须为有限数值") from None
        if not np.isfinite(gain_db):
            raise ValueError("监听增益必须为有限数值")
        if not MONITOR_GAIN_MIN_DB <= gain_db <= MONITOR_GAIN_MAX_DB:
            raise ValueError(
                f"监听增益必须在 {MONITOR_GAIN_MIN_DB:g} 到 "
                f"{MONITOR_GAIN_MAX_DB:g} dB 之间"
            )
        return gain_db

    @staticmethod
    def _create_audio_monitor(
        *,
        device_name: str,
        hostapi: str,
        sample_rate: float,
        monitor_channel: int,
        gain_db: float,
    ):
        kwargs = {
            "device_name": device_name,
            "hostapi": hostapi,
            "sample_rate": sample_rate,
            "monitor_channel": monitor_channel,
        }
        if TestManager._call_accepts_keyword(AudioMonitor, "gain_db"):
            kwargs["gain_db"] = gain_db
        return AudioMonitor(**kwargs)

    @staticmethod
    def _call_accepts_keyword(callable_obj, keyword: str) -> bool:
        try:
            parameters = inspect.signature(callable_obj).parameters
        except (TypeError, ValueError):
            return True
        if keyword in parameters:
            return True
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    @staticmethod
    def _channel_is_selected_for_session(session, channel_index: int) -> bool:
        config = getattr(session, "_config", None)
        if not isinstance(config, dict):
            return False
        return channel_index in TestManager._selected_channels(config)

    @staticmethod
    def _selected_channels(config: dict) -> set[int]:
        selected_channels = set()
        for channel in config.get("selected_channels", []) or ():
            normalized_channel = TestManager._as_non_bool_int(channel)
            if normalized_channel is not None:
                selected_channels.add(normalized_channel)
        return selected_channels

    @staticmethod
    def _as_non_bool_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
            self._stop_replay()
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
        self._stop_replay_if_config(config_id)
        self._stop_current_collection(config_id)
        session = self._sessions.pop(config_id, None)
        rec = self._recorders.pop(config_id, None)
        mon = self._monitors.pop(config_id, None)

        if session is not None and rec is not None:
            self._disconnect_session_signals(session, rec, mon)
        if session is not None:
            self._stop_session_live_display_safely(
                config_id,
                session,
                context="stopping",
            )

        should_notify_factory_abort = (
            notify_factory_runtime_abort
            and session is not None
            and self._factory_run_id(session) is not None
        )
        factory_abort_error_message = None

        cleanup_errors = []
        try:
            if session is not None and rec is not None and not silent:
                try:
                    partial_buf = rec.get_buffer_from(
                        getattr(session, "_attach_snapshot", None)
                    )
                    if isinstance(partial_buf, np.ndarray) and partial_buf.size > 0:
                        saved_buf = self._capture_session_recorder_buffer(
                            session,
                            partial_buf,
                            rec._sample_rate,
                        )
                        try:
                            session._save_wav(saved_buf, rec._sample_rate)
                        except Exception as exc:
                            self._bus.log.emit(
                                f"[TestManager] ERROR saving partial recording for "
                                f"config={config_id}: {exc}"
                            )
                        else:
                            self._bus.log.emit(
                                f"[TestManager] saved partial recording for config={config_id}"
                            )
                except Exception as exc:
                    factory_abort_error_message = str(exc)
                    self._bus.log.emit(
                        f"[TestManager] ERROR capturing partial recording for "
                        f"config={config_id}: {exc}"
                    )
        finally:
            cleanup_errors = self._cleanup_monitor_recorder_safely(
                config_id,
                mon=mon,
                rec=rec,
                context="stopping",
            )

            self._analyzing_sessions.discard(config_id)
            self._accepted_factory_stops.discard(config_id)

            if should_notify_factory_abort:
                if factory_abort_error_message is None and cleanup_errors:
                    factory_abort_error_message = "; ".join(
                        str(error) for error in cleanup_errors
                    )
                self._emit_optional_signal(
                    "factory_runtime_aborted",
                    self._make_factory_runtime_abort_payload(
                        config_id,
                        session,
                        error_message=factory_abort_error_message,
                    ),
                )

            if not silent:
                self._bus.log.emit(f"[TestManager] stopped config={config_id}")
                self._bus.status.emit("Stopped")

    def _cleanup_failed_start_setup(
        self,
        config_id: int,
        session=None,
        recorder=None,
        monitor=None,
    ) -> None:
        registered_session = self._sessions.pop(config_id, None)
        self._stop_current_collection(config_id)
        registered_recorder = self._recorders.pop(config_id, None)
        registered_monitor = self._monitors.pop(config_id, None)

        cleanup_session = registered_session or session
        cleanup_recorder = registered_recorder or recorder
        cleanup_monitor = registered_monitor or monitor

        if cleanup_session is not None and cleanup_recorder is not None:
            try:
                self._disconnect_session_signals(
                    cleanup_session,
                    cleanup_recorder,
                    cleanup_monitor,
                )
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR cleaning failed start signals for "
                    f"config={config_id}: {exc}"
                )

        if cleanup_session is not None:
            self._stop_session_live_display_safely(
                config_id,
                cleanup_session,
                context="failed start",
            )

        if cleanup_monitor is not None:
            try:
                cleanup_monitor.stop()
                self._wait_thread_if_not_current(cleanup_monitor, 1000)
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR cleaning failed start monitor for "
                    f"config={config_id}: {exc}"
                )

        if cleanup_recorder is not None:
            try:
                self._stop_recorder_if_unused(cleanup_recorder)
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR cleaning failed start recorder for "
                    f"config={config_id}: {exc}"
                )
                self._discard_shared_recorder(cleanup_recorder)

    @staticmethod
    def _get_factory_start_request_id(workorder: dict) -> str | None:
        request_id = workorder.get("factory_start_request_id")
        if not request_id:
            return None
        return str(request_id)

    def _clear_last_start_error(self) -> None:
        self._last_start_error = None

    def _fail_start_before_acceptance(self, request_id: str | None, message: str) -> bool:
        self._last_start_error = message
        self._clear_pending_factory_start(request_id)
        return False

    @classmethod
    def _config_input_device_fields_missing(cls, config: dict) -> bool:
        if not isinstance(config, dict):
            return True
        if not cls._non_empty_text(config.get("input_device_name")):
            return True
        if not cls._non_empty_text(config.get("input_device_hostapi")):
            return True
        return not cls._positive_float(config.get("input_device_samplerate"))

    @classmethod
    def _saved_input_hardware_error(cls, device_info) -> str | None:
        missing_message = "未配置输入硬件，请先在「硬件」页面保存输入设备。"
        invalid_message = "输入硬件配置无效，请重新保存硬件设备。"
        if not isinstance(device_info, dict) or not device_info:
            return missing_message
        if "input" not in device_info:
            return missing_message
        input_info = device_info.get("input")
        if not isinstance(input_info, dict):
            return missing_message
        if not cls._positive_float(input_info.get("samplerate")):
            return invalid_message
        if cls._positive_int_value(input_info.get("channel_count")) is None:
            return invalid_message
        return None

    @staticmethod
    def _non_empty_text(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _positive_float(value) -> bool:
        if isinstance(value, bool):
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        return bool(np.isfinite(numeric) and numeric > 0)

    @staticmethod
    def _positive_int_value(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(numeric):
            return None
        integer = int(numeric)
        if integer <= 0 or numeric != integer:
            return None
        return integer

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

    def apply_manual_judgment(
        self,
        *,
        config_id: int,
        run_id: str | None,
        label: str,
    ) -> ManualLabelUpdateResult:
        if config_id in self._sessions or config_id in self._analyzing_sessions:
            return ManualLabelUpdateResult(False, "测试仍在进行中，无法人工判定")

        session = self._completed_sessions.get(config_id)
        if session is None:
            return ManualLabelUpdateResult(False, "当前没有可判定的测试结果")
        if run_id:
            session_run_id = self._factory_run_id(session)
            if not session_run_id or session_run_id != run_id:
                return ManualLabelUpdateResult(False, "当前测试结果已过期，请重新测试")
        apply_manual_label = getattr(session, "apply_manual_label", None)
        if not callable(apply_manual_label):
            return ManualLabelUpdateResult(False, "当前测试不支持人工判定")

        result = apply_manual_label(label)
        if not getattr(result, "success", False):
            message = str(getattr(result, "message", "") or "保存人工判定失败")
            return ManualLabelUpdateResult(
                False,
                message,
                getattr(result, "audio_data_id", None),
            )

        audio_data_id = getattr(result, "audio_data_id", None)
        self._bus.log.emit(
            f"[TestManager] manual judgment saved: config={config_id}, "
            f"label={label}"
        )
        return ManualLabelUpdateResult(True, "", audio_data_id)

    def can_replay_selection(
        self,
        config_id,
        run_id,
        channel_index,
        start_sec,
        end_sec,
    ) -> bool:
        return self._validate_replay_selection(
            config_id,
            run_id,
            channel_index,
            start_sec,
            end_sec,
            log_failures=False,
        ) is not None

    def replay_selection(
        self,
        config_id,
        run_id,
        channel_index,
        start_sec,
        end_sec,
    ) -> bool:
        replay = self._prepare_replay_selection(
            config_id,
            run_id,
            channel_index,
            start_sec,
            end_sec,
        )
        if replay is None:
            return False

        if self._replay_config_id is not None:
            self._stop_replay()

        player = getattr(self, "_replay_player", None)
        play = getattr(player, "play", None)
        if not callable(play):
            self._log_replay_failure(replay["config_id"], "replay player unavailable")
            return False

        success = bool(
            play(
                replay["segment"],
                replay["sample_rate"],
                replay["device_name"],
                replay["hostapi"],
                gain_db=replay["gain_db"],
                error_callback=(
                    lambda message, cid=replay["config_id"]: self._bus.log.emit(
                        f"[TestManager] replay error for config={cid}: {message}"
                    )
                ),
            )
        )
        if success:
            self._replay_config_id = replay["config_id"]
            return True

        self._replay_config_id = None
        self._log_replay_failure(replay["config_id"], "playback did not start")
        return False

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
        attach_stft_score_summary(
            summary_payload,
            config=self._session_config(session),
            judgment_payload=result_dict,
            chart_payload=chart_payload,
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
            capture_error = None
            if rec is not None:
                try:
                    recorder_buffer = rec.get_buffer_from(
                        getattr(session, "_attach_snapshot", None)
                    )
                    self._capture_session_recorder_buffer(
                        session,
                        recorder_buffer,
                        rec._sample_rate,
                    )
                except Exception as exc:
                    capture_error = exc
                finally:
                    self._disconnect_session_signals(session, rec, mon)

            self._stop_session_live_display_safely(
                config_id,
                session,
                context="recording completion",
            )
            self._stop_current_collection(config_id)

            if capture_error is not None:
                if not self._emit_auto_analysis_failure_review(
                    config_id,
                    session,
                    capture_error,
                    context="capture",
                ):
                    return
            elif not self._emit_auto_analysis_review(config_id, session):
                return

            completed_session = self._sessions.pop(config_id, None)
            if completed_session is not None:
                self._completed_sessions[config_id] = completed_session

            self._recorders.pop(config_id, None)
            mon = self._monitors.pop(config_id, None)
            self._cleanup_monitor_recorder_safely(
                config_id,
                mon=mon,
                rec=rec,
                context="recording completion",
            )
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
        abort_payload = None
        if session is not None:
            abort_payload = self._make_factory_runtime_abort_payload(
                config_id,
                session,
                error_message=message,
            )
        self._stop_one(config_id, silent=True)
        if abort_payload is not None:
            self._emit_optional_signal("factory_runtime_aborted", abort_payload)

    def _on_monitor_error(self, config_id: int, message: str):
        if self._is_nonfatal_monitor_diagnostic(message):
            self._bus.log.emit(
                f"[TestManager] monitor diagnostic config={config_id}: {message}"
            )
            return
        self._on_error(config_id, message)

    @staticmethod
    def _is_nonfatal_monitor_diagnostic(message: str) -> bool:
        if not isinstance(message, str):
            return False
        if message in {
            "Monitoring buffer overflow",
            "Monitoring queue overflow",
            "Monitoring output underrun",
        }:
            return True
        return message.startswith("Monitoring output status:")

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

    def _emit_no_result(
        self,
        config_id: int,
        error: Exception | str,
        *,
        context: str,
        still_owned=None,
    ):
        payload = self._make_no_result_payload(error)
        if callable(still_owned) and not still_owned():
            return None
        self._bus.test_finished.emit(config_id, payload)
        if callable(still_owned) and not still_owned():
            return None
        self._bus.result_changed.emit(payload)
        if callable(still_owned) and not still_owned():
            return None
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
            payload = self._emit_no_result(
                config_id,
                analysis_error,
                context="analysis",
                still_owned=lambda: self._auto_analysis_review_still_owned(
                    config_id,
                    session,
                    allow_accepted_stop=allow_accepted_stop,
                ),
            )
            if payload is None:
                return False
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
            review_payload = self._make_factory_review_payload(
                config_id,
                session,
                no_result=True,
                error_message=payload["error_summary"],
                summary_payload=summary_payload,
            )
            self._emit_optional_signal(
                signal_name,
                review_payload,
            )
            self._emit_plc_test_result_if_needed(session, review_payload)
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
            attach_stft_score_summary(
                summary_payload,
                config=self._session_config(session),
                judgment_payload=result_dict,
                chart_payload=chart_payload,
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
            review_payload = self._make_factory_review_payload(
                config_id,
                session,
                judgment_payload=result_dict,
                summary_payload=summary_payload,
                chart_payload=chart_payload,
            )
            self._emit_optional_signal(
                "factory_runtime_auto_analysis_succeeded",
                review_payload,
            )
            self._emit_plc_test_result_if_needed(session, review_payload)

        if not self._auto_analysis_review_still_owned(
            config_id,
            session,
            allow_accepted_stop=allow_accepted_stop,
        ):
            return False
        self._bus.status.emit("Idle")
        return True

    def _emit_auto_analysis_failure_review(
        self,
        config_id: int,
        session,
        error: Exception,
        *,
        context: str,
        allow_accepted_stop: bool = False,
    ) -> bool:
        if not self._auto_analysis_review_still_owned(
            config_id,
            session,
            allow_accepted_stop=allow_accepted_stop,
        ):
            return False

        payload = self._emit_no_result(
            config_id,
            error,
            context=context,
            still_owned=lambda: self._auto_analysis_review_still_owned(
                config_id,
                session,
                allow_accepted_stop=allow_accepted_stop,
            ),
        )
        if payload is None:
            return False

        signal_name = (
            "factory_runtime_auto_analysis_no_result"
            if self._is_factory_no_result_error(error)
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

        review_payload = self._make_factory_review_payload(
            config_id,
            session,
            no_result=True,
            error_message=payload["error_summary"],
            summary_payload=summary_payload,
        )
        self._emit_optional_signal(
            signal_name,
            review_payload,
        )
        self._emit_plc_test_result_if_needed(session, review_payload)
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
        self._stop_session_live_display_safely(
            config_id,
            session,
            context="factory stop",
        )

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

        capture_error = capture.get("capture_error")
        if capture_error is not None:
            return self._run_stop_triggered_capture_failure(
                config_id,
                session,
                capture_error,
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
        self._stop_session_live_display_safely(
            config_id,
            session,
            context="factory stop",
        )

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
                "capture_error": None,
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
                "capture_error": None,
            }

        if isinstance(partial_buf, np.ndarray):
            try:
                partial_buf = self._capture_session_recorder_buffer(
                    session,
                    partial_buf,
                    rec._sample_rate,
                )
            except Exception as exc:
                self._bus.log.emit(
                    f"[TestManager] ERROR capturing partial recording for "
                    f"config={config_id}: {exc}"
                )
                return {
                    "partial_buf": None,
                    "partial_wav_ok": False,
                    "captured_duration_sec": None,
                    "capture_error": exc,
                }

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
            "capture_error": None,
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

    def _run_stop_triggered_capture_failure(
        self,
        config_id: int,
        session,
        error: Exception,
    ) -> bool:
        cleanup_settled_payload = self._make_cleanup_settled_payload(config_id, session)
        self._analyzing_sessions.add(config_id)
        self._emit_factory_analyzing_entered(
            config_id,
            session,
            log_message=f"[TestManager] stop-triggered capture failed for config={config_id}: {error}",
        )
        try:
            if not self._emit_auto_analysis_failure_review(
                config_id,
                session,
                error,
                context="capture",
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
                self._wait_thread_if_not_current(mon, 1000)
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
            self._stop_current_collection(config_id)

        if cleanup_error is not None:
            raise cleanup_error

    def _emit_factory_runtime_stopped(self, config_id: int, session) -> bool:
        self._analyzing_sessions.discard(config_id)
        self._accepted_factory_stops.discard(config_id)
        completed_session = self._sessions.pop(config_id, None)
        if (
            completed_session is not None
            and getattr(completed_session, "_audio_data_id", None)
            and callable(getattr(completed_session, "apply_manual_label", None))
        ):
            self._completed_sessions[config_id] = completed_session
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
        if rec is not None and not self._recorder_has_live_owner(rec):
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
            and captured_duration_sec >= self._MIN_MANUAL_REANALYSIS_DURATION_SEC
            and getattr(session, "_audio_file_path", None) is not None
        )
        fallback_window = None
        if manual_reanalysis_allowed and (
            default_window is None
            or default_window.start_sec < 0
            or default_window.end_sec > captured_duration_sec
        ):
            fallback_duration = (
                analysis_duration
                if analysis_duration > 0
                else self._MIN_MANUAL_REANALYSIS_DURATION_SEC
            )
            fallback_duration = min(float(fallback_duration), captured_duration_sec)
            fallback_window = FactoryReviewWindow(
                start_sec=0.0,
                end_sec=fallback_duration,
            )

        return FactoryReviewPayload(
            displayed_config_id=config_id,
            config_payload=copy.deepcopy(config) if isinstance(config, dict) else None,
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

    def _emit_plc_test_result_if_needed(self, session, review_payload) -> None:
        if getattr(review_payload, "trigger_source", None) != "plc":
            return
        plc_id = self._plc_id_from_session_workorder(session)
        if plc_id is None:
            self._bus.log.emit("[TestManager] PLC 测试结果未写入：未找到 plc_id")
            return
        test_result = self._make_plc_test_result_payload(session, review_payload)
        self._bus.write_test_result_sign.emit(plc_id, test_result)

    @staticmethod
    def _plc_id_from_session_workorder(session) -> int | None:
        workorder = getattr(session, "_workorder", None)
        if not isinstance(workorder, dict):
            return None
        plc_id = workorder.get("plc_id")
        if isinstance(plc_id, Integral) and not isinstance(plc_id, bool):
            return int(plc_id)
        return None

    @classmethod
    def _make_plc_test_result_payload(cls, session, review_payload) -> dict:
        summary_payload = getattr(review_payload, "summary_payload", None)
        judgment_payload = getattr(review_payload, "judgment_payload", None)
        chart_payload = getattr(review_payload, "chart_payload", None)
        passed = (
            isinstance(summary_payload, dict)
            and summary_payload.get("overall_status") == "pass"
        )
        score = cls._summary_actual_value(summary_payload, "stft_score")
        if score is None:
            score = cls._stft_score_from_chart_payload(chart_payload)
        ai_score = cls._value_from_mapping(judgment_payload, "ai_score")
        if ai_score is None:
            ai_score = cls._summary_actual_value(summary_payload, "ai")

        return {
            "finished": True,
            "error_code": "0",
            "sn_code": cls._session_sn_code(session),
            "test_result": passed,
            "score": cls._plc_float_value(score),
            "spl": cls._plc_float_value(
                cls._max_channel_result_value(judgment_payload, "spl_max_db")
            ),
            "loudness": cls._plc_float_value(
                cls._max_channel_result_value(judgment_payload, "loudness_max_sone")
            ),
            "vibrate": cls._plc_float_value(
                cls._vibration_rms_from_chart_payload(chart_payload)
            ),
            "ai_score": cls._plc_float_value(ai_score),
        }

    @staticmethod
    def _session_sn_code(session) -> str:
        workorder = getattr(session, "_workorder", None)
        if not isinstance(workorder, dict):
            return ""
        sn_code = workorder.get("sn")
        if sn_code is None:
            sn_code = workorder.get("sn_code")
        if sn_code is None:
            return ""
        return str(sn_code).strip()

    @classmethod
    def _summary_actual_value(cls, summary_payload, item_key: str) -> float | None:
        if not isinstance(summary_payload, dict):
            return None
        rows = summary_payload.get("rows")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("item_key") == item_key:
                return cls._value_from_mapping(row, "actual_value")
        return None

    @classmethod
    def _max_channel_result_value(cls, judgment_payload, field_name: str) -> float | None:
        if not isinstance(judgment_payload, dict):
            return None
        values = []
        channel_results = judgment_payload.get("channel_results")
        if not isinstance(channel_results, list):
            return None
        for channel_result in channel_results:
            value = cls._value_from_mapping(channel_result, field_name)
            if value is not None:
                values.append(value)
        return max(values) if values else None

    @classmethod
    def _stft_score_from_chart_payload(cls, chart_payload) -> float | None:
        for channel_payload in cls._iter_chart_channel_payloads(chart_payload):
            score_payload = channel_payload.get("stft_score")
            if isinstance(score_payload, dict):
                score = cls._value_from_mapping(score_payload, "summary_score")
                if score is not None:
                    return score
        return None

    @classmethod
    def _vibration_rms_from_chart_payload(cls, chart_payload) -> float | None:
        values = []
        for channel_payload in cls._iter_chart_channel_payloads(chart_payload):
            vibration_payload = channel_payload.get("vibration")
            if isinstance(vibration_payload, dict):
                value = cls._value_from_mapping(vibration_payload, "overall_rms")
                if value is not None:
                    values.append(value)
        return max(values) if values else None

    @staticmethod
    def _iter_chart_channel_payloads(chart_payload):
        if not isinstance(chart_payload, dict):
            return
        for channel_payload in chart_payload.values():
            if isinstance(channel_payload, dict):
                yield channel_payload

    @classmethod
    def _value_from_mapping(cls, mapping, key: str) -> float | None:
        if not isinstance(mapping, dict):
            return None
        value = mapping.get(key)
        if isinstance(value, bool):
            return None
        return cls._coerce_finite_float(value)

    @classmethod
    def _plc_float_value(cls, value) -> float:
        if isinstance(value, bool):
            return -1.0
        coerced = cls._coerce_finite_float(value)
        return coerced if coerced is not None else -1.0

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
        loudness_trace_cache = self._session_loudness_trace_cache(session)
        chart_payload = build_chart_payload(
            buffer=getattr(session, "_buffer", None),
            config=TestManager._session_config(session),
            sample_rate=float(getattr(session, "_sample_rate", 0) or 0),
            mic_cal=mic_cal,
            analysis_window=analysis_window,
            judgment_payload=judgment_payload,
            channel_column_map=self._session_channel_column_map(session),
            loudness_trace_cache=loudness_trace_cache,
            current_trace_payload=self._current_trace_payload(
                self._session_config(session).get("config_id"),
                current_meter=self._session_config(session).get("current_meter"),
            ),
        )
        self._warn_loudness_chart_issues(
            session,
            judgment_payload=judgment_payload,
            chart_payload=chart_payload,
        )
        return chart_payload

    def _start_current_collection(self, config_id: int, session) -> None:
        config = self._session_config(session)
        current_meter_config = config.get("current_meter")
        current_config = normalize_current_meter_config(current_meter_config)
        self._current_samples_by_config_id.pop(config_id, None)
        if not current_config["enabled"]:
            return
        run_id = self._factory_run_id(session)
        sample_callback = None
        if isinstance(run_id, str) and run_id:
            sample_callback = (
                lambda _sample, samples_snapshot, cid=config_id, rid=run_id, cfg=current_meter_config:
                self._emit_current_trace_update(cid, rid, samples_snapshot, cfg)
            )
        collector = CurrentMeterCollector(
            current_config,
            log_callback=lambda message, cid=config_id: self._bus.log.emit(
                f"[CurrentMeter] config={cid}: {message}"
            ),
            sample_callback=sample_callback,
        )
        if collector.start():
            self._current_collectors[config_id] = collector
            self._bus.log.emit(f"[CurrentMeter] started for config={config_id}")

    def _stop_current_collection(self, config_id: int) -> None:
        collector = self._current_collectors.pop(config_id, None)
        if collector is None:
            return
        try:
            collector.stop()
        finally:
            samples = collector.samples()
            if samples:
                self._current_samples_by_config_id[config_id] = samples
            self._bus.log.emit(
                f"[CurrentMeter] stopped for config={config_id}, samples={len(samples)}"
            )

    def _current_trace_payload(
        self,
        config_id,
        *,
        current_meter: dict | None = None,
    ) -> dict | None:
        normalized_config_id = self._as_non_bool_int(config_id)
        if normalized_config_id is None:
            return None
        collector = self._current_collectors.get(normalized_config_id)
        samples = (
            collector.samples()
            if collector is not None
            else self._current_samples_by_config_id.get(normalized_config_id, [])
        )
        return build_current_trace_payload(samples, config=current_meter)

    def _emit_current_trace_update(
        self,
        config_id: int,
        run_id: str | None,
        samples,
        current_meter: dict | None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            return
        payload = build_current_trace_payload(samples, config=current_meter)
        if payload is None:
            return
        signal = getattr(self._bus, "current_trace_updated", None)
        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit(config_id, run_id, payload)

    @staticmethod
    def _capture_session_recorder_buffer(
        session,
        hardware_buffer: np.ndarray,
        sample_rate: float,
    ) -> np.ndarray:
        capture_recorder_buffer = getattr(session, "capture_recorder_buffer", None)
        if callable(capture_recorder_buffer):
            captured = capture_recorder_buffer(hardware_buffer, sample_rate)
            if isinstance(captured, np.ndarray):
                return captured
        session._buffer = hardware_buffer
        session._sample_rate = sample_rate
        return hardware_buffer

    @staticmethod
    def _session_channel_column_map(session) -> dict[int, int] | None:
        channel_map = getattr(session, "_real_to_logical_channel", None)
        if isinstance(channel_map, dict) and channel_map:
            return dict(channel_map)
        return None

    @staticmethod
    def _session_loudness_trace_cache(session) -> dict[int, dict] | None:
        session_vars = getattr(session, "__dict__", None)
        if isinstance(session_vars, dict):
            cache = session_vars.get("_last_loudness_trace_cache")
        else:
            cache = getattr(session, "_last_loudness_trace_cache", None)
        return cache if isinstance(cache, dict) else None

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

            chart_peak = self._loudness_chart_peak_sone(loudness_payload)
            if chart_peak is None:
                self._bus.log.emit(
                    "[TestManager] WARNING: loudness chart recompute failed "
                    f"for config={config_id} channel=CH{channel_index + 1}"
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

    def _prepare_replay_selection(
        self,
        config_id,
        run_id,
        channel_index,
        start_sec,
        end_sec,
    ) -> dict | None:
        replay = self._validate_replay_selection(
            config_id,
            run_id,
            channel_index,
            start_sec,
            end_sec,
            log_failures=True,
        )
        if replay is None:
            return None

        segment = self._copy_replay_segment(
            replay["buffer"],
            replay["logical_channel"],
            replay["start_frame"],
            replay["end_frame"],
        )
        if segment.size <= 0:
            self._log_replay_failure(replay["config_id"], "empty replay segment")
            return None
        if not np.all(np.isfinite(segment)):
            self._log_replay_failure(
                replay["config_id"],
                "replay segment contains non-finite samples",
            )
            return None

        replay.pop("buffer", None)
        replay.pop("logical_channel", None)
        replay.pop("start_frame", None)
        replay.pop("end_frame", None)
        replay["segment"] = segment
        return replay

    def _validate_replay_selection(
        self,
        config_id,
        run_id,
        channel_index,
        start_sec,
        end_sec,
        *,
        log_failures: bool,
    ) -> dict | None:
        normalized_config_id = self._as_non_bool_int(config_id)
        if normalized_config_id is None:
            self._maybe_log_replay_failure(log_failures, config_id, "invalid config id")
            return None

        session = self._sessions.get(normalized_config_id) or self._completed_sessions.get(
            normalized_config_id
        )
        if session is None:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "no replay session",
            )
            return None

        if run_id:
            session_run_id = self._factory_run_id(session)
            if not session_run_id or session_run_id != run_id:
                self._maybe_log_replay_failure(
                    log_failures,
                    normalized_config_id,
                    "stale replay request",
                )
                return None

        buffer = getattr(session, "_buffer", None)
        if not isinstance(buffer, np.ndarray) or buffer.size <= 0:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "no replay buffer",
            )
            return None
        if buffer.ndim not in (1, 2):
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "replay buffer must be 1-D or 2-D",
            )
            return None

        sample_rate = self._coerce_finite_float(getattr(session, "_sample_rate", None))
        if sample_rate is None or sample_rate <= 0:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "invalid replay sample rate",
            )
            return None

        channel_map = getattr(session, "_real_to_logical_channel", None)
        if not isinstance(channel_map, dict) or not channel_map:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "no replay channel map",
            )
            return None

        real_channel = self._as_non_bool_int(channel_index)
        if real_channel is None:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "invalid replay channel",
            )
            return None
        logical_channel = self._as_non_bool_int(channel_map.get(real_channel))
        if logical_channel is None:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                f"channel CH{real_channel + 1} is not replayable",
            )
            return None
        if logical_channel < 0:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "invalid replay channel map",
            )
            return None
        if buffer.ndim == 1:
            if logical_channel != 0:
                self._maybe_log_replay_failure(
                    log_failures,
                    normalized_config_id,
                    "replay channel is outside buffer",
                )
                return None
        elif logical_channel >= buffer.shape[1]:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "replay channel is outside buffer",
            )
            return None

        start = self._coerce_finite_float(start_sec)
        end = self._coerce_finite_float(end_sec)
        if start is None or end is None or end <= start:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "invalid replay window",
            )
            return None

        frame_count = int(buffer.shape[0])
        duration_sec = float(frame_count) / sample_rate
        clamped_start = max(0.0, min(start, duration_sec))
        clamped_end = max(0.0, min(end, duration_sec))
        if clamped_end <= clamped_start:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "empty replay window",
            )
            return None

        start_frame = max(0, min(frame_count, int(clamped_start * sample_rate)))
        end_frame = max(start_frame, min(frame_count, int(clamped_end * sample_rate)))
        if end_frame <= start_frame:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "empty replay window",
            )
            return None

        config = getattr(session, "_config", None)
        if not isinstance(config, dict):
            config = {}
        device_name = config.get("output_device_name")
        if not self._non_empty_text(device_name):
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                "missing replay output device",
            )
            return None
        hostapi = config.get("output_device_hostapi") or ""
        if not isinstance(hostapi, str):
            hostapi = str(hostapi)

        try:
            gain_db = self._resolve_monitor_gain_db(config)
        except ValueError as exc:
            self._maybe_log_replay_failure(
                log_failures,
                normalized_config_id,
                str(exc),
            )
            return None

        return {
            "config_id": normalized_config_id,
            "session": session,
            "buffer": buffer,
            "logical_channel": logical_channel,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "sample_rate": sample_rate,
            "device_name": device_name,
            "hostapi": hostapi,
            "gain_db": gain_db,
        }

    @staticmethod
    def _copy_replay_segment(
        buffer: np.ndarray,
        logical_channel: int,
        start_frame: int,
        end_frame: int,
    ) -> np.ndarray:
        if buffer.ndim == 1:
            selected = buffer[start_frame:end_frame]
        else:
            selected = buffer[start_frame:end_frame, logical_channel]
        return np.asarray(selected, dtype=np.float32).copy()

    def _maybe_log_replay_failure(
        self,
        should_log: bool,
        config_id,
        message: str,
    ) -> None:
        if should_log:
            self._log_replay_failure(config_id, message)

    def _log_replay_failure(self, config_id, message: str) -> None:
        self._bus.log.emit(
            f"[TestManager] replay unavailable for config={config_id}: {message}"
        )

    def _stop_replay_if_config(self, config_id: int) -> None:
        if self._replay_config_id == config_id:
            self._stop_replay()

    def _stop_replay(self) -> None:
        player = getattr(self, "_replay_player", None)
        stop = getattr(player, "stop", None)
        if callable(stop):
            try:
                stop(wait=True)
            except TypeError:
                stop()
            except Exception as exc:
                self._bus.log.emit(f"[TestManager] ERROR stopping replay: {exc}")
        self._replay_config_id = None

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

    def _stop_session_live_display_safely(
        self,
        config_id: int,
        session,
        *,
        context: str,
    ) -> None:
        stop_live_display = getattr(session, "stop_live_display", None)
        if not callable(stop_live_display):
            return
        try:
            stop_live_display()
        except Exception as exc:
            self._bus.log.emit(
                f"[TestManager] ERROR {context} cleanup live display "
                f"for config={config_id}: {exc}"
            )

    def _cleanup_monitor_recorder_safely(
        self,
        config_id: int,
        *,
        mon=None,
        rec=None,
        context: str,
        excluding_config_id: int | None = None,
    ) -> list[Exception]:
        errors: list[Exception] = []

        if mon is not None:
            self._call_cleanup_safely(
                errors,
                config_id=config_id,
                context=context,
                action="monitor stop",
                callback=mon.stop,
            )
            self._call_cleanup_safely(
                errors,
                config_id=config_id,
                context=context,
                action="monitor wait",
                callback=lambda: self._wait_thread_if_not_current(mon, 1000),
            )

        if rec is not None and not self._recorder_has_live_owner(
            rec,
            excluding_config_id=excluding_config_id,
        ):
            self._call_cleanup_safely(
                errors,
                config_id=config_id,
                context=context,
                action="recorder stop",
                callback=rec.stop,
            )
            self._call_cleanup_safely(
                errors,
                config_id=config_id,
                context=context,
                action="recorder wait",
                callback=lambda: self._wait_thread_if_not_current(rec, 3000),
            )
            self._discard_shared_recorder(rec)

        return errors

    def _call_cleanup_safely(
        self,
        errors: list[Exception],
        *,
        config_id: int,
        context: str,
        action: str,
        callback,
    ) -> None:
        try:
            callback()
        except Exception as exc:
            errors.append(exc)
            self._bus.log.emit(
                f"[TestManager] ERROR {context} cleanup {action} "
                f"for config={config_id}: {exc}"
            )

    def _recorder_has_live_owner(
        self,
        recorder,
        *,
        excluding_config_id: int | None = None,
    ) -> bool:
        return any(
            cid != excluding_config_id and active_recorder is recorder
            for cid, active_recorder in self._recorders.items()
        )

    def _stop_recorder_if_unused(self, recorder, *, excluding_config_id: int | None = None):
        if not self._recorder_has_live_owner(
            recorder,
            excluding_config_id=excluding_config_id,
        ):
            recorder.stop()
            self._wait_thread_if_not_current(recorder, 3000)
            self._discard_shared_recorder(recorder)

    def _wait_thread_if_not_current(self, thread, timeout_ms: int):
        if self._is_current_thread(thread):
            return None
        wait = getattr(thread, "wait", None)
        if callable(wait):
            return wait(timeout_ms)
        return None

    @staticmethod
    def _is_current_thread(thread) -> bool:
        is_current_thread = getattr(thread, "isCurrentThread", None)
        if callable(is_current_thread):
            try:
                result = is_current_thread()
                if isinstance(result, (bool, np.bool_)):
                    return bool(result)
            except Exception:
                pass

        try:
            current_thread = QThread.currentThread()
            return current_thread is thread or current_thread == thread
        except Exception:
            return False

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
