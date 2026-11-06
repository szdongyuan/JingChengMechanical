"""Deterministic test doubles for the isolated analysis scheduler."""

from __future__ import annotations

from collections import deque
from pathlib import PureWindowsPath
from threading import Condition, Event, Lock, current_thread, get_ident
from typing import Callable

from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    AnalysisError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AnalysisOutcome,
    AudioStorageKind,
    AudioLoadError,
    PersistedAudioReference,
    canonical_toolchain_lane_key,
)


class FullAudioPayloadProbe:
    """An explicit stand-in for a semantically forbidden full-audio payload."""


class FullInputSamplesProbe:
    """One read-only complete-input view owned by a loaded-audio handle."""

    __slots__ = ()

    def __copy__(self) -> object:
        raise AssertionError("full-input samples were copied")

    def __deepcopy__(self, memo: object) -> object:
        raise AssertionError("full-input samples were deep-copied")

    def copy(self) -> object:
        raise AssertionError("full-input samples were cloned")

    def astype(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("full-input samples created a full view")


class MutableContextProbe:
    pass


class ManualClock:
    """A controllable exact-float monotonic port."""

    def __init__(self, value: object = 0.0) -> None:
        self._lock = Lock()
        self._value = value
        self._script: deque[object] = deque()
        self.call_count = 0

    def monotonic(self) -> object:
        with self._lock:
            self.call_count += 1
            if self._script:
                return self._script.popleft()
            return self._value

    def set(self, value: object) -> None:
        with self._lock:
            self._value = value

    def script(self, *values: object) -> None:
        with self._lock:
            self._script.extend(values)


class ThreadRecordingClock(ManualClock):
    """Record which threads consume scheduler monotonic timestamps."""

    def __init__(self, value: float = 0.0) -> None:
        super().__init__(value)
        self.calling_thread_names: list[str] = []

    def monotonic(self) -> object:
        with self._lock:
            self.call_count += 1
            self.calling_thread_names.append(current_thread().name)
            if self._script:
                return self._script.popleft()
            return self._value


class PassThroughStartGate:
    """Record exact jobs and allow durable-start admission immediately."""

    def __init__(self) -> None:
        self._condition = Condition()
        self.calls: list[AnalysisJob] = []

    def before_audio_load(self, job: AnalysisJob) -> None:
        with self._condition:
            self.calls.append(job)
            self._condition.notify_all()

    def wait_for_calls(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"start gate did not receive {count} calls"


class ScriptedStartGate(PassThroughStartGate):
    """Return or raise one scripted result per durable-start call."""

    def __init__(self, *results: object) -> None:
        super().__init__()
        self._results = deque(results)

    def before_audio_load(self, job: AnalysisJob) -> None:
        super().before_audio_load(job)
        with self._condition:
            result = self._results.popleft() if self._results else None
        if isinstance(result, BaseException):
            raise result


class GatedStartGate(PassThroughStartGate):
    """Expose gate entry and wait for deterministic release."""

    def __init__(self, result_or_exception: object = None) -> None:
        super().__init__()
        self.entered = Event()
        self._release = Event()
        self._result_or_exception = result_or_exception

    def before_audio_load(self, job: AnalysisJob) -> None:
        super().before_audio_load(job)
        self.entered.set()
        assert self._release.wait(2.0), "start gate was not released"
        if isinstance(self._result_or_exception, BaseException):
            raise self._result_or_exception

    def release(self) -> None:
        self._release.set()


class GatedShutdownCompletionClock(ManualClock):
    """Fail one shutdown owner's completion read after exposing a gate."""

    def __init__(
        self,
        *,
        owner_thread_name: str,
        started_value: float,
        completion_value_or_exception: object,
    ) -> None:
        super().__init__(started_value)
        self._owner_thread_name = owner_thread_name
        self._completion_value_or_exception = completion_value_or_exception
        self._owner_calls = 0
        self.completion_entered = Event()
        self._release = Event()

    def monotonic(self) -> object:
        gate_completion = False
        with self._lock:
            self.call_count += 1
            if current_thread().name == self._owner_thread_name:
                self._owner_calls += 1
                if self._owner_calls == 1:
                    return self._value
                if self._owner_calls == 2:
                    gate_completion = True
            if not gate_completion:
                return self._value

        self.completion_entered.set()
        assert self._release.wait(2.0), "shutdown completion clock was not released"
        completion = self._completion_value_or_exception
        if isinstance(completion, BaseException):
            raise completion
        return completion

    def release(self) -> None:
        self._release.set()


class WorkerDrainCompletionFaultClock(ManualClock):
    """Expose a later worker terminal time before one owner completion fault."""

    def __init__(
        self,
        *,
        owner_thread_name: str,
        completion_value_or_exception: object,
    ) -> None:
        super().__init__(10.0)
        self._owner_thread_name = owner_thread_name
        self._completion_value_or_exception = completion_value_or_exception
        self._owner_calls = 0
        self._use_worker_terminal_value = Event()
        self.worker_terminal_observed = Event()
        self.completion_call_count = 0

    def enable_worker_terminal_value(self) -> None:
        self._use_worker_terminal_value.set()

    def monotonic(self) -> object:
        completion = None
        with self._lock:
            self.call_count += 1
            thread_name = current_thread().name
            if thread_name == self._owner_thread_name:
                self._owner_calls += 1
                if self._owner_calls == 1:
                    return 20.0
                if self._owner_calls == 2:
                    self.completion_call_count += 1
                    completion = self._completion_value_or_exception
            elif (
                "analysis-scheduler-worker-" in thread_name
                and self._use_worker_terminal_value.is_set()
            ):
                self.worker_terminal_observed.set()
                return 30.0
            else:
                return self._value

        if isinstance(completion, BaseException):
            raise completion
        return completion


class ThreadFaultClock(ManualClock):
    """Return one transient bad value only on a named scheduler thread."""

    def __init__(
        self,
        thread_name_fragment: str,
        bad_value: object,
        *,
        value: float = 1.0,
    ) -> None:
        super().__init__(value)
        self._thread_name_fragment = thread_name_fragment
        self._bad_value = bad_value
        self._fault_remaining = True
        self.fault_observed = Event()
        self.matching_call_count = 0

    def monotonic(self) -> object:
        if self._thread_name_fragment in current_thread().name:
            with self._lock:
                self.call_count += 1
                self.matching_call_count += 1
                if self._fault_remaining:
                    self._fault_remaining = False
                    self.fault_observed.set()
                    return self._bad_value
                return self._value
        return super().monotonic()


class BlockingThreadFaultClock(ManualClock):
    """Hold one named-thread clock read, then return one bad value."""

    def __init__(
        self,
        thread_name_fragment: str,
        bad_value: object,
        *,
        value: float = 1.0,
    ) -> None:
        super().__init__(value)
        self._thread_name_fragment = thread_name_fragment
        self._bad_value = bad_value
        self._fault_remaining = True
        # A worker's first checked read is the required pre-open boundary.
        # Worker fault tests target the next boundary after the gated loader.
        self._skip_matching_calls = (
            1 if "analysis-scheduler-worker-" in thread_name_fragment else 0
        )
        self.entered = Event()
        self._release = Event()
        self.matching_call_count = 0

    def monotonic(self) -> object:
        if self._thread_name_fragment in current_thread().name:
            with self._lock:
                self.call_count += 1
                self.matching_call_count += 1
                should_fault = (
                    self._fault_remaining
                    and self.matching_call_count > self._skip_matching_calls
                )
                if should_fault:
                    self._fault_remaining = False
            if should_fault:
                self.entered.set()
                assert self._release.wait(2.0), "clock fault gate was not released"
                return self._bad_value
            with self._lock:
                return self._value
        return super().monotonic()

    def release(self) -> None:
        self._release.set()


class WorkerReadGateClock(ManualClock):
    """Pause one numbered worker clock read without holding the fake lock."""

    def __init__(
        self,
        *,
        gate_on_worker_read: int,
        value: float,
    ) -> None:
        super().__init__(value)
        self._gate_on_worker_read = gate_on_worker_read
        self.worker_reads = 0
        self.entered = Event()
        self._release = Event()

    def monotonic(self) -> object:
        if "analysis-scheduler-worker-" in current_thread().name:
            with self._lock:
                self.call_count += 1
                self.worker_reads += 1
                should_gate = (
                    self.worker_reads == self._gate_on_worker_read
                )
                if not should_gate:
                    return self._value
                gated_value = self._value
            self.entered.set()
            assert self._release.wait(2.0), "worker clock gate was not released"
            return gated_value
        return super().monotonic()

    def release(self) -> None:
        self._release.set()


class ControlDeadlineClock(ManualClock):
    """Advance exactly when the control loop rechecks an armed deadline."""

    def __init__(
        self,
        *,
        advance_on_control_read: int,
        deadline_value: float,
    ) -> None:
        super().__init__(0.0)
        self._advance_on_control_read = advance_on_control_read
        self._deadline_value = deadline_value
        self.control_reads = 0
        self.deadline_observed = Event()

    def monotonic(self) -> object:
        if "analysis-scheduler-control-" in current_thread().name:
            with self._lock:
                self.call_count += 1
                self.control_reads += 1
                if self.control_reads >= self._advance_on_control_read:
                    self._value = self._deadline_value
                    self.deadline_observed.set()
                return self._value
        return super().monotonic()


class FakeLoadedAudio:
    def __init__(self, reference: PersistedAudioReference) -> None:
        self._reference = reference
        self._samples = object()
        self.closed = Event()
        self.close_count = 0

    @property
    def samples(self) -> object:
        return self._samples

    @property
    def sample_rate_hz(self) -> int:
        return self._reference.sample_rate_hz

    @property
    def channel_count(self) -> int:
        return self._reference.channel_count

    @property
    def frame_count(self) -> int:
        return self._reference.frame_count

    def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class RecordingAudioLoader:
    def __init__(self) -> None:
        self._condition = Condition()
        self.calls: list[tuple[PersistedAudioReference, object]] = []
        self.call_thread_ids: list[int] = []
        self.handles: list[FakeLoadedAudio] = []

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: object,
    ) -> FakeLoadedAudio:
        handle = FakeLoadedAudio(reference)
        with self._condition:
            self.calls.append((reference, cancel_token))
            self.call_thread_ids.append(get_ident())
            self.handles.append(handle)
            self._condition.notify_all()
        return handle

    def wait_for_calls(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"loader did not receive {count} calls"


class AudioResidencyProbe:
    """Thread-safe full-input ownership counts shared by test handles."""

    def __init__(self) -> None:
        self._condition = Condition()
        self.current = 0
        self.peak = 0
        self.created = 0
        self.closed = 0

    def created_handle(self) -> None:
        with self._condition:
            self.current += 1
            self.created += 1
            self.peak = max(self.peak, self.current)
            self._condition.notify_all()

    def closed_handle(self) -> None:
        with self._condition:
            self.current -= 1
            self.closed += 1
            self._condition.notify_all()

    def wait_for_peak(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: self.peak >= count,
                timeout=timeout_seconds,
            ), f"audio residency did not reach {count}"


class CloneForbiddenLoadedAudio:
    """A single complete-input handle that forbids copies and context probing."""

    def __init__(
        self,
        reference: PersistedAudioReference,
        residency: AudioResidencyProbe,
    ) -> None:
        self._reference = reference
        self._residency = residency
        self._samples = FullInputSamplesProbe()
        self.closed = Event()
        self.close_count = 0
        residency.created_handle()

    @property
    def samples(self) -> object:
        return self._samples

    @property
    def sample_rate_hz(self) -> int:
        return self._reference.sample_rate_hz

    @property
    def channel_count(self) -> int:
        return self._reference.channel_count

    @property
    def frame_count(self) -> int:
        return self._reference.frame_count

    def close(self) -> None:
        self.close_count += 1
        if self.close_count != 1:
            raise AssertionError("loaded audio was closed more than once")
        self._samples = None
        self._residency.closed_handle()
        self.closed.set()

    def __copy__(self) -> object:
        raise AssertionError("loaded audio was copied")

    def __deepcopy__(self, memo: object) -> object:
        raise AssertionError("loaded audio was deep-copied")

    def __enter__(self) -> object:
        raise AssertionError("loaded audio context manager was probed")

    def __exit__(self, *args: object) -> None:
        raise AssertionError("loaded audio context manager was probed")


class CloneForbiddenAudioLoader:
    def __init__(
        self,
        residency: AudioResidencyProbe,
        *,
        error: AnalysisError | None = None,
        callback: Callable[[PersistedAudioReference, object], None] | None = None,
    ) -> None:
        self._condition = Condition()
        self._residency = residency
        self._error = error
        self._callback = callback
        self.calls: list[tuple[PersistedAudioReference, object]] = []
        self.handles: list[CloneForbiddenLoadedAudio] = []

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: object,
    ) -> CloneForbiddenLoadedAudio:
        with self._condition:
            self.calls.append((reference, cancel_token))
            self._condition.notify_all()
        if self._callback is not None:
            self._callback(reference, cancel_token)
        if self._error is not None:
            raise AudioLoadError(self._error)
        handle = CloneForbiddenLoadedAudio(reference, self._residency)
        with self._condition:
            self.handles.append(handle)
            self._condition.notify_all()
        return handle

    def wait_for_calls(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"loader did not receive {count} calls"


class RaisingAudioLoader:
    def __init__(self, exception: BaseException) -> None:
        self.exception = exception
        self.calls: list[tuple[PersistedAudioReference, object]] = []

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: object,
    ) -> FakeLoadedAudio:
        self.calls.append((reference, cancel_token))
        raise self.exception


class GatedAudioLoader(RecordingAudioLoader):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self._release = Event()

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: object,
    ) -> FakeLoadedAudio:
        self.entered.set()
        assert self._release.wait(2.0), "loader gate was not released"
        return super().open(reference, cancel_token)

    def release(self) -> None:
        self._release.set()


class CancellationGateAudioLoader:
    """Expose the worker token while holding the external loader call."""

    def __init__(self, residency: AudioResidencyProbe) -> None:
        self._condition = Condition()
        self._residency = residency
        self.entered = Event()
        self._release = Event()
        self.calls: list[tuple[PersistedAudioReference, object]] = []
        self.handles: list[CloneForbiddenLoadedAudio] = []

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: object,
    ) -> CloneForbiddenLoadedAudio:
        with self._condition:
            self.calls.append((reference, cancel_token))
            self.entered.set()
            self._condition.notify_all()
        assert self._release.wait(2.0), "loader gate was not released"
        handle = CloneForbiddenLoadedAudio(reference, self._residency)
        with self._condition:
            self.handles.append(handle)
            self._condition.notify_all()
        return handle

    def release(self) -> None:
        self._release.set()


class RecordingExecutor:
    def __init__(
        self,
        *,
        gated: bool = False,
        result_factory: Callable[[AnalysisJob], AnalysisExecutionResult] | None = None,
    ) -> None:
        self._condition = Condition()
        self._release = Event()
        if not gated:
            self._release.set()
        self._result_factory = result_factory or (
            lambda job: AnalysisExecutionResult(
                status=AnalysisExecutionStatus.SUCCEEDED,
                result_payload=("result", job.analysis_attempt_id),
            )
        )
        self.calls: list[tuple[AnalysisJob, object, object]] = []
        self.call_thread_ids: list[int] = []
        self.active = 0
        self.peak_active = 0

    def execute(
        self,
        job: AnalysisJob,
        audio: object,
        cancel_token: object,
    ) -> AnalysisExecutionResult:
        with self._condition:
            self.calls.append((job, audio, cancel_token))
            self.call_thread_ids.append(get_ident())
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self._condition.notify_all()
        assert self._release.wait(2.0), "executor gate was not released"
        try:
            return self._result_factory(job)
        finally:
            with self._condition:
                self.active -= 1
                self._condition.notify_all()

    def wait_for_calls(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"executor did not receive {count} calls"

    def release(self) -> None:
        self._release.set()


class GatedResultExecutor:
    """Hold execution, then deterministically return or raise one value."""

    def __init__(self, result_or_exception: object) -> None:
        self._condition = Condition()
        self._release = Event()
        self._result_or_exception = result_or_exception
        self.calls: list[tuple[AnalysisJob, object, object]] = []

    def execute(
        self,
        job: AnalysisJob,
        audio: object,
        cancel_token: object,
    ) -> AnalysisExecutionResult:
        with self._condition:
            self.calls.append((job, audio, cancel_token))
            self._condition.notify_all()
        assert self._release.wait(2.0), "executor gate was not released"
        if isinstance(self._result_or_exception, BaseException):
            raise self._result_or_exception
        return self._result_or_exception

    def wait_for_calls(self, count: int, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"executor did not receive {count} calls"

    def release(self) -> None:
        self._release.set()


class RaisingExecutor:
    def __init__(self, exception: BaseException) -> None:
        self.exception = exception
        self.calls: list[tuple[AnalysisJob, object, object]] = []

    def execute(
        self,
        job: AnalysisJob,
        audio: object,
        cancel_token: object,
    ) -> AnalysisExecutionResult:
        self.calls.append((job, audio, cancel_token))
        raise self.exception


class CallbackExecutor:
    def __init__(
        self,
        callback: Callable[[AnalysisJob, object, object], AnalysisExecutionResult],
    ) -> None:
        self._callback = callback
        self.calls: list[tuple[AnalysisJob, object, object]] = []

    def execute(
        self,
        job: AnalysisJob,
        audio: object,
        cancel_token: object,
    ) -> AnalysisExecutionResult:
        self.calls.append((job, audio, cancel_token))
        return self._callback(job, audio, cancel_token)


class PerAttemptGatedExecutor:
    """Track resource overlap while releasing attempts independently."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._gates: dict[str, Event] = {}
        self.calls: list[tuple[AnalysisJob, object, object]] = []
        self.active_attempt_ids: set[str] = set()
        self._active_run_counts: dict[str, int] = {}
        self._active_lane_counts: dict[str, int] = {}
        self.run_overlap_attempt_ids: list[str] = []
        self.toolchain_busy_attempt_ids: list[str] = []
        self.peak_active = 0

    @staticmethod
    def _lane_key(job: AnalysisJob) -> str | None:
        toolchain = job.snapshot.ai_toolchain
        return None if toolchain is None else toolchain.runtime_lane_key

    def hold(self, *analysis_attempt_ids: str) -> None:
        with self._condition:
            for attempt_id in analysis_attempt_ids:
                self._gates.setdefault(attempt_id, Event())

    def execute(
        self,
        job: AnalysisJob,
        audio: object,
        cancel_token: object,
    ) -> AnalysisExecutionResult:
        attempt_id = job.analysis_attempt_id
        lane_key = self._lane_key(job)
        with self._condition:
            gate = self._gates.setdefault(attempt_id, Event())
            if self._active_run_counts.get(job.run_id, 0):
                self.run_overlap_attempt_ids.append(attempt_id)
            if lane_key is not None and self._active_lane_counts.get(lane_key, 0):
                self.toolchain_busy_attempt_ids.append(attempt_id)
            self.calls.append((job, audio, cancel_token))
            self.active_attempt_ids.add(attempt_id)
            self._active_run_counts[job.run_id] = (
                self._active_run_counts.get(job.run_id, 0) + 1
            )
            if lane_key is not None:
                self._active_lane_counts[lane_key] = (
                    self._active_lane_counts.get(lane_key, 0) + 1
                )
            self.peak_active = max(self.peak_active, len(self.active_attempt_ids))
            self._condition.notify_all()
        assert gate.wait(10.0), f"executor gate for {attempt_id} was not released"
        try:
            return AnalysisExecutionResult(
                status=AnalysisExecutionStatus.SUCCEEDED,
                result_payload=("result", attempt_id),
            )
        finally:
            with self._condition:
                self.active_attempt_ids.remove(attempt_id)
                self._active_run_counts[job.run_id] -= 1
                if self._active_run_counts[job.run_id] == 0:
                    del self._active_run_counts[job.run_id]
                if lane_key is not None:
                    self._active_lane_counts[lane_key] -= 1
                    if self._active_lane_counts[lane_key] == 0:
                        del self._active_lane_counts[lane_key]
                self._condition.notify_all()

    def wait_for_calls(self, count: int, timeout_seconds: float = 1.0) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout_seconds,
            ), f"executor did not receive {count} calls"

    def wait_for_attempt(
        self,
        analysis_attempt_id: str,
        timeout_seconds: float = 1.0,
    ) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: any(
                    call[0].analysis_attempt_id == analysis_attempt_id
                    for call in self.calls
                ),
                timeout=timeout_seconds,
            ), f"executor did not receive {analysis_attempt_id}"

    def called_attempt_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(call[0].analysis_attempt_id for call in self.calls)

    def release(self, *analysis_attempt_ids: str) -> None:
        with self._condition:
            gates = tuple(
                self._gates.setdefault(attempt_id, Event())
                for attempt_id in analysis_attempt_ids
            )
        for gate in gates:
            gate.set()

    def release_all(self) -> None:
        with self._condition:
            gates = tuple(self._gates.values())
        for gate in gates:
            gate.set()


class OutcomeCollector:
    def __init__(self) -> None:
        self._condition = Condition()
        self.outcomes: list[AnalysisOutcome] = []

    def __call__(self, outcome: AnalysisOutcome) -> None:
        with self._condition:
            self.outcomes.append(outcome)
            self._condition.notify_all()

    def wait_for_count(
        self,
        count: int,
        timeout_seconds: float = 2.0,
    ) -> tuple[AnalysisOutcome, ...]:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.outcomes) >= count,
                timeout=timeout_seconds,
            ), f"outcome sink did not receive {count} outcomes"
            return tuple(self.outcomes)

    def wait_for_attempt(
        self,
        analysis_attempt_id: str,
        timeout_seconds: float = 2.0,
    ) -> tuple[AnalysisOutcome, ...]:
        with self._condition:
            assert self._condition.wait_for(
                lambda: any(
                    outcome.analysis_attempt_id == analysis_attempt_id
                    for outcome in self.outcomes
                ),
                timeout=timeout_seconds,
            ), f"outcome sink did not receive {analysis_attempt_id}"
            return tuple(self.outcomes)


class NeverInvokePort:
    """A callable/member spy whose invocations are always a test failure."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("constructor invoked a port")

    def open(self, *args: object, **kwargs: object) -> None:
        self(*args, **kwargs)

    def execute(self, *args: object, **kwargs: object) -> None:
        self(*args, **kwargs)

    def before_audio_load(self, *args: object, **kwargs: object) -> None:
        self(*args, **kwargs)

    def monotonic(self) -> float:
        self()
        raise AssertionError("unreachable")


def make_job(
    attempt_number: int,
    *,
    run_id: str | None = None,
    queue_timeout_seconds: float | None = None,
    config_snapshot_json: str = '{"config_id":"shared"}',
    kind: AnalysisKind = AnalysisKind.AUTOMATIC,
    toolchain_id: str | None = None,
    execution_revision: str = "toolchain-revision-1",
) -> AnalysisJob:
    attempt_id = f"attempt-{attempt_number}"
    ai_toolchain = (
        None
        if toolchain_id is None
        else AiToolchainSnapshot(
            toolchain_id=toolchain_id,
            runtime_lane_key=canonical_toolchain_lane_key(toolchain_id),
            execution_revision=execution_revision,
            execution_mode="resident",
            entrypoint="analyze.py",
            package_dir="C:/analysis/toolchains/package",
            params_json="{}",
            timeout_seconds=1.0,
        )
    )
    return AnalysisJob(
        run_id=run_id or f"run-{attempt_number}",
        analysis_attempt_id=attempt_id,
        kind=kind,
        audio=PersistedAudioReference(
            reference_id=f"audio-{attempt_number}",
            storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
            absolute_path=str(
                PureWindowsPath(f"C:/analysis/final/{attempt_id}.wav")
            ),
            sample_rate_hz=48_000,
            channel_count=1,
            frame_count=48_000,
            dtype="float32",
            content_revision=f"revision-{attempt_number}",
        ),
        snapshot=AnalysisInputSnapshot(
            config_snapshot_json=config_snapshot_json,
            calibration_snapshot_json="{}",
            workorder_snapshot_json="{}",
            channel_mapping_json="{}",
            analysis_window_start_seconds=0.0,
            analysis_window_end_seconds=1.0,
            algorithm_revision="algorithm-v1",
            ai_toolchain=ai_toolchain,
        ),
        execution_timeout_seconds=1.0,
        queue_timeout_seconds=queue_timeout_seconds,
    )
