"""Run completed-recording analysis outside the capture process."""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import queue
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.golden_reference import FrozenGoldenArtifact
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.ai_resident_runtime import ResidentToolchainRuntime
from app.service.analysis.scheduler import (
    AnalysisBundleIdentity,
    AiToolchainSnapshot,
    AnalysisBundleToken,
    AnalysisDecisionEnvelope,
    AnalysisEnrichmentReport,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisJob,
    AnalysisStage,
    FrozenResultObject,
)
from app.service.analysis.performance_diagnostics import (
    AnalysisStageTimingEvent,
    AnalysisStageTimings,
    AnalysisTimingBoundary,
    AnalysisTimingStatus,
)
from app.service.analysis.thread_priority import (
    IDLE_PRIORITY_CLASS,
    THREAD_PRIORITY_BELOW_NORMAL,
    set_current_process_priority,
    set_current_thread_priority,
)

_CANONICAL_TRANSPORT = "canonical-json-zlib-base64/v1"
_TRANSPORT_CHUNK_SIZE = 60_000
_TRANSPORT_ZLIB_LEVEL = 3
_DEFAULT_PROCESS_COUNT = 2
_SLOT_REPLACEMENT_ATTEMPTS = 3
_SLOT_REPLACEMENT_BACKOFF_SECONDS = 0.05
_ToolchainKey = tuple[str, str, str, str | None]


def _toolchain_key(snapshot: AiToolchainSnapshot) -> _ToolchainKey:
    return (
        snapshot.toolchain_id,
        snapshot.execution_revision,
        snapshot.entrypoint,
        snapshot.package_dir or None,
    )


def _binding_key(binding: Mapping[str, object]) -> _ToolchainKey:
    return (
        binding.get("toolchain_id"),
        binding.get("execution_revision"),
        binding.get("entrypoint"),
        binding.get("package_dir") or None,
    )


@dataclass(slots=True)
class _ProcessAudio:
    samples: np.ndarray
    sample_rate_hz: int
    channel_count: int
    frame_count: int

    def close(self) -> None:
        return None


class _ProcessCancellationToken:
    is_cancelled = False
    reason = None

    @staticmethod
    def wait(timeout_seconds=None) -> bool:
        del timeout_seconds
        return False


@dataclass(slots=True)
class _AnalysisProcessSlot:
    slot_id: int
    connection: object
    control_connection: object
    process: object
    ready: bool = False
    busy: bool = False
    ready_toolchains: set[_ToolchainKey] = field(default_factory=set)
    control_thread: threading.Thread | None = None
    quarantined: bool = False


@dataclass(frozen=True, slots=True)
class _CompressedEnrichmentTransport:
    identity: object
    judgment: str
    result_payload_zlib: bytes
    chart_manifest: tuple[tuple[int, tuple[str, ...]], ...]
    timings: AnalysisStageTimings


@dataclass(frozen=True, slots=True)
class _OwnedBundle:
    slot: _AnalysisProcessSlot
    token: AnalysisBundleToken
    cancel_token: object


@dataclass(frozen=True, slots=True)
class _PendingEnrichmentDelivery:
    report: AnalysisEnrichmentReport
    quiet_epoch_id: int


@dataclass(slots=True)
class _EnrichmentAttemptState:
    audio_frozen: bool = True
    decision_committed: bool = False
    charts_visible: bool = False
    owner_slot_id: int | None = None
    owner_token: AnalysisBundleToken | None = None
    retry_count: int = 0
    quiet_epoch_id: int = 0
    enrichment_queued: bool = False
    enrichment_inflight: bool = False
    checkpoint: str = "audio_frozen"
    parked_transport: object | None = None
    parked_report: AnalysisEnrichmentReport | None = None


def _process_error(summary: str) -> AnalysisExecutionError:
    return AnalysisExecutionError(
        AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            stage=AnalysisStage.EXECUTE,
            summary=summary[:1024],
        )
    )


def _plain_frozen_value(value: object) -> object:
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str):
        return value
    if value_type is tuple:
        return [_plain_frozen_value(item) for item in value]
    if value_type is FrozenResultObject:
        return {
            key: _plain_frozen_value(item)
            for key, item in value.items
        }
    raise TypeError(f"unsupported frozen result value {value_type.__name__}")


def _pack_result_for_transport(
    result: AnalysisExecutionResult,
) -> AnalysisExecutionResult:
    """Collapse the large result tree before it crosses into the UI process."""

    if result.status is not AnalysisExecutionStatus.SUCCEEDED:
        return result
    root = _plain_frozen_value(result.result_payload)
    if type(root) is not dict:
        raise TypeError("analysis result root is not an object")
    judgment = root.get("judgment")
    if judgment not in {"PASS", "FAIL"}:
        raise ValueError("analysis result judgment is invalid")
    canonical = json.dumps(
        root,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.b64encode(
        zlib.compress(canonical, level=_TRANSPORT_ZLIB_LEVEL)
    ).decode("ascii")
    chunks = tuple(
        encoded[offset : offset + _TRANSPORT_CHUNK_SIZE]
        for offset in range(0, len(encoded), _TRANSPORT_CHUNK_SIZE)
    )
    envelope = FrozenResultObject(
        (
            ("__transport__", _CANONICAL_TRANSPORT),
            ("judgment", judgment),
            ("chunks", chunks),
        )
    )
    return AnalysisExecutionResult(
        AnalysisExecutionStatus.SUCCEEDED,
        envelope,
    )


def _cancellation_error(cancel_token: object) -> AnalysisExecutionError:
    reason = getattr(cancel_token, "reason", None)
    return AnalysisExecutionError(
        AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            stage=AnalysisStage.CANCEL,
            summary=(
                reason
                if type(reason) is str and reason
                else "analysis canceled"
            )[:1024],
        )
    )


def _is_cancelled(cancel_token: object) -> bool:
    return bool(getattr(cancel_token, "is_cancelled", False))


def _frozen_json_value(value: object) -> object:
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str):
        return value
    if value_type is list:
        return tuple(_frozen_json_value(item) for item in value)
    if value_type is dict:
        return FrozenResultObject(
            tuple(
                (key, _frozen_json_value(item))
                for key, item in value.items()
            )
        )
    raise TypeError(f"unsupported JSON result value {value_type.__name__}")


class _PinnedResidentRuntime:
    """Bind one child command to the exact prewarmed Resident instance."""

    def __init__(self, runtime: ResidentToolchainRuntime) -> None:
        self._runtime = runtime
        self._active_snapshot: AiToolchainSnapshot | None = None
        self._active_instance_id: int | None = None

    def prewarm(self, snapshot: AiToolchainSnapshot) -> int:
        return self._runtime.prewarm(snapshot)

    def pin(self, snapshot: AiToolchainSnapshot | None) -> None:
        self._active_snapshot = snapshot
        self._active_instance_id = None
        if snapshot is None:
            return
        if not self._runtime.readiness(snapshot):
            raise RuntimeError("Frozen AI revision is not ready")
        instance_id = self._runtime.process_instance_id(snapshot)
        if instance_id is None:
            raise RuntimeError("Frozen AI revision has no ready instance")
        self._active_instance_id = instance_id

    def analyze(self, **kwargs) -> dict:
        snapshot = self._active_snapshot
        if snapshot is None or self._active_instance_id is None:
            raise RuntimeError("AI request has no frozen Resident instance")
        if (
            kwargs.get("toolchain_id") != snapshot.toolchain_id
            or kwargs.get("entrypoint") != snapshot.entrypoint
            or (kwargs.get("package_dir") or None)
            != (snapshot.package_dir or None)
        ):
            raise RuntimeError("AI request differs from its frozen revision")
        result = self._runtime.analyze(
            **kwargs,
            execution_revision=snapshot.execution_revision,
            required_instance_id=self._active_instance_id,
        )
        if result.get("module_status") != "success":
            raise RuntimeError(
                str(result.get("error_summary") or "Resident AI execution failed")
            )
        return result

    def shutdown_all(self) -> None:
        self._runtime.shutdown_all()


def _analysis_process_main(
    connection,
    golden_artifacts: Mapping[tuple[int, int], FrozenGoldenArtifact],
    selected_toolchains: tuple[AiToolchainSnapshot, ...] = (),
    slot_id: int = 0,
    require_loudness_backend: bool = False,
    control_connection=None,
) -> None:
    """Own the heavy DSP runtime for the lifetime of the production graph."""

    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"
    set_current_process_priority(IDLE_PRIORITY_CLASS)
    set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)

    executor = None
    control_send_lock = threading.Lock()

    def publish_readiness(snapshot: AiToolchainSnapshot, ready: bool) -> None:
        if control_connection is None:
            return
        with control_send_lock:
            try:
                control_connection.send(
                    ("readiness", _toolchain_key(snapshot), ready)
                )
            except (BrokenPipeError, EOFError, OSError):
                return

    runtime = ResidentToolchainRuntime(readiness_changed=publish_readiness)
    pinned_runtime = _PinnedResidentRuntime(runtime)
    pending_enrichment_transport: dict[
        str,
        tuple[AnalysisBundleToken, AnalysisEnrichmentReport],
    ] = {}
    try:
        executor = ProductionAnalysisExecutor(
            frozen_golden_artifacts=golden_artifacts,
            ai_runtime=pinned_runtime,
        )
        try:
            dsp.preload_loudness_backend()
        except Exception:
            if require_loudness_backend:
                raise
            # The actual analysis reports a backend error only when that
            # calculation is required; warm-up must not prevent other DSP.
            pass
        for snapshot in selected_toolchains:
            pinned_runtime.prewarm(snapshot)
        connection.send(
            (
                "ready",
                tuple(_toolchain_key(snapshot) for snapshot in selected_toolchains),
            )
        )
        while True:
            message = connection.recv()
            if not isinstance(message, tuple) or not message:
                connection.send(("protocol_error", "invalid analysis command"))
                continue
            if message[0] == "stop":
                if len(message) == 1:
                    return
                connection.send(("protocol_error", "invalid stop command"))
                continue
            kind = message[0]
            try:
                if kind == "warm" and len(message) == 2:
                    snapshot = message[1]
                    pinned_runtime.prewarm(snapshot)
                    connection.send(("warm", _toolchain_key(snapshot)))
                    continue
                if kind == "decision" and len(message) == 6:
                    (
                        _,
                        job,
                        samples,
                        sample_rate_hz,
                        channel_count,
                        frame_count,
                    ) = message
                    audio = _ProcessAudio(
                        samples=np.asarray(samples),
                        sample_rate_hz=int(sample_rate_hz),
                        channel_count=int(channel_count),
                        frame_count=int(frame_count),
                    )
                    pinned_runtime.pin(job.snapshot.ai_toolchain)
                    decision = executor.execute_decision(
                        job,
                        audio,
                        _ProcessCancellationToken(),
                    )
                    token = replace(decision.bundle_token, slot_id=slot_id)
                    if token != decision.bundle_token:
                        bundle = executor._bundles[token.bundle_id]
                        executor._bundles[token.bundle_id] = replace(
                            bundle,
                            token=token,
                        )
                        decision = replace(decision, bundle_token=token)
                    connection.send(("decision", decision))
                    continue
                if kind == "enrich" and len(message) == 2:
                    token = message[1]
                    report = executor.execute_enrichment(token)
                    attempt_id = token.identity.analysis_attempt_id
                    pending_enrichment_transport[attempt_id] = (token, report)
                    connection.send(("enrich_ready", report.identity))
                    continue
                if kind == "enrich_transport" and len(message) == 2:
                    token = message[1]
                    attempt_id = token.identity.analysis_attempt_id
                    pending = pending_enrichment_transport.get(attempt_id)
                    if pending is None or pending[0] != token:
                        raise _process_error(
                            "enrichment transport has no exact computed owner"
                        )
                    report = pending[1]
                    connection.send(
                        (
                            "enrich",
                            _CompressedEnrichmentTransport(
                                identity=report.identity,
                                judgment=report.judgment,
                                result_payload_zlib=report.result_payload_zlib,
                                chart_manifest=report.chart_manifest,
                                timings=report.timings,
                            ),
                        )
                    )
                    pending_enrichment_transport.pop(attempt_id, None)
                    continue
                if kind == "discard" and len(message) == 2:
                    token = message[1]
                    pending = pending_enrichment_transport.pop(
                        token.identity.analysis_attempt_id,
                        None,
                    )
                    if pending is None:
                        executor._require_bundle(token)
                        executor._bundles.pop(token.bundle_id)
                    elif pending[0] != token:
                        pending_enrichment_transport[
                            token.identity.analysis_attempt_id
                        ] = pending
                        raise _process_error(
                            "discard token differs from computed enrichment owner"
                        )
                    connection.send(("discard", token))
                    continue
                connection.send(("protocol_error", "unknown analysis command"))
                continue
            except AnalysisExecutionError as exc:
                connection.send(("analysis_error", exc.error))
            except BaseException as exc:
                detail = str(exc).strip()
                connection.send(
                    (
                        "unexpected_error",
                        (
                            f"analysis process failed: {type(exc).__name__}"
                            + (f": {detail}" if detail else "")
                        )[:1024],
                    )
                )
    except EOFError:
        return
    except BaseException as exc:
        try:
            detail = str(exc).strip()
            connection.send(
                (
                    "startup_error",
                    (
                        f"analysis process startup failed: {type(exc).__name__}"
                        + (f": {detail}" if detail else "")
                    )[:1024],
                )
            )
        except Exception:
            pass
    finally:
        try:
            if executor is not None:
                executor.close()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        if control_connection is not None:
            try:
                control_connection.close()
            except Exception:
                pass


class ProcessIsolatedAnalysisExecutor:
    """Resident process pool used by the parallel analysis scheduler."""

    validates_result_out_of_process = True

    def __init__(
        self,
        *,
        frozen_golden_artifacts: Mapping[
            tuple[int, int], FrozenGoldenArtifact
        ] | None = None,
        capture_active: Callable[[], bool] | None = None,
        process_count: int = _DEFAULT_PROCESS_COUNT,
        selected_toolchains: tuple[AiToolchainSnapshot, ...] = (),
        enrichment_sink: Callable[[AnalysisEnrichmentReport], None] | None = None,
        enrichment_failure_sink: (
            Callable[[AnalysisBundleIdentity, str], None] | None
        ) = None,
        enrichment_diagnostic: Callable[[str, str], None] | None = None,
        require_loudness_backend: bool = False,
        monotonic: Callable[[], float] = time.perf_counter,
        quiet_wait: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(process_count) is not int:
            raise TypeError("process_count must be an exact int")
        if not 1 <= process_count <= 6:
            raise ValueError("process_count must be between 1 and 6")
        self._golden = dict(frozen_golden_artifacts or {})
        self._capture_active = capture_active
        self._process_count = process_count
        if type(selected_toolchains) is not tuple or any(
            type(snapshot) is not AiToolchainSnapshot
            for snapshot in selected_toolchains
        ):
            raise TypeError(
                "selected_toolchains must be a tuple of AiToolchainSnapshot values"
            )
        if enrichment_sink is not None and not callable(enrichment_sink):
            raise TypeError("enrichment_sink must be callable or None")
        if enrichment_failure_sink is not None and not callable(
            enrichment_failure_sink
        ):
            raise TypeError("enrichment_failure_sink must be callable or None")
        if enrichment_diagnostic is not None and not callable(
            enrichment_diagnostic
        ):
            raise TypeError("enrichment_diagnostic must be callable or None")
        if type(require_loudness_backend) is not bool:
            raise TypeError("require_loudness_backend must be an exact bool")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if not callable(quiet_wait):
            raise TypeError("quiet_wait must be callable")
        self._selected_toolchains = selected_toolchains
        self._enrichment_sink = enrichment_sink
        self._enrichment_failure_sink = enrichment_failure_sink
        self._enrichment_diagnostic = enrichment_diagnostic
        self._require_loudness_backend = require_loudness_backend
        self._monotonic = monotonic
        self._quiet_wait = quiet_wait
        self._state_lock = threading.Lock()
        self._slot_condition = threading.Condition(self._state_lock)
        self._slots: tuple[_AnalysisProcessSlot, ...] = ()
        self._owned: dict[str, _OwnedBundle] = {}
        self._attempts: dict[str, _EnrichmentAttemptState] = {}
        self._admission_hook_active = False
        self._capture_observed_active: bool | None = None
        self._quiet_since: float | None = None
        self._quiet_epoch_id = 0
        self._pending_decisions = 0
        self._enrichment_queue: queue.Queue[AnalysisBundleToken] = queue.Queue(
            maxsize=6
        )
        self._enrichment_retries: dict[str, int] = {}
        self._delivery_pending: dict[str, _PendingEnrichmentDelivery] = {}
        self._delivery_failures: deque[str] = deque(maxlen=6)
        self._failure_notifications: deque[AnalysisBundleIdentity] = deque()
        self._failure_notification_set: set[AnalysisBundleIdentity] = set()
        self._dispatcher_stop = threading.Event()
        self._dispatchers: tuple[threading.Thread, ...] = ()
        self._process_context = None
        self._replacing_slot_ids: set[int] = set()
        self._pool_failure: str | None = None
        self._starting = False
        self._closed = False

    def start(self) -> None:
        """Start warming the child without waiting on the UI or capture path."""

        with self._slot_condition:
            while self._starting and not self._closed:
                self._slot_condition.wait(0.05)
            if self._closed or self._slots:
                return
            self._starting = True
        created: list[_AnalysisProcessSlot] = []
        try:
            context = multiprocessing.get_context("spawn")
            self._process_context = context
            for index in range(self._process_count):
                created.append(self._spawn_slot(context, index))
            for slot in created:
                self._wait_until_ready(slot)
                self._drain_pending_readiness(slot)
        except BaseException:
            self._stop_slots(tuple(created))
            with self._slot_condition:
                self._starting = False
                self._slot_condition.notify_all()
            raise
        with self._slot_condition:
            if self._closed:
                self._starting = False
                self._slot_condition.notify_all()
                slots_to_stop = tuple(created)
            else:
                self._slots = tuple(created)
                self._starting = False
                slots_to_stop = ()
            if not slots_to_stop and not self._dispatchers:
                self._dispatchers = tuple(
                    threading.Thread(
                        target=self._dispatch_enrichment,
                        name=f"analysis-enrichment-dispatcher-{index + 1}",
                        daemon=True,
                    )
                    for index in range(self._process_count)
                )
                for dispatcher in self._dispatchers:
                    dispatcher.start()
            self._slot_condition.notify_all()
        if slots_to_stop:
            self._stop_slots(slots_to_stop)
        else:
            for slot in created:
                self._start_control_monitor(slot)

    def _spawn_slot(self, context, slot_id: int) -> _AnalysisProcessSlot:
        parent_connection, child_connection = context.Pipe(duplex=True)
        control_parent, control_child = context.Pipe(duplex=False)
        process = context.Process(
            target=_analysis_process_main,
            args=(
                child_connection,
                self._golden,
                self._selected_toolchains,
                slot_id,
                self._require_loudness_backend,
                control_child,
            ),
            name=f"production-analysis-process-{slot_id + 1}",
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent_connection.close()
            control_parent.close()
            raise
        finally:
            child_connection.close()
            control_child.close()
        slot = _AnalysisProcessSlot(
            slot_id,
            parent_connection,
            control_parent,
            process,
        )
        return slot

    def _start_control_monitor(self, slot: _AnalysisProcessSlot) -> None:
        with self._slot_condition:
            if (
                self._closed
                or slot.slot_id >= len(self._slots)
                or self._slots[slot.slot_id] is not slot
            ):
                return
            monitor = threading.Thread(
                target=self._monitor_readiness,
                args=(slot,),
                name=f"analysis-readiness-{slot.slot_id + 1}",
                daemon=True,
            )
            slot.control_thread = monitor
            monitor.start()

    def _monitor_readiness(self, slot: _AnalysisProcessSlot) -> None:
        connection = slot.control_connection
        while not self._dispatcher_stop.is_set():
            try:
                if not connection.poll(0.05):
                    if not slot.process.is_alive():
                        self._quarantine_slot(slot)
                        return
                    continue
                message = connection.recv()
            except (EOFError, OSError):
                self._quarantine_slot(slot)
                return
            self._apply_readiness_message(slot, message)

    def _drain_pending_readiness(self, slot: _AnalysisProcessSlot) -> None:
        connection = slot.control_connection
        while connection.poll(0.0):
            try:
                message = connection.recv()
            except (EOFError, OSError) as exc:
                raise _process_error(
                    "analysis readiness control ended during startup"
                ) from exc
            self._apply_readiness_message(slot, message)

    def _apply_readiness_message(
        self,
        slot: _AnalysisProcessSlot,
        message,
    ) -> None:
        if (
            type(message) is not tuple
            or len(message) != 3
            or message[0] != "readiness"
            or type(message[1]) is not tuple
            or len(message[1]) != 4
            or type(message[2]) is not bool
        ):
            return
        key = message[1]
        with self._slot_condition:
            if message[2]:
                slot.ready_toolchains.add(key)
            else:
                slot.ready_toolchains.discard(key)
            self._slot_condition.notify_all()

    def prewarm(self, snapshot: AiToolchainSnapshot) -> None:
        """Publish an exact revision only after every resident slot is warm."""

        if type(snapshot) is not AiToolchainSnapshot:
            raise TypeError("snapshot must be an exact AiToolchainSnapshot")
        self.start()
        with self._state_lock:
            slots = self._slots
        warmed: list[_AnalysisProcessSlot] = []
        try:
            for slot in slots:
                self._acquire_specific_slot(slot)
                try:
                    try:
                        slot.connection.send(("warm", snapshot))
                    except (BrokenPipeError, EOFError, OSError):
                        self._quarantine_slot(slot)
                        raise
                    response = self._receive(slot, "toolchain prewarm")
                    if response != ("warm", _toolchain_key(snapshot)):
                        self._raise_response_error(response)
                    warmed.append(slot)
                finally:
                    self._release_slot(slot, decision=True)
        except Exception:
            for slot in warmed:
                slot.ready_toolchains.discard(_toolchain_key(snapshot))
            raise
        for slot in warmed:
            slot.ready_toolchains.add(_toolchain_key(snapshot))

    def _acquire_specific_slot(self, slot: _AnalysisProcessSlot) -> None:
        with self._slot_condition:
            self._pending_decisions += 1
            try:
                while True:
                    if self._closed:
                        raise _process_error("analysis process is closed")
                    if slot not in self._slots:
                        raise _process_error("analysis process slot is unavailable")
                    if slot.quarantined or not slot.process.is_alive():
                        raise _process_error("analysis process slot is unavailable")
                    if not slot.busy:
                        slot.busy = True
                        return
                    self._slot_condition.wait(0.05)
            except BaseException:
                self._pending_decisions -= 1
                self._slot_condition.notify_all()
                raise

    def revision_ready(self, snapshot: AiToolchainSnapshot) -> bool:
        if type(snapshot) is not AiToolchainSnapshot:
            raise TypeError("snapshot must be an exact AiToolchainSnapshot")
        with self._state_lock:
            return bool(self._slots) and all(
                slot.ready
                and not slot.quarantined
                and slot.process.is_alive()
                and _toolchain_key(snapshot) in slot.ready_toolchains
                for slot in self._slots
            )

    def binding_ready(self, binding: Mapping[str, object]) -> bool:
        key = _binding_key(binding)
        with self._state_lock:
            return bool(self._slots) and all(
                slot.ready
                and not slot.quarantined
                and slot.process.is_alive()
                and key in slot.ready_toolchains
                for slot in self._slots
            )

    def execute(
        self,
        job: AnalysisJob,
        audio,
        cancel_token,
    ) -> AnalysisExecutionResult:
        decision = self.execute_decision(job, audio, cancel_token)
        if _is_cancelled(cancel_token):
            self.discard_attempt(
                decision.identity.analysis_attempt_id,
                reason_code="ENRICHMENT_CANCELED",
            )
            raise _cancellation_error(cancel_token)
        root = json.loads(decision.canonical_result_json)
        return AnalysisExecutionResult(
            AnalysisExecutionStatus.SUCCEEDED,
            _frozen_json_value(root),
        )

    def execute_decision(
        self,
        job: AnalysisJob,
        audio,
        cancel_token,
    ) -> AnalysisDecisionEnvelope:
        self.start()
        if _is_cancelled(cancel_token):
            raise _cancellation_error(cancel_token)
        attempt_id = job.analysis_attempt_id
        with self._state_lock:
            state = self._attempts.get(attempt_id)
            if state is None:
                if self._admission_hook_active:
                    raise _process_error(
                        "analysis attempt was not registered at admission"
                    )
                if len(self._attempts) >= 6:
                    raise _process_error("analysis bundle capacity exceeded")
                state = _EnrichmentAttemptState(
                    quiet_epoch_id=(
                        self._quiet_epoch_id
                        if self._capture_observed_active is False
                        else 0
                    ),
                )
                self._attempts[attempt_id] = state
            elif state.owner_token is not None:
                raise _process_error("analysis attempt already owns a bundle")
        try:
            slot = self._acquire_decision_slot()
        except BaseException:
            with self._state_lock:
                self._attempts.pop(attempt_id, None)
            raise
        decision_succeeded = False
        try:
            if self._closed:
                raise _process_error("analysis process is closed")
            if _is_cancelled(cancel_token):
                raise _cancellation_error(cancel_token)
            connection = slot.connection
            try:
                connection.send(
                    (
                        "decision",
                        job,
                        np.asarray(audio.samples),
                        int(audio.sample_rate_hz),
                        int(audio.channel_count),
                        int(audio.frame_count),
                    )
                )
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._quarantine_slot(slot)
                raise _process_error("analysis process is unavailable") from exc
            response = self._receive(slot, "decision")
            if (
                not isinstance(response, tuple)
                or len(response) != 2
                or response[0] != "decision"
                or type(response[1]) is not AnalysisDecisionEnvelope
            ):
                self._raise_response_error(response)
            decision = response[1]
            if decision.bundle_token.slot_id != slot.slot_id:
                raise _process_error("analysis bundle owner slot is invalid")
            if _is_cancelled(cancel_token):
                self._discard_on_slot(slot, decision.bundle_token)
                raise _cancellation_error(cancel_token)
            attempt_id = decision.identity.analysis_attempt_id
            with self._state_lock:
                if attempt_id in self._owned:
                    registration_error = "analysis attempt already owns a bundle"
                elif len(self._owned) >= 6:
                    registration_error = "analysis bundle capacity exceeded"
                else:
                    self._owned[attempt_id] = _OwnedBundle(
                        slot,
                        decision.bundle_token,
                        cancel_token,
                    )
                    state = self._attempts[attempt_id]
                    state.owner_slot_id = slot.slot_id
                    state.owner_token = decision.bundle_token
                    state.checkpoint = "decision_ready"
                    registration_error = None
            if registration_error is not None:
                self._discard_on_slot(slot, decision.bundle_token)
                raise _process_error(registration_error)
            decision_succeeded = True
            return decision
        finally:
            if not decision_succeeded:
                with self._state_lock:
                    if attempt_id not in self._owned:
                        self._attempts.pop(attempt_id, None)
            self._release_slot(slot, decision=True)

    def register_staged_attempt(self, job: AnalysisJob) -> bool:
        attempt_id = job.analysis_attempt_id
        with self._state_lock:
            self._admission_hook_active = True
            if attempt_id in self._attempts:
                return False
            if len(self._attempts) >= 6:
                return False
            self._attempts[attempt_id] = _EnrichmentAttemptState(
                quiet_epoch_id=(
                    self._quiet_epoch_id
                    if self._capture_observed_active is False
                    else 0
                ),
            )
            return True

    def mark_decision_committed(self, analysis_attempt_id: str) -> bool:
        with self._state_lock:
            state = self._attempts.get(analysis_attempt_id)
            owner = self._owned.get(analysis_attempt_id)
            if (
                state is None
                or owner is None
                or state.owner_token != owner.token
                or state.decision_committed
            ):
                return False
            state.decision_committed = True
            state.checkpoint = "decision_committed"
            token = owner.token
        self.enqueue_enrichment(token)
        return True

    def retire_staged_attempt(
        self,
        analysis_attempt_id: str,
        reason_code: str,
    ) -> bool:
        with self._state_lock:
            state = self._attempts.get(analysis_attempt_id)
            owner = self._owned.get(analysis_attempt_id)
            if state is None:
                return False
            if owner is None:
                self._attempts.pop(analysis_attempt_id, None)
                self._enrichment_retries.pop(analysis_attempt_id, None)
                return True
        try:
            return self.discard_attempt(
                analysis_attempt_id,
                reason_code=reason_code,
            )
        except Exception:
            self._release_owned_attempt(
                analysis_attempt_id,
                reason_code=reason_code,
                fallback_identity=owner.token.identity,
            )
            return True

    def execute_enrichment(
        self,
        token: AnalysisBundleToken,
    ) -> AnalysisEnrichmentReport:
        if type(token) is not AnalysisBundleToken:
            raise TypeError("token must be an exact AnalysisBundleToken")
        quiet_gate_released_ns = None
        while True:
            with self._state_lock:
                waiting_owner = self._owned.get(
                    token.identity.analysis_attempt_id
                )
                if waiting_owner is None or waiting_owner.token != token:
                    raise _process_error(
                        "analysis bundle token has no exact owner"
                    )
                cancel_token = waiting_owner.cancel_token
                state = self._attempts.get(token.identity.analysis_attempt_id)
                if state is None or state.owner_token != token:
                    raise _process_error(
                        "analysis attempt has no exact staged state"
                    )
                if not state.decision_committed:
                    raise _process_error(
                        "analysis Decision is not durably committed"
                    )
                checkpoint = state.checkpoint

            epoch_id = self.require_quiet_epoch(
                state.quiet_epoch_id or None,
                "enrichment_admission",
                cancel_token,
            )
            if quiet_gate_released_ns is None:
                quiet_gate_released_ns = time.perf_counter_ns()
            with self._state_lock:
                current_state = self._attempts.get(
                    token.identity.analysis_attempt_id
                )
                if current_state is not None:
                    current_state.quiet_epoch_id = epoch_id

            if checkpoint in {"decision_committed", "audio_frozen"}:
                slot = self._acquire_owner_slot(token)
                with self._state_lock:
                    owner = self._owned.get(token.identity.analysis_attempt_id)
                    canceled = owner is not None and _is_cancelled(
                        owner.cancel_token
                    )
                if canceled:
                    try:
                        self._discard_on_slot(slot, token)
                    finally:
                        self._release_owned_attempt(
                            token.identity.analysis_attempt_id,
                            reason_code="ENRICHMENT_CANCELED",
                            fallback_identity=token.identity,
                        )
                        self._release_slot(slot)
                    raise _cancellation_error(owner.cancel_token)
                active, _, _ = self._observe_capture_state()
                if active:
                    self._release_slot(slot)
                    continue
                try:
                    try:
                        slot.connection.send(("enrich", token))
                    except (BrokenPipeError, EOFError, OSError) as exc:
                        self._quarantine_slot(slot)
                        raise _process_error(
                            "analysis process is unavailable"
                        ) from exc
                    response = self._receive(slot, "enrichment compute")
                    if response != ("enrich_ready", token.identity):
                        self._raise_response_error(response)
                    with self._state_lock:
                        current_state = self._attempts.get(
                            token.identity.analysis_attempt_id
                        )
                        if current_state is None:
                            raise _process_error(
                                "analysis attempt staged state was retired"
                            )
                        current_state.checkpoint = "computed"
                finally:
                    self._release_slot(slot)

            with self._state_lock:
                state = self._attempts[token.identity.analysis_attempt_id]
                checkpoint = state.checkpoint
                epoch_id = state.quiet_epoch_id
            if checkpoint == "computed":
                epoch_id = self.require_quiet_epoch(
                    epoch_id,
                    "child_compressed_transport",
                    cancel_token,
                )
                slot = self._acquire_owner_slot(token)
                active, _, _ = self._observe_capture_state()
                if active:
                    self._release_slot(slot)
                    continue
                try:
                    try:
                        slot.connection.send(("enrich_transport", token))
                    except (BrokenPipeError, EOFError, OSError) as exc:
                        self._quarantine_slot(slot)
                        raise _process_error(
                            "analysis process is unavailable"
                        ) from exc
                    response = self._receive(slot, "enrichment transport")
                    with self._state_lock:
                        current_state = self._attempts[
                            token.identity.analysis_attempt_id
                        ]
                        current_state.quiet_epoch_id = epoch_id
                        current_state.parked_transport = response
                        current_state.checkpoint = "transported"
                finally:
                    self._release_slot(slot)

            with self._state_lock:
                state = self._attempts[token.identity.analysis_attempt_id]
                checkpoint = state.checkpoint
                epoch_id = state.quiet_epoch_id
                parked_transport = state.parked_transport
            if checkpoint == "transported":
                epoch_id = self.require_quiet_epoch(
                    epoch_id,
                    "parent_decode_validation",
                    cancel_token,
                )
                report = self._decode_enrichment_response(parked_transport)
                if report.identity != token.identity:
                    raise _process_error(
                        "enrichment identity differs from owner token"
                    )
                with self._state_lock:
                    current_state = self._attempts[
                        token.identity.analysis_attempt_id
                    ]
                    current_state.quiet_epoch_id = epoch_id
                    current_state.parked_report = report
                    current_state.checkpoint = "decoded"

            with self._state_lock:
                state = self._attempts[token.identity.analysis_attempt_id]
                epoch_id = state.quiet_epoch_id
                report = state.parked_report
            epoch_id = self.require_quiet_epoch(
                epoch_id,
                "artifact_publication",
                cancel_token,
            )
            if report is None:
                raise _process_error("enrichment report was not decoded")
            with self._state_lock:
                current = self._owned.get(token.identity.analysis_attempt_id)
                canceled = current is not None and _is_cancelled(
                    current.cancel_token
                )
                if current is not None and current.token == token:
                    self._owned.pop(token.identity.analysis_attempt_id, None)
                state = self._attempts.get(token.identity.analysis_attempt_id)
                if state is not None:
                    state.quiet_epoch_id = epoch_id
                    state.checkpoint = "artifact_ready"
            if canceled:
                self._release_owned_attempt(
                    token.identity.analysis_attempt_id,
                    reason_code="ENRICHMENT_CANCELED",
                    fallback_identity=token.identity,
                )
                raise _cancellation_error(current.cancel_token)
            quiet_event = AnalysisStageTimingEvent(
                run_id=report.identity.run_id,
                analysis_attempt_id=report.identity.analysis_attempt_id,
                boundary=AnalysisTimingBoundary.QUIET_GATE_RELEASED,
                started_ns=quiet_gate_released_ns,
                finished_ns=quiet_gate_released_ns,
                duration_ns=0,
                byte_count=0,
                status=AnalysisTimingStatus.SUCCEEDED,
            )
            return replace(
                report,
                timings=AnalysisStageTimings(
                    (*report.timings.events, quiet_event)
                ),
            )

    def enqueue_enrichment(self, token: AnalysisBundleToken) -> None:
        if type(token) is not AnalysisBundleToken:
            raise TypeError("token must be an exact AnalysisBundleToken")
        with self._state_lock:
            owner = self._owned.get(token.identity.analysis_attempt_id)
            if owner is None or owner.token != token:
                raise _process_error("analysis bundle token has no exact owner")
            canceled = _is_cancelled(owner.cancel_token)
            state = self._attempts.get(token.identity.analysis_attempt_id)
            if state is None or state.owner_token != token:
                raise _process_error("analysis attempt has no exact staged state")
            if not state.decision_committed:
                raise _process_error(
                    "analysis Decision is not durably committed"
                )
            if state.enrichment_queued or state.enrichment_inflight:
                return
            state.enrichment_queued = True
        if canceled:
            self.discard_attempt(
                token.identity.analysis_attempt_id,
                reason_code="ENRICHMENT_CANCELED",
            )
            raise _cancellation_error(owner.cancel_token)
        try:
            self._enrichment_queue.put_nowait(token)
        except queue.Full as exc:
            with self._state_lock:
                state = self._attempts.get(token.identity.analysis_attempt_id)
                if state is not None and state.owner_token == token:
                    state.enrichment_queued = False
            self.discard_attempt(
                token.identity.analysis_attempt_id,
                reason_code="ENRICHMENT_QUEUE_FULL",
            )
            raise _process_error("enrichment queue capacity exceeded") from exc

    def owned_attempt_ids(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(sorted(self._owned))

    def attempt_state_snapshot(
        self,
    ) -> dict[str, _EnrichmentAttemptState]:
        with self._state_lock:
            return {
                attempt_id: replace(state)
                for attempt_id, state in self._attempts.items()
            }

    def quiet_epoch_id(self) -> int:
        self._observe_capture_state()
        with self._state_lock:
            return self._quiet_epoch_id

    def discard_attempt(
        self,
        analysis_attempt_id: str,
        *,
        reason_code: str = "ENRICHMENT_DISCARDED",
    ) -> bool:
        with self._state_lock:
            owner = self._owned.get(analysis_attempt_id)
        if owner is None:
            return False
        slot = self._acquire_owner_slot(owner.token)
        try:
            self._discard_on_slot(slot, owner.token)
            with self._state_lock:
                current = self._owned.get(analysis_attempt_id)
                if current is not None and current.token == owner.token:
                    self._owned.pop(analysis_attempt_id, None)
                    self._enrichment_retries.pop(analysis_attempt_id, None)
                    self._attempts.pop(analysis_attempt_id, None)
            self._notify_enrichment_failure(owner.token.identity, reason_code)
            return True
        finally:
            self._release_slot(slot)

    def _discard_on_slot(
        self,
        slot: _AnalysisProcessSlot,
        token: AnalysisBundleToken,
    ) -> None:
        try:
            slot.connection.send(("discard", token))
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._quarantine_slot(slot)
            raise _process_error("analysis process is unavailable") from exc
        response = self._receive(slot, "discard")
        if response != ("discard", token):
            self._raise_response_error(response)

    def _acquire_decision_slot(self) -> _AnalysisProcessSlot:
        with self._slot_condition:
            self._pending_decisions += 1
            try:
                while True:
                    if self._closed:
                        raise _process_error("analysis process is closed")
                    if self._pool_failure is not None:
                        raise _process_error(self._pool_failure)
                    available = [
                        slot
                        for slot in self._slots
                        if not slot.busy
                        and not slot.quarantined
                        and slot.process.is_alive()
                    ]
                    if available:
                        owned_counts = {
                            slot.slot_id: sum(
                                owner.slot is slot
                                for owner in self._owned.values()
                            )
                            for slot in available
                        }
                        slot = min(
                            available,
                            key=lambda candidate: (
                                owned_counts[candidate.slot_id],
                                candidate.slot_id,
                            ),
                        )
                        slot.busy = True
                        return slot
                    self._slot_condition.wait(0.05)
            except BaseException:
                self._pending_decisions -= 1
                self._slot_condition.notify_all()
                raise

    def _acquire_owner_slot(
        self,
        token: AnalysisBundleToken,
    ) -> _AnalysisProcessSlot:
        attempt_id = token.identity.analysis_attempt_id
        with self._slot_condition:
            while True:
                if self._closed:
                    raise _process_error("analysis process is closed")
                owner = self._owned.get(attempt_id)
                if owner is None or owner.token != token:
                    raise _process_error("analysis bundle token has no exact owner")
                if owner.slot.slot_id != token.slot_id:
                    raise _process_error("analysis bundle token has the wrong owner slot")
                if owner.slot.quarantined:
                    raise _process_error("analysis bundle owner slot is unavailable")
                canceling = _is_cancelled(owner.cancel_token)
                if not owner.slot.busy and (
                    self._pending_decisions == 0 or canceling
                ):
                    owner.slot.busy = True
                    return owner.slot
                self._slot_condition.wait(0.05)

    def _release_slot(
        self,
        slot: _AnalysisProcessSlot,
        *,
        decision: bool = False,
    ) -> None:
        with self._slot_condition:
            slot.busy = False
            if decision:
                self._pending_decisions -= 1
            self._slot_condition.notify_all()

    def _observe_capture_state(self) -> tuple[bool, int, float | None]:
        capture_active = self._capture_active
        if capture_active is None:
            with self._state_lock:
                if self._capture_observed_active is None:
                    self._capture_observed_active = False
                    self._quiet_epoch_id = 1
                    self._quiet_since = self._monotonic() - 0.25
                return False, self._quiet_epoch_id, self._quiet_since
        active = bool(capture_active())
        now = self._monotonic()
        with self._state_lock:
            previous = self._capture_observed_active
            if previous is None:
                self._capture_observed_active = active
                if not active:
                    self._quiet_epoch_id += 1
                    self._quiet_since = now
            elif previous != active:
                self._capture_observed_active = active
                if active:
                    self._quiet_since = None
                else:
                    self._quiet_epoch_id += 1
                    self._quiet_since = now
            if not active and (previous is None or previous is True):
                for state in self._attempts.values():
                    if state.audio_frozen and not state.charts_visible:
                        state.quiet_epoch_id = self._quiet_epoch_id
            return active, self._quiet_epoch_id, self._quiet_since

    def require_quiet_epoch(
        self,
        epoch_id: int | None = None,
        checkpoint: str = "enrichment",
        cancel_token: object = None,
    ) -> int:
        """Return only after one unchanged capture-free epoch passes 250 ms."""

        del checkpoint
        while True:
            if self._dispatcher_stop.is_set():
                raise _process_error("analysis process is closed")
            if _is_cancelled(cancel_token):
                return self.quiet_epoch_id()
            active, current_epoch, quiet_since = self._observe_capture_state()
            if (
                not active
                and quiet_since is not None
                and self._monotonic() - quiet_since >= 0.25
            ):
                return current_epoch
            self._quiet_wait(0.01)

    def _wait_until_ready(self, slot: _AnalysisProcessSlot) -> None:
        if slot.ready:
            return
        connection = slot.connection
        process = slot.process
        while True:
            if self._dispatcher_stop.is_set():
                raise _process_error("analysis process is closed")
            if connection.poll(0.05):
                try:
                    response = connection.recv()
                except (EOFError, OSError) as exc:
                    raise _process_error(
                        "analysis process failed during startup"
                    ) from exc
                if (
                    isinstance(response, tuple)
                    and len(response) == 2
                    and response[0] == "ready"
                ):
                    keys = response[1]
                    if type(keys) is not tuple or any(
                        type(key) is not tuple or len(key) != 4
                        for key in keys
                    ):
                        raise _process_error(
                            "analysis process returned invalid readiness"
                        )
                    slot.ready_toolchains.update(keys)
                    slot.ready = True
                    return
                self._raise_response_error(response)
            if not process.is_alive():
                raise _process_error("analysis process failed during startup")

    def _receive(self, slot: _AnalysisProcessSlot, stage: str):
        while True:
            if slot.connection.poll(0.05):
                try:
                    return slot.connection.recv()
                except (EOFError, OSError) as exc:
                    self._quarantine_slot(slot)
                    raise _process_error(
                        f"analysis process ended during {stage}"
                    ) from exc
            if not slot.process.is_alive():
                self._quarantine_slot(slot)
                raise _process_error(f"analysis process ended during {stage}")

    @staticmethod
    def _raise_response_error(response) -> None:
        if not isinstance(response, tuple) or len(response) != 2:
            raise _process_error("analysis process returned an invalid response")
        kind, payload = response
        if kind == "analysis_error" and isinstance(payload, AnalysisError):
            raise AnalysisExecutionError(payload)
        if kind in {"startup_error", "unexpected_error"}:
            raise _process_error(str(payload))
        raise _process_error("analysis process returned an invalid response")

    @staticmethod
    def _decode_enrichment_response(response) -> AnalysisEnrichmentReport:
        if not isinstance(response, tuple) or len(response) != 2:
            ProcessIsolatedAnalysisExecutor._raise_response_error(response)
        kind, payload = response
        if kind != "enrich":
            ProcessIsolatedAnalysisExecutor._raise_response_error(response)
        if type(payload) is AnalysisEnrichmentReport:
            return payload
        if type(payload) is not _CompressedEnrichmentTransport:
            raise _process_error("analysis process returned invalid enrichment")
        try:
            canonical = zlib.decompress(payload.result_payload_zlib).decode("utf-8")
            return AnalysisEnrichmentReport(
                identity=payload.identity,
                judgment=payload.judgment,
                canonical_result_json=canonical,
                result_payload_zlib=payload.result_payload_zlib,
                chart_manifest=payload.chart_manifest,
                timings=payload.timings,
            )
        except Exception as exc:
            raise _process_error("analysis enrichment transport is invalid") from exc

    def _retire_owned_slot_locked(
        self,
        slot: _AnalysisProcessSlot,
    ) -> tuple[tuple[str, AnalysisBundleIdentity], ...]:
        stale = tuple(
            (attempt_id, owner.token.identity)
            for attempt_id, owner in self._owned.items()
            if owner.slot is slot
        )
        for attempt_id, _identity in stale:
            self._owned.pop(attempt_id, None)
            self._attempts.pop(attempt_id, None)
            self._enrichment_retries.pop(attempt_id, None)
        return stale

    def _release_owned_slot(self, slot: _AnalysisProcessSlot) -> None:
        with self._state_lock:
            stale = self._retire_owned_slot_locked(slot)
        for _attempt_id, identity in stale:
            self._notify_enrichment_failure(
                identity,
                "ENRICHMENT_OWNER_LOST",
            )

    def _quarantine_slot(self, slot: _AnalysisProcessSlot) -> None:
        start_replacement = False
        with self._slot_condition:
            current = (
                slot.slot_id < len(self._slots)
                and self._slots[slot.slot_id] is slot
            )
            if not current or slot.quarantined:
                return
            slot.quarantined = True
            slot.ready = False
            slot.ready_toolchains.clear()
            stale = self._retire_owned_slot_locked(slot)
            if (
                not self._closed
                and slot.slot_id not in self._replacing_slot_ids
            ):
                self._replacing_slot_ids.add(slot.slot_id)
                start_replacement = True
            self._slot_condition.notify_all()
        for _attempt_id, identity in stale:
            self._notify_enrichment_failure(
                identity,
                "ENRICHMENT_SLOT_LOST",
            )
        if start_replacement:
            threading.Thread(
                target=self._replace_slot,
                args=(slot,),
                name=f"analysis-slot-restart-{slot.slot_id + 1}",
                daemon=True,
            ).start()

    def _replace_slot(self, old_slot: _AnalysisProcessSlot) -> None:
        self._stop_slots((old_slot,))
        replacement = None
        for replacement_attempt in range(_SLOT_REPLACEMENT_ATTEMPTS):
            with self._state_lock:
                if self._closed:
                    break
                context = self._process_context
            candidate = None
            try:
                candidate = self._spawn_slot(context, old_slot.slot_id)
                self._wait_until_ready(candidate)
                self._drain_pending_readiness(candidate)
            except Exception:
                if candidate is not None:
                    self._stop_slots((candidate,))
                if replacement_attempt + 1 < _SLOT_REPLACEMENT_ATTEMPTS:
                    if self._dispatcher_stop.wait(
                        _SLOT_REPLACEMENT_BACKOFF_SECONDS
                    ):
                        break
                    continue
                break
            replacement = candidate
            break

        if replacement is None:
            with self._slot_condition:
                if not self._closed and self._pool_failure is None:
                    self._pool_failure = (
                        f"analysis process slot {old_slot.slot_id} "
                        "failed to restart"
                    )
                self._replacing_slot_ids.discard(old_slot.slot_id)
                self._slot_condition.notify_all()
            return
        with self._slot_condition:
            current = (
                not self._closed
                and old_slot.slot_id < len(self._slots)
                and self._slots[old_slot.slot_id] is old_slot
            )
            if current:
                slots = list(self._slots)
                slots[old_slot.slot_id] = replacement
                self._slots = tuple(slots)
            self._replacing_slot_ids.discard(old_slot.slot_id)
            self._slot_condition.notify_all()
        if not current:
            self._stop_slots((replacement,))
        else:
            self._start_control_monitor(replacement)

    def _deliver_enrichment_report(
        self,
        report: AnalysisEnrichmentReport,
    ) -> None:
        sink = self._enrichment_sink
        attempt_id = report.identity.analysis_attempt_id
        if sink is None:
            with self._state_lock:
                self._attempts.pop(attempt_id, None)
                self._enrichment_retries.pop(attempt_id, None)
            return
        with self._state_lock:
            state = self._attempts.get(attempt_id)
            epoch_id = 0 if state is None else state.quiet_epoch_id
        pending = _PendingEnrichmentDelivery(report, epoch_id)
        with self._state_lock:
            self._delivery_pending[attempt_id] = pending
        for delivery_attempt in range(2):
            try:
                current_epoch = self.require_quiet_epoch(
                    pending.quiet_epoch_id or None,
                    "artifact_publication",
                )
                sink(pending.report)
            except Exception:
                if delivery_attempt == 0:
                    continue
                summary = "enrichment sink delivery failed twice"
                with self._state_lock:
                    current = self._delivery_pending.get(attempt_id)
                    if current is pending:
                        self._delivery_pending.pop(attempt_id, None)
                    self._delivery_failures.append(attempt_id)
                    self._attempts.pop(attempt_id, None)
                    self._enrichment_retries.pop(attempt_id, None)
                diagnostic = self._enrichment_diagnostic
                if diagnostic is not None:
                    try:
                        diagnostic(attempt_id, summary)
                    except Exception:
                        pass
                self._notify_enrichment_failure(
                    report.identity,
                    "ENRICHMENT_SINK_FAILED",
                )
                return
            else:
                with self._state_lock:
                    current = self._delivery_pending.get(attempt_id)
                    if current is pending:
                        self._delivery_pending.pop(attempt_id, None)
                    state = self._attempts.get(attempt_id)
                    if state is not None:
                        state.quiet_epoch_id = current_epoch
                        state.charts_visible = True
                    self._attempts.pop(attempt_id, None)
                    self._enrichment_retries.pop(attempt_id, None)
                return

    def pending_delivery_attempt_ids(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(sorted(self._delivery_pending))

    def enrichment_delivery_failures(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(self._delivery_failures)

    def _notify_enrichment_failure(
        self,
        identity: AnalysisBundleIdentity,
        reason_code: str,
    ) -> None:
        sink = self._enrichment_failure_sink
        if sink is None:
            return
        with self._state_lock:
            if identity in self._failure_notification_set:
                return
            if len(self._failure_notifications) >= 64:
                retired = self._failure_notifications.popleft()
                self._failure_notification_set.discard(retired)
            self._failure_notifications.append(identity)
            self._failure_notification_set.add(identity)
        try:
            sink(identity, reason_code)
        except Exception:
            pass

    def _dispatch_enrichment(self) -> None:
        while not self._dispatcher_stop.is_set():
            self._observe_capture_state()
            try:
                token = self._enrichment_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                with self._state_lock:
                    owner = self._owned.get(token.identity.analysis_attempt_id)
                    if owner is None or owner.token != token:
                        continue
                    state = self._attempts.get(
                        token.identity.analysis_attempt_id
                    )
                    if state is None or state.owner_token != token:
                        continue
                    state.enrichment_queued = False
                    state.enrichment_inflight = True
                report = self.execute_enrichment(token)
                self._deliver_enrichment_report(report)
            except Exception:
                attempt_id = token.identity.analysis_attempt_id
                with self._state_lock:
                    owner = self._owned.get(attempt_id)
                    retries = self._enrichment_retries.get(attempt_id, 0)
                    should_retry = owner is not None and retries < 1
                    state = self._attempts.get(attempt_id)
                    if state is not None:
                        state.enrichment_inflight = False
                    if should_retry:
                        self._enrichment_retries[attempt_id] = retries + 1
                        if state is not None:
                            state.retry_count = retries + 1
                            state.enrichment_queued = True
                if should_retry:
                    try:
                        self._enrichment_queue.put_nowait(token)
                    except queue.Full:
                        self.discard_attempt(
                            attempt_id,
                            reason_code="ENRICHMENT_QUEUE_FULL",
                        )
                else:
                    try:
                        self.discard_attempt(
                            attempt_id,
                            reason_code="ENRICHMENT_COMPUTE_FAILED",
                        )
                    except Exception:
                        self._release_owned_attempt(
                            attempt_id,
                            reason_code="ENRICHMENT_COMPUTE_FAILED",
                            fallback_identity=token.identity,
                        )
            finally:
                self._enrichment_queue.task_done()

    def _release_owned_attempt(
        self,
        attempt_id: str,
        *,
        reason_code: str | None = None,
        fallback_identity: AnalysisBundleIdentity | None = None,
    ) -> None:
        with self._state_lock:
            owner = self._owned.pop(attempt_id, None)
            self._enrichment_retries.pop(attempt_id, None)
            self._attempts.pop(attempt_id, None)
        identity = owner.token.identity if owner is not None else fallback_identity
        if reason_code is not None and identity is not None:
            self._notify_enrichment_failure(identity, reason_code)

    def toolchains_ready(self) -> bool:
        with self._state_lock:
            required = {
                _toolchain_key(snapshot)
                for snapshot in self._selected_toolchains
            }
            return bool(self._slots) and all(
                slot.ready
                and not slot.quarantined
                and slot.process.is_alive()
                and required.issubset(slot.ready_toolchains)
                for slot in self._slots
            )

    def pool_failure(self) -> str | None:
        with self._state_lock:
            return self._pool_failure

    def close(self) -> None:
        with self._slot_condition:
            if self._closed:
                return
            self._closed = True
            slots = self._slots
            self._slots = ()
            retired_identities = tuple(
                owner.token.identity for owner in self._owned.values()
            )
            self._owned.clear()
            self._attempts.clear()
            self._delivery_pending.clear()
            self._slot_condition.notify_all()
        for identity in retired_identities:
            self._notify_enrichment_failure(
                identity,
                "ENRICHMENT_SHUTDOWN",
            )
        self._dispatcher_stop.set()
        for dispatcher in self._dispatchers:
            if dispatcher is not threading.current_thread():
                dispatcher.join(1.0)
        self._stop_slots(slots)

    @staticmethod
    def _stop_slots(slots: tuple[_AnalysisProcessSlot, ...]) -> None:
        for slot in slots:
            connection = slot.connection
            if connection is not None:
                try:
                    connection.send(("stop",))
                except Exception:
                    pass
        graceful_deadline = time.monotonic() + 5.0
        for slot in slots:
            process = slot.process
            if process is not None:
                process.join(max(0.0, graceful_deadline - time.monotonic()))
        alive = [
            slot.process
            for slot in slots
            if slot.process is not None and slot.process.is_alive()
        ]
        for process in alive:
            process.terminate()
        terminate_deadline = time.monotonic() + 2.0
        for process in alive:
            process.join(max(0.0, terminate_deadline - time.monotonic()))
        for slot in slots:
            connection = slot.connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            control_connection = slot.control_connection
            if control_connection is not None:
                try:
                    control_connection.close()
                except Exception:
                    pass
            control_thread = slot.control_thread
            if (
                control_thread is not None
                and control_thread is not threading.current_thread()
            ):
                control_thread.join(0.2)


__all__ = ["ProcessIsolatedAnalysisExecutor"]
