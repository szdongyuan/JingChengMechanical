"""TestManager - replaces the fake TestService.

Manages concurrent test sessions. Exposes the same start(workorder)/stop()
API so ControlModule needs zero changes.
"""
from __future__ import annotations

import copy
import inspect
import os
import threading
import uuid
from dataclasses import asdict, dataclass, is_dataclass
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
    attach_event_current_labels,
    attach_stft_score_summary,
    build_chart_payload,
    build_summary_payload,
)
from app.service.analysis.selected_channels import normalize_selected_channels
from app.service.analysis.session_capture import CaptureNotAcceptedError
from app.service.analysis.test_session import ManualLabelUpdateResult, TestSession
from app.service.current_meter_service import (
    CurrentMeterCollector,
    build_current_trace_payload,
    normalize_current_meter_config,
)
from app.service.factory_review_payloads import FactoryReviewPayload, FactoryReviewWindow
from consts.running_consts import base_dir


class _StartupRecorderErrorLatch:
    """Bridge one recorder generation from setup through runtime routing."""

    _STARTUP = "startup"
    _ACCEPTING = "accepting"
    _RUNTIME = "runtime"
    _INVALID = "invalid"

    def __init__(self, source_token, route_runtime_error):
        self._lock = threading.RLock()
        self._route_runtime_error = route_runtime_error
        self._source_token = source_token
        self._phase = self._STARTUP
        self._latched_error: str | None = None

    def handle(self, source_token, message) -> None:
        normalized_message = str(message)
        route_runtime_error = None
        with self._lock:
            if (
                source_token is not self._source_token
                or self._phase == self._INVALID
            ):
                return
            if self._latched_error is None:
                self._latched_error = normalized_message
            if self._phase != self._RUNTIME:
                return
            route_runtime_error = self._route_runtime_error
        route_runtime_error(normalized_message)

    def checkpoint(self, source_token) -> bool:
        """Raise the first setup error and report whether this generation is live."""

        with self._lock:
            if (
                source_token is not self._source_token
                or self._phase == self._INVALID
            ):
                return False
            self._raise_latched_error_locked()
            return True

    def begin_acceptance(self, source_token) -> bool:
        """Linearize a clean startup immediately before publishing acceptance."""

        with self._lock:
            if (
                source_token is not self._source_token
                or self._phase == self._INVALID
            ):
                return False
            if self._phase != self._STARTUP:
                raise RuntimeError("recorder startup acceptance already began")
            self._raise_latched_error_locked()
            self._phase = self._ACCEPTING
            return True

    def complete_startup(self, source_token) -> tuple[bool, str | None]:
        """Transition acceptance to runtime, or return its boundary error."""

        with self._lock:
            if (
                source_token is not self._source_token
                or self._phase == self._INVALID
            ):
                return False, self._latched_error
            if self._phase != self._ACCEPTING:
                raise RuntimeError("recorder startup acceptance is not active")
            if self._latched_error is not None:
                return True, self._latched_error
            self._phase = self._RUNTIME
            return True, None

    def error_snapshot(self, source_token) -> str | None:
        with self._lock:
            if (
                source_token is not self._source_token
                or self._phase == self._INVALID
            ):
                return None
            return self._latched_error

    def invalidate(self, source_token) -> None:
        with self._lock:
            if source_token is not self._source_token:
                return
            self._phase = self._INVALID
            self._source_token = None

    def _raise_latched_error_locked(self) -> None:
        if self._latched_error is not None:
            raise RuntimeError(self._latched_error)


class _SessionErrorRoute:
    """Identity-complete route for one manager-owned capture generation."""

    __slots__ = (
        "config_id",
        "session",
        "recorder",
        "monitor",
        "source_token",
        "generation",
        "error_latch",
    )

    def __init__(
        self,
        *,
        config_id,
        session,
        recorder,
        monitor,
        source_token,
        generation,
    ):
        self.config_id = config_id
        self.session = session
        self.recorder = recorder
        self.monitor = monitor
        self.source_token = source_token
        self.generation = generation
        self.error_latch = None


class _CurrentCollectionClaim:
    """Collector detached from one exact session generation for safe cleanup."""

    __slots__ = (
        "config_id",
        "session",
        "generation",
        "collector",
        "retain_samples",
        "pending_start",
        "_cleanup_lock",
        "_cleanup_started",
        "_cleanup_owner_thread_id",
        "_cleanup_result",
        "_cleanup_complete",
    )

    def __init__(
        self,
        *,
        config_id,
        session,
        generation,
        collector,
        retain_samples,
        pending_start=None,
    ):
        self.config_id = config_id
        self.session = session
        self.generation = generation
        self.collector = collector
        self.retain_samples = retain_samples
        self.pending_start = pending_start
        self._cleanup_lock = threading.RLock()
        self._cleanup_started = False
        self._cleanup_owner_thread_id = None
        self._cleanup_result = None
        self._cleanup_complete = threading.Event()


@dataclass(frozen=True)
class _CleanupFailureRecord:
    """One cleanup failure ordered by the instant it was caught."""

    error: BaseException
    phase: str
    classification: str
    sequence: int

    @property
    def is_fatal(self) -> bool:
        return self.classification == "fatal"


@dataclass(frozen=True)
class _CurrentCollectionCleanupResult:
    samples: object
    stop_failure: _CleanupFailureRecord | None
    samples_failure: _CleanupFailureRecord | None
    failures: tuple[_CleanupFailureRecord, ...]

    @staticmethod
    def _diagnostic_error(record) -> Exception | None:
        if record is None or record.is_fatal:
            return None
        return record.error

    @staticmethod
    def _fatal_error(record) -> BaseException | None:
        if record is None or not record.is_fatal:
            return None
        return record.error

    @property
    def stop_diagnostic_error(self) -> Exception | None:
        return self._diagnostic_error(self.stop_failure)

    @property
    def samples_diagnostic_error(self) -> Exception | None:
        return self._diagnostic_error(self.samples_failure)

    @property
    def stop_fatal_error(self) -> BaseException | None:
        return self._fatal_error(self.stop_failure)

    @property
    def samples_fatal_error(self) -> BaseException | None:
        return self._fatal_error(self.samples_failure)

    @property
    def diagnostic_error(self) -> Exception | None:
        diagnostics = [failure for failure in self.failures if not failure.is_fatal]
        if not diagnostics:
            return None
        return min(diagnostics, key=lambda failure: failure.sequence).error

    @property
    def fatal_failure(self) -> _CleanupFailureRecord | None:
        fatals = [failure for failure in self.failures if failure.is_fatal]
        if not fatals:
            return None
        return min(fatals, key=lambda failure: failure.sequence)

    @property
    def fatal_error(self) -> BaseException | None:
        fatal_failure = self.fatal_failure
        return fatal_failure.error if fatal_failure is not None else None


class _PendingCurrentContinuation:
    """Typed, identity-complete continuation waiting on one terminal snapshot."""

    __slots__ = (
        "config_id",
        "session",
        "generation",
        "request_id",
        "requester_thread_id",
        "claimed",
        "consumer_thread_id",
        "done",
        "result",
        "error",
    )

    kind = "base"

    def __init__(self, *, config_id, session, generation):
        self.config_id = config_id
        self.session = session
        self.generation = generation
        self.request_id = object()
        self.requester_thread_id = threading.get_ident()
        self.claimed = False
        self.consumer_thread_id = None
        self.done = threading.Event()
        self.result = None
        self.error = None


class _PendingCompletionContinuation(_PendingCurrentContinuation):
    kind = "completion"


class _PendingFactoryStopContinuation(_PendingCurrentContinuation):
    kind = "factory_stop"


class _PendingManualStopContinuation(_PendingCurrentContinuation):
    __slots__ = ("silent", "notify_factory_runtime_abort", "expected_error_route")
    kind = "manual_stop"

    def __init__(
        self,
        *,
        config_id,
        session,
        generation,
        silent,
        notify_factory_runtime_abort,
        expected_error_route,
    ):
        super().__init__(
            config_id=config_id,
            session=session,
            generation=generation,
        )
        self.silent = silent
        self.notify_factory_runtime_abort = notify_factory_runtime_abort
        self.expected_error_route = expected_error_route


class _PendingErrorContinuation(_PendingCurrentContinuation):
    __slots__ = ("error_route", "message")
    kind = "error"

    def __init__(self, *, error_route, message):
        super().__init__(
            config_id=error_route.config_id,
            session=error_route.session,
            generation=error_route.generation,
        )
        self.error_route = error_route
        self.message = str(message)


@dataclass(frozen=True)
class _PendingCurrentTerminalSnapshot:
    """Immutable release value for all work owned by a pending collector."""

    config_id: int
    session: object
    generation: object
    terminal_kind: str
    start_succeeded: bool
    collector_registered: bool
    cleanup_claim: _CurrentCollectionClaim | None
    cleanup_result: _CurrentCollectionCleanupResult | None
    start_error: BaseException | None
    cleanup_diagnostic_error: Exception | None
    fatal_error: BaseException | None
    owner_thread_id: int
    cleanup_owner_thread_id: int | None
    continuation_kind: str | None
    continuation_request_id: object | None

class _PendingCurrentCollection:
    """Exact collector ownership until one complete terminal snapshot is published."""

    __slots__ = (
        "config_id",
        "session",
        "generation",
        "collector",
        "start_owner_thread_id",
        "settled",
        "terminal_snapshot",
        "terminalizing",
        "cleanup_owner_thread_id",
        "cleanup_claim",
        "completion_continuation",
        "stop_continuation",
        "error_continuation",
    )

    def __init__(self, *, config_id, session, generation, collector):
        self.config_id = config_id
        self.session = session
        self.generation = generation
        self.collector = collector
        self.start_owner_thread_id = threading.get_ident()
        self.settled = threading.Event()
        self.terminal_snapshot = None
        self.terminalizing = False
        self.cleanup_owner_thread_id = None
        self.cleanup_claim = None
        self.completion_continuation = None
        self.stop_continuation = None
        self.error_continuation = None

    def owned_by_current_thread(self) -> bool:
        thread_id = threading.get_ident()
        return thread_id in {
            self.start_owner_thread_id,
            self.cleanup_owner_thread_id,
        }

    def stage_terminal(self, snapshot: _PendingCurrentTerminalSnapshot) -> None:
        """Install the immutable value while the manager lifecycle lock is held."""

        if self.terminal_snapshot is not None or self.settled.is_set():
            raise RuntimeError("pending current collection already published")
        if snapshot.config_id != self.config_id:
            raise RuntimeError("pending current terminal config identity mismatch")
        if snapshot.session is not self.session or snapshot.generation is not self.generation:
            raise RuntimeError("pending current terminal run identity mismatch")
        self.terminal_snapshot = snapshot

    def publish_terminal(self, snapshot: _PendingCurrentTerminalSnapshot) -> None:
        """Release one fully staged snapshot; Event.set is the final operation."""

        if self.terminal_snapshot is not snapshot:
            raise RuntimeError("pending current terminal snapshot was not staged")
        if self.settled.is_set():
            raise RuntimeError("pending current collection already released")
        self.settled.set()


class _CaptureFinalizationState:
    __slots__ = (
        "owner_thread_id",
        "complete",
        "buffer",
        "error",
        "failure",
    )

    def __init__(self):
        self.owner_thread_id = threading.get_ident()
        self.complete = False
        self.buffer = None
        self.error: BaseException | None = None
        self.failure: _CleanupFailureRecord | None = None


class _CaptureReleaseState:
    __slots__ = (
        "attachment",
        "owner_thread_id",
        "complete",
        "error",
        "failure",
    )

    def __init__(self, attachment):
        self.attachment = attachment
        self.owner_thread_id = threading.get_ident()
        self.complete = False
        self.error: BaseException | None = None
        self.failure: _CleanupFailureRecord | None = None


class _CleanupActionState:
    """Frozen result for one exact run/resource cleanup action."""

    __slots__ = (
        "identity",
        "owner_thread_id",
        "complete",
        "value",
        "failures",
    )

    def __init__(self, identity):
        self.identity = identity
        self.owner_thread_id = threading.get_ident()
        self.complete = threading.Event()
        self.value = None
        self.failures: tuple[_CleanupFailureRecord, ...] = ()


class TestManager(QObject):
    """Manages TestSession instances and shared AudioRecorders."""

    __test__ = False
    _MIN_MANUAL_REANALYSIS_DURATION_SEC = 0.1

    _deferred_runtime_error_occurred = pyqtSignal(object, str)
    _deferred_monitor_error_occurred = pyqtSignal(object, str)

    def __init__(self, bus, services):
        super().__init__()
        self._bus = bus
        self._services = services

        self._sessions: dict[int, TestSession] = {}
        self._completed_sessions: dict[int, TestSession] = {}
        self._recorders: dict[int, AudioRecorder] = {}
        self._monitors: dict[int, AudioMonitor] = {}
        self._session_error_routes: dict[int, _SessionErrorRoute] = {}
        self._session_generations: dict[int, object] = {}
        self._shared_recorders: dict[str, AudioRecorder] = {}
        self._pending_factory_starts: dict[str, int] = {}
        self._canceled_factory_start_requests: set[str] = set()
        self._last_start_error: str | None = None
        self._analyzing_sessions: set[int] = set()
        self._accepted_factory_stops: set[int] = set()
        self._live_loudness_channel_by_config_id: dict[int, int] = {}
        self._current_collectors: dict[int, CurrentMeterCollector] = {}
        self._current_collector_generations: dict[int, object] = {}
        self._pending_current_collections: dict[int, _PendingCurrentCollection] = {}
        self._current_collection_generations: dict[int, object] = {}
        self._current_sample_generations: dict[int, object] = {}
        self._current_samples_by_config_id: dict[int, list] = {}
        self._staged_current_samples_by_generation: dict[object, list] = {}
        self._replay_player = OneShotReplayPlayer()
        self._replay_config_id: int | None = None
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_changed = threading.Condition(self._lifecycle_lock)
        self._completion_claims: set[int] = set()
        self._cleanup_failure_lock = threading.Lock()
        self._cleanup_failure_sequence = 0
        self._cleanup_action_lock = threading.RLock()
        self._cleanup_action_states: dict[tuple, _CleanupActionState] = {}

        display_channel_signal = getattr(self._bus, "factory_display_channel_changed", None)
        connect = getattr(display_channel_signal, "connect", None)
        if callable(connect):
            connect(self._on_factory_display_channel_changed)
        self._deferred_runtime_error_occurred.connect(
            self._on_error,
            Qt.ConnectionType.QueuedConnection,
        )
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

        if request_id is not None:
            self._pending_factory_starts[request_id] = config_id

        session = None
        recorder = None
        monitor = None
        requested_capture = None
        source_token = None
        run_generation = None
        startup_error_latch = None
        error_route = None
        failed_start_cleanup_errors = None

        try:
            session = TestSession(
                config=config,
                bus=self._bus,
                ai_service=self._services.ai_service,
                mic_cal=mic_cal,
                workorder=session_workorder,
                audio_dir=audio_dir,
            )
            run_generation = object()
            session._manager_generation_token = run_generation
            session._on_session_done_cb = (
                lambda completed_config_id,
                expected_session=session,
                expected_generation=run_generation: self._on_session_done(
                    completed_config_id,
                    expected_session=expected_session,
                    expected_generation=expected_generation,
                )
            )

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

            session.set_monitor_feed(monitor.feed if monitor is not None else None)

            for capture_attempt in range(2):
                source_token = object()
                error_route = _SessionErrorRoute(
                    config_id=config_id,
                    session=session,
                    recorder=recorder,
                    monitor=monitor,
                    source_token=source_token,
                    generation=run_generation,
                )
                startup_error_latch = _StartupRecorderErrorLatch(
                    source_token,
                    lambda message, route=error_route: self._deferred_runtime_error_occurred.emit(
                        route,
                        message,
                    ),
                )
                error_route.error_latch = startup_error_latch
                session._manager_error_route = error_route
                session.prepare_for_attach(source_token)
                captured_block_conn = (
                    lambda block, s=session, token=source_token: s._on_captured_block(
                        block,
                        source_token=token,
                    )
                )
                session._captured_block_conn = captured_block_conn
                def recorder_error_conn(
                    message,
                    latch=startup_error_latch,
                    token=source_token,
                ):
                    latch.handle(
                        token,
                        message,
                    )

                session._error_lambda = recorder_error_conn
                recorder.captured_block_ready.connect(captured_block_conn)
                recorder.error_occurred.connect(
                    recorder_error_conn,
                    Qt.ConnectionType.DirectConnection,
                )
                try:
                    requested_capture = recorder.request_capture(
                        record_duration=record_duration,
                    )
                except CaptureNotAcceptedError:
                    rejection_cleanup_failures = self._disconnect_session_signals(
                        session,
                        recorder,
                    )
                    self._extend_cleanup_failures(
                        rejection_cleanup_failures,
                        self._invalidate_error_route(
                            error_route,
                            context="rejected recorder",
                        ),
                    )
                    self._discard_shared_recorder(recorder)
                    self._raise_first_fatal(
                        rejection_cleanup_failures,
                        context="rejected recorder cleanup",
                    )
                    if capture_attempt:
                        raise
                    recorder = AudioRecorder(
                        device_name=config.get("input_device_name", ""),
                        hostapi=config.get("input_device_hostapi", ""),
                        sample_rate=sample_rate,
                        n_channels=n_channels,
                        record_duration=record_duration,
                    )
                    self._shared_recorders[device_key] = recorder
                    if session._sample_rate != recorder._sample_rate:
                        session._sample_rate = recorder._sample_rate
                        session._setup_pipelines()
                        self._apply_stored_live_loudness_channel(session, config)
                    continue

                try:
                    session.attach_to_recorder(
                        requested_capture,
                        source_token=source_token,
                    )
                except Exception:
                    self._release_session_capture(
                        session,
                        recorder,
                        attachment=requested_capture,
                    )
                    requested_capture = None
                    raise
                startup_error_latch.checkpoint(source_token)
                break

            if monitor is not None:
                def monitor_error_conn(
                    message,
                    route=error_route,
                    latch=startup_error_latch,
                    token=source_token,
                ):
                    normalized_message = str(message)
                    if self._is_nonfatal_monitor_diagnostic(normalized_message):
                        self._deferred_monitor_error_occurred.emit(
                            route,
                            normalized_message,
                        )
                        return
                    latch.handle(token, normalized_message)

                session._monitor_error_lambda = monitor_error_conn
                monitor.error_occurred.connect(
                    monitor_error_conn,
                    Qt.ConnectionType.DirectConnection,
                )

            with self._lifecycle_lock:
                # A same-config replacement begins with fresh lifecycle flags;
                # cleanup from the displaced generation must not own them.
                self._analyzing_sessions.discard(config_id)
                self._accepted_factory_stops.discard(config_id)
                self._sessions[config_id] = session
                self._recorders[config_id] = recorder
                if monitor is not None:
                    self._monitors[config_id] = monitor
                self._session_error_routes[config_id] = error_route
                self._session_generations[config_id] = run_generation

            def checked_startup_state():
                latch_live = startup_error_latch.checkpoint(source_token)
                state = self._startup_error_route_state(error_route)
                if state == "active" and not latch_live:
                    return "missing"
                return state

            def finish_inactive_before_acceptance(state):
                if state in {"completed", "completing"}:
                    if callable(on_start_accepted):
                        on_start_accepted(session_workorder["factory_run_id"])
                    self._clear_pending_factory_start(request_id)
                    self._clear_last_start_error()
                    return True
                self._clear_pending_factory_start(request_id)
                return False

            state = checked_startup_state()
            if state != "active":
                return finish_inactive_before_acceptance(state)
            self._start_current_collection(config_id, session)
            state = checked_startup_state()
            if state != "active":
                return finish_inactive_before_acceptance(state)

            self._bus.recording_progress.emit(config_id, 0.0)
            state = checked_startup_state()
            if state != "active":
                return finish_inactive_before_acceptance(state)

            if monitor is not None:
                monitor.start()
                state = checked_startup_state()
                if state != "active":
                    return finish_inactive_before_acceptance(state)

            if self._factory_start_canceled(request_id):
                failed_start_cleanup_errors = self._cleanup_failed_start_setup(
                    config_id,
                    session,
                    recorder,
                    monitor,
                    requested_capture=requested_capture,
                    error_route=error_route,
                )
                self._raise_first_fatal(
                    failed_start_cleanup_errors,
                    context="canceled start rollback",
                )
                self._clear_pending_factory_start(request_id)
                return False

            if not startup_error_latch.begin_acceptance(source_token):
                return finish_inactive_before_acceptance(
                    self._startup_error_route_state(error_route)
                )
            if callable(on_start_accepted):
                on_start_accepted(session_workorder["factory_run_id"])

            if self._factory_start_canceled(request_id):
                failed_start_cleanup_errors = self._cleanup_failed_start_setup(
                    config_id,
                    session,
                    recorder,
                    monitor,
                    requested_capture=requested_capture,
                    error_route=error_route,
                )
                self._raise_first_fatal(
                    failed_start_cleanup_errors,
                    context="canceled accepted-start rollback",
                )
                self._clear_pending_factory_start(request_id)
                return False

            latch_live, acceptance_error = startup_error_latch.complete_startup(
                source_token
            )
            if acceptance_error is not None:
                self._on_error(error_route, acceptance_error)
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                return False
            if not latch_live:
                state = self._startup_error_route_state(error_route)
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                if state in {"completed", "completing"}:
                    return True
                return False
        except BaseException as exc:
            rollback_logging_errors = []
            self._report_cleanup_safely(
                rollback_logging_errors,
                f"[TestManager] ERROR starting config {config_id}: {exc}",
                phase="failed start root reporting",
            )
            if failed_start_cleanup_errors is None:
                failed_start_cleanup_errors = self._cleanup_failed_start_setup(
                    config_id,
                    session,
                    recorder,
                    monitor,
                    requested_capture=requested_capture,
                    error_route=error_route,
                )
            failed_start_cleanup_errors = (
                rollback_logging_errors + failed_start_cleanup_errors
            )
            self._clear_pending_factory_start(request_id)
            if not isinstance(exc, Exception):
                for failure in self._ordered_cleanup_failures(
                    failed_start_cleanup_errors,
                    phase="failed start rollback",
                ):
                    cleanup_exc = failure.error
                    if not failure.is_fatal or cleanup_exc is exc:
                        continue
                    self._note_secondary_fatal(
                        exc,
                        cleanup_exc,
                        context=f"failed start rollback [{failure.phase}]",
                    )
                raise
            cleanup_fatal = self._first_fatal(failed_start_cleanup_errors)
            if cleanup_fatal is not None:
                self._raise_first_fatal(
                    failed_start_cleanup_errors,
                    context="failed start rollback",
                )
            return self._fail_start_before_acceptance(
                request_id,
                f"启动失败：{exc}",
            )

        try:
            state = self._startup_error_route_state(error_route)
            if state != "active":
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                return True

            # A running shared recorder can deliver enough queued retained
            # audio while attach_to_recorder() drains its pending handoff.  The
            # session marks itself finished before manager dictionaries are
            # registered, so settle that completion before starting recorder.
            if getattr(session, "_finished", False):
                self._on_session_done(
                    config_id,
                    expected_session=session,
                    expected_generation=run_generation,
                )
                if self._startup_error_route_state(error_route) != "active":
                    self._clear_pending_factory_start(request_id)
                    self._clear_last_start_error()
                    return True
        except BaseException as exc:
            return self._rollback_accepted_start_failure(
                config_id=config_id,
                session=session,
                error_route=error_route,
                request_id=request_id,
                error=exc,
            )

        try:
            recorder_running = recorder.isRunning()
            if self._startup_error_route_state(error_route) != "active":
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                return True
            if not recorder_running:
                recorder.start()
            runtime_error = startup_error_latch.error_snapshot(source_token)
            if runtime_error is not None:
                self._on_error(error_route, runtime_error)
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                return True
        except BaseException as exc:
            return self._rollback_accepted_start_failure(
                config_id=config_id,
                session=session,
                error_route=error_route,
                request_id=request_id,
                error=exc,
            )

        try:
            if self._startup_error_route_state(error_route) != "active":
                self._clear_pending_factory_start(request_id)
                self._clear_last_start_error()
                return True

            with self._lifecycle_lock:
                if self._error_route_is_current_locked(error_route):
                    prior_completed_session = self._completed_sessions.get(config_id)
                    if (
                        prior_completed_session is not None
                        and prior_completed_session is not session
                    ):
                        self._retire_completed_owner_locked(
                            config_id,
                            prior_completed_session,
                        )

            self._clear_pending_factory_start(request_id)
            self._clear_last_start_error()
            self._bus.test_started.emit(config_id)
            if self._startup_error_route_state(error_route) != "active":
                return True
            self._bus.status.emit("Recording...")
            if self._startup_error_route_state(error_route) != "active":
                return True
            self._bus.log.emit(
                f"[TestManager] started config={config_id}, workorder={session_workorder}"
            )
            return True
        except BaseException as exc:
            return self._rollback_accepted_start_failure(
                config_id=config_id,
                session=session,
                error_route=error_route,
                request_id=request_id,
                error=exc,
            )

    def _rollback_accepted_start_failure(
        self,
        *,
        config_id: int,
        session,
        error_route,
        request_id: str | None,
        error: BaseException,
    ) -> bool:
        cleanup_errors = []
        self._report_cleanup_safely(
            cleanup_errors,
            f"[TestManager] ERROR starting config {config_id}: {error}",
            phase="accepted start rollback reporting",
        )
        try:
            stopped = self._stop_one(
                config_id,
                silent=True,
                expected_error_route=error_route,
            )
        except BaseException as cleanup_exc:
            cleanup_errors.append(
                self._record_cleanup_failure(
                    cleanup_exc,
                    phase="accepted start rollback stop",
                )
            )
            with self._lifecycle_lock:
                stopped = self._sessions.get(config_id) is not session
        if stopped:
            try:
                self._emit_optional_signal(
                    "factory_runtime_aborted",
                    self._make_factory_runtime_abort_payload(
                        config_id,
                        session,
                        error_message=str(error),
                    ),
                )
            except BaseException as cleanup_exc:
                cleanup_errors.append(
                    self._record_cleanup_failure(
                        cleanup_exc,
                        phase="accepted start rollback abort signal",
                    )
                )
        self._clear_pending_factory_start(request_id)
        if not isinstance(error, Exception):
            for failure in self._ordered_cleanup_failures(
                cleanup_errors,
                phase="accepted start rollback",
            ):
                if not failure.is_fatal:
                    continue
                self._note_secondary_fatal(
                    error,
                    failure.error,
                    context=f"accepted start rollback [{failure.phase}]",
                )
            raise error
        self._raise_first_fatal(
            cleanup_errors,
            context="accepted start rollback",
        )
        return not stopped

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
            stop_errors = []
            try:
                self._stop_replay()
            except BaseException as exc:
                stop_errors.append(
                    self._record_cleanup_failure(
                        exc,
                        phase="stop-all replay",
                    )
                )
            for cid in list(self._sessions.keys()):
                try:
                    self._stop_one(cid, silent, notify_factory_runtime_abort=True)
                except BaseException as exc:
                    stop_errors.append(
                        self._record_cleanup_failure(
                            exc,
                            phase=f"stop-all config {cid}",
                        )
                    )
            self._raise_first_fatal(stop_errors, context="stop-all cleanup")
            if stop_errors:
                raise self._cleanup_error(stop_errors[0])

    def stop_active_run(self, config_id: int | None = None) -> bool:
        """Factory-mode hook for stopping an active session."""
        if config_id is None:
            if not self._sessions:
                return False
            stop_errors = []
            for cid in list(self._sessions.keys()):
                try:
                    self._stop_factory_run(cid)
                except BaseException as exc:
                    stop_errors.append(
                        self._record_cleanup_failure(
                            exc,
                            phase=f"factory stop-all config {cid}",
                        )
                    )
            self._raise_first_fatal(
                stop_errors,
                context="factory stop-all cleanup",
            )
            if stop_errors:
                raise self._cleanup_error(stop_errors[0])
            return True

        return self._stop_factory_run(config_id)

    def _stop_factory_run(self, config_id: int) -> bool:
        with self._lifecycle_lock:
            session = self._sessions.get(config_id)
            generation = self._run_generation_token(
                session,
                self._session_error_routes.get(config_id),
            )
            pending = self._pending_current_for_identity_locked(
                config_id,
                session=session,
                generation=generation,
            )
            if pending is None:
                continuation = None
                defer_to_owner = False
                settled = None
            else:
                continuation = pending.stop_continuation
                if continuation is None:
                    if pending.terminal_snapshot is not None:
                        return False
                    continuation = _PendingFactoryStopContinuation(
                        config_id=config_id,
                        session=session,
                        generation=generation,
                    )
                    pending.stop_continuation = continuation
                defer_to_owner = (
                    pending.terminal_snapshot is None
                    and pending.owned_by_current_thread()
                )
                settled = pending.settled

        if continuation is not None:
            if defer_to_owner:
                return True
            settled.wait()
            return bool(
                self._consume_pending_current_continuation(
                    pending,
                    continuation,
                )
            )
        return self._continue_factory_stop(config_id)

    def _continue_factory_stop(
        self,
        config_id: int,
        *,
        expected_session=None,
        expected_generation=None,
        preclaimed_current_collection=None,
    ) -> bool:
        with self._lifecycle_lock:
            session = self._sessions.get(config_id)
            if session is None:
                return False
            if expected_session is not None and session is not expected_session:
                return False
            if config_id in self._accepted_factory_stops:
                return True

            self._accepted_factory_stops.add(config_id)
            session_is_analyzing = self._session_is_analyzing(config_id)
            rec = self._recorders.get(config_id)
            mon = self._monitors.get(config_id)
            error_route = self._session_error_routes.pop(config_id, None)
            generation = self._run_generation_token(session, error_route)
            if (
                expected_generation is not None
                and generation is not expected_generation
            ):
                self._accepted_factory_stops.discard(config_id)
                if error_route is not None:
                    self._session_error_routes[config_id] = error_route
                return False
            self._retire_session_generation_locked(config_id, generation)
            detached_current_collection = self._detach_current_collection_locked(
                config_id,
                session=session,
                generation=generation,
                retain_samples=True,
            )
            current_collection_claim = (
                preclaimed_current_collection
                if preclaimed_current_collection is not None
                else detached_current_collection
            )

        route_cleanup_errors = self._invalidate_error_route(
            error_route,
            context="factory stop",
        )

        if session_is_analyzing:
            return self._stop_to_stopped(
                config_id,
                session,
                rec=rec,
                mon=mon,
                current_collection_claim=current_collection_claim,
                initial_cleanup_errors=route_cleanup_errors,
            )
        return self._stop_recording_with_optional_analysis(
            config_id,
            session,
            rec=rec,
            mon=mon,
            current_collection_claim=current_collection_claim,
            initial_cleanup_errors=route_cleanup_errors,
        )

    def _stop_one(
        self,
        config_id: int,
        silent: bool = False,
        *,
        notify_factory_runtime_abort: bool = False,
        expected_error_route=None,
        expected_session=None,
        expected_generation=None,
        preclaimed_current_collection=None,
    ):
        if preclaimed_current_collection is not None:
            return self._continue_stop_one(
                config_id,
                silent,
                notify_factory_runtime_abort=notify_factory_runtime_abort,
                expected_error_route=expected_error_route,
                expected_session=expected_session,
                expected_generation=expected_generation,
                preclaimed_current_collection=preclaimed_current_collection,
            )
        with self._lifecycle_lock:
            session = self._sessions.get(config_id)
            error_route = self._session_error_routes.get(config_id)
            generation = self._run_generation_token(session, error_route)
            pending = self._pending_current_for_identity_locked(
                config_id,
                session=session,
                generation=generation,
            )
            if pending is None:
                continuation = None
                defer_to_owner = False
                settled = None
            else:
                continuation = pending.stop_continuation
                if continuation is None:
                    if pending.terminal_snapshot is not None:
                        return False
                    continuation = _PendingManualStopContinuation(
                        config_id=config_id,
                        session=session,
                        generation=generation,
                        silent=silent,
                        notify_factory_runtime_abort=notify_factory_runtime_abort,
                        expected_error_route=expected_error_route,
                    )
                    pending.stop_continuation = continuation
                defer_to_owner = (
                    pending.terminal_snapshot is None
                    and pending.owned_by_current_thread()
                )
                settled = pending.settled

        if continuation is not None:
            if defer_to_owner:
                return True
            settled.wait()
            return bool(
                self._consume_pending_current_continuation(
                    pending,
                    continuation,
                )
            )
        return self._continue_stop_one(
            config_id,
            silent,
            notify_factory_runtime_abort=notify_factory_runtime_abort,
            expected_error_route=expected_error_route,
            expected_session=expected_session,
            expected_generation=expected_generation,
        )

    def _continue_stop_one(
        self,
        config_id: int,
        silent: bool = False,
        *,
        notify_factory_runtime_abort: bool = False,
        expected_error_route=None,
        expected_session=None,
        expected_generation=None,
        preclaimed_current_collection=None,
    ):
        with self._lifecycle_lock:
            if (
                expected_error_route is not None
                and not self._error_route_is_current_locked(expected_error_route)
            ):
                return False
            current_session = self._sessions.get(config_id)
            if (
                expected_session is not None
                and current_session is not expected_session
            ):
                return False
            current_error_route = self._session_error_routes.get(config_id)
            current_generation = self._run_generation_token(
                current_session,
                current_error_route,
            )
            if (
                expected_generation is not None
                and current_generation is not expected_generation
            ):
                return False
            session = self._sessions.pop(config_id, None)
            rec = self._recorders.pop(config_id, None)
            mon = self._monitors.pop(config_id, None)
            error_route = self._session_error_routes.pop(config_id, None)
            generation = self._run_generation_token(session, error_route)
            self._retire_session_generation_locked(config_id, generation)
            detached_current_collection = self._detach_current_collection_locked(
                config_id,
                session=session,
                generation=generation,
                retain_samples=False,
            )
            current_collection_claim = (
                preclaimed_current_collection
                if preclaimed_current_collection is not None
                else detached_current_collection
            )
            self._retire_current_samples_locked(config_id, generation)
            self._analyzing_sessions.discard(config_id)
            self._accepted_factory_stops.discard(config_id)

        cleanup_errors = self._invalidate_error_route(
            error_route,
            context="manual stop",
        )
        try:
            self._stop_replay_if_config(config_id)
        except BaseException as exc:
            failure = self._record_cleanup_failure(
                exc,
                phase="manual stop replay",
            )
            cleanup_errors.append(failure)
            self._report_cleanup_safely(
                cleanup_errors,
                f"[TestManager] ERROR stopping replay for config={config_id}: {exc}",
                phase="manual stop replay",
            )

        partial_buf = None
        capture_error = None
        if session is not None:
            try:
                if rec is not None:
                    partial_buf = self._freeze_and_capture_session_buffer(session, rec)
                else:
                    session.freeze_capture()
            except BaseException as exc:
                if isinstance(exc, Exception):
                    capture_error = exc
                else:
                    cleanup_errors.append(
                        self._capture_finalization_failure(
                            session,
                            exc,
                            phase="manual stop capture freeze",
                        )
                    )
                    self._report_cleanup_safely(
                        cleanup_errors,
                        f"[TestManager] ERROR fatal capture while stopping "
                        f"config={config_id}: {exc}",
                        phase="manual stop capture freeze",
                    )
            if rec is not None:
                disconnect_failures = self._disconnect_session_signals(
                    session,
                    rec,
                    mon,
                )
                self._extend_cleanup_failures(
                    cleanup_errors,
                    disconnect_failures,
                )
                if capture_error is None:
                    disconnect_diagnostic = next(
                        (
                            failure.error
                            for failure in disconnect_failures
                            if not failure.is_fatal
                        ),
                        None,
                    )
                    if disconnect_diagnostic is not None:
                        capture_error = disconnect_diagnostic
                try:
                    self._release_session_capture(session, rec)
                except BaseException as exc:
                    if isinstance(exc, Exception) and capture_error is None:
                        capture_error = exc
                    else:
                        cleanup_errors.append(
                            self._capture_release_failure(
                                session,
                                exc,
                                phase="manual stop capture release",
                            )
                        )
                    self._report_cleanup_safely(
                        cleanup_errors,
                        f"[TestManager] ERROR releasing capture while stopping "
                        f"config={config_id}: {exc}",
                        phase="manual stop capture release",
                    )

        self._collect_current_cleanup_failures(
            current_collection_claim,
            cleanup_errors,
        )
        if session is not None:
            self._extend_cleanup_failures(
                cleanup_errors,
                self._stop_session_live_display_safely(
                    config_id,
                    session,
                    context="stopping",
                ),
            )

        should_notify_factory_abort = (
            notify_factory_runtime_abort
            and session is not None
            and self._factory_run_id(session) is not None
        )
        factory_abort_error_message = None

        try:
            if session is not None and rec is not None and not silent:
                if capture_error is not None:
                    factory_abort_error_message = str(capture_error)
                    self._report_cleanup_safely(
                        cleanup_errors,
                        f"[TestManager] ERROR capturing partial recording for "
                        f"config={config_id}: {capture_error}",
                        phase="manual stop capture reporting",
                    )
                elif isinstance(partial_buf, np.ndarray) and partial_buf.shape[0] > 0:
                    try:
                        session._save_wav(partial_buf, rec._sample_rate)
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            cleanup_errors.append(
                                self._record_cleanup_failure(
                                    exc,
                                    phase="manual stop partial save",
                                )
                            )
                        self._report_cleanup_safely(
                            cleanup_errors,
                            f"[TestManager] ERROR saving partial recording for "
                            f"config={config_id}: {exc}",
                            phase="manual stop partial save",
                        )
                    else:
                        self._report_cleanup_safely(
                            cleanup_errors,
                            f"[TestManager] saved partial recording for config={config_id}",
                            phase="manual stop partial save",
                        )
        finally:
            cleanup_errors.extend(
                self._cleanup_monitor_recorder_safely(
                    config_id,
                    session=session,
                    mon=mon,
                    rec=rec,
                    context="stopping",
                )
            )

            if should_notify_factory_abort:
                if factory_abort_error_message is None and cleanup_errors:
                    fatal_cleanup_errors = [
                        failure.error
                        for failure in self._ordered_cleanup_failures(
                            cleanup_errors,
                            phase="manual stop cleanup",
                        )
                        if failure.is_fatal
                    ]
                    if fatal_cleanup_errors:
                        factory_abort_error_message = "; ".join(
                            str(error) for error in fatal_cleanup_errors
                        )
                try:
                    self._emit_optional_signal(
                        "factory_runtime_aborted",
                        self._make_factory_runtime_abort_payload(
                            config_id,
                            session,
                            error_message=factory_abort_error_message,
                        ),
                    )
                except BaseException as exc:
                    cleanup_errors.append(
                        self._record_cleanup_failure(
                            exc,
                            phase="manual stop factory abort signal",
                        )
                    )

            if not silent:
                self._report_cleanup_safely(
                    cleanup_errors,
                    f"[TestManager] stopped config={config_id}",
                    phase="manual stop reporting",
                )
                try:
                    self._bus.status.emit("Stopped")
                except BaseException as exc:
                    cleanup_errors.append(
                        self._record_cleanup_failure(
                            exc,
                            phase="manual stop status",
                        )
                    )
        self._raise_first_fatal(
            cleanup_errors,
            context="manual stop cleanup",
        )
        return session is not None

    def _cleanup_failed_start_setup(
        self,
        config_id: int,
        session=None,
        recorder=None,
        monitor=None,
        *,
        requested_capture=None,
        error_route=None,
    ) -> list[BaseException]:
        with self._lifecycle_lock:
            current_session = self._sessions.get(config_id)
            if session is not None and current_session is session:
                registered_session = self._sessions.pop(config_id, None)
                registered_recorder = None
                if self._recorders.get(config_id) is recorder:
                    registered_recorder = self._recorders.pop(config_id, None)
                registered_monitor = None
                if monitor is not None and self._monitors.get(config_id) is monitor:
                    registered_monitor = self._monitors.pop(config_id, None)
                registered_error_route = self._session_error_routes.get(config_id)
                if (
                    registered_error_route is error_route
                    or getattr(registered_error_route, "session", None) is session
                ):
                    self._session_error_routes.pop(config_id, None)
                generation = self._run_generation_token(
                    session,
                    (
                        registered_error_route
                        if registered_error_route is not None
                        else error_route
                    ),
                )
                self._retire_session_generation_locked(config_id, generation)
            else:
                registered_session = None
                registered_recorder = None
                registered_monitor = None
                registered_error_route = None
                generation = self._run_generation_token(session, error_route)
            current_collection_claim = self._detach_current_collection_locked(
                config_id,
                session=session,
                generation=generation,
                retain_samples=False,
            )
            self._retire_current_samples_locked(config_id, generation)

        cleanup_errors: list[_CleanupFailureRecord] = []
        self._extend_cleanup_failures(
            cleanup_errors,
            self._invalidate_error_route(
                registered_error_route,
                context="failed start",
            ),
        )
        if error_route is not registered_error_route:
            self._extend_cleanup_failures(
                cleanup_errors,
                self._invalidate_error_route(
                    error_route,
                    context="failed start",
                ),
            )
        self._collect_current_cleanup_failures(
            current_collection_claim,
            cleanup_errors,
        )

        cleanup_session = registered_session if registered_session is not None else session
        cleanup_recorder = (
            registered_recorder if registered_recorder is not None else recorder
        )
        cleanup_monitor = (
            registered_monitor if registered_monitor is not None else monitor
        )

        if cleanup_session is not None:
            try:
                if cleanup_recorder is not None:
                    self._freeze_and_capture_session_buffer(
                        cleanup_session,
                        cleanup_recorder,
                    )
                else:
                    cleanup_session.freeze_capture()
            except BaseException as exc:
                cleanup_errors.append(
                    self._capture_finalization_failure(
                        cleanup_session,
                        exc,
                        phase="failed start capture freeze",
                    )
                )
                self._report_cleanup_safely(
                    cleanup_errors,
                    f"[TestManager] ERROR freezing failed start capture for "
                    f"config={config_id}: {exc}",
                    phase="failed start capture freeze",
                )

        if cleanup_session is not None and cleanup_recorder is not None:
            self._extend_cleanup_failures(
                cleanup_errors,
                self._disconnect_session_signals(
                    cleanup_session,
                    cleanup_recorder,
                    cleanup_monitor,
                ),
            )

            try:
                self._release_session_capture(
                    cleanup_session,
                    cleanup_recorder,
                    attachment=requested_capture,
                )
            except BaseException as exc:
                cleanup_errors.append(
                    self._capture_release_failure(
                        cleanup_session,
                        exc,
                        phase="failed start capture release",
                    )
                )
                self._report_cleanup_safely(
                    cleanup_errors,
                    f"[TestManager] ERROR releasing failed start capture for "
                    f"config={config_id}: {exc}",
                    phase="failed start capture release",
                )

        if cleanup_session is not None:
            self._extend_cleanup_failures(
                cleanup_errors,
                self._stop_session_live_display_safely(
                    config_id,
                    cleanup_session,
                    context="failed start",
                ),
            )

        cleanup_errors.extend(
            self._cleanup_monitor_recorder_safely(
                config_id,
                session=cleanup_session,
                mon=cleanup_monitor,
                rec=cleanup_recorder,
                context="failed start",
            )
        )
        return cleanup_errors

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

    def _error_route_is_current_locked(self, error_route) -> bool:
        if not isinstance(error_route, _SessionErrorRoute):
            return False
        config_id = error_route.config_id
        if self._session_error_routes.get(config_id) is not error_route:
            return False
        if self._session_generations.get(config_id) is not error_route.generation:
            return False
        if self._sessions.get(config_id) is not error_route.session:
            return False
        if (
            getattr(error_route.session, "_manager_generation_token", None)
            is not error_route.generation
        ):
            return False
        if self._recorders.get(config_id) is not error_route.recorder:
            return False
        if error_route.monitor is None:
            if config_id in self._monitors:
                return False
        elif self._monitors.get(config_id) is not error_route.monitor:
            return False
        return (
            getattr(error_route.session, "_manager_error_route", None)
            is error_route
        )

    @staticmethod
    def _run_generation_token(session=None, error_route=None):
        route_generation = getattr(error_route, "generation", None)
        session_generation = getattr(session, "_manager_generation_token", None)
        if (
            route_generation is not None
            and session_generation is not None
            and route_generation is not session_generation
        ):
            return None
        return (
            route_generation
            if route_generation is not None
            else session_generation
        )

    def _retire_session_generation_locked(self, config_id: int, generation) -> None:
        if (
            generation is not None
            and self._session_generations.get(config_id) is generation
        ):
            self._session_generations.pop(config_id, None)

    def _retire_current_samples_locked(self, config_id: int, generation) -> None:
        if generation is not None:
            self._staged_current_samples_by_generation.pop(generation, None)
        if (
            generation is not None
            and self._current_sample_generations.get(config_id) is generation
        ):
            self._current_sample_generations.pop(config_id, None)
            self._current_samples_by_config_id.pop(config_id, None)

    def _retire_completed_owner_locked(
        self,
        config_id: int,
        completed_session,
    ) -> bool:
        if self._completed_sessions.get(config_id) is not completed_session:
            return False
        generation = self._run_generation_token(completed_session)
        self._completed_sessions.pop(config_id, None)
        self._retire_current_samples_locked(config_id, generation)
        return True

    def _commit_completed_owner_locked(
        self,
        config_id: int,
        session,
        generation,
    ) -> None:
        prior_completed_session = self._completed_sessions.get(config_id)
        if (
            prior_completed_session is not None
            and prior_completed_session is not session
        ):
            self._retire_completed_owner_locked(
                config_id,
                prior_completed_session,
            )

        staged_present = generation in self._staged_current_samples_by_generation
        staged_samples = self._staged_current_samples_by_generation.pop(
            generation,
            None,
        )
        self._completed_sessions[config_id] = session
        if not staged_present:
            return
        if staged_samples:
            self._current_samples_by_config_id[config_id] = staged_samples
            self._current_sample_generations[config_id] = generation
        else:
            self._retire_current_samples_locked(config_id, generation)

    def _completion_ingress_is_current_locked(
        self,
        config_id: int,
        *,
        session,
        generation,
    ) -> bool:
        if generation is None or self._sessions.get(config_id) is not session:
            return False
        if self._session_generations.get(config_id) is not generation:
            return False
        if getattr(session, "_manager_generation_token", None) is not generation:
            return False
        error_route = self._session_error_routes.get(config_id)
        if error_route is None:
            return True
        return (
            isinstance(error_route, _SessionErrorRoute)
            and error_route.generation is generation
            and error_route.session is session
            and self._recorders.get(config_id) is error_route.recorder
            and (
                (
                    error_route.monitor is None
                    and config_id not in self._monitors
                )
                or self._monitors.get(config_id) is error_route.monitor
            )
            and getattr(session, "_manager_error_route", None) is error_route
        )

    def _run_resources_are_current_locked(
        self,
        config_id: int,
        *,
        session,
        recorder,
        monitor,
        error_route,
        generation,
    ) -> bool:
        if self._sessions.get(config_id) is not session:
            return False
        if generation is None:
            return False
        if self._session_generations.get(config_id) is not generation:
            return False
        if getattr(session, "_manager_generation_token", None) is not generation:
            return False
        if recorder is None:
            if config_id in self._recorders:
                return False
        elif self._recorders.get(config_id) is not recorder:
            return False
        if monitor is None:
            if config_id in self._monitors:
                return False
        elif self._monitors.get(config_id) is not monitor:
            return False
        if error_route is None:
            return config_id not in self._session_error_routes
        current_error_route = self._session_error_routes.get(config_id)
        if current_error_route is not None and current_error_route is not error_route:
            return False
        return (
            error_route.config_id == config_id
            and error_route.generation is generation
            and error_route.session is session
            and error_route.recorder is recorder
            and error_route.monitor is monitor
            and getattr(session, "_manager_error_route", None) is error_route
        )

    def _startup_error_route_state(self, error_route) -> str:
        if not isinstance(error_route, _SessionErrorRoute):
            return "missing"
        config_id = error_route.config_id
        session = error_route.session
        with self._lifecycle_lock:
            if (
                self._sessions.get(config_id) is session
                and id(session) in self._completion_claims
            ):
                return "completing"
            if self._error_route_is_current_locked(error_route):
                return "active"
            if self._completed_sessions.get(config_id) is session:
                return "completed"
            return "missing"

    def _invalidate_error_route(
        self,
        error_route,
        *,
        context: str = "session",
    ) -> list[_CleanupFailureRecord]:
        if not isinstance(error_route, _SessionErrorRoute):
            return []
        latch = error_route.error_latch
        if not isinstance(latch, _StartupRecorderErrorLatch):
            return []
        _, failures = self._run_cleanup_action_once(
            action="error route invalidate",
            session=error_route.session,
            generation=error_route.generation,
            resource=latch,
            token=error_route.source_token,
            callback=lambda: latch.invalidate(error_route.source_token),
            report_message=(
                f"[TestManager] ERROR {context} cleanup route invalidation "
                f"for config={error_route.config_id}"
            ),
        )
        return list(failures)

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
        attach_event_current_labels(summary_payload, chart_payload)
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

    def _on_session_done(
        self,
        config_id: int,
        *,
        expected_session,
        expected_generation,
    ):
        if self._route_pending_current_completion(
            config_id,
            session=expected_session,
            generation=expected_generation,
        ):
            return
        return self._continue_session_done(
            config_id,
            expected_session=expected_session,
            expected_generation=expected_generation,
        )

    def _continue_session_done(
        self,
        config_id: int,
        *,
        expected_session,
        expected_generation,
        preclaimed_current_collection=None,
    ):
        error_route = None
        current_collection_claim = None
        with self._lifecycle_lock:
            if not self._completion_ingress_is_current_locked(
                config_id,
                session=expected_session,
                generation=expected_generation,
            ):
                return
            if config_id in self._accepted_factory_stops:
                return
            session = expected_session
            rec = self._recorders.get(config_id)
            mon = self._monitors.get(config_id)
            completion_key = id(session)
            duplicate_completion = completion_key in self._completion_claims
            if not duplicate_completion:
                self._completion_claims.add(completion_key)
                self._analyzing_sessions.add(config_id)
                error_route = self._session_error_routes.get(config_id)
                if getattr(error_route, "session", None) is not session:
                    error_route = None
                else:
                    self._session_error_routes.pop(config_id, None)
                detached_current_collection = self._detach_current_collection_locked(
                    config_id,
                    session=session,
                    generation=expected_generation,
                    retain_samples=True,
                )
                current_collection_claim = (
                    preclaimed_current_collection
                    if preclaimed_current_collection is not None
                    else detached_current_collection
                )

        route_cleanup_errors = self._invalidate_error_route(
            error_route,
            context="recording completion",
        )

        if duplicate_completion:
            # Observe the stable one-shot result before returning.  Calling the
            # helper also closes the narrow gap where the first completion has
            # claimed the workflow but has not claimed capture extraction yet.
            if rec is not None:
                try:
                    self._freeze_and_capture_session_buffer(session, rec)
                except Exception:
                    pass
            return

        cleanup_settled_payload = self._make_cleanup_settled_payload(config_id, session)
        try:
            capture_error = None
            cleanup_errors: list[_CleanupFailureRecord] = list(
                route_cleanup_errors
            )
            if rec is not None:
                try:
                    self._freeze_and_capture_session_buffer(session, rec)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        capture_error = exc
                    else:
                        cleanup_errors.append(
                            self._capture_finalization_failure(
                                session,
                                exc,
                                phase="recording completion capture",
                            )
                        )
                disconnect_failures = self._disconnect_session_signals(
                    session,
                    rec,
                    mon,
                )
                self._extend_cleanup_failures(
                    cleanup_errors,
                    disconnect_failures,
                )
                if capture_error is None:
                    capture_error = next(
                        (
                            failure.error
                            for failure in disconnect_failures
                            if not failure.is_fatal
                        ),
                        None,
                    )
                try:
                    self._release_session_capture(session, rec)
                except BaseException as exc:
                    if isinstance(exc, Exception) and capture_error is None:
                        capture_error = exc
                    else:
                        cleanup_errors.append(
                            self._capture_release_failure(
                                session,
                                exc,
                                phase="recording completion capture release",
                            )
                        )
                    self._report_cleanup_safely(
                        cleanup_errors,
                        f"[TestManager] ERROR releasing completed capture "
                        f"for config={config_id}: {exc}",
                        phase="recording completion capture release",
                    )

            self._extend_cleanup_failures(
                cleanup_errors,
                self._stop_session_live_display_safely(
                    config_id,
                    session,
                    context="recording completion",
                ),
            )
            self._collect_current_cleanup_failures(
                current_collection_claim,
                cleanup_errors,
            )

            cleanup_errors.extend(
                self._cleanup_monitor_recorder_safely(
                    config_id,
                    session=session,
                    mon=mon,
                    rec=rec,
                    context="recording completion",
                    excluding_config_id=config_id,
                )
            )

            fatal_error = self._first_fatal(cleanup_errors)
            if fatal_error is not None:
                with self._lifecycle_lock:
                    if self._sessions.get(config_id) is session:
                        self._sessions.pop(config_id, None)
                        if self._recorders.get(config_id) is rec:
                            self._recorders.pop(config_id, None)
                        if self._monitors.get(config_id) is mon:
                            self._monitors.pop(config_id, None)
                        final_error_route = self._session_error_routes.pop(
                            config_id,
                            None,
                        )
                        self._retire_session_generation_locked(
                            config_id,
                            expected_generation,
                        )
                        self._retire_current_samples_locked(
                            config_id,
                            expected_generation,
                        )
                    else:
                        final_error_route = None
                self._extend_cleanup_failures(
                    cleanup_errors,
                    self._invalidate_error_route(
                        final_error_route,
                        context="recording completion",
                    ),
                )
                self._raise_first_fatal(
                    cleanup_errors,
                    context="recording completion cleanup",
                )

            with self._lifecycle_lock:
                still_owned = (
                    self._sessions.get(config_id) is session
                    and config_id not in self._accepted_factory_stops
                )
            if not still_owned:
                return

            # Canonical capture is frozen, extracted, and released before any
            # external analyzing notification can re-enter a stop path.
            self._emit_factory_analyzing_entered(
                config_id,
                session,
                log_message=f"[TestManager] recording done for config={config_id}",
            )
            if not self._auto_analysis_review_still_owned(config_id, session):
                return

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

            with self._lifecycle_lock:
                if not self._run_resources_are_current_locked(
                    config_id,
                    session=session,
                    recorder=rec,
                    monitor=mon,
                    error_route=error_route,
                    generation=expected_generation,
                ):
                    final_commit_won = False
                    final_error_route = None
                else:
                    self._sessions.pop(config_id, None)
                    self._commit_completed_owner_locked(
                        config_id,
                        session,
                        expected_generation,
                    )
                    if rec is not None:
                        self._recorders.pop(config_id, None)
                    if mon is not None:
                        self._monitors.pop(config_id, None)
                    final_error_route = self._session_error_routes.pop(
                        config_id,
                        None,
                    )
                    self._retire_session_generation_locked(
                        config_id,
                        expected_generation,
                    )
                    final_commit_won = True
            if not final_commit_won:
                return
            self._invalidate_error_route(final_error_route)
            if cleanup_settled_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_auto_analysis_cleanup_settled",
                    cleanup_settled_payload,
                )
        finally:
            with self._lifecycle_lock:
                current_session = self._sessions.get(config_id)
                if current_session is None or current_session is session:
                    self._analyzing_sessions.discard(config_id)
                self._completion_claims.discard(completion_key)

    def _on_error(self, error_route, message: str) -> bool:
        if not isinstance(error_route, _SessionErrorRoute):
            return False
        with self._lifecycle_lock:
            pending = self._pending_current_for_identity_locked(
                error_route.config_id,
                session=error_route.session,
                generation=error_route.generation,
            )
            if pending is None:
                continuation = None
                defer_to_owner = False
                settled = None
            else:
                continuation = pending.error_continuation
                if continuation is None:
                    if pending.terminal_snapshot is not None:
                        return False
                    continuation = _PendingErrorContinuation(
                        error_route=error_route,
                        message=message,
                    )
                    pending.error_continuation = continuation
                defer_to_owner = (
                    pending.terminal_snapshot is None
                    and pending.owned_by_current_thread()
                )
                settled = pending.settled

        if continuation is not None:
            if defer_to_owner:
                return True
            settled.wait()
            return bool(
                self._consume_pending_current_continuation(
                    pending,
                    continuation,
                )
            )
        return self._continue_error(error_route, message)

    def _continue_error(
        self,
        error_route,
        message: str,
        *,
        preclaimed_current_collection=None,
    ) -> bool:
        if not isinstance(error_route, _SessionErrorRoute):
            return False
        config_id = error_route.config_id
        abort_payload = self._make_factory_runtime_abort_payload(
            config_id,
            error_route.session,
            error_message=message,
        )
        cleanup_failures: list[_CleanupFailureRecord] = []
        try:
            stopped = self._continue_stop_one(
                config_id,
                silent=True,
                expected_error_route=error_route,
                expected_session=error_route.session,
                expected_generation=error_route.generation,
                preclaimed_current_collection=preclaimed_current_collection,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            cleanup_failures.append(
                self._record_cleanup_failure(
                    exc,
                    phase="runtime error cleanup",
                )
            )
            with self._lifecycle_lock:
                stopped = self._sessions.get(config_id) is not error_route.session
        if not stopped:
            self._raise_first_fatal(
                cleanup_failures,
                context="runtime error cleanup",
            )
            return False

        self._report_cleanup_safely(
            cleanup_failures,
            f"[TestManager] ERROR config={config_id}: {message}",
            phase="runtime error reporting",
        )
        try:
            self._bus.status.emit(f"Error: {message}")
        except BaseException as exc:
            cleanup_failures.append(
                self._record_cleanup_failure(
                    exc,
                    phase="runtime error status",
                )
            )
        try:
            self._emit_optional_signal("factory_runtime_aborted", abort_payload)
        except BaseException as exc:
            cleanup_failures.append(
                self._record_cleanup_failure(
                    exc,
                    phase="runtime error abort signal",
                )
            )
        self._raise_first_fatal(
            cleanup_failures,
            context="runtime error cleanup",
        )
        return True

    def _on_monitor_error(self, error_route, message: str):
        if not isinstance(error_route, _SessionErrorRoute):
            return
        config_id = error_route.config_id
        if self._is_nonfatal_monitor_diagnostic(message):
            with self._lifecycle_lock:
                route_is_current = self._error_route_is_current_locked(error_route)
            if not route_is_current:
                return
            self._bus.log.emit(
                f"[TestManager] monitor diagnostic config={config_id}: {message}"
            )
            return
        self._on_error(error_route, message)

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
        with self._lifecycle_lock:
            return (
                self._sessions.get(config_id) is session
                and (
                    allow_accepted_stop
                    or config_id not in self._accepted_factory_stops
                )
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
            attach_event_current_labels(summary_payload, chart_payload)
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

    def _stop_recording_with_optional_analysis(
        self,
        config_id: int,
        session,
        *,
        rec,
        mon,
        current_collection_claim,
        initial_cleanup_errors=(),
    ) -> bool:
        cleanup_errors: list[_CleanupFailureRecord] = list(
            initial_cleanup_errors
        )
        try:
            capture = self._capture_partial_stop_buffer(config_id, session, rec)
            self._extend_cleanup_failures(
                cleanup_errors,
                capture.get("cleanup_failures", ()),
            )
        except BaseException as exc:
            cleanup_errors.append(
                self._capture_finalization_failure(
                    session,
                    exc,
                    phase="factory stop partial capture",
                )
            )
            capture = {
                "partial_buf": None,
                "partial_wav_ok": False,
                "captured_duration_sec": None,
                "capture_error": None,
                "cleanup_failures": (),
            }
        if rec is not None:
            disconnect_failures = self._disconnect_session_signals(
                session,
                rec,
                mon,
            )
            self._extend_cleanup_failures(cleanup_errors, disconnect_failures)
            if capture.get("capture_error") is None:
                capture["capture_error"] = next(
                    (
                        failure.error
                        for failure in disconnect_failures
                        if not failure.is_fatal
                    ),
                    None,
                )
            try:
                self._release_session_capture(session, rec)
            except BaseException as exc:
                cleanup_errors.append(
                    self._capture_release_failure(
                        session,
                        exc,
                        phase="factory stop capture release",
                    )
                )
                self._report_cleanup_safely(
                    cleanup_errors,
                    f"[TestManager] ERROR releasing stopped capture for "
                    f"config={config_id}: {exc}",
                    phase="factory stop capture release",
                )
                if isinstance(exc, Exception) and capture.get("capture_error") is None:
                    capture["capture_error"] = exc
        self._extend_cleanup_failures(
            cleanup_errors,
            self._stop_session_live_display_safely(
                config_id,
                session,
                context="factory stop",
            ),
        )

        self._extend_cleanup_failures(
            cleanup_errors,
            self._stop_factory_runtime_io(
                config_id,
                session=session,
                rec=rec,
                mon=mon,
                current_collection_claim=current_collection_claim,
            ),
        )

        fatal_error = self._first_fatal(cleanup_errors)
        if fatal_error is not None:
            self._emit_factory_stop_abort(
                config_id,
                session,
                fatal_error,
                rec=rec,
                mon=mon,
            )
            self._raise_first_fatal(
                cleanup_errors,
                context="factory stop cleanup",
            )
        ordinary_cleanup_error = self._first_actionable_cleanup_exception(
            cleanup_errors
        )
        if ordinary_cleanup_error is not None:
            return self._emit_factory_stop_abort(
                config_id,
                session,
                ordinary_cleanup_error,
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

    def _stop_to_stopped(
        self,
        config_id: int,
        session,
        *,
        rec,
        mon,
        current_collection_claim,
        initial_cleanup_errors=(),
    ) -> bool:
        cleanup_errors: list[_CleanupFailureRecord] = list(
            initial_cleanup_errors
        )
        try:
            if rec is not None:
                self._freeze_and_capture_session_buffer(session, rec)
            else:
                session.freeze_capture()
        except BaseException as exc:
            cleanup_errors.append(
                self._capture_finalization_failure(
                    session,
                    exc,
                    phase="factory stopped capture freeze",
                )
            )
        if rec is not None:
            self._extend_cleanup_failures(
                cleanup_errors,
                self._disconnect_session_signals(session, rec, mon),
            )
            try:
                self._release_session_capture(session, rec)
            except BaseException as exc:
                cleanup_errors.append(
                    self._capture_release_failure(
                        session,
                        exc,
                        phase="factory stopped capture release",
                    )
                )
                self._report_cleanup_safely(
                    cleanup_errors,
                    f"[TestManager] ERROR releasing stopped capture for "
                    f"config={config_id}: {exc}",
                    phase="factory stopped capture release",
                )
        self._extend_cleanup_failures(
            cleanup_errors,
            self._stop_session_live_display_safely(
                config_id,
                session,
                context="factory stop",
            ),
        )

        self._extend_cleanup_failures(
            cleanup_errors,
            self._stop_factory_runtime_io(
                config_id,
                session=session,
                rec=rec,
                mon=mon,
                current_collection_claim=current_collection_claim,
            ),
        )

        fatal_error = self._first_fatal(cleanup_errors)
        if fatal_error is not None:
            self._emit_factory_stop_abort(
                config_id,
                session,
                fatal_error,
                rec=rec,
                mon=mon,
            )
            self._raise_first_fatal(
                cleanup_errors,
                context="factory stop cleanup",
            )
        ordinary_cleanup_error = self._first_actionable_cleanup_exception(
            cleanup_errors
        )
        if ordinary_cleanup_error is not None:
            return self._emit_factory_stop_abort(
                config_id,
                session,
                ordinary_cleanup_error,
                rec=rec,
                mon=mon,
            )
        return self._emit_factory_runtime_stopped(config_id, session)

    def _capture_partial_stop_buffer(self, config_id: int, session, rec) -> dict[str, object]:
        partial_buf = None
        partial_wav_ok = False
        cleanup_failures: list[_CleanupFailureRecord] = []

        if rec is None:
            session.freeze_capture()
            return {
                "partial_buf": None,
                "partial_wav_ok": False,
                "captured_duration_sec": self._captured_duration_sec(session),
                "capture_error": None,
                "cleanup_failures": (),
            }

        try:
            partial_buf = self._freeze_and_capture_session_buffer(session, rec)
        except Exception as exc:
            self._report_cleanup_safely(
                cleanup_failures,
                f"[TestManager] ERROR capturing partial recording for config={config_id}: {exc}",
                phase="factory stop partial capture",
            )
            return {
                "partial_buf": None,
                "partial_wav_ok": False,
                "captured_duration_sec": None,
                "capture_error": exc,
                "cleanup_failures": tuple(cleanup_failures),
            }

        captured_duration_sec = self._captured_duration_sec(session)
        if isinstance(partial_buf, np.ndarray) and partial_buf.shape[0] > 0:
            try:
                saved_path = session._save_wav(partial_buf, rec._sample_rate)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    cleanup_failures.append(
                        self._record_cleanup_failure(
                            exc,
                            phase="factory stop partial save",
                        )
                    )
                self._report_cleanup_safely(
                    cleanup_failures,
                    f"[TestManager] ERROR saving partial recording for config={config_id}: {exc}",
                    phase="factory stop partial save",
                )
            else:
                if isinstance(saved_path, str) and saved_path:
                    session._audio_file_path = saved_path
                partial_wav_ok = bool(getattr(session, "_audio_file_path", None))
                self._report_cleanup_safely(
                    cleanup_failures,
                    f"[TestManager] saved partial recording for config={config_id}",
                    phase="factory stop partial save",
                )

        if captured_duration_sec is None:
            self._report_cleanup_safely(
                cleanup_failures,
                f"[TestManager] WARNING captured duration unavailable for config={config_id}",
                phase="factory stop capture duration",
            )

        return {
            "partial_buf": partial_buf,
            "partial_wav_ok": partial_wav_ok,
            "captured_duration_sec": captured_duration_sec,
            "capture_error": None,
            "cleanup_failures": tuple(cleanup_failures),
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
        with self._lifecycle_lock:
            if self._sessions.get(config_id) is session:
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

            with self._lifecycle_lock:
                if self._sessions.get(config_id) is not session:
                    final_commit_won = False
                else:
                    self._sessions.pop(config_id, None)
                    self._commit_completed_owner_locked(
                        config_id,
                        session,
                        self._run_generation_token(session),
                    )
                    self._analyzing_sessions.discard(config_id)
                    self._accepted_factory_stops.discard(config_id)
                    final_commit_won = True

            if final_commit_won and cleanup_settled_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_auto_analysis_cleanup_settled",
                    cleanup_settled_payload,
                )
            return True
        finally:
            with self._lifecycle_lock:
                current_session = self._sessions.get(config_id)
                if current_session is None or current_session is session:
                    self._analyzing_sessions.discard(config_id)
                    self._accepted_factory_stops.discard(config_id)

    def _run_stop_triggered_capture_failure(
        self,
        config_id: int,
        session,
        error: Exception,
    ) -> bool:
        cleanup_settled_payload = self._make_cleanup_settled_payload(config_id, session)
        with self._lifecycle_lock:
            if self._sessions.get(config_id) is session:
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

            with self._lifecycle_lock:
                if self._sessions.get(config_id) is not session:
                    final_commit_won = False
                else:
                    self._sessions.pop(config_id, None)
                    self._commit_completed_owner_locked(
                        config_id,
                        session,
                        self._run_generation_token(session),
                    )
                    self._analyzing_sessions.discard(config_id)
                    self._accepted_factory_stops.discard(config_id)
                    final_commit_won = True

            if final_commit_won and cleanup_settled_payload is not None:
                self._emit_optional_signal(
                    "factory_runtime_auto_analysis_cleanup_settled",
                    cleanup_settled_payload,
                )
            return True
        finally:
            with self._lifecycle_lock:
                current_session = self._sessions.get(config_id)
                if current_session is None or current_session is session:
                    self._analyzing_sessions.discard(config_id)
                    self._accepted_factory_stops.discard(config_id)

    def _stop_factory_runtime_io(
        self,
        config_id: int,
        *,
        session,
        rec,
        mon,
        current_collection_claim,
    ) -> list[_CleanupFailureRecord]:
        cleanup_errors = self._cleanup_monitor_recorder_safely(
            config_id,
            session=session,
            mon=mon,
            rec=rec,
            context="factory stop",
            excluding_config_id=config_id,
        )
        with self._lifecycle_lock:
            if self._sessions.get(config_id) is session:
                if self._recorders.get(config_id) is rec:
                    self._recorders.pop(config_id, None)
                if self._monitors.get(config_id) is mon:
                    self._monitors.pop(config_id, None)

        self._collect_current_cleanup_failures(
            current_collection_claim,
            cleanup_errors,
        )
        return cleanup_errors

    def _emit_factory_runtime_stopped(self, config_id: int, session) -> bool:
        with self._lifecycle_lock:
            if self._sessions.get(config_id) is not session:
                transition_won = False
                completed_session = None
            else:
                self._analyzing_sessions.discard(config_id)
                self._accepted_factory_stops.discard(config_id)
                completed_session = self._sessions.pop(config_id, None)
                if (
                    completed_session is not None
                    and getattr(completed_session, "_audio_data_id", None)
                    and callable(
                        getattr(completed_session, "apply_manual_label", None)
                    )
                ):
                    self._commit_completed_owner_locked(
                        config_id,
                        completed_session,
                        self._run_generation_token(completed_session),
                    )
                else:
                    self._retire_current_samples_locked(
                        config_id,
                        self._run_generation_token(session),
                    )
                transition_won = True
        if not transition_won:
            return True
        stopped_payload = self._make_factory_runtime_stopped_payload(config_id, session)
        if stopped_payload is not None:
            self._emit_optional_signal(
                "factory_runtime_stopped",
                stopped_payload,
            )
        self._bus.log.emit(f"[TestManager] stopped config={config_id}")
        self._bus.status.emit("Stopped")
        return True

    def _emit_factory_stop_abort(
        self,
        config_id: int,
        session,
        error: BaseException,
        *,
        rec,
        mon,
    ) -> bool:
        with self._lifecycle_lock:
            if self._sessions.get(config_id) is not session:
                transition_won = False
            else:
                self._analyzing_sessions.discard(config_id)
                self._accepted_factory_stops.discard(config_id)
                self._sessions.pop(config_id, None)
                self._retire_current_samples_locked(
                    config_id,
                    self._run_generation_token(session),
                )
                if self._recorders.get(config_id) is rec:
                    self._recorders.pop(config_id, None)
                if self._monitors.get(config_id) is mon:
                    self._monitors.pop(config_id, None)
                transition_won = True
        cleanup_errors = []
        recorder_is_shared = rec is not None and any(
            shared_recorder is rec
            for shared_recorder in self._shared_recorders.values()
        )
        if recorder_is_shared:
            cleanup_errors = self._cleanup_monitor_recorder_safely(
                config_id,
                session=session,
                rec=rec,
                context="factory stop abort",
            )
        if not transition_won:
            if not isinstance(error, Exception):
                raise error
            self._raise_first_fatal(
                cleanup_errors,
                context="factory stop abort cleanup",
            )
            return False
        self._report_cleanup_safely(
            cleanup_errors,
            f"[TestManager] ERROR stopping config={config_id}: {error}",
            phase="factory stop abort reporting",
        )
        try:
            self._emit_optional_signal(
                "factory_runtime_aborted",
                self._make_factory_runtime_abort_payload(
                    config_id,
                    session,
                    error_message=str(error),
                ),
            )
        except BaseException as exc:
            cleanup_errors.append(
                self._record_cleanup_failure(
                    exc,
                    phase="factory stop abort signal",
                )
            )
        try:
            self._bus.status.emit(f"Error: {error}")
        except BaseException as exc:
            cleanup_errors.append(
                self._record_cleanup_failure(
                    exc,
                    phase="factory stop abort status",
                )
            )
        if not isinstance(error, Exception):
            for failure in self._ordered_cleanup_failures(
                cleanup_errors,
                phase="factory stop abort cleanup",
            ):
                if not failure.is_fatal:
                    continue
                self._note_secondary_fatal(
                    error,
                    failure.error,
                    context=f"factory stop abort cleanup [{failure.phase}]",
                )
            raise error
        self._raise_first_fatal(
            cleanup_errors,
            context="factory stop abort cleanup",
        )
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
                session=session,
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
        with self._lifecycle_lock:
            current_session = self._sessions.get(config_id)
            if current_session is not session:
                return
            error_route = self._session_error_routes.get(config_id)
            generation = self._run_generation_token(session, error_route)
            if (
                generation is None
                or self._session_generations.get(config_id) is not generation
            ):
                return
        if not current_config["enabled"]:
            return
        run_id = self._factory_run_id(session)
        sample_callback = None
        if isinstance(run_id, str) and run_id:
            sample_callback = (
                lambda _sample, samples_snapshot, cid=config_id, rid=run_id,
                cfg=current_meter_config, expected_generation=generation:
                self._emit_current_trace_update(
                    cid,
                    rid,
                    samples_snapshot,
                    cfg,
                    expected_generation=expected_generation,
                )
            )
        collector = CurrentMeterCollector(
            current_config,
            log_callback=lambda message, cid=config_id: self._bus.log.emit(
                f"[CurrentMeter] config={cid}: {message}"
            ),
            sample_callback=sample_callback,
        )
        pending = _PendingCurrentCollection(
            config_id=config_id,
            session=session,
            generation=generation,
            collector=collector,
        )
        with self._lifecycle_lock:
            if (
                self._sessions.get(config_id) is not session
                or self._session_generations.get(config_id) is not generation
                or getattr(session, "_manager_generation_token", None)
                is not generation
            ):
                register_pending = False
            else:
                self._current_collection_generations[config_id] = generation
                self._pending_current_collections[config_id] = pending
                register_pending = True
        if not register_pending:
            self._stop_current_collection_claim(
                _CurrentCollectionClaim(
                    config_id=config_id,
                    session=session,
                    generation=generation,
                    collector=collector,
                    retain_samples=False,
                )
            )
            return

        start_error = None
        try:
            collector_started = bool(collector.start())
        except BaseException as exc:
            collector_started = False
            start_error = exc

        cleanup_claim = None
        cleanup_result = None
        cleanup_error = None
        cleanup_call_error = None
        selected_continuation = None
        with self._lifecycle_lock:
            pending_is_current = (
                self._pending_current_collections.get(config_id) is pending
            )
            generation_is_current = (
                self._current_collection_generations.get(config_id) is generation
                and self._session_generations.get(config_id) is generation
            )
            session_is_current = self._sessions.get(config_id) is session
            selected_continuation = self._select_pending_current_continuation_locked(
                pending,
                start_error=start_error,
            )
            can_register = (
                start_error is None
                and collector_started
                and selected_continuation is None
                and pending_is_current
                and generation_is_current
                and session_is_current
            )
            if can_register:
                self._pending_current_collections.pop(config_id, None)
                self._current_collectors[config_id] = collector
                self._current_collector_generations[config_id] = generation
                snapshot = _PendingCurrentTerminalSnapshot(
                    config_id=config_id,
                    session=session,
                    generation=generation,
                    terminal_kind="running",
                    start_succeeded=True,
                    collector_registered=True,
                    cleanup_claim=None,
                    cleanup_result=None,
                    start_error=None,
                    cleanup_diagnostic_error=None,
                    fatal_error=None,
                    owner_thread_id=pending.start_owner_thread_id,
                    cleanup_owner_thread_id=None,
                    continuation_kind=None,
                    continuation_request_id=None,
                )
                pending.stage_terminal(snapshot)
                collector_registered = True
            else:
                collector_registered = False
                pending.terminalizing = True
                pending.cleanup_owner_thread_id = threading.get_ident()
                if (
                    self._current_collection_generations.get(config_id)
                    is generation
                ):
                    self._current_collection_generations.pop(config_id, None)
                retain_samples = (
                    start_error is None
                    and selected_continuation is not None
                    and selected_continuation.kind
                    in {"completion", "factory_stop"}
                )
                cleanup_claim = pending.cleanup_claim
                if cleanup_claim is None:
                    cleanup_claim = _CurrentCollectionClaim(
                        config_id=config_id,
                        session=session,
                        generation=generation,
                        collector=collector,
                        retain_samples=retain_samples,
                        pending_start=pending,
                    )
                    pending.cleanup_claim = cleanup_claim

        if collector_registered:
            pending.publish_terminal(snapshot)
            self._bus.log.emit(f"[CurrentMeter] started for config={config_id}")
            return

        try:
            cleanup_result = self._stop_current_collection_claim(cleanup_claim)
        except BaseException as exc:
            cleanup_call_error = exc
            cleanup_result = cleanup_claim._cleanup_result
        cleanup_diagnostic_error = (
            cleanup_result.diagnostic_error
            if cleanup_result is not None
            else None
        )
        cleanup_fatal_error = (
            cleanup_call_error
            if cleanup_call_error is not None
            and not isinstance(cleanup_call_error, Exception)
            else (
                cleanup_result.fatal_error
                if cleanup_result is not None
                else None
            )
        )
        start_fatal_error = (
            start_error
            if start_error is not None and not isinstance(start_error, Exception)
            else None
        )

        with self._lifecycle_lock:
            selected_continuation = self._select_pending_current_continuation_locked(
                pending,
                start_error=start_error,
            )
            terminal_kind = self._pending_current_terminal_kind(
                start_error=start_error,
                cleanup_error=(
                    cleanup_fatal_error
                    if cleanup_fatal_error is not None
                    else cleanup_diagnostic_error
                ),
                continuation=selected_continuation,
                start_succeeded=bool(collector_started),
                pending_is_current=(
                    self._pending_current_collections.get(config_id) is pending
                ),
            )
            snapshot = _PendingCurrentTerminalSnapshot(
                config_id=config_id,
                session=session,
                generation=generation,
                terminal_kind=terminal_kind,
                start_succeeded=bool(collector_started),
                collector_registered=False,
                cleanup_claim=cleanup_claim,
                cleanup_result=cleanup_result,
                start_error=start_error,
                cleanup_diagnostic_error=cleanup_diagnostic_error,
                fatal_error=(
                    start_fatal_error
                    if start_fatal_error is not None
                    else cleanup_fatal_error
                ),
                owner_thread_id=pending.start_owner_thread_id,
                cleanup_owner_thread_id=pending.cleanup_owner_thread_id,
                continuation_kind=(
                    selected_continuation.kind
                    if selected_continuation is not None
                    else None
                ),
                continuation_request_id=(
                    selected_continuation.request_id
                    if selected_continuation is not None
                    else None
                ),
            )
            pending.stage_terminal(snapshot)
            if (
                selected_continuation is None
                and self._pending_current_collections.get(config_id) is pending
            ):
                self._pending_current_collections.pop(config_id, None)

        pending.publish_terminal(snapshot)
        if selected_continuation is not None:
            self._consume_pending_current_continuation(
                pending,
                selected_continuation,
            )

        if start_fatal_error is not None:
            if cleanup_fatal_error is not None:
                self._note_secondary_fatal(
                    start_fatal_error,
                    cleanup_fatal_error,
                    context="current collector startup cleanup",
                )
                raise start_fatal_error from cleanup_fatal_error
            raise start_fatal_error
        if cleanup_fatal_error is not None and selected_continuation is None:
            if isinstance(start_error, Exception):
                raise cleanup_fatal_error from start_error
            raise cleanup_fatal_error
        if start_error is not None:
            if cleanup_diagnostic_error is not None:
                raise start_error from cleanup_diagnostic_error
            raise start_error
        if cleanup_call_error is not None and selected_continuation is None:
            raise cleanup_call_error

    @staticmethod
    def _pending_current_terminal_kind(
        *,
        start_error,
        cleanup_error,
        continuation,
        start_succeeded,
        pending_is_current,
    ) -> str:
        if start_error is not None:
            root_kind = "failed_start"
        elif continuation is not None:
            root_kind = continuation.kind
        elif not pending_is_current:
            root_kind = "superseded"
        elif not start_succeeded:
            root_kind = "skipped"
        else:
            root_kind = "stopped"
        if cleanup_error is not None:
            return f"{root_kind}_cleanup_error"
        return root_kind

    @staticmethod
    def _select_pending_current_continuation_locked(
        pending: _PendingCurrentCollection,
        *,
        start_error,
    ):
        # Failed startup is terminal without a run continuation.  Runtime
        # errors outrank accepted stops, and stops outrank successful
        # completion.  Collector cleanup errors are attached diagnostics and
        # never participate in root-outcome selection.
        if start_error is not None:
            return None
        if pending.error_continuation is not None:
            return pending.error_continuation
        if pending.stop_continuation is not None:
            return pending.stop_continuation
        return pending.completion_continuation

    def _pending_current_for_identity_locked(
        self,
        config_id: int,
        *,
        session,
        generation,
    ) -> _PendingCurrentCollection | None:
        pending = self._pending_current_collections.get(config_id)
        if (
            pending is None
            or pending.session is not session
            or pending.generation is not generation
        ):
            return None
        return pending

    def _route_pending_current_completion(
        self,
        config_id: int,
        *,
        session,
        generation,
    ) -> bool:
        with self._lifecycle_lock:
            if not self._completion_ingress_is_current_locked(
                config_id,
                session=session,
                generation=generation,
            ):
                return False
            pending = self._pending_current_for_identity_locked(
                config_id,
                session=session,
                generation=generation,
            )
            if pending is None:
                return False
            continuation = pending.completion_continuation
            if continuation is None:
                if pending.terminal_snapshot is not None:
                    return True
                continuation = _PendingCompletionContinuation(
                    config_id=config_id,
                    session=session,
                    generation=generation,
                )
                pending.completion_continuation = continuation
            defer_to_owner = (
                pending.terminal_snapshot is None
                and pending.owned_by_current_thread()
            )
            settled = pending.settled

        if defer_to_owner:
            return True
        settled.wait()
        self._consume_pending_current_continuation(pending, continuation)
        return True

    def _consume_pending_current_continuation(
        self,
        pending: _PendingCurrentCollection,
        continuation: _PendingCurrentContinuation,
    ):
        pending.settled.wait()
        release_suppressed = False
        with self._lifecycle_lock:
            snapshot = pending.terminal_snapshot
            if snapshot is None:
                raise RuntimeError("pending collector released without terminal snapshot")
            exact_identity = (
                snapshot.config_id == continuation.config_id
                and snapshot.session is continuation.session
                and snapshot.generation is continuation.generation
            )
            selected = (
                exact_identity
                and snapshot.continuation_kind == continuation.kind
                and snapshot.continuation_request_id is continuation.request_id
            )
            if not selected:
                if not continuation.done.is_set():
                    continuation.result = False if continuation.kind != "completion" else None
                    release_suppressed = True
                suppressed_result = continuation.result
                wait_for_consumer = False
            elif continuation.claimed:
                if continuation.consumer_thread_id == threading.get_ident():
                    return continuation.result
                wait_for_consumer = True
            else:
                continuation.claimed = True
                continuation.consumer_thread_id = threading.get_ident()
                wait_for_consumer = False

        if not selected:
            if release_suppressed:
                continuation.done.set()
            return suppressed_result
        if wait_for_consumer:
            continuation.done.wait()
            if continuation.error is not None:
                raise continuation.error
            return continuation.result

        result = None
        error = None
        try:
            result = self._execute_pending_current_continuation(
                snapshot,
                continuation,
            )
        except BaseException as exc:
            error = exc
        finally:
            with self._lifecycle_lock:
                continuation.result = result
                continuation.error = error
                if self._pending_current_collections.get(pending.config_id) is pending:
                    self._pending_current_collections.pop(pending.config_id, None)
            continuation.done.set()

        if error is not None:
            raise error
        return result

    def _execute_pending_current_continuation(
        self,
        snapshot: _PendingCurrentTerminalSnapshot,
        continuation: _PendingCurrentContinuation,
    ):
        claim = snapshot.cleanup_claim
        if continuation.kind == "completion":
            return self._continue_session_done(
                continuation.config_id,
                expected_session=continuation.session,
                expected_generation=continuation.generation,
                preclaimed_current_collection=claim,
            )
        if continuation.kind == "factory_stop":
            return self._continue_factory_stop(
                continuation.config_id,
                expected_session=continuation.session,
                expected_generation=continuation.generation,
                preclaimed_current_collection=claim,
            )
        if continuation.kind == "manual_stop":
            return self._continue_stop_one(
                continuation.config_id,
                continuation.silent,
                notify_factory_runtime_abort=(
                    continuation.notify_factory_runtime_abort
                ),
                expected_error_route=continuation.expected_error_route,
                expected_session=continuation.session,
                expected_generation=continuation.generation,
                preclaimed_current_collection=claim,
            )
        if continuation.kind == "error":
            return self._continue_error(
                continuation.error_route,
                continuation.message,
                preclaimed_current_collection=claim,
            )
        raise RuntimeError(
            f"unsupported pending current continuation: {continuation.kind}"
        )

    def _detach_current_collection_locked(
        self,
        config_id: int,
        *,
        session,
        generation,
        retain_samples: bool,
    ) -> _CurrentCollectionClaim | None:
        if generation is None:
            return None
        if self._current_collection_generations.get(config_id) is generation:
            self._current_collection_generations.pop(config_id, None)

        pending = self._pending_current_collections.get(config_id)
        if (
            pending is not None
            and pending.session is session
            and pending.generation is generation
        ):
            self._pending_current_collections.pop(config_id, None)
            if pending.cleanup_claim is not None:
                return None
            claim = _CurrentCollectionClaim(
                config_id=config_id,
                session=session,
                generation=generation,
                collector=pending.collector,
                retain_samples=retain_samples,
                pending_start=pending,
            )
            pending.cleanup_claim = claim
            return claim

        collector = self._current_collectors.get(config_id)
        collector_generation = self._current_collector_generations.get(config_id)
        if collector_generation is not generation:
            return None
        self._current_collector_generations.pop(config_id, None)
        if collector is None:
            return None
        self._current_collectors.pop(config_id, None)
        return _CurrentCollectionClaim(
            config_id=config_id,
            session=session,
            generation=generation,
            collector=collector,
            retain_samples=retain_samples,
        )

    def _collect_current_cleanup_failures(self, claim, failures):
        """Consume one collector cleanup snapshot without replaying its catch time."""

        if claim is None:
            return None
        result = None
        caught = None
        try:
            result = self._stop_current_collection_claim(claim)
        except BaseException as exc:
            caught = exc
            result = claim._cleanup_result
        if result is not None:
            self._extend_cleanup_failures(failures, result.failures)
            return result
        if caught is not None:
            failures.append(
                self._record_cleanup_failure(
                    caught,
                    phase="current collector cleanup publication",
                )
            )
        return result

    def _stop_current_collection_claim(
        self,
        claim: _CurrentCollectionClaim | None,
    ) -> _CurrentCollectionCleanupResult | None:
        if claim is None:
            return None

        with claim._cleanup_lock:
            if claim._cleanup_started:
                cleanup_complete = claim._cleanup_complete
                cleanup_owner_thread_id = claim._cleanup_owner_thread_id
                perform_cleanup = False
            else:
                claim._cleanup_started = True
                claim._cleanup_owner_thread_id = threading.get_ident()
                cleanup_complete = None
                cleanup_owner_thread_id = None
                perform_cleanup = True
        if not perform_cleanup:
            if (
                cleanup_owner_thread_id == threading.get_ident()
                and not cleanup_complete.is_set()
            ):
                raise RuntimeError("collector cleanup owner cannot wait on itself")
            cleanup_complete.wait()
            result = claim._cleanup_result
            if result is None:
                raise RuntimeError("collector cleanup completed without a result")
            if result.fatal_error is not None:
                raise result.fatal_error
            return result

        failures: list[_CleanupFailureRecord] = []
        stop_failure = None
        samples_failure = None
        samples = []
        try:
            try:
                claim.collector.stop()
            except BaseException as exc:
                stop_failure = self._record_cleanup_failure(
                    exc,
                    phase="current collector stop",
                )
                failures.append(stop_failure)
            try:
                samples = claim.collector.samples()
            except BaseException as exc:
                samples_failure = self._record_cleanup_failure(
                    exc,
                    phase="current collector samples",
                )
                failures.append(samples_failure)

            if stop_failure is not None:
                label = "fatal cleanup" if stop_failure.is_fatal else "cleanup diagnostic"
                self._report_cleanup_safely(
                    failures,
                    f"[CurrentMeter] {label} for config={claim.config_id}: "
                    f"stop failed: {stop_failure.error}",
                    phase="current collector stop",
                )
            if samples_failure is not None:
                label = (
                    "fatal cleanup"
                    if samples_failure.is_fatal
                    else "cleanup diagnostic"
                )
                self._report_cleanup_safely(
                    failures,
                    f"[CurrentMeter] {label} for config={claim.config_id}: "
                    f"samples failed: {samples_failure.error}",
                    phase="current collector samples",
                )
            if stop_failure is None and samples_failure is None:
                self._report_cleanup_safely(
                    failures,
                    f"[CurrentMeter] stopped for config={claim.config_id}, "
                    f"samples={len(samples)}",
                    phase="current collector stopped",
                )

            result = _CurrentCollectionCleanupResult(
                samples=samples,
                stop_failure=stop_failure,
                samples_failure=samples_failure,
                failures=tuple(failures),
            )
            with self._lifecycle_lock:
                still_owned = (
                    self._sessions.get(claim.config_id) is claim.session
                    and getattr(
                        claim.session,
                        "_manager_generation_token",
                        None,
                    )
                    is claim.generation
                )
                if (
                    claim.retain_samples
                    and still_owned
                    and result.samples_diagnostic_error is None
                    and result.fatal_error is None
                ):
                    prior_completed_session = self._completed_sessions.get(
                        claim.config_id
                    )
                    if (
                        prior_completed_session is not None
                        and prior_completed_session is not claim.session
                    ):
                        self._staged_current_samples_by_generation[
                            claim.generation
                        ] = samples
                    elif samples:
                        self._current_samples_by_config_id[
                            claim.config_id
                        ] = samples
                        self._current_sample_generations[claim.config_id] = (
                            claim.generation
                        )
                    else:
                        self._retire_current_samples_locked(
                            claim.config_id,
                            claim.generation,
                        )
                else:
                    self._retire_current_samples_locked(
                        claim.config_id,
                        claim.generation,
                    )
        finally:
            if "result" not in locals():
                result = _CurrentCollectionCleanupResult(
                    samples=samples,
                    stop_failure=stop_failure,
                    samples_failure=samples_failure,
                    failures=tuple(failures),
                )
            with claim._cleanup_lock:
                claim._cleanup_result = result
            claim._cleanup_complete.set()

        if result.fatal_error is not None:
            raise result.fatal_error
        return result

    def _current_trace_payload(
        self,
        config_id,
        *,
        current_meter: dict | None = None,
        session=None,
    ) -> dict | None:
        normalized_config_id = self._as_non_bool_int(config_id)
        if normalized_config_id is None:
            return None
        with self._lifecycle_lock:
            if session is None:
                completed_session = self._completed_sessions.get(normalized_config_id)
                completed_generation = self._run_generation_token(completed_session)
                if completed_session is not None:
                    collector = None
                    if (
                        completed_generation is not None
                        and self._current_sample_generations.get(normalized_config_id)
                        is completed_generation
                    ):
                        samples = self._current_samples_by_config_id.get(
                            normalized_config_id,
                            [],
                        )
                    else:
                        samples = []
                else:
                    active_session = self._sessions.get(normalized_config_id)
                    active_generation = self._run_generation_token(active_session)
                    if (
                        active_session is not None
                        and active_generation is not None
                        and self._current_collector_generations.get(
                            normalized_config_id
                        )
                        is active_generation
                    ):
                        collector = self._current_collectors.get(normalized_config_id)
                        samples = []
                    else:
                        collector = None
                        if (
                            active_session is not None
                            and active_generation is not None
                            and self._current_sample_generations.get(
                                normalized_config_id
                            )
                            is active_generation
                        ):
                            samples = self._current_samples_by_config_id.get(
                                normalized_config_id,
                                [],
                            )
                        elif (
                            active_generation
                            in self._staged_current_samples_by_generation
                        ):
                            samples = self._staged_current_samples_by_generation[
                                active_generation
                            ]
                        else:
                            samples = []
            else:
                generation = self._run_generation_token(session)
                collector = None
                if (
                    generation is not None
                    and self._current_collector_generations.get(normalized_config_id)
                    is generation
                ):
                    collector = self._current_collectors.get(normalized_config_id)
                if generation in self._staged_current_samples_by_generation:
                    samples = self._staged_current_samples_by_generation[generation]
                elif (
                    generation is not None
                    and self._current_sample_generations.get(normalized_config_id)
                    is generation
                ):
                    samples = self._current_samples_by_config_id.get(
                        normalized_config_id,
                        [],
                    )
                else:
                    samples = []
        if collector is not None:
            samples = collector.samples()
        return build_current_trace_payload(samples, config=current_meter)

    def _emit_current_trace_update(
        self,
        config_id: int,
        run_id: str | None,
        samples,
        current_meter: dict | None,
        *,
        expected_generation=None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            return
        if expected_generation is not None:
            with self._lifecycle_lock:
                if (
                    self._current_collection_generations.get(config_id)
                    is not expected_generation
                ):
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

    def _freeze_and_capture_session_buffer(self, session, recorder) -> np.ndarray:
        """Return the one stable canonical capture shared by all finalizers."""

        claimant = False
        with self._lifecycle_changed:
            state = getattr(
                session,
                "_manager_capture_finalization_state",
                None,
            )
            if not isinstance(state, _CaptureFinalizationState):
                state = _CaptureFinalizationState()
                session._manager_capture_finalization_state = state
                claimant = True
            else:
                if (
                    not state.complete
                    and state.owner_thread_id == threading.get_ident()
                ):
                    raise RuntimeError("reentrant session capture finalization")
                while not state.complete:
                    self._lifecycle_changed.wait()

            if not claimant:
                if state.error is not None:
                    raise state.error
                return state.buffer

        buffer = None
        error = None
        failure = None
        try:
            buffer = self._freeze_and_capture_session_buffer_once(
                session,
                recorder,
            )
        except BaseException as exc:
            error = exc
            failure = self._record_cleanup_failure(
                exc,
                phase="capture freeze and extraction",
            )

        with self._lifecycle_changed:
            state.buffer = buffer
            state.error = error
            state.failure = failure
            state.complete = True
            self._lifecycle_changed.notify_all()

        if error is not None:
            raise error
        return buffer

    def _freeze_and_capture_session_buffer_once(
        self,
        session,
        recorder,
    ) -> np.ndarray:
        """Freeze and extract exactly one retained canonical prefix."""

        retained_frames = int(session.freeze_capture())
        if retained_frames <= 0:
            return np.empty(
                (0, self._recorder_channel_count(recorder, session=session)),
                dtype=np.float32,
            )

        attachment = self._session_capture_attachment(session)
        if attachment is None:
            raise RuntimeError("session capture attachment is unavailable")
        raw = recorder.get_capture_buffer(
            attachment,
            retained_frames=retained_frames,
        )
        if not isinstance(raw, np.ndarray) or raw.ndim < 1:
            raise TypeError("recorder capture buffer must be a numpy array")
        if raw.shape[0] != retained_frames:
            raise RuntimeError(
                "recorder capture buffer does not match the frozen retained prefix"
            )
        return self._capture_session_recorder_buffer(
            session,
            raw,
            recorder._sample_rate,
        )

    def _release_session_capture(
        self,
        session,
        recorder,
        *,
        attachment=None,
    ) -> None:
        """Release one recorder owner once; repeated cleanup calls are harmless."""

        if session is None or recorder is None:
            return
        if attachment is None:
            attachment = self._session_capture_attachment(session)
        if attachment is None:
            return

        claimant = False
        with self._lifecycle_changed:
            release_states = getattr(
                session,
                "_manager_capture_release_states",
                None,
            )
            if not isinstance(release_states, dict):
                release_states = {}
                session._manager_capture_release_states = release_states
            release_key = id(attachment)
            state = release_states.get(release_key)
            if not (
                isinstance(state, _CaptureReleaseState)
                and state.attachment is attachment
            ):
                state = _CaptureReleaseState(attachment)
                release_states[release_key] = state
                claimant = True
            else:
                if (
                    not state.complete
                    and state.owner_thread_id == threading.get_ident()
                ):
                    return
                while not state.complete:
                    self._lifecycle_changed.wait()

            if not claimant:
                if state.error is not None:
                    raise state.error
                return

        release_error = None
        release_failure = None
        try:
            recorder.release_capture(attachment)
        except BaseException as exc:
            release_error = exc
            release_failure = self._record_cleanup_failure(
                exc,
                phase="capture release",
            )

        with self._lifecycle_changed:
            state.error = release_error
            state.failure = release_failure
            state.complete = True
            if release_error is None:
                session._released_capture_attachment = attachment
            self._lifecycle_changed.notify_all()

        if release_error is not None:
            raise release_error

    def _capture_finalization_failure(
        self,
        session,
        error,
        *,
        phase: str,
    ) -> _CleanupFailureRecord:
        state = getattr(session, "_manager_capture_finalization_state", None)
        if (
            isinstance(state, _CaptureFinalizationState)
            and state.error is error
            and state.failure is not None
        ):
            return state.failure
        return self._record_cleanup_failure(error, phase=phase)

    def _capture_release_failure(
        self,
        session,
        error,
        *,
        phase: str,
    ) -> _CleanupFailureRecord:
        release_states = getattr(session, "_manager_capture_release_states", None)
        if isinstance(release_states, dict):
            for state in release_states.values():
                if (
                    isinstance(state, _CaptureReleaseState)
                    and state.error is error
                    and state.failure is not None
                ):
                    return state.failure
        return self._record_cleanup_failure(error, phase=phase)

    @staticmethod
    def _session_capture_attachment(session):
        attachment = getattr(session, "capture_attachment", None)
        return attachment() if callable(attachment) else attachment

    @staticmethod
    def _recorder_channel_count(recorder, *, session=None) -> int:
        value = getattr(recorder, "_n_channels", None)
        if isinstance(value, Integral) and not isinstance(value, bool) and value >= 0:
            return int(value)
        buffer = getattr(recorder, "_buffer", None)
        if isinstance(buffer, np.ndarray) and buffer.ndim >= 2:
            return int(buffer.shape[1])
        config = getattr(session, "_config", {}) if session is not None else {}
        selected = config.get("selected_channels", []) if isinstance(config, dict) else []
        normalized = [
            int(channel)
            for channel in selected
            if isinstance(channel, Integral) and not isinstance(channel, bool) and channel >= 0
        ]
        return max(normalized, default=-1) + 1

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
        try:
            sample_rate = float(getattr(session, "_sample_rate", 0) or 0)
        except (TypeError, ValueError):
            sample_rate = 0.0
        if sample_rate <= 0:
            return None

        captured_frame_count = getattr(session, "captured_frame_count", None)
        if callable(captured_frame_count):
            captured_frame_count = captured_frame_count()
        if isinstance(captured_frame_count, Integral) and not isinstance(
            captured_frame_count,
            bool,
        ):
            return max(0, int(captured_frame_count)) / sample_rate

        buffer = getattr(session, "_buffer", None)
        if not isinstance(buffer, np.ndarray) or buffer.size <= 0:
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

    def _record_cleanup_failure(
        self,
        error: BaseException,
        *,
        phase: str,
    ) -> _CleanupFailureRecord:
        """Assign cleanup chronology without taking the lifecycle lock."""

        with self._cleanup_failure_lock:
            self._cleanup_failure_sequence += 1
            sequence = self._cleanup_failure_sequence
        return _CleanupFailureRecord(
            error=error,
            phase=str(phase),
            classification=(
                "diagnostic" if isinstance(error, Exception) else "fatal"
            ),
            sequence=sequence,
        )

    def _coerce_cleanup_failure(
        self,
        failure,
        *,
        phase: str,
    ) -> _CleanupFailureRecord:
        if isinstance(failure, _CleanupFailureRecord):
            return failure
        if not isinstance(failure, BaseException):
            failure = RuntimeError(str(failure))
        return self._record_cleanup_failure(failure, phase=phase)

    @staticmethod
    def _extend_cleanup_failures(target, failures) -> None:
        seen = {
            failure.sequence
            for failure in target
            if isinstance(failure, _CleanupFailureRecord)
        }
        for failure in failures:
            if (
                isinstance(failure, _CleanupFailureRecord)
                and failure.sequence in seen
            ):
                continue
            target.append(failure)
            if isinstance(failure, _CleanupFailureRecord):
                seen.add(failure.sequence)

    def _report_cleanup_safely(
        self,
        failures,
        message: str,
        *,
        phase: str,
    ) -> None:
        """Attempt one cleanup report without ever reporting its own failure."""

        try:
            self._bus.log.emit(message)
        except BaseException as exc:
            failures.append(
                self._record_cleanup_failure(
                    exc,
                    phase=f"{phase}.reporting",
                )
            )

    def _run_cleanup_action_once(
        self,
        *,
        action: str,
        session,
        generation,
        resource,
        token=None,
        callback,
        report_message: str | None = None,
    ) -> tuple[object, tuple[_CleanupFailureRecord, ...]]:
        """Run one exact physical cleanup action and freeze its full outcome."""

        identity = (session, generation, resource, token)
        key = (
            str(action),
            id(session),
            id(generation),
            id(resource),
            id(token),
        )
        claimant = False
        with self._cleanup_action_lock:
            if session is None:
                states = self._cleanup_action_states
            else:
                states = getattr(session, "_manager_cleanup_action_states", None)
                if not isinstance(states, dict):
                    states = {}
                    session._manager_cleanup_action_states = states
            state = states.get(key)
            if state is None or any(
                current is not expected
                for current, expected in zip(state.identity, identity)
            ):
                state = _CleanupActionState(identity)
                states[key] = state
                claimant = True
            elif (
                not state.complete.is_set()
                and state.owner_thread_id == threading.get_ident()
            ):
                # A cleanup callback may re-enter manager cleanup.  The owner
                # must finish and publish; it must never wait on itself.
                return state.value, state.failures

        if not claimant:
            state.complete.wait()
            return state.value, state.failures

        value = None
        failures: list[_CleanupFailureRecord] = []
        try:
            value = callback()
        except BaseException as exc:
            failures.append(self._record_cleanup_failure(exc, phase=action))
            if report_message is not None:
                self._report_cleanup_safely(
                    failures,
                    f"{report_message}: {exc}",
                    phase=action,
                )

        with self._cleanup_action_lock:
            state.value = value
            state.failures = tuple(failures)
        state.complete.set()
        return state.value, state.failures

    @staticmethod
    def _disconnect(signal, callback):
        if callback is None:
            return
        try:
            signal.disconnect(callback)
        except (TypeError, RuntimeError, ValueError):
            pass

    def _disconnect_session_signals(self, session, recorder, monitor=None):
        errors: list[_CleanupFailureRecord] = []
        generation = self._run_generation_token(session)
        disconnects = [
            (
                "captured block signal disconnect",
                recorder.captured_block_ready,
                getattr(session, "_captured_block_conn", None),
            ),
            (
                "recorder error signal disconnect",
                recorder.error_occurred,
                getattr(session, "_error_lambda", None),
            ),
        ]
        if monitor is not None:
            disconnects.append(
                (
                    "monitor error signal disconnect",
                    monitor.error_occurred,
                    getattr(session, "_monitor_error_lambda", None),
                )
            )
        for action, signal, callback in disconnects:
            _, failures = self._run_cleanup_action_once(
                action=action,
                session=session,
                generation=generation,
                resource=signal,
                token=callback,
                callback=lambda sig=signal, cb=callback: self._disconnect(sig, cb),
                report_message=(
                    f"[TestManager] ERROR cleanup {action} "
                    f"for config={getattr(session, 'config_id', None)}"
                ),
            )
            self._extend_cleanup_failures(errors, failures)
        return errors

    def _stop_session_live_display_safely(
        self,
        config_id: int,
        session,
        *,
        context: str,
    ) -> list[_CleanupFailureRecord]:
        stop_live_display = getattr(session, "stop_live_display", None)
        if not callable(stop_live_display):
            return []
        _, failures = self._run_cleanup_action_once(
            action="live display stop",
            session=session,
            generation=self._run_generation_token(session),
            resource=session,
            callback=stop_live_display,
            report_message=(
                f"[TestManager] ERROR {context} cleanup live display "
                f"for config={config_id}"
            ),
        )
        return list(failures)

    def _cleanup_monitor_recorder_safely(
        self,
        config_id: int,
        *,
        session=None,
        mon=None,
        rec=None,
        context: str,
        excluding_config_id: int | None = None,
    ) -> list[_CleanupFailureRecord]:
        errors: list[_CleanupFailureRecord] = []
        generation = self._run_generation_token(session)

        if mon is not None:
            _, failures = self._run_cleanup_action_once(
                action="monitor stop",
                session=session,
                generation=generation,
                resource=mon,
                callback=mon.stop,
                report_message=(
                    f"[TestManager] ERROR {context} cleanup monitor stop "
                    f"for config={config_id}"
                ),
            )
            self._extend_cleanup_failures(errors, failures)
            _, failures = self._run_cleanup_action_once(
                action="monitor wait",
                session=session,
                generation=generation,
                resource=mon,
                callback=lambda: self._wait_thread_if_not_current(mon, 1000),
                report_message=(
                    f"[TestManager] ERROR {context} cleanup monitor wait "
                    f"for config={config_id}"
                ),
            )
            self._extend_cleanup_failures(errors, failures)

        shutdown_won = False
        recorder_stop_fatal = False
        if rec is not None:
            shutdown_result, failures = self._run_cleanup_action_once(
                action="recorder stop",
                session=session,
                generation=generation,
                resource=rec,
                callback=lambda: self._claim_unused_recorder_shutdown(
                    rec,
                    excluding_config_id=excluding_config_id,
                ),
                report_message=(
                    f"[TestManager] ERROR {context} cleanup recorder stop "
                    f"for config={config_id}"
                ),
            )
            self._extend_cleanup_failures(errors, failures)
            shutdown_won = shutdown_result is True
            recorder_stop_fatal = any(
                failure.is_fatal and failure.phase == "recorder stop"
                for failure in failures
            )

        if rec is not None and (shutdown_won or recorder_stop_fatal):
            _, failures = self._run_cleanup_action_once(
                action="recorder wait",
                session=session,
                generation=generation,
                resource=rec,
                callback=lambda: self._wait_thread_if_not_current(rec, 3000),
                report_message=(
                    f"[TestManager] ERROR {context} cleanup recorder wait "
                    f"for config={config_id}"
                ),
            )
            self._extend_cleanup_failures(errors, failures)
            self._discard_shared_recorder(rec)

        return errors

    @staticmethod
    def _cleanup_error(failure):
        if isinstance(failure, _CleanupFailureRecord):
            return failure.error
        return failure

    def _ordered_cleanup_failures(self, failures, *, phase: str):
        records = [
            self._coerce_cleanup_failure(failure, phase=phase)
            for failure in failures
        ]
        return sorted(records, key=lambda failure: failure.sequence)

    def _first_actionable_cleanup_exception(self, failures):
        """Return an ordinary operational failure, excluding diagnostics."""

        for failure in self._ordered_cleanup_failures(
            failures,
            phase="cleanup diagnostic aggregation",
        ):
            if failure.is_fatal:
                continue
            if (
                failure.phase.startswith("current collector")
                or failure.phase.startswith("error route invalidate")
                or failure.phase.startswith("live display stop")
                or failure.phase.endswith(".reporting")
            ):
                continue
            return failure.error
        return None

    def _first_fatal(self, errors) -> BaseException | None:
        fatal_records = [
            failure
            for failure in self._ordered_cleanup_failures(
                errors,
                phase="legacy cleanup aggregation",
            )
            if failure.is_fatal
        ]
        if not fatal_records:
            return None
        return fatal_records[0].error

    @staticmethod
    def _note_secondary_fatal(
        primary: BaseException,
        secondary: BaseException,
        *,
        context: str,
    ) -> None:
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(f"{context}: {type(secondary).__name__}: {secondary}")

    def _raise_first_fatal(self, errors, *, context: str) -> None:
        ordered = self._ordered_cleanup_failures(errors, phase=context)
        fatals = [failure for failure in ordered if failure.is_fatal]
        if not fatals:
            return
        fatal_error = fatals[0].error
        for secondary_record in fatals[1:]:
            secondary = secondary_record.error
            if secondary is fatal_error:
                continue
            self._note_secondary_fatal(
                fatal_error,
                secondary,
                context=f"{context} [{secondary_record.phase}]",
            )
        raise fatal_error

    def _recorder_has_live_owner(
        self,
        recorder,
        *,
        excluding_config_id: int | None = None,
    ) -> bool:
        with self._lifecycle_lock:
            return any(
                cid != excluding_config_id and active_recorder is recorder
                for cid, active_recorder in self._recorders.items()
            )

    def _claim_unused_recorder_shutdown(
        self,
        recorder,
        *,
        excluding_config_id: int | None = None,
    ) -> bool:
        """Linearize unowned shutdown without nesting manager/recorder locks."""

        if self._recorder_has_live_owner(
            recorder,
            excluding_config_id=excluding_config_id,
        ):
            return False

        shutdown_won = recorder.stop_if_no_capture_owners()
        if not isinstance(shutdown_won, (bool, np.bool_)):
            raise TypeError(
                "recorder stop_if_no_capture_owners() must return bool"
            )
        return bool(shutdown_won)

    def _stop_recorder_if_unused(self, recorder, *, excluding_config_id: int | None = None):
        if self._claim_unused_recorder_shutdown(
            recorder,
            excluding_config_id=excluding_config_id,
        ):
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
