"""Bounded resource-aware scheduling for isolated analysis jobs."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Final

from .models import (
    MAX_CANCEL_REASON_UTF8_BYTES,
    AnalysisCancelOrigin,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisJob,
    AnalysisKind,
    AnalysisOutcome,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStartGateCode,
    AnalysisStatus,
    AttemptDiagnostic,
    AttemptState,
    CancelReceipt,
    CancelStatus,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    SchedulerDiagnosticEventKind,
    SchedulerDiagnostics,
    SchedulerLifecycle,
    ShutdownReport,
    ShutdownStatus,
    SubmitReceipt,
    SubmitStatus,
    TerminalReceiptDiagnostic,
    ToolchainLaneDiagnostic,
    validate_frozen_result_payload,
)
from .ports import (
    AnalysisExecutionError,
    AnalysisExecutor,
    AnalysisStartGate,
    AnalysisStartGateError,
    AudioLoadError,
    AudioLoader,
    CancellationToken,
    DiagnosticSink,
    MonotonicClock,
    OutcomeSink,
)
from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_BELOW_NORMAL,
    set_current_thread_priority,
)


@dataclass(slots=True)
class _AttemptRecord:
    job: AnalysisJob
    submission_sequence: int
    submitted_monotonic: float
    queue_timeout_seconds: float | None
    state: AttemptState = AttemptState.PENDING
    worker_id: int | None = None
    started_monotonic: float | None = None
    load_started_monotonic: float | None = None
    load_finished_monotonic: float | None = None
    execution_started_monotonic: float | None = None
    dispatch_token: object | None = None
    cancel_token: _CancellationToken | None = None
    cancel_origin: AnalysisCancelOrigin | None = None
    cancel_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _DispatchCommand:
    instance_epoch: object
    dispatch_token: object
    record: _AttemptRecord


@dataclass(frozen=True, slots=True)
class _ShutdownAction:
    owner_thread: threading.Thread
    started_monotonic: float
    wall_deadline: float
    canceled_pending: int
    cancel_requested_running: int


class _CancellationToken:
    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._event.wait(timeout_seconds)

    def cancel(self, reason: str) -> None:
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()


_NO_COMMAND: Final = object()


class AnalysisScheduler:
    def __init__(
        self,
        *,
        config: AnalysisSchedulerConfig,
        audio_loader: AudioLoader,
        analysis_executor: AnalysisExecutor,
        start_gate: AnalysisStartGate,
        outcome_sink: OutcomeSink,
        clock: MonotonicClock,
        diagnostic_sink: DiagnosticSink | None = None,
    ) -> None:
        self._validate_constructor_arguments(
            config=config,
            audio_loader=audio_loader,
            analysis_executor=analysis_executor,
            start_gate=start_gate,
            outcome_sink=outcome_sink,
            clock=clock,
            diagnostic_sink=diagnostic_sink,
        )

        self._config = config
        self._audio_loader = audio_loader
        self._analysis_executor = analysis_executor
        self._start_gate = start_gate
        self._outcome_sink = outcome_sink
        self._clock = clock
        self._diagnostic_sink = diagnostic_sink
        self._instance_epoch = object()

        self._clock_lock = threading.Lock()
        self._last_clock_value: float | None = None
        self._condition = threading.Condition(threading.Lock())
        self._wake_generation = 0
        self._lifecycle = SchedulerLifecycle.RUNNING
        self._submission_sequence = 0
        self._pending: list[_AttemptRecord] = []
        self._active: dict[str, _AttemptRecord] = {}
        self._run_lane_owners: dict[str, _AttemptRecord] = {}
        self._toolchain_lane_owners: dict[str, _AttemptRecord] = {}
        self._free_worker_ids = set(range(1, config.worker_count + 1))
        self._commands: dict[int, object] = {
            worker_id: _NO_COMMAND
            for worker_id in range(1, config.worker_count + 1)
        }
        self._shutdown_report: ShutdownReport | None = None
        self._shutdown_in_progress = False
        self._shutdown_action: _ShutdownAction | None = None
        self._callback_context = threading.local()
        self._full_audio_residency_count = 0
        self._peak_full_audio_residency_count = 0
        self._peak_running_workers = 0
        self._terminal_receipts: deque[TerminalReceiptDiagnostic] = deque(
            maxlen=config.terminal_receipt_capacity
        )
        self._counters = {
            key: 0
            for key in SchedulerCounterKey
        }

        instance_label = f"{id(self):x}"
        self._workers = tuple(
            threading.Thread(
                target=self._worker_loop,
                args=(worker_id,),
                name=f"analysis-scheduler-worker-{worker_id}-{instance_label}",
                daemon=False,
            )
            for worker_id in range(1, config.worker_count + 1)
        )
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name=f"analysis-scheduler-control-{instance_label}",
            daemon=False,
        )

        started: list[threading.Thread] = []
        try:
            for worker in self._workers:
                worker.start()
                started.append(worker)
            self._control_thread.start()
            started.append(self._control_thread)
        except BaseException:
            with self._condition:
                self._lifecycle = SchedulerLifecycle.SHUTTING_DOWN
                self._wake_locked()
            for thread in started:
                thread.join(config.shutdown_timeout_seconds)
            raise

    @staticmethod
    def _validate_constructor_arguments(
        *,
        config: object,
        audio_loader: object,
        analysis_executor: object,
        start_gate: object,
        outcome_sink: object,
        clock: object,
        diagnostic_sink: object,
    ) -> None:
        if type(config) is not AnalysisSchedulerConfig:
            raise TypeError("config must be an exact AnalysisSchedulerConfig")
        try:
            AnalysisSchedulerConfig(
                worker_count=config.worker_count,
                pending_capacity=config.pending_capacity,
                terminal_receipt_capacity=config.terminal_receipt_capacity,
                shutdown_timeout_seconds=config.shutdown_timeout_seconds,
            )
        except (TypeError, ValueError, AttributeError):
            raise TypeError("config fields are invalid") from None
        if audio_loader is None or not callable(getattr(audio_loader, "open", None)):
            raise TypeError("audio_loader must provide callable open")
        if analysis_executor is None or not callable(
            getattr(analysis_executor, "execute", None)
        ):
            raise TypeError("analysis_executor must provide callable execute")
        if start_gate is None or not callable(
            getattr(start_gate, "before_audio_load", None)
        ):
            raise TypeError(
                "start_gate must provide callable before_audio_load"
            )
        if outcome_sink is None or not callable(outcome_sink):
            raise TypeError("outcome_sink must be callable")
        if clock is None or not callable(getattr(clock, "monotonic", None)):
            raise TypeError("clock must provide callable monotonic")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink must be callable or None")

    def _read_clock(self) -> float:
        value = self._clock.monotonic()
        if type(value) is not float:
            raise TypeError("clock.monotonic() must return an exact float")
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(
                "clock.monotonic() must return a finite non-negative float"
            )
        with self._clock_lock:
            if self._last_clock_value is not None and value < self._last_clock_value:
                raise ValueError("clock.monotonic() must be nondecreasing")
            self._last_clock_value = value
            return value

    def _read_clock_in_background(self) -> float:
        while True:
            observed_generation = self._wake_generation
            try:
                return self._read_clock()
            except (TypeError, ValueError):
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._wake_generation != observed_generation
                    )

    def _wake_locked(self) -> None:
        self._wake_generation += 1
        self._condition.notify_all()

    @staticmethod
    def _duration_seconds(start: float, end: float) -> float:
        return max(0.0, end - start)

    @classmethod
    def _duration_ms(cls, start: float, end: float) -> int:
        seconds = cls._duration_seconds(start, end)
        scaled = seconds * 1000.0
        if math.isfinite(scaled):
            return int(scaled)
        whole_seconds = int(seconds)
        fractional_seconds = seconds - whole_seconds
        return whole_seconds * 1000 + int(fractional_seconds * 1000.0)

    @staticmethod
    def _validated_job(job: object) -> AnalysisJob | None:
        if type(job) is not AnalysisJob:
            return None
        try:
            AnalysisJob(
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                kind=job.kind,
                audio=job.audio,
                snapshot=job.snapshot,
                execution_timeout_seconds=job.execution_timeout_seconds,
                queue_timeout_seconds=job.queue_timeout_seconds,
            )
        except (TypeError, ValueError, AttributeError):
            return None
        return job

    @staticmethod
    def _safe_ids(job: object) -> tuple[str, str]:
        if type(job) is not AnalysisJob:
            return "", ""
        run_id = job.run_id if type(job.run_id) is str else ""
        attempt_id = (
            job.analysis_attempt_id
            if type(job.analysis_attempt_id) is str
            else ""
        )
        return run_id, attempt_id

    @staticmethod
    def _admission_error(
        category: AnalysisErrorCategory,
        summary: str,
    ) -> AnalysisError:
        return AnalysisError(
            category=category,
            stage=AnalysisStage.ADMISSION,
            summary=summary,
        )

    def submit(self, job: AnalysisJob) -> SubmitReceipt:
        validated = self._validated_job(job)
        if validated is None:
            run_id, attempt_id = self._safe_ids(job)
            receipt = SubmitReceipt(
                status=SubmitStatus.INVALID_JOB,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                submission_sequence=None,
                error=self._admission_error(
                    AnalysisErrorCategory.INVALID_JOB,
                    "analysis job is structurally invalid",
                ),
            )
            with self._condition:
                self._record_submit_rejection_locked(receipt.status)
            return receipt

        with self._condition:
            rejection = self._submit_rejection_locked(job)
            if rejection is not None:
                self._record_submit_rejection_locked(rejection.status)
                self._wake_locked()
                return rejection

        try:
            now = self._read_clock()
        except (TypeError, ValueError):
            with self._condition:
                rejection = self._submit_rejection_locked(job)
                if rejection is not None:
                    self._record_submit_rejection_locked(rejection.status)
                    self._wake_locked()
                    return rejection
            raise
        with self._condition:
            rejection = self._submit_rejection_locked(job)
            if rejection is not None:
                self._record_submit_rejection_locked(rejection.status)
                self._wake_locked()
                return rejection
            register_staged = getattr(
                self._analysis_executor,
                "register_staged_attempt",
                None,
            )
            if callable(register_staged) and register_staged(job) is False:
                rejection = SubmitReceipt(
                    status=SubmitStatus.QUEUE_FULL,
                    run_id=job.run_id,
                    analysis_attempt_id=job.analysis_attempt_id,
                    submission_sequence=None,
                    error=self._admission_error(
                        AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
                        "staged analysis capacity is full",
                    ),
                )
                self._record_submit_rejection_locked(rejection.status)
                self._wake_locked()
                return rejection
            self._submission_sequence += 1
            sequence = self._submission_sequence
            record = _AttemptRecord(
                job=job,
                submission_sequence=sequence,
                submitted_monotonic=now,
                queue_timeout_seconds=job.queue_timeout_seconds,
            )
            self._pending.append(record)
            self._active[job.analysis_attempt_id] = record
            self._counters[SchedulerCounterKey.ACCEPTED] += 1
            receipt = SubmitReceipt(
                status=SubmitStatus.ACCEPTED,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                submission_sequence=sequence,
                error=None,
            )
            self._wake_locked()
            return receipt

    def _record_submit_rejection_locked(self, status: SubmitStatus) -> None:
        key = {
            SubmitStatus.INVALID_JOB: SchedulerCounterKey.INVALID_JOB_REJECTED,
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
                SchedulerCounterKey.DUPLICATE_ATTEMPT_REJECTED,
            SubmitStatus.QUEUE_FULL: SchedulerCounterKey.QUEUE_FULL_REJECTED,
            SubmitStatus.NOT_ACCEPTING:
                SchedulerCounterKey.NOT_ACCEPTING_REJECTED,
        }[status]
        self._counters[key] += 1

    @staticmethod
    def _bounded_cancel_reason(reason: str) -> str:
        encoded = reason.encode("utf-8")
        if len(encoded) <= MAX_CANCEL_REASON_UTF8_BYTES:
            return reason
        return encoded[:MAX_CANCEL_REASON_UTF8_BYTES].decode(
            "utf-8",
            errors="ignore",
        )

    @staticmethod
    def _cancel_miss_receipt(
        status: CancelStatus,
        run_id: str,
        analysis_attempt_id: str,
    ) -> CancelReceipt:
        summary = (
            "analysis attempt belongs to a different run"
            if status is CancelStatus.RUN_ID_MISMATCH
            else "analysis attempt was not found"
        )
        return CancelReceipt(
            status=status,
            run_id=run_id,
            analysis_attempt_id=analysis_attempt_id,
            drain_pending=False,
            error=AnalysisError(
                category=AnalysisErrorCategory.INVALID_JOB,
                stage=AnalysisStage.CANCEL,
                summary=summary,
            ),
        )

    @staticmethod
    def _cancel_receipt(
        status: CancelStatus,
        run_id: str,
        analysis_attempt_id: str,
    ) -> CancelReceipt:
        return CancelReceipt(
            status=status,
            run_id=run_id,
            analysis_attempt_id=analysis_attempt_id,
            drain_pending=(
                status is CancelStatus.CANCEL_REQUESTED_RUNNING
            ),
            error=None,
        )

    def _retained_receipt_locked(
        self,
        analysis_attempt_id: str,
    ) -> TerminalReceiptDiagnostic | None:
        return next(
            (
                receipt
                for receipt in self._terminal_receipts
                if receipt.analysis_attempt_id == analysis_attempt_id
            ),
            None,
        )

    def cancel(
        self,
        run_id: str,
        analysis_attempt_id: str,
        origin: AnalysisCancelOrigin,
        reason: str,
    ) -> CancelReceipt:
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty exact str")
        if type(analysis_attempt_id) is not str or not analysis_attempt_id:
            raise ValueError(
                "analysis_attempt_id must be a non-empty exact str"
            )
        if type(origin) is not AnalysisCancelOrigin:
            raise TypeError("origin must be an exact AnalysisCancelOrigin")
        if type(reason) is not str:
            raise TypeError("reason must be an exact str")
        bounded_reason = self._bounded_cancel_reason(reason)
        summary = bounded_reason or "analysis canceled"

        while True:
            with self._condition:
                record = self._active.get(analysis_attempt_id)
                if record is None:
                    retained = self._retained_receipt_locked(
                        analysis_attempt_id
                    )
                    if retained is None:
                        return self._cancel_miss_receipt(
                            CancelStatus.NOT_FOUND,
                            run_id,
                            analysis_attempt_id,
                        )
                    if retained.run_id != run_id:
                        return self._cancel_miss_receipt(
                            CancelStatus.RUN_ID_MISMATCH,
                            run_id,
                            analysis_attempt_id,
                        )
                    return self._cancel_receipt(
                        CancelStatus.ALREADY_TERMINAL,
                        run_id,
                        analysis_attempt_id,
                    )
                if record.job.run_id != run_id:
                    return self._cancel_miss_receipt(
                        CancelStatus.RUN_ID_MISMATCH,
                        run_id,
                        analysis_attempt_id,
                    )
                if record.state is AttemptState.CANCEL_REQUESTED:
                    return self._cancel_receipt(
                        CancelStatus.CANCEL_REQUESTED_RUNNING,
                        run_id,
                        analysis_attempt_id,
                    )

            now = self._read_clock()
            outcome: AnalysisOutcome | None = None
            with self._condition:
                current = self._active.get(analysis_attempt_id)
                if current is not record or record.job.run_id != run_id:
                    continue
                if record.state is AttemptState.CANCEL_REQUESTED:
                    return self._cancel_receipt(
                        CancelStatus.CANCEL_REQUESTED_RUNNING,
                        run_id,
                        analysis_attempt_id,
                    )
                if record.state is AttemptState.PENDING:
                    if record not in self._pending:
                        continue
                    self._pending.remove(record)
                    record.state = AttemptState.CANCELED
                    del self._active[analysis_attempt_id]
                    outcome = AnalysisOutcome(
                        run_id=run_id,
                        analysis_attempt_id=analysis_attempt_id,
                        kind=record.job.kind,
                        status=AnalysisStatus.CANCELED,
                        audio_reference_id=record.job.audio.reference_id,
                        result_payload=None,
                        error=self._cancel_error(summary),
                        submitted_monotonic=record.submitted_monotonic,
                        started_monotonic=None,
                        finished_monotonic=now,
                        queue_wait_ms=self._duration_ms(
                            record.submitted_monotonic,
                            now,
                        ),
                        execution_ms=None,
                        cancel_origin=origin,
                    )
                    self._record_terminal_locked(outcome)
                    event = SchedulerDiagnosticEvent(
                        event_kind=(
                            SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
                        ),
                        occurred_monotonic=now,
                        run_id=run_id,
                        analysis_attempt_id=analysis_attempt_id,
                        analysis_kind=record.job.kind,
                        audio_reference_id=record.job.audio.reference_id,
                        submission_sequence=record.submission_sequence,
                        state_before=AttemptState.PENDING,
                        state_after=AttemptState.CANCELED,
                        worker_id=None,
                        runtime_lane_key=self._runtime_lane_key(record),
                        queue_wait_ms=outcome.queue_wait_ms,
                        load_ms=None,
                        execution_ms=None,
                        error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
                        error_stage=AnalysisStage.CANCEL,
                        summary=summary,
                    )
                    receipt = self._cancel_receipt(
                        CancelStatus.CANCELED_PENDING,
                        run_id,
                        analysis_attempt_id,
                    )
                elif record.state is AttemptState.RUNNING:
                    token = record.cancel_token
                    if token is None:
                        continue
                    record.state = AttemptState.CANCEL_REQUESTED
                    record.cancel_origin = origin
                    record.cancel_reason = summary
                    token.cancel(summary)
                    load_started = record.load_started_monotonic
                    load_finished = record.load_finished_monotonic
                    event = SchedulerDiagnosticEvent(
                        event_kind=(
                            SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                        ),
                        occurred_monotonic=now,
                        run_id=run_id,
                        analysis_attempt_id=analysis_attempt_id,
                        analysis_kind=record.job.kind,
                        audio_reference_id=record.job.audio.reference_id,
                        submission_sequence=record.submission_sequence,
                        state_before=AttemptState.RUNNING,
                        state_after=AttemptState.CANCEL_REQUESTED,
                        worker_id=record.worker_id,
                        runtime_lane_key=self._runtime_lane_key(record),
                        queue_wait_ms=self._duration_ms(
                            record.submitted_monotonic,
                            record.started_monotonic,
                        ),
                        load_ms=(
                            None
                            if load_started is None or load_finished is None
                            else self._duration_ms(
                                load_started,
                                load_finished,
                            )
                        ),
                        execution_ms=(
                            None
                            if record.execution_started_monotonic is None
                            else self._duration_ms(
                                record.execution_started_monotonic,
                                now,
                            )
                        ),
                        error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
                        error_stage=AnalysisStage.CANCEL,
                        summary=summary,
                    )
                    receipt = self._cancel_receipt(
                        CancelStatus.CANCEL_REQUESTED_RUNNING,
                        run_id,
                        analysis_attempt_id,
                    )
                else:
                    continue
                self._wake_locked()

            self._publish_diagnostic(event)
            if outcome is not None:
                self._publish_outcome(outcome, event)
            return receipt

    def _submit_rejection_locked(
        self,
        job: AnalysisJob,
    ) -> SubmitReceipt | None:
        if self._lifecycle is not SchedulerLifecycle.RUNNING:
            return SubmitReceipt(
                status=SubmitStatus.NOT_ACCEPTING,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                submission_sequence=None,
                error=self._admission_error(
                    AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
                    "analysis scheduler is not accepting jobs",
                ),
            )
        if (
            job.analysis_attempt_id in self._active
            or any(
                receipt.analysis_attempt_id == job.analysis_attempt_id
                for receipt in self._terminal_receipts
            )
        ):
            return SubmitReceipt(
                status=SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                submission_sequence=None,
                error=self._admission_error(
                    AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
                    "analysis attempt ID is already active",
                ),
            )
        if len(self._pending) >= self._config.pending_capacity:
            return SubmitReceipt(
                status=SubmitStatus.QUEUE_FULL,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                submission_sequence=None,
                error=self._admission_error(
                    AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
                    "analysis pending queue is full",
                ),
            )
        return None

    def _control_loop(self) -> None:
        while True:
            record = None
            current = None
            retained = None
            expired_outcomes: list[AnalysisOutcome] = []
            with self._condition:
                self._condition.wait_for(
                    lambda: self._lifecycle is not SchedulerLifecycle.RUNNING
                    or bool(self._pending)
                )
                if self._lifecycle is not SchedulerLifecycle.RUNNING:
                    self._wake_locked()
                    return
                observed_generation = self._wake_generation
            try:
                now = self._read_clock()
            except (TypeError, ValueError):
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._wake_generation != observed_generation
                    )
                continue

            with self._condition:
                if self._lifecycle is not SchedulerLifecycle.RUNNING:
                    self._wake_locked()
                    return
                retained: list[_AttemptRecord] = []
                for record in self._pending:
                    if (
                        record.queue_timeout_seconds is not None
                        and self._duration_seconds(
                            record.submitted_monotonic,
                            now,
                        )
                        >= record.queue_timeout_seconds
                    ):
                        current = self._active.get(
                            record.job.analysis_attempt_id
                        )
                        if current is record and record.state is AttemptState.PENDING:
                            outcome = self._queue_timeout_outcome(record, now)
                            del self._active[record.job.analysis_attempt_id]
                            record.state = AttemptState.FAILED
                            self._record_terminal_locked(outcome)
                            expired_outcomes.append(outcome)
                    else:
                        retained.append(record)
                self._pending = retained

                while self._free_worker_ids:
                    record = self._next_eligible_record_locked()
                    if record is None:
                        break
                    self._pending.remove(record)
                    worker_id = min(self._free_worker_ids)
                    self._free_worker_ids.remove(worker_id)
                    record.state = AttemptState.RUNNING
                    record.worker_id = worker_id
                    record.started_monotonic = now
                    record.dispatch_token = object()
                    record.cancel_token = _CancellationToken()
                    self._run_lane_owners[record.job.run_id] = record
                    runtime_lane_key = self._runtime_lane_key(record)
                    if runtime_lane_key is not None:
                        self._toolchain_lane_owners[runtime_lane_key] = record
                    self._commands[worker_id] = _DispatchCommand(
                        instance_epoch=self._instance_epoch,
                        dispatch_token=record.dispatch_token,
                        record=record,
                    )
                    running_workers = (
                        self._config.worker_count - len(self._free_worker_ids)
                    )
                    self._peak_running_workers = max(
                        self._peak_running_workers,
                        running_workers,
                    )
                    self._wake_locked()

                next_timeout = min(
                    (
                        max(
                            0.0,
                            record.queue_timeout_seconds
                            - self._duration_seconds(
                                record.submitted_monotonic,
                                now,
                            ),
                        )
                        for record in self._pending
                        if record.queue_timeout_seconds is not None
                    ),
                    default=None,
                )
                record = None
                current = None
                retained = None
                if not expired_outcomes:
                    timeout = (
                        None
                        if next_timeout is None
                        else min(next_timeout, threading.TIMEOUT_MAX)
                    )
                    self._condition.wait(timeout=timeout)

            for outcome in expired_outcomes:
                self._publish_outcome(outcome)

    @staticmethod
    def _runtime_lane_key(record: _AttemptRecord) -> str | None:
        toolchain = record.job.snapshot.ai_toolchain
        return None if toolchain is None else toolchain.runtime_lane_key

    def _next_eligible_record_locked(self) -> _AttemptRecord | None:
        earliest_by_run: dict[str, _AttemptRecord] = {}
        earliest_by_toolchain: dict[str, _AttemptRecord] = {}
        for record in self._pending:
            earliest_run = earliest_by_run.get(record.job.run_id)
            if (
                earliest_run is None
                or record.submission_sequence
                < earliest_run.submission_sequence
            ):
                earliest_by_run[record.job.run_id] = record
            runtime_lane_key = self._runtime_lane_key(record)
            if runtime_lane_key is not None:
                earliest_toolchain = earliest_by_toolchain.get(runtime_lane_key)
                if (
                    earliest_toolchain is None
                    or record.submission_sequence
                    < earliest_toolchain.submission_sequence
                ):
                    earliest_by_toolchain[runtime_lane_key] = record

        eligible: list[_AttemptRecord] = []
        for record in self._pending:
            run_id = record.job.run_id
            if run_id in self._run_lane_owners:
                continue
            if earliest_by_run[run_id] is not record:
                continue
            runtime_lane_key = self._runtime_lane_key(record)
            if runtime_lane_key is not None:
                if runtime_lane_key in self._toolchain_lane_owners:
                    continue
                if earliest_by_toolchain[runtime_lane_key] is not record:
                    continue
            eligible.append(record)
        return min(
            eligible,
            key=lambda item: item.submission_sequence,
            default=None,
        )

    @staticmethod
    def _queue_timeout_outcome(
        record: _AttemptRecord,
        finished: float,
    ) -> AnalysisOutcome:
        return AnalysisOutcome(
            run_id=record.job.run_id,
            analysis_attempt_id=record.job.analysis_attempt_id,
            kind=record.job.kind,
            status=AnalysisStatus.FAILED,
            audio_reference_id=record.job.audio.reference_id,
            result_payload=None,
            error=AnalysisError(
                category=AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
                stage=AnalysisStage.QUEUE,
                summary="analysis queue wait deadline expired",
            ),
            submitted_monotonic=record.submitted_monotonic,
            started_monotonic=None,
            finished_monotonic=finished,
            queue_wait_ms=AnalysisScheduler._duration_ms(
                record.submitted_monotonic,
                finished,
            ),
            execution_ms=None,
            cancel_origin=None,
        )

    def _worker_loop(self, worker_id: int) -> None:
        # This thread now only transfers results to the isolated analysis
        # process.  Keep result decoding below capture and monitor priority.
        set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._commands[worker_id] is not _NO_COMMAND
                    or self._lifecycle is not SchedulerLifecycle.RUNNING
                )
                command = self._commands[worker_id]
                if command is _NO_COMMAND:
                    return
                self._commands[worker_id] = _NO_COMMAND
            self._execute_successful_attempt(worker_id, command)
            del command

    def _execute_successful_attempt(
        self,
        worker_id: int,
        command: _DispatchCommand,
    ) -> None:
        record = command.record
        token: CancellationToken = record.cancel_token
        loaded_audio = None
        result: AnalysisExecutionResult | None = None
        outcome_status = AnalysisStatus.FAILED
        outcome_payload = None
        outcome_error: AnalysisError | None = None
        with self._condition:
            record.load_started_monotonic = None
            record.load_finished_monotonic = None
        load_started: float | None = None
        load_finished: float | None = None
        execution_started: float | None = None
        try:
            try:
                self._start_gate.before_audio_load(record.job)
            except AnalysisStartGateError as exc:
                outcome_error = AnalysisError(
                    category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
                    stage=AnalysisStage.START_GATE,
                    summary=exc.failure.summary,
                    details=(("code", exc.failure.code.value),),
                )
            except Exception:
                outcome_error = AnalysisError(
                    category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
                    stage=AnalysisStage.START_GATE,
                    summary="analysis start gate failed",
                    details=(
                        (
                            "code",
                            AnalysisStartGateCode.ATTEMPT_START_GATE_EXCEPTION.value,
                        ),
                    ),
                )
            else:
                load_started = self._read_clock_in_background()
                try:
                    loaded_audio = self._audio_loader.open(
                        record.job.audio,
                        token,
                    )
                except AudioLoadError as exc:
                    load_finished = self._read_clock_in_background()
                    if exc.error.stage is AnalysisStage.AUDIO_LOAD:
                        outcome_error = exc.error
                    else:
                        execution_started = load_finished
                        outcome_error = self._execution_failure_error()
                except Exception:
                    load_finished = self._read_clock_in_background()
                    outcome_error = self._audio_load_failure_error()
                else:
                    load_finished = self._read_clock_in_background()
                    with self._condition:
                        self._full_audio_residency_count += 1
                        self._peak_full_audio_residency_count = max(
                            self._peak_full_audio_residency_count,
                            self._full_audio_residency_count,
                        )
                        record.load_started_monotonic = load_started
                        record.load_finished_monotonic = load_finished
                    execution_started = self._read_clock_in_background()
                    record.execution_started_monotonic = execution_started
                    try:
                        result = self._analysis_executor.execute(
                            record.job,
                            loaded_audio,
                            token,
                        )
                    except AnalysisExecutionError as exc:
                        outcome_error = (
                            exc.error
                            if exc.error.stage is AnalysisStage.EXECUTE
                            else self._execution_failure_error()
                        )
                    except Exception:
                        outcome_error = self._execution_failure_error()
                    else:
                        (
                            outcome_status,
                            outcome_payload,
                            outcome_error,
                        ) = self._normalize_execution_result(result)
        finally:
            if load_started is not None and load_finished is not None:
                with self._condition:
                    record.load_started_monotonic = load_started
                    record.load_finished_monotonic = load_finished
            if loaded_audio is not None:
                close_failed = False
                try:
                    loaded_audio.close()
                except Exception:
                    close_failed = True
                finally:
                    loaded_audio = None
                    with self._condition:
                        self._full_audio_residency_count -= 1
                        self._wake_locked()
                if close_failed:
                    outcome_status = AnalysisStatus.FAILED
                    outcome_payload = None
                    outcome_error = self._execution_failure_error(
                        "loaded audio cleanup failed"
                    )

        finished = self._read_clock_in_background()
        if (
            outcome_error is not None
            and outcome_status is not AnalysisStatus.NO_RESULT
        ):
            outcome_status = AnalysisStatus.FAILED
            outcome_payload = None
        execution_ms = (
            None
            if execution_started is None
            else self._duration_ms(execution_started, finished)
        )
        outcome_values = {
            "run_id": record.job.run_id,
            "analysis_attempt_id": record.job.analysis_attempt_id,
            "kind": record.job.kind,
            "status": outcome_status,
            "audio_reference_id": record.job.audio.reference_id,
            "result_payload": outcome_payload,
            "error": outcome_error,
            "submitted_monotonic": record.submitted_monotonic,
            "started_monotonic": record.started_monotonic,
            "finished_monotonic": finished,
            "queue_wait_ms": self._duration_ms(
                record.submitted_monotonic,
                record.started_monotonic,
            ),
            "execution_ms": execution_ms,
            "cancel_origin": None,
        }
        trusted_process_result = bool(
            getattr(
                self._analysis_executor,
                "validates_result_out_of_process",
                False,
            )
        )
        if (
            outcome_status is AnalysisStatus.SUCCEEDED
            and trusted_process_result
        ):
            # The child process already validated this immutable result.
            # Rewalking the multi-megabyte chart tree here can starve audio
            # callbacks in the capture process.
            outcome = object.__new__(AnalysisOutcome)
            for name, value in outcome_values.items():
                object.__setattr__(outcome, name, value)
        else:
            outcome = AnalysisOutcome(**outcome_values)

        terminalized = self._terminalize_worker_attempt(
            worker_id,
            command,
            outcome,
        )
        if terminalized is None:
            return
        terminal_event, outcome = terminalized
        self._publish_diagnostic(terminal_event)
        self._publish_outcome(outcome, terminal_event)

    @staticmethod
    def _audio_load_failure_error() -> AnalysisError:
        return AnalysisError(
            category=AnalysisErrorCategory.AUDIO_LOAD_FAILED,
            stage=AnalysisStage.AUDIO_LOAD,
            summary="audio loading failed",
        )

    @staticmethod
    def _execution_failure_error(
        summary: str = "analysis execution failed",
    ) -> AnalysisError:
        return AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            stage=AnalysisStage.EXECUTE,
            summary=summary,
        )

    def _normalize_execution_result(
        self,
        result: object,
    ) -> tuple[AnalysisStatus, object | None, AnalysisError | None]:
        try:
            if type(result) is not AnalysisExecutionResult:
                raise TypeError("executor returned the wrong result type")
            trusted_process_result = bool(
                getattr(
                    self._analysis_executor,
                    "validates_result_out_of_process",
                    False,
                )
            )
            if not trusted_process_result:
                AnalysisExecutionResult(
                    status=result.status,
                    result_payload=result.result_payload,
                    summary=result.summary,
                )
            if result.status is AnalysisExecutionStatus.SUCCEEDED:
                if result.result_payload is None:
                    raise ValueError("successful result payload is missing")
                if not trusted_process_result:
                    validate_frozen_result_payload(result.result_payload)
                return AnalysisStatus.SUCCEEDED, result.result_payload, None
            if (
                result.status is not AnalysisExecutionStatus.NO_RESULT
                or result.result_payload is not None
            ):
                raise ValueError("executor result is incoherent")
            summary = (
                "analysis produced no result"
                if result.summary is None
                else result.summary
            )
            return (
                AnalysisStatus.NO_RESULT,
                None,
                AnalysisError(
                    category=AnalysisErrorCategory.ANALYSIS_NO_RESULT,
                    stage=AnalysisStage.EXECUTE,
                    summary=summary,
                ),
            )
        except (TypeError, ValueError, AttributeError):
            return AnalysisStatus.FAILED, None, self._execution_failure_error()

    def _terminalize_worker_attempt(
        self,
        worker_id: int,
        command: _DispatchCommand,
        outcome: AnalysisOutcome,
    ) -> tuple[SchedulerDiagnosticEvent, AnalysisOutcome] | None:
        record = command.record
        with self._condition:
            current = self._active.get(record.job.analysis_attempt_id)
            identity_matches = (
                command.instance_epoch is self._instance_epoch
                and command.dispatch_token is record.dispatch_token
                and current is record
                and current.job.run_id == record.job.run_id
                and current.job.analysis_attempt_id
                == record.job.analysis_attempt_id
                and record.worker_id == worker_id
                and worker_id not in self._free_worker_ids
                and self._run_lane_owners.get(record.job.run_id) is record
                and record.state
                in (AttemptState.RUNNING, AttemptState.CANCEL_REQUESTED)
            )
            runtime_lane_key = self._runtime_lane_key(record)
            identity_matches = identity_matches and (
                runtime_lane_key is None
                or self._toolchain_lane_owners.get(runtime_lane_key) is record
            )
            if not identity_matches:
                return None
            state_before = record.state
            if state_before is AttemptState.CANCEL_REQUESTED:
                outcome = self._running_canceled_outcome(record, outcome)
                self._counters[
                    SchedulerCounterKey.LATE_RESULTS_SUPPRESSED
                ] += 1
                state_after = AttemptState.CANCELED
            else:
                state_after = {
                    AnalysisStatus.SUCCEEDED: AttemptState.SUCCEEDED,
                    AnalysisStatus.NO_RESULT: AttemptState.NO_RESULT,
                    AnalysisStatus.FAILED: AttemptState.FAILED,
                    AnalysisStatus.CANCELED: AttemptState.CANCELED,
                }[outcome.status]
            record.state = state_after
            del self._active[record.job.analysis_attempt_id]
            del self._run_lane_owners[record.job.run_id]
            if runtime_lane_key is not None:
                del self._toolchain_lane_owners[runtime_lane_key]
            self._free_worker_ids.add(worker_id)
            self._record_terminal_locked(outcome)
            load_finished = record.load_finished_monotonic
            load_started = record.load_started_monotonic
            load_ms = (
                None
                if load_started is None or load_finished is None
                else self._duration_ms(load_started, load_finished)
            )
            event = SchedulerDiagnosticEvent(
                event_kind=SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
                occurred_monotonic=outcome.finished_monotonic,
                run_id=record.job.run_id,
                analysis_attempt_id=record.job.analysis_attempt_id,
                analysis_kind=record.job.kind,
                audio_reference_id=record.job.audio.reference_id,
                submission_sequence=record.submission_sequence,
                state_before=state_before,
                state_after=state_after,
                worker_id=worker_id,
                runtime_lane_key=runtime_lane_key,
                queue_wait_ms=outcome.queue_wait_ms,
                load_ms=load_ms,
                execution_ms=outcome.execution_ms,
                error_category=(
                    None if outcome.error is None else outcome.error.category
                ),
                error_stage=(
                    None if outcome.error is None else outcome.error.stage
                ),
                summary=None if outcome.error is None else outcome.error.summary,
                details=() if outcome.error is None else outcome.error.details,
            )
            self._wake_locked()
            return event, outcome

    @staticmethod
    def _cancel_error(summary: str) -> AnalysisError:
        return AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            stage=AnalysisStage.CANCEL,
            summary=summary,
        )

    def _running_canceled_outcome(
        self,
        record: _AttemptRecord,
        late_outcome: AnalysisOutcome,
    ) -> AnalysisOutcome:
        return AnalysisOutcome(
            run_id=record.job.run_id,
            analysis_attempt_id=record.job.analysis_attempt_id,
            kind=record.job.kind,
            status=AnalysisStatus.CANCELED,
            audio_reference_id=record.job.audio.reference_id,
            result_payload=None,
            error=self._cancel_error(record.cancel_reason or "analysis canceled"),
            submitted_monotonic=record.submitted_monotonic,
            started_monotonic=record.started_monotonic,
            finished_monotonic=late_outcome.finished_monotonic,
            queue_wait_ms=late_outcome.queue_wait_ms,
            execution_ms=late_outcome.execution_ms,
            cancel_origin=record.cancel_origin,
        )

    def _record_terminal_locked(self, outcome: AnalysisOutcome) -> None:
        status_key = {
            AnalysisStatus.SUCCEEDED: SchedulerCounterKey.SUCCEEDED,
            AnalysisStatus.NO_RESULT: SchedulerCounterKey.NO_RESULT,
            AnalysisStatus.FAILED: SchedulerCounterKey.FAILED,
            AnalysisStatus.CANCELED: SchedulerCounterKey.CANCELED,
        }[outcome.status]
        self._counters[status_key] += 1
        if outcome.status is AnalysisStatus.FAILED:
            category = outcome.error.category
            if category is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT:
                self._counters[SchedulerCounterKey.QUEUE_TIMEOUT] += 1
            elif category is AnalysisErrorCategory.ATTEMPT_START_FAILED:
                self._counters[SchedulerCounterKey.START_GATE_FAILURE] += 1
            elif category in (
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
            ):
                self._counters[SchedulerCounterKey.AUDIO_LOAD_FAILURE] += 1
            elif category is AnalysisErrorCategory.ANALYSIS_TIMEOUT:
                self._counters[SchedulerCounterKey.ANALYSIS_TIMEOUT] += 1
            else:
                self._counters[SchedulerCounterKey.EXECUTOR_FAILURE] += 1
        retained = tuple(
            receipt
            for receipt in self._terminal_receipts
            if receipt.analysis_attempt_id != outcome.analysis_attempt_id
        )
        self._terminal_receipts.clear()
        self._terminal_receipts.extend(retained)
        self._terminal_receipts.append(
            TerminalReceiptDiagnostic(
                run_id=outcome.run_id,
                analysis_attempt_id=outcome.analysis_attempt_id,
                terminal_status=outcome.status,
                finished_monotonic=outcome.finished_monotonic,
            )
        )

    def _publish_diagnostic(self, event: SchedulerDiagnosticEvent) -> None:
        if self._diagnostic_sink is None:
            return
        depth = getattr(self._callback_context, "depth", 0)
        self._callback_context.depth = depth + 1
        try:
            try:
                self._diagnostic_sink(event)
            except Exception:
                with self._condition:
                    self._counters[
                        SchedulerCounterKey.DIAGNOSTIC_SINK_FAILURE
                    ] += 1
        finally:
            self._callback_context.depth = depth

    def _publish_outcome(
        self,
        outcome: AnalysisOutcome,
        terminal_event: SchedulerDiagnosticEvent | None = None,
    ) -> None:
        depth = getattr(self._callback_context, "depth", 0)
        self._callback_context.depth = depth + 1
        try:
            try:
                self._outcome_sink(outcome)
            except Exception:
                with self._condition:
                    self._counters[SchedulerCounterKey.OUTCOME_SINK_FAILURE] += 1
                if terminal_event is not None:
                    sink_event = SchedulerDiagnosticEvent(
                        event_kind=SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
                        occurred_monotonic=outcome.finished_monotonic,
                        run_id=terminal_event.run_id,
                        analysis_attempt_id=terminal_event.analysis_attempt_id,
                        analysis_kind=terminal_event.analysis_kind,
                        audio_reference_id=terminal_event.audio_reference_id,
                        submission_sequence=terminal_event.submission_sequence,
                        state_before=terminal_event.state_after,
                        state_after=terminal_event.state_after,
                        worker_id=None,
                        runtime_lane_key=terminal_event.runtime_lane_key,
                        queue_wait_ms=terminal_event.queue_wait_ms,
                        load_ms=terminal_event.load_ms,
                        execution_ms=terminal_event.execution_ms,
                        error_category=AnalysisErrorCategory.OUTCOME_SINK_FAILED,
                        error_stage=AnalysisStage.OUTCOME_SINK,
                        summary="outcome sink failed",
                    )
                    self._publish_diagnostic(sink_event)
        finally:
            self._callback_context.depth = depth
            if outcome.status is not AnalysisStatus.SUCCEEDED:
                retire_staged = getattr(
                    self._analysis_executor,
                    "retire_staged_attempt",
                    None,
                )
                if callable(retire_staged):
                    try:
                        retire_staged(
                            outcome.analysis_attempt_id,
                            (
                                outcome.error.category.value
                                if outcome.error is not None
                                else outcome.status.value
                            ),
                        )
                    except Exception:
                        pass

    def diagnostics(self) -> SchedulerDiagnostics:
        now = self._read_clock()
        with self._condition:
            attempts = tuple(
                AttemptDiagnostic(
                    run_id=record.job.run_id,
                    analysis_attempt_id=record.job.analysis_attempt_id,
                    kind=record.job.kind,
                    state=record.state,
                    submission_sequence=record.submission_sequence,
                    worker_id=record.worker_id,
                    runtime_lane_key=self._runtime_lane_key(record),
                    wait_ms=self._duration_ms(
                        record.submitted_monotonic,
                        (
                            now
                            if record.started_monotonic is None
                            else record.started_monotonic
                        ),
                    ),
                    execution_ms=(
                        None
                        if record.execution_started_monotonic is None
                        else self._duration_ms(
                            record.execution_started_monotonic,
                            now,
                        )
                    ),
                )
                for record in sorted(
                    self._active.values(),
                    key=lambda item: item.submission_sequence,
                )
            )
            pending = sum(
                attempt.state is AttemptState.PENDING
                for attempt in attempts
            )
            running = sum(
                attempt.state is AttemptState.RUNNING
                for attempt in attempts
            )
            cancel_requested = sum(
                attempt.state is AttemptState.CANCEL_REQUESTED
                for attempt in attempts
            )
            pending_by_kind = tuple(
                (
                    kind,
                    sum(
                        attempt.state is AttemptState.PENDING
                        and attempt.kind is kind
                        for attempt in attempts
                    ),
                )
                for kind in AnalysisKind
            )
            lane_counts: dict[str, list[int]] = {}
            for attempt in attempts:
                if attempt.runtime_lane_key is None:
                    continue
                counts = lane_counts.setdefault(
                    attempt.runtime_lane_key,
                    [0, 0],
                )
                if attempt.state is AttemptState.PENDING:
                    counts[1] += 1
                else:
                    counts[0] += 1
            toolchain_lanes = tuple(
                ToolchainLaneDiagnostic(
                    runtime_lane_key=lane_key,
                    running=counts[0],
                    pending=counts[1],
                )
                for lane_key, counts in sorted(lane_counts.items())
            )
            blocked_on_run_lane = sum(
                record.state is AttemptState.PENDING
                and record.job.run_id in self._run_lane_owners
                for record in self._active.values()
            )
            blocked_on_toolchain_lane = sum(
                record.state is AttemptState.PENDING
                and (lane_key := self._runtime_lane_key(record)) is not None
                and lane_key in self._toolchain_lane_owners
                for record in self._active.values()
            )
            return SchedulerDiagnostics(
                lifecycle=self._lifecycle,
                worker_count=self._config.worker_count,
                pending_capacity=self._config.pending_capacity,
                terminal_receipt_capacity=(
                    self._config.terminal_receipt_capacity
                ),
                pending=pending,
                running=running,
                cancel_requested=cancel_requested,
                full_audio_residency_count=(
                    self._full_audio_residency_count
                ),
                peak_full_audio_residency_count=(
                    self._peak_full_audio_residency_count
                ),
                peak_running_workers=self._peak_running_workers,
                pending_by_kind=pending_by_kind,
                oldest_pending_wait_ms=max(
                    (
                        attempt.wait_ms
                        for attempt in attempts
                        if attempt.state is AttemptState.PENDING
                    ),
                    default=0,
                ),
                blocked_on_run_lane=blocked_on_run_lane,
                blocked_on_toolchain_lane=blocked_on_toolchain_lane,
                attempts=attempts,
                toolchain_lanes=toolchain_lanes,
                terminal_receipts=tuple(self._terminal_receipts),
                counters=tuple(
                    (key.value, self._counters[key])
                    for key in SchedulerCounterKey
                ),
            )

    def shutdown(
        self,
        timeout_seconds: float | None = None,
    ) -> ShutdownReport:
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) is not float
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0.0
            ):
                raise ValueError("timeout_seconds must be a finite positive float")

        timeout = (
            self._config.shutdown_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        pending_handoffs: list[
            tuple[SchedulerDiagnosticEvent, AnalysisOutcome]
        ] = []
        cancel_events: list[SchedulerDiagnosticEvent] = []
        with self._condition:
            if self._shutdown_report is not None:
                return self._shutdown_report
            if self._shutdown_in_progress:
                action = self._shutdown_action
                if (
                    action is not None
                    and getattr(self._callback_context, "depth", 0) > 0
                ):
                    return self._shutdown_progress_report_locked(action)
                self._condition.wait_for(
                    lambda: self._shutdown_report is not None
                )
                return self._shutdown_report

        with self._condition:
            if self._shutdown_report is not None:
                return self._shutdown_report
            if self._shutdown_in_progress:
                action = self._shutdown_action
                if (
                    action is not None
                    and getattr(self._callback_context, "depth", 0) > 0
                ):
                    return self._shutdown_progress_report_locked(action)
                self._condition.wait_for(
                    lambda: self._shutdown_report is not None
                )
                return self._shutdown_report

            started = self._read_clock()
            self._shutdown_in_progress = True
            wall_deadline = time.perf_counter() + timeout
            self._lifecycle = SchedulerLifecycle.SHUTTING_DOWN

            canceled_pending = 0
            for record in tuple(self._pending):
                if (
                    self._active.get(record.job.analysis_attempt_id) is not record
                    or record.state is not AttemptState.PENDING
                ):
                    continue
                self._pending.remove(record)
                del self._active[record.job.analysis_attempt_id]
                record.state = AttemptState.CANCELED
                outcome = AnalysisOutcome(
                    run_id=record.job.run_id,
                    analysis_attempt_id=record.job.analysis_attempt_id,
                    kind=record.job.kind,
                    status=AnalysisStatus.CANCELED,
                    audio_reference_id=record.job.audio.reference_id,
                    result_payload=None,
                    error=self._cancel_error("analysis scheduler shutdown"),
                    submitted_monotonic=record.submitted_monotonic,
                    started_monotonic=None,
                    finished_monotonic=started,
                    queue_wait_ms=self._duration_ms(
                        record.submitted_monotonic,
                        started,
                    ),
                    execution_ms=None,
                    cancel_origin=AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
                )
                self._record_terminal_locked(outcome)
                pending_handoffs.append(
                    (
                        SchedulerDiagnosticEvent(
                            event_kind=(
                                SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
                            ),
                            occurred_monotonic=started,
                            run_id=record.job.run_id,
                            analysis_attempt_id=record.job.analysis_attempt_id,
                            analysis_kind=record.job.kind,
                            audio_reference_id=record.job.audio.reference_id,
                            submission_sequence=record.submission_sequence,
                            state_before=AttemptState.PENDING,
                            state_after=AttemptState.CANCELED,
                            worker_id=None,
                            runtime_lane_key=self._runtime_lane_key(record),
                            queue_wait_ms=outcome.queue_wait_ms,
                            load_ms=None,
                            execution_ms=None,
                            error_category=(
                                AnalysisErrorCategory.ANALYSIS_CANCELED
                            ),
                            error_stage=AnalysisStage.CANCEL,
                            summary="analysis scheduler shutdown",
                        ),
                        outcome,
                    )
                )
                canceled_pending += 1

            cancel_requested_running = 0
            for record in self._active.values():
                if record.state is not AttemptState.RUNNING:
                    continue
                token = record.cancel_token
                if token is None:
                    continue
                record.state = AttemptState.CANCEL_REQUESTED
                record.cancel_origin = AnalysisCancelOrigin.SCHEDULER_SHUTDOWN
                record.cancel_reason = "analysis scheduler shutdown"
                token.cancel(record.cancel_reason)
                load_started = record.load_started_monotonic
                load_finished = record.load_finished_monotonic
                cancel_events.append(
                    SchedulerDiagnosticEvent(
                        event_kind=(
                            SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                        ),
                        occurred_monotonic=started,
                        run_id=record.job.run_id,
                        analysis_attempt_id=record.job.analysis_attempt_id,
                        analysis_kind=record.job.kind,
                        audio_reference_id=record.job.audio.reference_id,
                        submission_sequence=record.submission_sequence,
                        state_before=AttemptState.RUNNING,
                        state_after=AttemptState.CANCEL_REQUESTED,
                        worker_id=record.worker_id,
                        runtime_lane_key=self._runtime_lane_key(record),
                        queue_wait_ms=self._duration_ms(
                            record.submitted_monotonic,
                            record.started_monotonic,
                        ),
                        load_ms=(
                            None
                            if load_started is None or load_finished is None
                            else self._duration_ms(
                                load_started,
                                load_finished,
                            )
                        ),
                        execution_ms=(
                            None
                            if record.execution_started_monotonic is None
                            else self._duration_ms(
                                record.execution_started_monotonic,
                                started,
                            )
                        ),
                        error_category=(
                            AnalysisErrorCategory.ANALYSIS_CANCELED
                        ),
                        error_stage=AnalysisStage.CANCEL,
                        summary="analysis scheduler shutdown",
                    )
                )
                cancel_requested_running += 1
            action = _ShutdownAction(
                owner_thread=threading.current_thread(),
                started_monotonic=started,
                wall_deadline=wall_deadline,
                canceled_pending=canceled_pending,
                cancel_requested_running=cancel_requested_running,
            )
            self._shutdown_action = action
            self._wake_locked()

        for event in cancel_events:
            self._publish_diagnostic(event)
        for event, outcome in pending_handoffs:
            self._publish_diagnostic(event)
            self._publish_outcome(outcome, event)

        return self._finalize_shutdown(action)

    def _shutdown_progress_report_locked(
        self,
        action: _ShutdownAction,
    ) -> ShutdownReport:
        with self._clock_lock:
            finished = max(
                action.started_monotonic,
                self._last_clock_value
                if self._last_clock_value is not None
                else action.started_monotonic,
            )
        return ShutdownReport(
            status=ShutdownStatus.IN_PROGRESS,
            started_monotonic=action.started_monotonic,
            finished_monotonic=finished,
            canceled_pending=action.canceled_pending,
            cancel_requested_running=action.cancel_requested_running,
            remaining_attempt_ids=tuple(sorted(self._active)),
            remaining_worker_ids=tuple(
                worker_id
                for worker_id, worker in enumerate(
                    self._workers,
                    start=1,
                )
                if worker.is_alive()
            ),
            remaining_run_lanes=tuple(sorted(self._run_lane_owners)),
            remaining_toolchain_lanes=tuple(
                sorted(self._toolchain_lane_owners)
            ),
            control_thread_alive=self._control_thread.is_alive(),
        )

    def _finalize_shutdown(
        self,
        action: _ShutdownAction,
    ) -> ShutdownReport:
        with self._condition:
            if self._shutdown_report is not None:
                return self._shutdown_report

        for thread in (self._control_thread, *self._workers):
            if thread is action.owner_thread:
                continue
            remaining = max(
                0.0,
                action.wall_deadline - time.perf_counter(),
            )
            thread.join(min(remaining, threading.TIMEOUT_MAX))

        try:
            finished = self._read_clock()
        except Exception:
            with self._clock_lock:
                finished = max(
                    action.started_monotonic,
                    self._last_clock_value
                    if self._last_clock_value is not None
                    else action.started_monotonic,
                )

        with self._condition:
            if self._shutdown_report is not None:
                return self._shutdown_report
            with self._clock_lock:
                finished = max(
                    action.started_monotonic,
                    finished,
                    self._last_clock_value
                    if self._last_clock_value is not None
                    else action.started_monotonic,
                )
            control_thread_alive = self._control_thread.is_alive()
            remaining_worker_ids = tuple(
                worker_id
                for worker_id, worker in enumerate(
                    self._workers,
                    start=1,
                )
                if worker.is_alive()
            )
            clean = (
                not self._active
                and not self._pending
                and not self._run_lane_owners
                and not self._toolchain_lane_owners
                and len(self._free_worker_ids) == self._config.worker_count
                and self._full_audio_residency_count == 0
                and not control_thread_alive
                and not remaining_worker_ids
            )
            if clean:
                status = ShutdownStatus.CLOSED
                lifecycle_after = SchedulerLifecycle.CLOSED
                remaining_attempt_ids: tuple[str, ...] = ()
                remaining_run_lanes: tuple[str, ...] = ()
                remaining_toolchain_lanes: tuple[str, ...] = ()
            else:
                status = ShutdownStatus.SHUTDOWN_INCOMPLETE
                lifecycle_after = SchedulerLifecycle.SHUTDOWN_INCOMPLETE
                remaining_attempt_ids = tuple(sorted(self._active))
                remaining_run_lanes = tuple(sorted(self._run_lane_owners))
                remaining_toolchain_lanes = tuple(
                    sorted(self._toolchain_lane_owners)
                )
                self._counters[
                    SchedulerCounterKey.SHUTDOWN_INCOMPLETE
                ] += 1

            report = ShutdownReport(
                status=status,
                started_monotonic=action.started_monotonic,
                finished_monotonic=finished,
                canceled_pending=action.canceled_pending,
                cancel_requested_running=(
                    action.cancel_requested_running
                ),
                remaining_attempt_ids=remaining_attempt_ids,
                remaining_worker_ids=remaining_worker_ids,
                remaining_run_lanes=remaining_run_lanes,
                remaining_toolchain_lanes=remaining_toolchain_lanes,
                control_thread_alive=control_thread_alive,
            )
            self._lifecycle = lifecycle_after
            self._shutdown_report = report
            self._shutdown_in_progress = False
            self._shutdown_action = None
            self._wake_locked()

        if status is ShutdownStatus.SHUTDOWN_INCOMPLETE:
            self._publish_diagnostic(
                SchedulerDiagnosticEvent(
                    event_kind=(
                        SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
                    ),
                    occurred_monotonic=finished,
                    lifecycle_before=SchedulerLifecycle.SHUTTING_DOWN,
                    lifecycle_after=(
                        SchedulerLifecycle.SHUTDOWN_INCOMPLETE
                    ),
                    error_category=(
                        AnalysisErrorCategory.SHUTDOWN_INCOMPLETE
                    ),
                    error_stage=AnalysisStage.SHUTDOWN,
                    summary="analysis scheduler shutdown incomplete",
                )
            )
        return report
