"""Serialized two-phase owner for the production multi-run graph."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import wave
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.resident_audio_mvp import MAX_ALLOCATABLE_FRAMES
from app.service.analysis.judgment import _runtime_channel_defaults
from app.service.analysis.live_display_dispatcher import LiveDisplayDispatcher
from app.service.analysis.pipeline import AnalysisPipeline
from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseStatus,
)
from app.service.analysis.scheduler import AnalysisScheduler
from app.service.analysis.scheduler.models import AnalysisSchedulerConfig
from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_ABOVE_NORMAL,
    THREAD_PRIORITY_HIGHEST,
    set_current_thread_priority,
)
from app.service.current_meter_service import (
    CurrentMeterCollector,
    PlcCurrentCollector,
    build_current_trace_payload,
    normalize_current_meter_config,
)
from app.service.fault_types import (
    decode_fault_types_json,
    encode_history_labels_payload,
    encode_run_config_payload,
)
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    ManagedHardwareId,
)
from app.service.managed_input_sounddevice_backend import (
    ManagedInputSoundDeviceBackend,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    ABSENT,
    AudioSaveSourceKind,
    AudioMetadataConstraints,
    CommitResult,
    FinalPathRequest,
    FrozenCapturedRun,
    FrozenImportedRun,
    FrozenPayload,
    HistoryProjectionFact,
    ImportedSourceFormat,
    ImportedSourceReference,
    ManualProjectionResult,
    ManualProjectionStatus,
    PersistedAudioFormat,
    PersistedAudioReference,
    ProjectedHistoryEvent,
    ResultControlFacts,
    ResultProjection,
    ResultProjectionPhase,
    TerminalConclusion,
)
from app.service.multi_run_coordinator_mvp.coordinator import MultiRunCoordinator
from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
    ManagedResidentCapturePort,
    ManagedResidentCaptureRoute,
)
from app.service.multi_run_coordinator_mvp.save_dispatcher import (
    BoundedAudioSaveDispatcher,
)
from app.service.multi_run_integration.audio_saver import ProductionAudioSaver
from app.service.multi_run_integration.durable_history_sink import (
    DurableHistorySink,
)
from app.service.plc_result_sequencer import PlcResultSequencer
from app.service.run_persistence import (
    AnalysisPersistenceBoundary,
    RunPersistenceSchedulerBridge,
    decode_analysis_envelope,
    freeze_analysis_envelope,
)
from app.service.run_persistence.attempt_repository import AttemptRepository
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.audio_publication import AudioPublication
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.audio_service import AudioPersistenceService
from app.service.run_persistence.contracts import (
    IngressKind,
    Judgment,
    RunInsert,
    RunState,
    TransactionState,
    TriggerSource as PersistenceTriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.production_run_audio_adapter import (
    ProductionRunAudioAdapter,
)
from app.service.run_persistence.run_repository import (
    RunRepository,
    RunTerminal,
)
from app.service.run_scoped_playback.backend import SoundDevicePlaybackBackend
from app.service.run_scoped_playback.service import RunScopedPlaybackService

from app.service.managed_input_sounddevice_backend import (
    PYTHON_SYSTEM_MONOTONIC_DOMAIN,
    PythonSystemMonotonicAuthority,
    validate_python_system_monotonic_authority,
)


_PRODUCTION_ANALYSIS_WORKER_COUNT = 2


class ProductionCompositionError(RuntimeError):
    """Production composition could not be prepared or activated."""


def _utc_now_text() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _payload(value_id: str, value: object) -> FrozenPayload:
    return FrozenPayload(
        value_id,
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _decode_payload(value: FrozenPayload) -> dict[str, object]:
    decoded = json.loads(value.canonical_bytes)
    if type(decoded) is not dict:
        raise ValueError("frozen ingress payload must be an object")
    return decoded


def _result_payload_with_judgment(
    payload: FrozenPayload,
    conclusion: TerminalConclusion,
) -> FrozenPayload:
    if conclusion not in {TerminalConclusion.OK, TerminalConclusion.NG}:
        raise ValueError("result payload cannot be projected manually")
    canonical = payload.canonical_bytes
    pass_prefix = b'{"judgment":"PASS","result":'
    fail_prefix = b'{"judgment":"FAIL","result":'
    if not canonical.endswith(b"}"):
        raise ValueError("result payload cannot be projected manually")
    if canonical.startswith(pass_prefix):
        current_prefix = pass_prefix
    elif canonical.startswith(fail_prefix):
        current_prefix = fail_prefix
    else:
        raise ValueError("result payload cannot be projected manually")
    target_prefix = (
        pass_prefix if conclusion is TerminalConclusion.OK else fail_prefix
    )
    if current_prefix == target_prefix:
        return payload
    return FrozenPayload(
        payload.value_id,
        target_prefix + canonical[len(current_prefix):],
    )


def _freeze_config_routes(
    prerequisites: _PreparedPrerequisites,
) -> tuple[tuple[int, int | None], ...]:
    selected = tuple(prerequisites.selected_config_ids)
    routes = []
    for config in prerequisites.configs:
        config_id = getattr(config, "config_id", None)
        if type(config_id) is not int or config_id <= 0:
            raise ProductionCompositionError(
                "prepared config routing identity is invalid"
            )
        values = getattr(config, "values", None)
        if not isinstance(values, Mapping):
            raise ProductionCompositionError(
                "prepared config routing facts are missing"
            )
        raw_plc_id = values.get("plc_id")
        plc_id = (
            raw_plc_id
            if type(raw_plc_id) is int and raw_plc_id > 0
            else None
        )
        routes.append((config_id, plc_id))
    frozen = tuple(routes)
    if tuple(config_id for config_id, _plc_id in frozen) != selected:
        raise ProductionCompositionError(
            "prepared config routing identities do not match selection"
        )
    return frozen


class _PreparedPrerequisites(Protocol):
    selection_generation: int
    selected_config_ids: tuple[int, ...]
    monotonic_authority: object

    def validate_activation(self) -> None: ...

    def close(self) -> None: ...


class _PrerequisiteProvider(Protocol):
    def prepare(self, *, monotonic_authority: object) -> _PreparedPrerequisites: ...


class _PreparedUpstream(Protocol):
    streams: tuple[object, ...]

    def close(self) -> None: ...


class _GraphFactory(Protocol):
    def prepare_upstream(
        self,
        prerequisites: _PreparedPrerequisites,
    ) -> _PreparedUpstream: ...

    def activate(
        self,
        prerequisites: _PreparedPrerequisites,
        upstream: _PreparedUpstream,
        history_sink: object,
    ) -> object: ...


class _HistoryProjector:
    def project(self, fact: HistoryProjectionFact) -> ProjectedHistoryEvent:
        proposed = (
            1
            if fact.expected_revision is ABSENT
            else fact.expected_revision + 1
        )
        return ProjectedHistoryEvent(
            kind=fact.kind,
            run_id=fact.run_id,
            expected_revision=fact.expected_revision,
            operation_id=fact.operation_id,
            proposed_revision=proposed,
            projection=fact.projection,
        )


class _Diagnostics:
    def __init__(self, callback) -> None:
        self._callback = callback

    def record(self, fact) -> None:
        if self._callback is not None:
            self._callback(fact)


class ProductionCurrentCollectorFactory:
    """Build run-owned collectors solely from the frozen run snapshot."""

    def __init__(self, *, bus: object, monotonic, log_callback=None) -> None:
        if not callable(monotonic):
            raise ValueError("current collector monotonic clock must be callable")
        self._bus = bus
        self._monotonic = monotonic
        self._log = log_callback

    def __call__(self, run_id: str, frozen: FrozenCapturedRun):
        decoded = _decode_payload(frozen.config_payload)
        config = decoded.get("config")
        if type(config) is not dict:
            raise ValueError("frozen current collector config is missing")
        raw_current = config.get("current_meter")
        if type(raw_current) is not dict:
            raise ValueError("frozen current_meter must be an object")
        current = normalize_current_meter_config(raw_current)
        if current["enabled"] is False:
            return None
        log_callback = (
            None
            if self._log is None
            else lambda message: self._log(
                f"[CurrentMeter] run={run_id}: {message}"
            )
        )
        common = {
            "time_func": self._monotonic,
            "time_origin": frozen.trigger_monotonic_ns / 1_000_000_000,
            "log_callback": log_callback,
            "sample_callback": self._sample_callback(
                run_id=run_id,
                config_id=config.get("config_id"),
                current=current,
            ),
        }
        if current["source"] == "modbus_tcp":
            return CurrentMeterCollector(current, **common)
        if current["source"] == "plc":
            plc_id = config.get("plc_id")
            if type(plc_id) is not int or plc_id <= 0:
                raise ValueError(
                    "enabled frozen PLC current requires a positive PLC ID"
                )
            return PlcCurrentCollector(
                current,
                bus=self._bus,
                plc_id=plc_id,
                **common,
            )
        raise ValueError("frozen current source is unsupported")

    def _sample_callback(
        self,
        *,
        run_id: str,
        config_id: object,
        current: dict,
    ):
        signal = getattr(self._bus, "current_trace_updated", None)
        if signal is None or not callable(getattr(signal, "emit", None)):
            return None
        try:
            numeric_config_id = int(config_id)
        except (TypeError, ValueError):
            return None

        def publish(_sample, samples) -> None:
            payload = build_current_trace_payload(
                samples,
                config=current,
            )
            if payload is not None:
                signal.emit(numeric_config_id, run_id, payload)

        return publish


@dataclass(slots=True)
class _LiveRun:
    run_id: str
    hardware_id: str
    config_id: int
    trigger_monotonic_ns: int
    target_frame_count: int
    sample_rate_hz: int
    dispatcher: LiveDisplayDispatcher
    live_loudness_channel_index: int | None
    monitor: object | None = None
    processed_frames: int = 0
    monitor_processed_frames: int = 0


@dataclass(frozen=True, slots=True)
class _QueuedLiveBlock:
    hardware_id: str
    samples: np.ndarray
    frame_count: int
    callback_monotonic_ns: int
    sample_rate_hz: int
    status: BackendCallbackStatus


class _ProductionLiveDisplayManager:
    """Best-effort live analysis tap; capture never depends on this path."""

    def __init__(self, *, bus: object, diagnostic_callback=None) -> None:
        self._bus = bus
        self._diagnostic_callback = diagnostic_callback
        self._lock = threading.Lock()
        self._runs_by_hardware: dict[str, dict[str, _LiveRun]] = {}
        self._closed = False
        self._block_condition = threading.Condition()
        self._block_queue: deque[_QueuedLiveBlock] = deque()
        self._block_active = False
        self._block_stopping = False
        self._block_thread = threading.Thread(
            target=self._run_block_dispatch,
            name="production-monitor-display-fanout",
            daemon=True,
        )
        self._block_thread.start()

    def register_frozen(self, frozen: FrozenCapturedRun) -> None:
        if self._bus is None:
            return
        config = _decode_payload(frozen.config_payload).get("config")
        if not isinstance(config, dict):
            return
        try:
            config_id = int(config["config_id"])
            sample_rate = int(frozen.metadata.expected_sample_rate_hz)
            selected = [
                int(channel)
                for channel in config.get("selected_channels", [])
                if isinstance(channel, int) and not isinstance(channel, bool)
            ]
            channels = {
                item.get("channel_index"): item
                for item in config.get("channels", [])
                if isinstance(item, dict)
            }
            pipelines = []
            try:
                calibration = _decode_payload(frozen.calibration_payload)
            except Exception:
                calibration = {}
            for channel_index in selected:
                channel_config = _runtime_channel_defaults(channel_index)
                if isinstance(channels.get(channel_index), dict):
                    channel_config.update(channels[channel_index])
                pipelines.append(
                    AnalysisPipeline(
                        channel_index=channel_index,
                        channel_config=channel_config,
                        sample_rate=float(sample_rate),
                        mic_sensitivity=dsp.mic_sensitivity_from_calibration(
                            calibration,
                            channel_index,
                        ),
                    )
                )
            if not pipelines:
                return
            dispatcher = LiveDisplayDispatcher(
                pipelines=pipelines,
                bus=self._bus,
                config_id=config_id,
                coalesce_pending=True,
                stft_emit_interval_sec=0.1,
            )
            dispatcher.start()
            monitor = self._create_monitor(
                config,
                selected=selected,
                input_sample_rate=sample_rate,
                run_id=frozen.run_id,
            )
            vibration_channel = config.get("vibration_view_channel_index")
            loudness_channel = next(
                (
                    channel
                    for channel in selected
                    if channel != vibration_channel
                ),
                None,
            )
            run = _LiveRun(
                run_id=frozen.run_id,
                hardware_id=f"managed-input-{frozen.hardware_id}",
                config_id=config_id,
                trigger_monotonic_ns=frozen.trigger_monotonic_ns,
                target_frame_count=frozen.target_frame_count,
                sample_rate_hz=sample_rate,
                dispatcher=dispatcher,
                live_loudness_channel_index=loudness_channel,
                monitor=monitor,
            )
        except Exception as exc:
            self._diagnose(f"live display setup failed: {exc}")
            return
        with self._lock:
            if self._closed:
                self._drain_and_stop(run)
                return
            self._runs_by_hardware.setdefault(run.hardware_id, {})[
                run.run_id
            ] = run

    def accept_block(
        self,
        hardware_id: str,
        samples,
        frame_count: int,
        callback_monotonic_ns: int,
        sample_rate_hz: int,
        status: BackendCallbackStatus,
    ) -> None:
        if status is not BackendCallbackStatus.OK or frame_count <= 0:
            return
        array = np.asarray(samples)
        if array.ndim not in (1, 2) or array.shape[0] < frame_count:
            return
        hardware_key = str(hardware_id)
        with self._lock:
            if self._closed or not self._runs_by_hardware.get(hardware_key):
                return
        copied = np.array(array[:frame_count], copy=True, order="C")

        # Monitoring is the highest-priority consumer.  Feed its ring buffer
        # directly from the resident input callback instead of making it wait
        # behind live STFT/loudness display work.
        monitor_submissions: list[tuple[object, np.ndarray]] = []
        with self._lock:
            if not self._closed:
                active = tuple(
                    self._runs_by_hardware.get(hardware_key, {}).values()
                )
                for run in active:
                    monitor = getattr(run, "monitor", None)
                    if (
                        monitor is None
                        or run.sample_rate_hz != sample_rate_hz
                    ):
                        continue
                    block_start_ns = callback_monotonic_ns - round(
                        frame_count * 1_000_000_000 / sample_rate_hz
                    )
                    if callback_monotonic_ns <= run.trigger_monotonic_ns:
                        continue
                    skip = max(
                        0,
                        min(
                            frame_count,
                            math.ceil(
                                (
                                    run.trigger_monotonic_ns - block_start_ns
                                )
                                * sample_rate_hz
                                / 1_000_000_000
                            ),
                        ),
                    )
                    remaining = (
                        run.target_frame_count
                        - run.monitor_processed_frames
                    )
                    take = min(frame_count - skip, remaining)
                    if take <= 0:
                        continue
                    run.monitor_processed_frames += take
                    monitor_submissions.append(
                        (
                            monitor,
                            np.asarray(copied[skip : skip + take]),
                        )
                    )
        for monitor, chunk in monitor_submissions:
            try:
                monitor.feed(chunk)
            except Exception as exc:
                self._diagnose(f"monitor feed failed: {exc}")

        with self._block_condition:
            if self._block_stopping:
                return
            self._block_queue.append(
                _QueuedLiveBlock(
                    hardware_id=hardware_key,
                    samples=copied,
                    frame_count=frame_count,
                    callback_monotonic_ns=callback_monotonic_ns,
                    sample_rate_hz=sample_rate_hz,
                    status=status,
                )
            )
            self._block_condition.notify()

    def _run_block_dispatch(self) -> None:
        set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        while True:
            with self._block_condition:
                self._block_condition.wait_for(
                    lambda: self._block_queue or self._block_stopping
                )
                if not self._block_queue and self._block_stopping:
                    return
                block = self._block_queue.popleft()
                self._block_active = True
            try:
                self._process_block(block)
            except Exception as exc:
                self._diagnose(f"live monitor/display fanout failed: {exc}")
            finally:
                with self._block_condition:
                    self._block_active = False
                    self._block_condition.notify_all()

    def _process_block(self, block: _QueuedLiveBlock) -> None:
        hardware_id = block.hardware_id
        array = block.samples
        frame_count = block.frame_count
        callback_monotonic_ns = block.callback_monotonic_ns
        sample_rate_hz = block.sample_rate_hz
        completed: list[_LiveRun] = []
        submissions: list[tuple[_LiveRun, np.ndarray, int, int]] = []
        with self._lock:
            if self._closed:
                return
            active = tuple(
                self._runs_by_hardware.get(str(hardware_id), {}).values()
            )
            for run in active:
                if run.sample_rate_hz != sample_rate_hz:
                    continue
                block_start_ns = callback_monotonic_ns - round(
                    frame_count * 1_000_000_000 / sample_rate_hz
                )
                if callback_monotonic_ns <= run.trigger_monotonic_ns:
                    continue
                skip = max(
                    0,
                    min(
                        frame_count,
                        math.ceil(
                            (
                                run.trigger_monotonic_ns - block_start_ns
                            )
                            * sample_rate_hz
                            / 1_000_000_000
                        ),
                    ),
                )
                remaining = run.target_frame_count - run.processed_frames
                take = min(frame_count - skip, remaining)
                if take <= 0:
                    continue
                sample_start = run.processed_frames
                sample_end = sample_start + take
                run.processed_frames = sample_end
                submissions.append(
                    (
                        run,
                        np.asarray(array[skip : skip + take]),
                        sample_start,
                        sample_end,
                    )
                )
                if sample_end >= run.target_frame_count:
                    completed.append(run)
                    self._runs_by_hardware.get(run.hardware_id, {}).pop(
                        run.run_id,
                        None,
                    )
        for run, chunk, sample_start, sample_end in submissions:
            try:
                run.dispatcher.submit(
                    chunk,
                    elapsed_time=sample_end / run.sample_rate_hz,
                    frame_run_id=run.run_id,
                    live_loudness_channel_index=(
                        run.live_loudness_channel_index
                    ),
                    sample_start=sample_start,
                    sample_end=sample_end,
                )
            except Exception as exc:
                self._diagnose(f"live display submission failed: {exc}")
        for run in completed:
            self._drain_and_stop(run)

    def wait_until_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._block_condition:
            while self._block_queue or self._block_active:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._block_condition.wait(remaining)
            return True

    def finish(self, run_id: str) -> None:
        found = None
        with self._lock:
            for hardware_runs in self._runs_by_hardware.values():
                found = hardware_runs.pop(run_id, None)
                if found is not None:
                    break
        if found is not None:
            self._drain_and_stop(found)

    def has_active_runs(self) -> bool:
        with self._lock:
            return any(self._runs_by_hardware.values())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            runs = [
                run
                for hardware_runs in self._runs_by_hardware.values()
                for run in hardware_runs.values()
            ]
            self._runs_by_hardware.clear()
        with self._block_condition:
            self._block_queue.clear()
            self._block_stopping = True
            self._block_condition.notify_all()
        if self._block_thread is not threading.current_thread():
            self._block_thread.join(1.0)
        for run in runs:
            run.dispatcher.stop()
            self._stop_monitor(run.monitor, wait=True)

    def _create_monitor(
        self,
        config: dict[str, object],
        *,
        selected: list[int],
        input_sample_rate: int,
        run_id: str,
    ) -> object | None:
        output_name = config.get("output_device_name")
        if not isinstance(output_name, str) or not output_name.strip():
            return None
        output_hostapi = config.get("output_device_hostapi")
        if not isinstance(output_hostapi, str):
            output_hostapi = ""
        output_rate = config.get(
            "output_device_samplerate",
            input_sample_rate,
        )
        try:
            output_rate = float(output_rate)
        except (TypeError, ValueError):
            output_rate = float(input_sample_rate)
        monitor_channel = config.get("monitor_channel_index")
        if not isinstance(monitor_channel, int) or isinstance(
            monitor_channel,
            bool,
        ):
            monitor_channel = selected[0]
        gain_db = config.get("monitor_gain_db", 0.0)
        try:
            gain_db = float(gain_db)
        except (TypeError, ValueError):
            gain_db = 0.0
        try:
            monitor = AudioMonitor(
                device_name=output_name,
                hostapi=output_hostapi,
                sample_rate=output_rate,
                monitor_channel=monitor_channel,
                gain_db=gain_db,
            )
            monitor.error_occurred.connect(
                lambda message, rid=run_id: self._diagnose(
                    f"monitor run={rid}: {message}"
                )
            )
            monitor.start()
            return monitor
        except Exception as exc:
            self._diagnose(f"monitor setup failed: {exc}")
            return None

    @staticmethod
    def _stop_monitor(monitor: object | None, *, wait: bool) -> None:
        if monitor is None:
            return
        try:
            monitor.stop()
        except Exception:
            return
        if wait:
            try:
                monitor.wait(1_000)
            except Exception:
                pass

    @classmethod
    def _drain_and_stop(cls, run: _LiveRun) -> None:
        def drain() -> None:
            run.dispatcher.wait_until_idle(2.0)
            run.dispatcher.stop()
            cls._stop_monitor(run.monitor, wait=True)

        cls._stop_monitor(run.monitor, wait=False)
        threading.Thread(
            target=drain,
            name="production-live-display-drain",
            daemon=True,
        ).start()

    def _diagnose(self, detail: str) -> None:
        if callable(self._diagnostic_callback):
            try:
                self._diagnostic_callback(detail[:240])
            except Exception:
                pass


@dataclass(slots=True)
class _AcceptedHeartbeat:
    condition: threading.Condition = field(
        default_factory=threading.Condition
    )
    sequence: int = 0
    last_callback_ns: int | None = None
    faulted: bool = False


@dataclass(slots=True)
class _HardwareInputRegistration:
    spec: object
    callback: object
    heartbeat: _AcceptedHeartbeat
    generation: object | None
    handle: object | None = None


class _RebuildableInputHandle:
    def __init__(
        self,
        owner: "_LiveDisplayAudioBackend",
        resident_hardware_id: str,
    ) -> None:
        self._owner = owner
        self._resident_hardware_id = resident_hardware_id

    def start(self, deadline_monotonic_ns: int) -> None:
        self._owner._start(self._resident_hardware_id, deadline_monotonic_ns)

    def request_stop(self) -> None:
        self._owner._request_stop(self._resident_hardware_id)

    def close(self, deadline_monotonic_ns: int):
        return self._owner._close(
            self._resident_hardware_id,
            deadline_monotonic_ns,
        )


class _LiveDisplayAudioBackend:
    _MAX_CALLBACK_AGE_NS = 1_000_000_000
    _RECOVERY_TIMEOUT_NS = 1_000_000_000

    def __init__(self, delegate: object, manager: _ProductionLiveDisplayManager):
        self._delegate = delegate
        self._manager = manager
        self._registry_lock = threading.Lock()
        self._registrations: dict[str, _HardwareInputRegistration] = {}

    def create_input(self, spec, callback, on_fault):
        resident_hardware_id = str(spec.hardware_id)
        heartbeat = _AcceptedHeartbeat()
        generation = object()
        registration = _HardwareInputRegistration(
            spec=spec,
            callback=callback,
            heartbeat=heartbeat,
            generation=generation,
        )
        tapped, tapped_fault = self._generation_callbacks(
            registration,
            generation,
        )
        handle = self._delegate.create_input(spec, tapped, tapped_fault)
        registration.handle = handle
        with self._registry_lock:
            self._registrations[resident_hardware_id] = registration
        return _RebuildableInputHandle(self, resident_hardware_id)

    def _generation_callbacks(self, registration, generation):
        heartbeat = registration.heartbeat
        spec = registration.spec
        priority_applied = False

        def tapped(samples, frame_count, callback_ns, status):
            nonlocal priority_applied
            if not priority_applied:
                set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
                priority_applied = True
            with heartbeat.condition:
                if registration.generation is not generation:
                    return BackendCallbackDirective.STOP
                if status is not BackendCallbackStatus.OK:
                    heartbeat.faulted = True
                    heartbeat.condition.notify_all()
                    directive = BackendCallbackDirective.STOP
                else:
                    directive = registration.callback(
                        samples,
                        frame_count,
                        callback_ns,
                        status,
                    )
                    if directive is BackendCallbackDirective.CONTINUE:
                        heartbeat.sequence += 1
                        heartbeat.last_callback_ns = time.monotonic_ns()
                        heartbeat.faulted = False
                        heartbeat.condition.notify_all()
            try:
                self._manager.accept_block(
                    spec.hardware_id,
                    samples,
                    frame_count,
                    callback_ns,
                    spec.sample_rate_hz,
                    status,
                )
            except Exception:
                pass
            return directive

        def tapped_fault(_fault):
            with heartbeat.condition:
                if registration.generation is generation:
                    heartbeat.faulted = True
                    heartbeat.condition.notify_all()

        return tapped, tapped_fault

    def _registration(self, resident_hardware_id: str):
        with self._registry_lock:
            return self._registrations.get(resident_hardware_id)

    def _start(
        self,
        resident_hardware_id: str,
        deadline_monotonic_ns: int,
    ) -> None:
        registration = self._registration(resident_hardware_id)
        if registration is None or registration.handle is None:
            raise RuntimeError("resident input registration is unavailable")
        registration.handle.start(deadline_monotonic_ns)

    def _request_stop(self, resident_hardware_id: str) -> None:
        registration = self._registration(resident_hardware_id)
        if registration is not None and registration.handle is not None:
            registration.handle.request_stop()

    def _close(
        self,
        resident_hardware_id: str,
        deadline_monotonic_ns: int,
    ):
        registration = self._registration(resident_hardware_id)
        if registration is None or registration.handle is None:
            raise RuntimeError("resident input registration is unavailable")
        with registration.heartbeat.condition:
            registration.generation = None
            registration.heartbeat.condition.notify_all()
        return registration.handle.close(deadline_monotonic_ns)

    def capture_ready(self, coordinator_hardware_id: str) -> bool:
        resident_hardware_id = f"managed-input-{coordinator_hardware_id}"
        registration = self._registration(resident_hardware_id)
        if registration is None:
            return False
        heartbeat = registration.heartbeat
        with heartbeat.condition:
            last_callback_ns = heartbeat.last_callback_ns
            now_ns = time.monotonic_ns()
            if (
                not heartbeat.faulted
                and last_callback_ns is not None
                and now_ns - last_callback_ns <= self._MAX_CALLBACK_AGE_NS
            ):
                return True
            deadline_monotonic_ns = now_ns + self._RECOVERY_TIMEOUT_NS
            baseline_sequence = heartbeat.sequence
        if not self._rebuild_once(registration, deadline_monotonic_ns):
            return False
        with heartbeat.condition:
            while heartbeat.sequence <= baseline_sequence:
                remaining_ns = deadline_monotonic_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    return False
                heartbeat.condition.wait(remaining_ns / 1_000_000_000)
            return not heartbeat.faulted

    def _rebuild_once(
        self,
        registration: _HardwareInputRegistration,
        deadline_monotonic_ns: int,
    ) -> bool:
        heartbeat = registration.heartbeat
        with heartbeat.condition:
            old_handle = registration.handle
            registration.generation = None
            heartbeat.faulted = True
            heartbeat.condition.notify_all()
        if old_handle is None:
            return False
        try:
            old_handle.request_stop()
            close_result = old_handle.close(deadline_monotonic_ns)
        except Exception:
            return False
        if (
            getattr(close_result, "status", None)
            is not BackendCloseStatus.CLOSED
            or time.monotonic_ns() >= deadline_monotonic_ns
        ):
            return False

        generation = object()
        tapped, tapped_fault = self._generation_callbacks(
            registration,
            generation,
        )
        try:
            new_handle = self._delegate.create_input(
                registration.spec,
                tapped,
                tapped_fault,
            )
        except Exception:
            return False
        with heartbeat.condition:
            registration.handle = new_handle
            registration.generation = generation
            heartbeat.last_callback_ns = None
            heartbeat.faulted = False
        try:
            new_handle.start(deadline_monotonic_ns)
        except Exception:
            with heartbeat.condition:
                if registration.generation is generation:
                    registration.generation = None
                    heartbeat.faulted = True
                    heartbeat.condition.notify_all()
            try:
                new_handle.request_stop()
                new_handle.close(deadline_monotonic_ns)
            except Exception:
                pass
            return False
        return True

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


class _FrozenRunFreezer:
    """Freeze Coordinator runs only from the prepare-owned immutable facts."""

    def __init__(
        self,
        prerequisites,
        output_directory: Path,
        *,
        live_display: _ProductionLiveDisplayManager | None = None,
        dashboard_config_loader=None,
        calibration_loader=None,
    ) -> None:
        if (
            dashboard_config_loader is not None
            and not callable(dashboard_config_loader)
        ):
            raise TypeError("dashboard_config_loader must be callable or None")
        self._configs = {
            int(item.config_id): item for item in prerequisites.configs
        }
        self._output_directory = output_directory.resolve()
        self._live_display = live_display
        self._dashboard_config_loader = dashboard_config_loader
        self._calibration_loader = calibration_loader

    def _config(self, request) -> tuple[object, dict[str, object]]:
        ingress = _decode_payload(request.ingress_payload)
        raw_config = ingress.get("config")
        if type(raw_config) is not dict:
            raise ValueError("frozen ingress config is missing")
        config_id = raw_config.get("config_id")
        if type(config_id) is str and config_id.isdecimal():
            config_id = int(config_id)
        if type(config_id) is not int:
            if len(self._configs) != 1:
                raise ValueError("frozen ingress config identity is ambiguous")
            config_id = next(iter(self._configs))
        config = self._configs.get(config_id)
        if config is None:
            raise ValueError("frozen ingress config was not prepared")
        return config, ingress

    @staticmethod
    def _semantic_config(config) -> dict[str, object]:
        values = {
            key: value
            for key, value in dict(config.values).items()
            if value is None
            or type(value) in {bool, int, float, str, list, dict}
        }
        raw_fault_types = values.get("fault_types_json", "[]")
        values["config_id"] = int(config.config_id)
        values["config_name"] = str(
            values.get("config_name") or f"Config {config.config_id}"
        )
        values["fault_types"] = (
            decode_fault_types_json(raw_fault_types)
            if type(raw_fault_types) is str
            else []
        )
        values["selected_channels"] = list(config.selected_channels)
        runtime_channels = []
        for channel in config.channels:
            channel_values = dict(channel.values)
            for key, expected_type in (
                ("fft_band_limits", list),
                ("oct_band_limits", list),
                ("ai_config", dict),
            ):
                raw_value = channel_values.get(key)
                if type(raw_value) is str:
                    raw_value = json.loads(raw_value)
                if type(raw_value) is not expected_type:
                    raise ValueError(
                        f"frozen channel {key} has an invalid shape"
                    )
                channel_values[key] = raw_value
            runtime_channels.append(channel_values)
        values["channels"] = runtime_channels
        current_keys = (
            "enabled",
            "source",
            "host",
            "port",
            "unit_id",
            "register_type",
            "register_address",
            "register_count",
            "data_type",
            "byte_order",
            "word_order",
            "scale",
            "offset",
            "poll_interval_ms",
            "timeout_ms",
        )
        values["current_meter"] = normalize_current_meter_config(
            {
                key: values.get(f"current_meter_{key}")
                for key in current_keys
            }
        )
        return values

    def _semantic_config_at_acceptance(
        self,
        config,
    ) -> dict[str, object]:
        values = self._semantic_config(config)
        loader = self._dashboard_config_loader
        if loader is None:
            return values
        try:
            current = loader(int(config.config_id))
        except Exception:
            return values
        if not isinstance(current, Mapping):
            return values
        current_config_id = current.get("config_id")
        if (
            type(current_config_id) is str
            and current_config_id.isdecimal()
        ):
            current_config_id = int(current_config_id)
        if current_config_id != int(config.config_id):
            return values
        current_selected = current.get("selected_channels")
        if (
            type(current_selected) is not list
            or tuple(current_selected) != tuple(config.selected_channels)
        ):
            return values
        layout = current.get("result_dashboard_layout")
        if type(layout) not in (dict, str):
            return values
        values["result_dashboard_layout"] = deepcopy(layout)
        return values

    @staticmethod
    def _config_payload(values: Mapping[str, object]) -> FrozenPayload:
        value_id, canonical = encode_run_config_payload(
            values
        )
        return FrozenPayload(value_id, canonical)

    def _calibration_payload(self, config) -> FrozenPayload:
        calibration = config.managed_input.calibration
        loader = self._calibration_loader
        if callable(loader):
            try:
                hardware = loader(int(config.input_hardware_device_id))
            except Exception:
                hardware = None
            if isinstance(hardware, Mapping):
                raw_calibration = hardware.get("mic_cal")
                if isinstance(raw_calibration, Mapping):
                    latest = []
                    channel_count = int(
                        config.managed_input.descriptor_input_channels
                    )
                    for channel_index in range(channel_count):
                        entry = raw_calibration.get(
                            f"In{channel_index + 1}",
                            {},
                        )
                        if not isinstance(entry, Mapping):
                            entry = {}
                        enabled = bool(entry.get("is_selected", 1))
                        try:
                            correction = float(
                                entry.get("deviation_value", 0.0)
                            )
                        except (TypeError, ValueError):
                            correction = 0.0
                        if not math.isfinite(correction):
                            correction = 0.0
                        latest.append(
                            (
                                channel_index,
                                enabled,
                                1 if enabled else 0,
                                correction if enabled else None,
                            )
                        )
                    calibration = tuple(latest)
        value = {
            "hardware_id": config.input_hardware_device_id,
            "calibration_revision": config.managed_input.calibration_revision,
            "channels": list(calibration),
        }
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return FrozenPayload(
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
            canonical,
        )

    @staticmethod
    def _analysis_input(
        config,
        ingress: Mapping[str, object],
        values: Mapping[str, object],
    ) -> FrozenPayload:
        offset_ms = values.get("analysis_window_offset_ms", 0)
        duration = values.get("analysis_duration")
        if (
            type(offset_ms) is not int
            or offset_ms < 0
            or type(duration) not in (int, float)
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
        ):
            raise ValueError("frozen analysis window is invalid")
        start_seconds = float(offset_ms) / 1000.0
        end_seconds = start_seconds + float(duration)
        envelope = {
            "schema": "analysis-input/v1",
            "config": values,
            "workorder": dict(ingress.get("work_order", {}) or {}),
            "channel_mapping": {
                "schema": "audio-channel-map/v1",
                "channel_order": list(config.selected_channels),
            },
            "analysis": {
                "window_start_seconds": start_seconds,
                "window_end_seconds": end_seconds,
                "algorithm_revision": "legacy-judgment/v1",
                "execution_timeout_seconds": max(
                    30.0,
                    float(duration) * 2.0,
                ),
                "queue_timeout_seconds": None,
                "toolchain": None,
            },
        }
        canonical = freeze_analysis_envelope(envelope).encode("utf-8")
        return FrozenPayload(
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
            canonical,
        )

    def _final_path(self, run_id: str) -> FinalPathRequest:
        return FinalPathRequest(
            str((self._output_directory / f"{run_id}.wav").resolve())
        )

    @staticmethod
    def _target_frames(config) -> int:
        values = config.values
        duration = values.get("analysis_duration")
        offset_ms = values.get("analysis_window_offset_ms", 0)
        rate = config.managed_input.descriptor_sample_rate_hz
        if (
            type(duration) not in (int, float)
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
            or type(offset_ms) is not int
            or offset_ms < 0
        ):
            raise ValueError("frozen analysis window is invalid")
        capture_duration = float(offset_ms) / 1000.0 + float(duration)
        if "record_duration" in values:
            record_duration = values["record_duration"]
            if (
                type(record_duration) not in (int, float)
                or not math.isfinite(float(record_duration))
                or float(record_duration) <= 0.0
            ):
                raise ValueError("frozen recording duration is invalid")
            capture_duration = max(
                capture_duration,
                float(record_duration),
            )
        frames = math.ceil(capture_duration * float(rate))
        if not 1 <= frames <= MAX_ALLOCATABLE_FRAMES:
            raise ValueError("frozen capture frame count is unsupported")
        return frames

    def freeze(self, request) -> FrozenCapturedRun:
        config, ingress = self._config(request)
        semantic_config = self._semantic_config_at_acceptance(config)
        frames = self._target_frames(config)
        channel_count = len(config.selected_channels)
        frozen = FrozenCapturedRun(
            run_id=request.run_id,
            trigger_monotonic_ns=request.trigger_monotonic_ns,
            source=request.source,
            config_id=str(config.config_id),
            hardware_id=str(config.input_hardware_device_id),
            target_frame_count=frames,
            config_payload=self._config_payload(semantic_config),
            hardware_payload=_payload(
                f"hardware:{config.input_hardware_device_id}",
                dict(config.managed_input.values),
            ),
            calibration_payload=self._calibration_payload(config),
            work_order_payload=_payload(
                f"work-order:{request.run_id}",
                ingress.get("work_order", {}),
            ),
            history_labels=request.fallback_history_labels,
            analysis_input=self._analysis_input(
                config,
                ingress,
                semantic_config,
            ),
            final_path_request=self._final_path(request.run_id),
            metadata=AudioMetadataConstraints(
                expected_sample_rate_hz=int(
                    config.managed_input.descriptor_sample_rate_hz
                ),
                expected_channel_count=channel_count,
                expected_frame_count=frames,
                format=PersistedAudioFormat.PCM16_WAV,
            ),
        )
        if self._live_display is not None:
            self._live_display.register_frozen(frozen)
        return frozen


class _ImportedRunFreezer(_FrozenRunFreezer):
    def freeze(self, request) -> FrozenImportedRun:
        config, ingress = self._config(request)
        semantic_config = self._semantic_config_at_acceptance(config)
        source_value = ingress.get("source_path")
        if type(source_value) is not str:
            raise ValueError("frozen imported source path is missing")
        source = Path(source_value).expanduser().resolve(strict=True)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with wave.open(str(source), "rb") as reader:
            if reader.getsampwidth() != 2:
                raise ValueError("imported source must be PCM16 WAV")
            rate = reader.getframerate()
            channels = reader.getnchannels()
            frames = reader.getnframes()
        reference = ImportedSourceReference(
            source_identity=digest,
            absolute_path=str(source),
            sample_rate_hz=rate,
            channel_count=channels,
            frame_count=frames,
            format=ImportedSourceFormat.PCM16_WAV,
        )
        metadata = AudioMetadataConstraints(
            expected_sample_rate_hz=rate,
            expected_channel_count=channels,
            expected_frame_count=frames,
            format=PersistedAudioFormat.PCM16_WAV,
        )
        return FrozenImportedRun(
            run_id=request.run_id,
            trigger_monotonic_ns=request.trigger_monotonic_ns,
            source=request.source,
            config_id=str(config.config_id),
            source_reference=reference,
            config_payload=self._config_payload(semantic_config),
            calibration_payload=self._calibration_payload(config),
            work_order_payload=_payload(
                f"work-order:{request.run_id}",
                {},
            ),
            history_labels=request.fallback_history_labels,
            analysis_input=self._analysis_input(
                config,
                ingress,
                semantic_config,
            ),
            final_path_request=self._final_path(request.run_id),
            metadata=metadata,
        )


class _CoordinatorPersistence:
    def __init__(self, runtime, result_artifact_store=None) -> None:
        self._runs = RunRepository(runtime)
        self._audio = AudioRepository(runtime)
        self._attempts = AttemptRepository(
            runtime,
            result_artifact_store=result_artifact_store,
        )

    @staticmethod
    def _committed(result) -> CommitResult:
        if result.state is TransactionState.COMMITTED and result.value is True:
            return CommitResult.COMMITTED
        if result.state is TransactionState.UNKNOWN:
            return CommitResult.UNAVAILABLE
        return CommitResult.REJECTED

    def create_active(self, frozen_run) -> CommitResult:
        captured = type(frozen_run) is FrozenCapturedRun
        analysis_input = _decode_payload(frozen_run.analysis_input)
        hardware = (
            _decode_payload(frozen_run.hardware_payload)
            if captured
            else None
        )
        calibration = _decode_payload(frozen_run.calibration_payload)
        inserted = RunInsert(
            run_id=frozen_run.run_id,
            ingress_kind=(
                IngressKind.CAPTURED if captured else IngressKind.IMPORTED
            ),
            trigger_source=PersistenceTriggerSource[frozen_run.source.name],
            config_id=int(frozen_run.config_id),
            hardware_id=int(frozen_run.hardware_id) if captured else None,
            snapshots=freeze_run_inputs(
                analysis_input,
                hardware,
                calibration,
            ),
            accepted_at=_utc_now_text(),
            imported_source_identity=(
                None if captured else frozen_run.source_reference.source_identity
            ),
            imported_source_path=(
                None if captured else frozen_run.source_reference.absolute_path
            ),
            imported_expected_frame_count=(
                None if captured else frozen_run.source_reference.frame_count
            ),
        )
        return self._committed(self._runs.insert_active(inserted))

    def commit_initial_terminal(self, fact) -> CommitResult:
        if fact.conclusion in (TerminalConclusion.OK, TerminalConclusion.NG):
            state = RunState.SUCCEEDED
            judgment = (
                Judgment.PASS
                if fact.conclusion is TerminalConclusion.OK
                else Judgment.FAIL
            )
        elif fact.conclusion is TerminalConclusion.NO_RESULT:
            state, judgment = RunState.NO_RESULT, None
        elif fact.conclusion is TerminalConclusion.MANUAL_STOPPED:
            state, judgment = RunState.CANCELED, None
        else:
            state, judgment = RunState.FAILED, None
        terminal = RunTerminal(
            state=state,
            code=(
                fact.error_category.name
                if fact.error_category is not None
                else fact.conclusion.name
            ),
            summary=fact.reason or None,
            terminal_at=fact.terminal_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            judgment=judgment,
        )
        return self._committed(
            self._runs.finish_active(fact.run_id, terminal)
        )

    def apply_manual_judgment(self, request, facts) -> ManualProjectionResult:
        config = _decode_payload(facts.config_payload)["config"]
        accepted_fault_types = tuple(config.get("fault_types", ()))
        judgment = (
            Judgment.PASS
            if request.conclusion is TerminalConclusion.OK
            else Judgment.FAIL
        )
        committed = self._runs.set_manual_judgment(
            request.run_id,
            judgment,
            _utc_now_text(),
            expected_revision=request.expected_revision,
            fault_type=request.fault_type,
            accepted_fault_types=accepted_fault_types,
        )
        if (
            committed.state is TransactionState.COMMITTED
            and committed.value is True
        ):
            revision = request.expected_revision + 1
            projection = ResultProjection(
                phase=ResultProjectionPhase.TERMINAL,
                conclusion=request.conclusion,
                error_category=None,
                reason="",
                history_labels=facts.current_projection.history_labels,
                result_payload=_result_payload_with_judgment(
                    facts.current_projection.result_payload,
                    request.conclusion,
                ),
                fault_type=(
                    request.fault_type
                    if request.conclusion is TerminalConclusion.NG
                    else None
                ),
                manual_judgment_revision=revision,
            )
            return ManualProjectionResult(
                ManualProjectionStatus.COMMITTED,
                request.run_id,
                projection,
                revision,
                "",
            )
        status = (
            ManualProjectionStatus.UNAVAILABLE
            if committed.state is TransactionState.UNKNOWN
            else ManualProjectionStatus.REJECTED
        )
        return ManualProjectionResult(
            status,
            request.run_id,
            None,
            None,
            committed.reason or "manual judgment persistence rejected",
        )

    def load_result_control_facts(self, run_id):
        run = self._runs.get(run_id)
        if (
            run is None
            or run.state != RunState.SUCCEEDED.value
            or run.judgment not in {Judgment.PASS.value, Judgment.FAIL.value}
            or run.causal_revision <= 0
        ):
            return None
        attempt = self._attempts.latest_successful_for_run(run_id)
        if attempt is None or not attempt.result_payload_json:
            return None
        envelope = decode_analysis_envelope(run.config_snapshot_json)
        config = json.loads(run.config_snapshot_json)["config"]
        config_value_id, config_bytes = encode_run_config_payload(config)
        config_payload = FrozenPayload(config_value_id, config_bytes)
        calibration_bytes = json.dumps(
            json.loads(run.calibration_snapshot_json),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        calibration_payload = FrozenPayload(
            "sha256:" + hashlib.sha256(calibration_bytes).hexdigest(),
            calibration_bytes,
        )
        analysis_bytes = run.config_snapshot_json.encode("utf-8")
        analysis_input = FrozenPayload(
            "sha256:" + hashlib.sha256(analysis_bytes).hexdigest(),
            analysis_bytes,
        )
        audio_record = self._audio.get_for_run(run_id)
        audio = (
            None
            if audio_record is None
            else PersistedAudioReference(
                audio_id=audio_record.audio_id,
                save_job_id=audio_record.save_job_id,
                run_id=audio_record.run_id,
                source_kind=AudioSaveSourceKind[audio_record.source_kind],
                source_identity=audio_record.source_identity,
                final_path=audio_record.final_path,
                sample_rate_hz=audio_record.sample_rate_hz,
                channel_count=audio_record.channel_count,
                frame_count=audio_record.frame_count,
                byte_size=audio_record.byte_size,
                format=PersistedAudioFormat.PCM16_WAV,
                content_revision=audio_record.content_revision,
            )
        )
        trigger_source = {
            "PLC": "plc",
            "MANUAL": "manual",
            "SCANNER": "scan",
            "IMPORT": "import_audio",
        }[run.trigger_source]
        selected_channels = config.get("selected_channels", [])
        details = [
            {
                "label": "通道",
                "value": ", ".join(
                    f"CH{int(channel) + 1}"
                    for channel in selected_channels
                    if isinstance(channel, int) and not isinstance(channel, bool)
                ),
            }
        ]
        if run.hardware_id is not None:
            details.append({"label": "硬件", "value": str(run.hardware_id)})
        labels_value_id, labels_bytes = encode_history_labels_payload(
            config_id=run.config_id,
            name=str(config.get("config_name") or f"Config {run.config_id}"),
            fault_types=config.get("fault_types", []),
            details=details,
            sn=str(
                envelope.workorder.get("sn")
                or envelope.workorder.get("sn_code")
                or ""
            ),
            trigger_source=trigger_source,
        )
        history_labels = FrozenPayload(labels_value_id, labels_bytes)
        result_payload = FrozenPayload(
            f"bridge-result:{attempt.attempt_id}",
            attempt.result_payload_json.encode("utf-8"),
        )
        conclusion = (
            TerminalConclusion.OK
            if run.judgment == Judgment.PASS.value
            else TerminalConclusion.NG
        )
        projection = ResultProjection(
            phase=ResultProjectionPhase.TERMINAL,
            conclusion=conclusion,
            error_category=None,
            reason="",
            history_labels=history_labels,
            result_payload=_result_payload_with_judgment(
                result_payload,
                conclusion,
            ),
            fault_type=run.fault_type,
            manual_judgment_revision=run.manual_judgment_revision,
        )
        return ResultControlFacts(
            run_id=run_id,
            config_payload=config_payload,
            calibration_payload=calibration_payload,
            analysis_input=analysis_input,
            audio=audio,
            manual_judgment_allowed=True,
            manual_reanalysis_allowed=audio is not None,
            current_projection=projection,
            causal_revision=run.causal_revision,
        )

    def attach_current_trace(self, run_id, trace) -> CommitResult:
        return self._committed(self._runs.attach_current_trace(run_id, trace))


class _PreparedRealUpstream:
    def __init__(
        self,
        managed_resident,
        resident_capture,
        streams,
        live_display,
    ) -> None:
        self.managed_resident = managed_resident
        self.resident_capture = resident_capture
        self.streams = streams
        self.live_display = live_display
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.resident_capture.close_and_join(5.0)
        finally:
            try:
                self.managed_resident.close_all(5.0)
            finally:
                self.live_display.close()

    def complete_ordered_shutdown(self) -> None:
        self._closed = True


@dataclass(slots=True)
class _RealGraph:
    coordinator: object
    scheduler_bridge: object
    save_dispatcher: object
    resident_capture: object
    managed_resident: object
    plc_dispatch_ports: object
    plc_result_sequencer: object
    playback: object
    history_sink: object
    streams: tuple[object, ...]
    scheduler: object
    analysis_executor: object
    audio_authority: object
    playback_backend: object
    shared_backend: object | None
    live_display: object
    monotonic: object
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.playback.shutdown(5.0)
        except Exception:
            pass
        try:
            self.scheduler.shutdown(5.0)
        except Exception:
            pass
        try:
            self.analysis_executor.close()
        except Exception:
            pass
        try:
            self.save_dispatcher.shutdown(self.monotonic() + 5.0)
        except Exception:
            pass
        try:
            self.live_display.close()
        except Exception:
            pass

    def complete_ordered_shutdown(self) -> None:
        try:
            self.analysis_executor.close()
        except Exception:
            pass
        self._closed = True


class ProductionGraphFactory:
    """Construct the real production leaf graph from frozen prerequisites."""

    def __init__(
        self,
        *,
        runtime,
        plc_dispatch_ports,
        plc_result_sequencer: PlcResultSequencer,
        output_directory: str | Path,
        diagnostic_callback=None,
        sounddevice_module=None,
        event_bus=None,
        dashboard_config_loader=None,
        calibration_loader=None,
        result_artifact_store: AnalysisResultArtifactStore | None = None,
    ) -> None:
        self._runtime = runtime
        self._plc_dispatch_ports = plc_dispatch_ports
        self._plc_result_sequencer = plc_result_sequencer
        self._output_directory = Path(output_directory)
        self._diagnostic_callback = diagnostic_callback
        self._sounddevice_module = sounddevice_module
        self._event_bus = event_bus
        self._dashboard_config_loader = dashboard_config_loader
        self._calibration_loader = calibration_loader
        self._result_artifact_store = result_artifact_store

    def prepare_upstream(self, prerequisites):
        self._prepare_output_directory()
        if self._result_artifact_store is not None:
            self._result_artifact_store.ensure_ready()
        raw_backend = ManagedInputSoundDeviceBackend(self._sounddevice_module)
        scan_id = BackendScanId("production-composition", 1)
        scan = raw_backend.query_raw_input_endpoints(scan_id)
        endpoints = []
        for frozen in prerequisites.managed_inputs:
            matches = tuple(
                observation
                for observation in scan.observations
                if (
                    observation.stable_backend_uid
                    == frozen.stable_backend_uid
                    and observation.descriptor.device_name
                    == frozen.descriptor_device_name
                    and observation.descriptor.hostapi_name
                    == frozen.descriptor_hostapi_name
                )
            )
            if len(matches) != 1:
                self._emit_startup_diagnostic(
                    f"managed input {frozen.hardware_id} did not resolve "
                    f"exactly ({len(matches)} matches); skipped"
                )
                continue
            observation = matches[0]
            endpoints.append(
                HardwareEndpointIdentity(
                    hardware_id=ManagedHardwareId(frozen.hardware_id),
                    scan_id=scan_id,
                    device_index=observation.device_index,
                    hostapi_index=observation.hostapi_index,
                    stable_backend_uid=observation.stable_backend_uid,
                    device_name=observation.descriptor.device_name,
                    hostapi_name=observation.descriptor.hostapi_name,
                    sample_rate_hz=frozen.descriptor_sample_rate_hz,
                    input_channels=frozen.descriptor_input_channels,
                )
            )
        if not endpoints:
            raise ProductionCompositionError(
                "production has zero exact managed input endpoints"
            )
        if len(endpoints) > 6:
            raise ProductionCompositionError(
                "production requires one through six managed inputs"
            )
        max_frames = max(
            _FrozenRunFreezer._target_frames(config)
            for config in prerequisites.configs
        )
        live_display = _ProductionLiveDisplayManager(
            bus=self._event_bus,
            diagnostic_callback=self._diagnostic_callback,
        )
        raw_audio_backend = prerequisites.build_sounddevice_audio_backend(
            self._sounddevice_module
        )
        backend = _LiveDisplayAudioBackend(raw_audio_backend, live_display)
        resident = prerequisites.build_resident_adapter(
            backend=backend,
            level_meter=lambda samples, _rate: float(
                20.0
                * np.log10(
                    max(float(np.sqrt(np.mean(np.square(samples)))), 1e-12)
                )
            ),
            max_capture_frames=max_frames,
        )
        startup = resident.open_startup_streams(tuple(endpoints))
        for failure in startup.failures:
            self._emit_startup_diagnostic(
                f"managed input {failure.hardware_id.value} Resident stream "
                f"open failed ({failure.error_code}); skipped"
            )
        if not startup.ready_streams:
            resident.close_all(5.0)
            raise ProductionCompositionError(
                "production has zero ready Resident streams"
            )
        routes = tuple(
            ManagedResidentCaptureRoute(
                coordinator_hardware_id=str(item.endpoint.hardware_id.value),
                managed_hardware_id=item.endpoint.hardware_id,
                generation=item.generation,
            )
            for item in startup.ready_streams
        )
        capture = ManagedResidentCapturePort(
            resident,
            routes,
            readiness_probe=backend.capture_ready,
        )
        return _PreparedRealUpstream(
            resident,
            capture,
            tuple(startup.ready_streams),
            live_display,
        )

    def _prepare_output_directory(self) -> None:
        try:
            self._output_directory.mkdir(parents=True, exist_ok=True)
            if not self._output_directory.is_dir():
                raise NotADirectoryError(str(self._output_directory))
        except OSError as exc:
            raise ProductionCompositionError(
                f"production output directory is unavailable: "
                f"{self._output_directory}"
            ) from exc
        probe_id = uuid4().hex
        source = self._output_directory / (
            f".jingcheng-publication-probe-{probe_id}.tmp"
        )
        linked = self._output_directory / (
            f".jingcheng-publication-probe-{probe_id}.link"
        )
        failure: Exception | None = None
        try:
            with source.open("xb") as probe:
                probe.write(b"jingcheng-publication-probe")
                probe.flush()
                os.fsync(probe.fileno())
            os.link(source, linked)
        except Exception as exc:
            failure = exc
        for owned_path in (linked, source):
            try:
                owned_path.unlink(missing_ok=True)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise ProductionCompositionError(
                "production output directory publication probe failed"
            ) from failure

    def _emit_startup_diagnostic(self, detail: str) -> None:
        if callable(self._diagnostic_callback):
            self._diagnostic_callback(detail)

    def activate(self, prerequisites, upstream, history_sink):
        save_dispatcher = None
        scheduler = None
        analysis_executor = None
        try:
            runs = RunRepository(self._runtime)
            audio = AudioRepository(self._runtime)
            attempts = AttemptRepository(
                self._runtime,
                result_artifact_store=self._result_artifact_store,
            )
            durable_history = DurableHistorySink(
                runs,
                history_sink,
                diagnostic_callback=self._diagnostic_callback,
            )
            audio_authority = ProductionRunAudioAdapter(
                AudioPersistenceService(AudioPublication(), audio, runs)
            )
            saver = ProductionAudioSaver(audio_authority)
            save_dispatcher = BoundedAudioSaveDispatcher(
                saver,
                saver,
                worker_count=2,
                outstanding_capacity=6,
            )
            boundary = AnalysisPersistenceBoundary(
                runtime=self._runtime,
                attempt_repository=attempts,
                now_utc=_utc_now_text,
            )
            live_display = getattr(upstream, "live_display", None)
            capture_active = getattr(live_display, "has_active_runs", None)
            if callable(capture_active):
                analysis_executor = prerequisites.build_analysis_executor(
                    capture_active=capture_active,
                    process_count=_PRODUCTION_ANALYSIS_WORKER_COUNT,
                )
            else:
                analysis_executor = prerequisites.build_analysis_executor(
                    process_count=_PRODUCTION_ANALYSIS_WORKER_COUNT,
                )
            start_analysis = getattr(analysis_executor, "start", None)
            if callable(start_analysis):
                start_analysis()
            scheduler = AnalysisScheduler(
                config=AnalysisSchedulerConfig(
                    worker_count=_PRODUCTION_ANALYSIS_WORKER_COUNT,
                    pending_capacity=6,
                ),
                audio_loader=audio_authority,
                analysis_executor=analysis_executor,
                start_gate=boundary,
                outcome_sink=boundary,
                clock=prerequisites.monotonic_authority,
            )
            bridge = RunPersistenceSchedulerBridge(
                runtime=self._runtime,
                run_repository=runs,
                audio_repository=audio,
                attempt_repository=attempts,
                boundary=boundary,
                scheduler=scheduler,
                now_utc=_utc_now_text,
            )
            captured_freezer = _FrozenRunFreezer(
                prerequisites,
                self._output_directory,
                live_display=upstream.live_display,
                dashboard_config_loader=self._dashboard_config_loader,
                calibration_loader=self._calibration_loader,
            )
            imported_freezer = _ImportedRunFreezer(
                prerequisites,
                self._output_directory,
                dashboard_config_loader=self._dashboard_config_loader,
                calibration_loader=self._calibration_loader,
            )
            coordinator = MultiRunCoordinator(
                captured_freezer=captured_freezer,
                imported_freezer=imported_freezer,
                resident=upstream.resident_capture,
                persistence=_CoordinatorPersistence(
                    self._runtime,
                    self._result_artifact_store,
                ),
                save_dispatcher=save_dispatcher,
                scheduler=bridge,
                history_projector=_HistoryProjector(),
                history_sink=durable_history,
                plc=self._plc_result_sequencer,
                diagnostics=_Diagnostics(self._diagnostic_callback),
                save_job_id_factory=lambda: str(uuid4()),
                wall_clock=lambda: datetime.now(timezone.utc),
                monotonic_clock=prerequisites.monotonic_authority.monotonic,
                current_collector_factory=ProductionCurrentCollectorFactory(
                    bus=self._event_bus,
                    monotonic=prerequisites.monotonic_authority.monotonic,
                    log_callback=self._diagnostic_callback,
                ),
            )
            playback_backend = SoundDevicePlaybackBackend()
            playback = RunScopedPlaybackService(
                run_repository=runs,
                audio_repository=audio,
                audio_loader=audio_authority,
                backend=playback_backend,
                monotonic=prerequisites.monotonic_authority.monotonic,
            )
            return _RealGraph(
                coordinator=coordinator,
                scheduler_bridge=bridge,
                save_dispatcher=save_dispatcher,
                resident_capture=upstream.resident_capture,
                managed_resident=upstream.managed_resident,
                plc_dispatch_ports=self._plc_dispatch_ports,
                plc_result_sequencer=self._plc_result_sequencer,
                playback=playback,
                history_sink=durable_history,
                streams=upstream.streams,
                scheduler=scheduler,
                analysis_executor=analysis_executor,
                audio_authority=audio_authority,
                playback_backend=playback_backend,
                shared_backend=None,
                live_display=upstream.live_display,
                monotonic=prerequisites.monotonic_authority.monotonic,
            )
        except Exception:
            if scheduler is not None:
                try:
                    scheduler.shutdown(5.0)
                except Exception:
                    pass
            if save_dispatcher is not None:
                try:
                    save_dispatcher.shutdown(
                        prerequisites.monotonic_authority.monotonic() + 5.0
                    )
                except Exception:
                    pass
            if analysis_executor is not None:
                try:
                    analysis_executor.close()
                except Exception:
                    pass
            raise


def _validate_authority(authority: object) -> None:
    try:
        validate_python_system_monotonic_authority(authority)
    except (TypeError, ValueError) as exc:
        raise ProductionCompositionError(
            "production composition requires the Python/system monotonic domain"
        ) from exc


def _close_once(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _run_cleanup_actions(
    *actions: Callable[[], None],
) -> Exception | None:
    first_failure = None
    for action in actions:
        try:
            action()
        except Exception as exc:
            if first_failure is None:
                first_failure = exc
    return first_failure


@dataclass(frozen=True, slots=True)
class _PublishedOwners:
    coordinator: object
    scheduler_bridge: object
    save_dispatcher: object
    resident_capture: object
    managed_resident: object
    plc_dispatch_ports: object
    plc_result_sequencer: object
    playback: object
    history_sink: object
    streams: tuple[object, ...]

    @classmethod
    def from_graph(cls, graph: object) -> "_PublishedOwners":
        try:
            values = {
                name: getattr(graph, name)
                for name in (
                    "coordinator",
                    "scheduler_bridge",
                    "save_dispatcher",
                    "resident_capture",
                    "managed_resident",
                    "plc_dispatch_ports",
                    "plc_result_sequencer",
                    "playback",
                    "history_sink",
                    "streams",
                )
            }
        except AttributeError as exc:
            raise ProductionCompositionError(
                f"active production graph is missing owner {exc.name}"
            ) from exc
        streams = values["streams"]
        if type(streams) is not tuple or not 1 <= len(streams) <= 6:
            raise ProductionCompositionError(
                "production graph must own one through six Resident streams"
            )
        return cls(**values)


class ActiveProductionComposition:
    """Published immutable owner handles for the subsequently ordered shutdown."""

    def __init__(
        self,
        *,
        prerequisites: _PreparedPrerequisites,
        upstream: _PreparedUpstream,
        graph: object,
    ) -> None:
        owners = _PublishedOwners.from_graph(graph)
        self.coordinator = owners.coordinator
        self.scheduler_bridge = owners.scheduler_bridge
        self.save_dispatcher = owners.save_dispatcher
        self.resident_capture = owners.resident_capture
        self.managed_resident = owners.managed_resident
        self.plc_dispatch_ports = owners.plc_dispatch_ports
        self.plc_result_sequencer = owners.plc_result_sequencer
        self.playback = owners.playback
        self.history_sink = owners.history_sink
        self.streams = owners.streams
        self.scheduler = getattr(graph, "scheduler", None)
        self.audio_authority = getattr(graph, "audio_authority", None)
        self.playback_backend = getattr(graph, "playback_backend", None)
        self.shared_backend = getattr(graph, "shared_backend", None)
        self.selection_generation = prerequisites.selection_generation
        self.selected_config_ids = prerequisites.selected_config_ids
        self.config_routes = _freeze_config_routes(prerequisites)
        self.monotonic_authority = prerequisites.monotonic_authority
        self.monotonic_domain = PYTHON_SYSTEM_MONOTONIC_DOMAIN
        self.ingress_open = False
        self._prerequisites = prerequisites
        self._upstream = upstream
        self._graph = graph
        self._closed = False
        self._close_failure: Exception | None = None

    def close(self) -> None:
        """Fault-cleanup close; normal ordered shutdown is owned by Task 14."""
        if self._closed:
            if self._close_failure is not None:
                raise self._close_failure
            return
        self._closed = True
        self._close_failure = _run_cleanup_actions(
            lambda: _close_once(self._graph),
            lambda: _close_once(self._upstream),
            self._prerequisites.close,
        )
        if self._close_failure is not None:
            raise self._close_failure

    def complete_ordered_shutdown(self) -> None:
        """Release composition ownership after the ordered owner settled cleanly."""
        if self._closed:
            return
        self._graph.complete_ordered_shutdown()
        self._upstream.complete_ordered_shutdown()
        self._prerequisites.close()
        self._closed = True


class PreparedProductionComposition:
    """Prepared upstream resources with the Coordinator deliberately absent."""

    def __init__(
        self,
        *,
        prerequisites: _PreparedPrerequisites,
        upstream: _PreparedUpstream,
        graph_factory: _GraphFactory,
    ) -> None:
        self.selection_generation = prerequisites.selection_generation
        self.selected_config_ids = prerequisites.selected_config_ids
        self.config_routes = _freeze_config_routes(prerequisites)
        self.monotonic_authority = prerequisites.monotonic_authority
        self.monotonic_domain = PYTHON_SYSTEM_MONOTONIC_DOMAIN
        self.coordinator = None
        self.ingress_open = False
        self.streams = upstream.streams
        self._prerequisites = prerequisites
        self._upstream = upstream
        self._graph_factory = graph_factory
        self._active: ActiveProductionComposition | None = None
        self._closed = False
        self._close_failure: Exception | None = None

    def activate(self, history_sink: object) -> ActiveProductionComposition:
        if self._closed:
            raise ProductionCompositionError("prepared composition is closed")
        if self._active is not None:
            return self._active
        if not callable(getattr(history_sink, "publish", None)):
            raise ProductionCompositionError(
                "an attached production history sink is required"
            )
        graph = None
        try:
            self._prerequisites.validate_activation()
            graph = self._graph_factory.activate(
                self._prerequisites,
                self._upstream,
                history_sink,
            )
            active = ActiveProductionComposition(
                prerequisites=self._prerequisites,
                upstream=self._upstream,
                graph=graph,
            )
        except Exception as exc:
            cleanup_actions = []
            if graph is not None:
                cleanup_actions.append(lambda: _close_once(graph))
            cleanup_actions.append(self.close)
            cleanup_failure = _run_cleanup_actions(*cleanup_actions)
            if cleanup_failure is not None:
                self._closed = True
                self._close_failure = cleanup_failure
                raise cleanup_failure
            if isinstance(exc, ProductionCompositionError):
                raise
            raise ProductionCompositionError(str(exc) or type(exc).__name__) from exc
        self._active = active
        return active

    def close(self) -> None:
        if self._closed:
            if self._close_failure is not None:
                raise self._close_failure
            return
        self._closed = True
        if self._active is not None:
            self._close_failure = _run_cleanup_actions(
                self._active.close
            )
        else:
            self._close_failure = _run_cleanup_actions(
                lambda: _close_once(self._upstream),
                self._prerequisites.close,
            )
        if self._close_failure is not None:
            raise self._close_failure


class ProductionCompositionBuilder:
    """Prepare once, then activate once after Dashboard history attachment."""

    def __init__(
        self,
        *,
        prerequisites: _PrerequisiteProvider,
        graph_factory: _GraphFactory,
        monotonic_authority: object | None = None,
    ) -> None:
        self._prerequisites = prerequisites
        self._graph_factory = graph_factory
        self._monotonic_authority = (
            PythonSystemMonotonicAuthority()
            if monotonic_authority is None
            else monotonic_authority
        )
        self._prepared: PreparedProductionComposition | None = None

    def prepare(self) -> PreparedProductionComposition:
        if self._prepared is not None:
            return self._prepared
        prerequisites: _PreparedPrerequisites | None = None
        upstream: _PreparedUpstream | None = None
        try:
            _validate_authority(self._monotonic_authority)
            prerequisites = self._prerequisites.prepare(
                monotonic_authority=self._monotonic_authority,
            )
            if prerequisites.monotonic_authority is not self._monotonic_authority:
                raise ProductionCompositionError(
                    "prepared prerequisites changed the monotonic authority"
                )
            _validate_authority(prerequisites.monotonic_authority)
            upstream = self._graph_factory.prepare_upstream(prerequisites)
            streams = getattr(upstream, "streams", None)
            if type(streams) is not tuple or not 1 <= len(streams) <= 6:
                raise ProductionCompositionError(
                    "production must prepare one through six Resident streams"
                )
            prepared = PreparedProductionComposition(
                prerequisites=prerequisites,
                upstream=upstream,
                graph_factory=self._graph_factory,
            )
        except Exception as exc:
            if upstream is not None:
                _close_once(upstream)
            if prerequisites is not None:
                prerequisites.close()
            if isinstance(exc, ProductionCompositionError):
                raise
            raise ProductionCompositionError(str(exc) or type(exc).__name__) from exc
        self._prepared = prepared
        return prepared


def build_production_composition_builder(container) -> ProductionCompositionBuilder:
    """Build the one non-injected production builder selected by ServiceContainer."""
    from app.service.multi_run_integration.production_prerequisites import (
        ProductionPrerequisites,
    )
    from consts.running_consts import base_dir

    authority = PythonSystemMonotonicAuthority()
    container.monotonic_ns = authority.monotonic_ns
    prerequisites = ProductionPrerequisites(
        container.factory_selection_settings,
        golden_sample_service=container.config_service.golden_sample_service,
    )
    graph_factory = ProductionGraphFactory(
        runtime=container.run_persistence_database,
        plc_dispatch_ports=container.plc_dispatch_ports,
        plc_result_sequencer=container.plc_result_sequencer,
        output_directory=Path(base_dir) / "data" / "run_audio",
        diagnostic_callback=lambda fact: container.bus.log.emit(str(fact)),
        event_bus=container.bus,
        dashboard_config_loader=(
            container.config_service.load_config_for_result_dashboard
        ),
        calibration_loader=container.hss_service.get_input_hardware,
        result_artifact_store=container.analysis_result_artifact_store,
    )
    return ProductionCompositionBuilder(
        prerequisites=prerequisites,
        graph_factory=graph_factory,
        monotonic_authority=authority,
    )


__all__ = [
    "ActiveProductionComposition",
    "PreparedProductionComposition",
    "ProductionCompositionBuilder",
    "ProductionCompositionError",
    "ProductionCurrentCollectorFactory",
    "ProductionGraphFactory",
    "build_production_composition_builder",
]
