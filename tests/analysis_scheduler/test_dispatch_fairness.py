"""Deterministic resource-aware FIFO and lane-serialization tests."""

from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest

from app.service.analysis.scheduler import (
    AnalysisErrorCategory,
    AnalysisKind,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStatus,
    ShutdownStatus,
    SubmitStatus,
)

from .fakes import (
    ControlDeadlineClock,
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    PerAttemptGatedExecutor,
    RecordingAudioLoader,
    make_job,
)


def make_scheduler(
    *,
    worker_count: int,
    executor: PerAttemptGatedExecutor,
    clock: ManualClock | None = None,
    pending_capacity: int = 32,
) -> tuple[AnalysisScheduler, OutcomeCollector]:
    sink = OutcomeCollector()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            pending_capacity=pending_capacity,
        ),
        audio_loader=RecordingAudioLoader(),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=sink,
        clock=clock or ManualClock(1.0),
    )
    return scheduler, sink


def submit_accepted(scheduler: AnalysisScheduler, *jobs) -> None:
    for job in jobs:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED


def test_no_conflict_jobs_dispatch_fifo_and_later_submit_cannot_displace():
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-2", "attempt-3", "attempt-4")
    scheduler, sink = make_scheduler(worker_count=1, executor=executor)
    try:
        submit_accepted(scheduler, make_job(1))
        executor.wait_for_attempt("attempt-1")
        submit_accepted(scheduler, make_job(2), make_job(3))

        executor.release("attempt-1")
        executor.wait_for_attempt("attempt-2")
        submit_accepted(scheduler, make_job(4))
        assert executor.called_attempt_ids() == ("attempt-1", "attempt-2")

        executor.release("attempt-2")
        executor.wait_for_attempt("attempt-3")
        assert executor.called_attempt_ids() == (
            "attempt-1",
            "attempt-2",
            "attempt-3",
        )

        executor.release("attempt-3")
        executor.wait_for_attempt("attempt-4")
    finally:
        executor.release_all()
        sink.wait_for_count(4)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert executor.called_attempt_ids() == (
        "attempt-1",
        "attempt-2",
        "attempt-3",
        "attempt-4",
    )


@pytest.mark.parametrize("worker_count", range(1, 7))
def test_configured_worker_limit_is_reached_and_never_exceeded(worker_count):
    executor = PerAttemptGatedExecutor()
    attempt_ids = tuple(
        f"attempt-{number}" for number in range(1, worker_count + 2)
    )
    executor.hold(*attempt_ids)
    scheduler, sink = make_scheduler(
        worker_count=worker_count,
        executor=executor,
    )
    try:
        submit_accepted(
            scheduler,
            *(make_job(number) for number in range(1, worker_count + 2)),
        )
        executor.wait_for_calls(worker_count)
        assert len(executor.active_attempt_ids) == worker_count
        assert executor.peak_active == worker_count
    finally:
        executor.release_all()
        sink.wait_for_count(worker_count + 1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert executor.peak_active == worker_count


def test_same_run_serializes_in_submission_order_while_unrelated_runs_progress():
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-2", "attempt-3", "attempt-4")
    scheduler, sink = make_scheduler(worker_count=2, executor=executor)
    try:
        submit_accepted(
            scheduler,
            make_job(1, run_id="run-shared", kind=AnalysisKind.AUTOMATIC),
        )
        executor.wait_for_attempt("attempt-1")
        submit_accepted(
            scheduler,
            make_job(2, run_id="run-shared", kind=AnalysisKind.REANALYSIS),
            make_job(3, run_id="run-unrelated-1"),
            make_job(4, run_id="run-unrelated-2"),
        )
        executor.wait_for_attempt("attempt-3")

        executor.release("attempt-3")
        sink.wait_for_attempt("attempt-3")
        executor.wait_for_attempt("attempt-4")
        assert "attempt-2" not in executor.called_attempt_ids()
        assert executor.run_overlap_attempt_ids == []

        executor.release("attempt-1")
        sink.wait_for_attempt("attempt-1")
        executor.wait_for_attempt("attempt-2")
    finally:
        executor.release_all()
        sink.wait_for_count(4)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    same_run_order = tuple(
        call[0].analysis_attempt_id
        for call in executor.calls
        if call[0].run_id == "run-shared"
    )
    assert same_run_order == ("attempt-1", "attempt-2")
    assert executor.run_overlap_attempt_ids == []


def test_canonical_toolchain_lane_serializes_all_kinds_and_revisions():
    executor = PerAttemptGatedExecutor()
    executor.hold(
        "attempt-1",
        "attempt-2",
        "attempt-3",
        "attempt-4",
        "attempt-5",
    )
    scheduler, sink = make_scheduler(worker_count=3, executor=executor)
    try:
        submit_accepted(
            scheduler,
            make_job(
                1,
                kind=AnalysisKind.AUTOMATIC,
                toolchain_id="shared-toolchain",
                execution_revision="revision-a",
            ),
        )
        executor.wait_for_attempt("attempt-1")
        submit_accepted(
            scheduler,
            make_job(
                2,
                kind=AnalysisKind.REANALYSIS,
                toolchain_id="shared-toolchain",
                execution_revision="revision-b",
            ),
            make_job(
                3,
                kind=AnalysisKind.IMPORTED,
                toolchain_id="shared-toolchain",
                execution_revision="revision-c",
            ),
            make_job(4, toolchain_id="different-toolchain"),
            make_job(5),
        )
        executor.wait_for_calls(3)
        assert set(executor.called_attempt_ids()) == {
            "attempt-1",
            "attempt-4",
            "attempt-5",
        }
        assert executor.peak_active == 3
        assert executor.toolchain_busy_attempt_ids == []

        executor.release("attempt-4", "attempt-5")
        sink.wait_for_count(2)
        executor.release("attempt-1")
        executor.wait_for_attempt("attempt-2")
        assert "attempt-3" not in executor.called_attempt_ids()
        executor.release("attempt-2")
        executor.wait_for_attempt("attempt-3")
    finally:
        executor.release_all()
        outcomes = sink.wait_for_count(5)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    shared_lane_order = tuple(
        call[0].analysis_attempt_id
        for call in executor.calls
        if (
            call[0].snapshot.ai_toolchain is not None
            and call[0].snapshot.ai_toolchain.toolchain_id
            == "shared-toolchain"
        )
    )
    assert shared_lane_order == ("attempt-1", "attempt-2", "attempt-3")
    assert executor.toolchain_busy_attempt_ids == []
    assert all(outcome.status is AnalysisStatus.SUCCEEDED for outcome in outcomes)


def test_same_toolchain_later_job_cannot_overtake_earlier_run_blocked_job():
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-2", "attempt-3", "attempt-4")
    scheduler, sink = make_scheduler(worker_count=2, executor=executor)
    try:
        submit_accepted(
            scheduler,
            make_job(1, run_id="run-a", toolchain_id="lane-x"),
        )
        executor.wait_for_attempt("attempt-1")
        submit_accepted(
            scheduler,
            make_job(2, run_id="run-a", toolchain_id="lane-y"),
            make_job(3, run_id="run-b", toolchain_id="lane-y"),
            make_job(4, run_id="run-c", toolchain_id="lane-z"),
        )
        executor.wait_for_attempt("attempt-4")
        assert executor.called_attempt_ids() == ("attempt-1", "attempt-4")

        executor.release("attempt-4")
        sink.wait_for_attempt("attempt-4")
        assert "attempt-3" not in executor.called_attempt_ids()

        executor.release("attempt-1")
        executor.wait_for_attempt("attempt-2")
        assert "attempt-3" not in executor.called_attempt_ids()
        executor.release("attempt-2")
        executor.wait_for_attempt("attempt-3")
    finally:
        executor.release_all()
        sink.wait_for_count(4)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert executor.toolchain_busy_attempt_ids == []


def test_resource_blocked_head_is_bypassed_then_runs_after_resources_release():
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-2", "attempt-3")
    scheduler, sink = make_scheduler(worker_count=2, executor=executor)
    try:
        submit_accepted(
            scheduler,
            make_job(1, run_id="run-a", toolchain_id="lane-x"),
        )
        executor.wait_for_attempt("attempt-1")
        submit_accepted(
            scheduler,
            make_job(2, run_id="run-a", toolchain_id="lane-x"),
            make_job(3, run_id="run-b", toolchain_id="lane-y"),
        )
        executor.wait_for_attempt("attempt-3")
        assert executor.called_attempt_ids() == ("attempt-1", "attempt-3")

        executor.release("attempt-3")
        sink.wait_for_attempt("attempt-3")
        assert "attempt-2" not in executor.called_attempt_ids()

        executor.release("attempt-1")
        executor.wait_for_attempt("attempt-2")
    finally:
        executor.release_all()
        sink.wait_for_count(3)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_submit_completion_and_queue_deadline_each_wake_dispatch_control():
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-3")
    clock = ControlDeadlineClock(
        advance_on_control_read=3,
        deadline_value=10.0,
    )
    scheduler, sink = make_scheduler(
        worker_count=1,
        executor=executor,
        clock=clock,
    )
    try:
        submit_accepted(scheduler, make_job(1, run_id="run-blocked"))
        executor.wait_for_attempt("attempt-1")

        submit_accepted(
            scheduler,
            make_job(
                2,
                run_id="run-blocked",
                queue_timeout_seconds=0.001,
            ),
        )
        assert clock.deadline_observed.wait(1.0)
        timed_out = next(
            outcome
            for outcome in sink.wait_for_attempt("attempt-2")
            if outcome.analysis_attempt_id == "attempt-2"
        )
        assert timed_out.status is AnalysisStatus.FAILED
        assert timed_out.error is not None
        assert (
            timed_out.error.category
            is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
        )

        submit_accepted(scheduler, make_job(3))
        executor.release("attempt-1")
        sink.wait_for_attempt("attempt-1")
        executor.wait_for_attempt("attempt-3")
    finally:
        executor.release_all()
        sink.wait_for_count(3)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_stale_worker_identity_cannot_release_run_or_toolchain_lane(monkeypatch):
    original = AnalysisScheduler._execute_successful_attempt
    stale_returned = Event()
    allow_authoritative = Event()

    def execute_stale_then_authoritative(self, worker_id, command):
        if not stale_returned.is_set():
            stale = replace(command, instance_epoch=object())
            original(self, worker_id, stale)
            stale_returned.set()
            assert allow_authoritative.wait(10.0)
        return original(self, worker_id, command)

    monkeypatch.setattr(
        AnalysisScheduler,
        "_execute_successful_attempt",
        execute_stale_then_authoritative,
    )
    executor = PerAttemptGatedExecutor()
    executor.hold("attempt-1", "attempt-2", "attempt-3")
    scheduler, sink = make_scheduler(worker_count=2, executor=executor)
    try:
        submit_accepted(
            scheduler,
            make_job(1, run_id="run-a", toolchain_id="lane-x"),
        )
        executor.wait_for_attempt("attempt-1")
        executor.release("attempt-1")
        assert stale_returned.wait(1.0)

        submit_accepted(
            scheduler,
            make_job(2, run_id="run-a", toolchain_id="lane-x"),
            make_job(3, run_id="run-unrelated"),
        )
        executor.wait_for_attempt("attempt-3")
        assert "attempt-2" not in executor.called_attempt_ids()

        allow_authoritative.set()
        sink.wait_for_attempt("attempt-1")
        executor.wait_for_attempt("attempt-2")
    finally:
        allow_authoritative.set()
        executor.release_all()
        sink.wait_for_count(3)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert executor.called_attempt_ids().count("attempt-1") == 2
    assert executor.run_overlap_attempt_ids == []
    assert executor.toolchain_busy_attempt_ids == []
