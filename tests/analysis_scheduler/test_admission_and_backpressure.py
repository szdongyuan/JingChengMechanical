from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import threading
from pathlib import Path

import pytest

import app.service.analysis.scheduler as scheduler_contracts
import app.service.analysis.scheduler.scheduler as scheduler_module
from app.service.analysis.scheduler import (
    AnalysisErrorCategory,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStatus,
    ShutdownStatus,
    SubmitStatus,
)
from tests.analysis_scheduler.fakes import (
    BlockingThreadFaultClock,
    GatedAudioLoader,
    ManualClock,
    NeverInvokePort,
    OutcomeCollector,
    PassThroughStartGate,
    RecordingAudioLoader,
    RecordingExecutor,
    ThreadFaultClock,
    make_job,
)


def make_scheduler(
    *,
    config: AnalysisSchedulerConfig | None = None,
    loader: object | None = None,
    executor: object | None = None,
    start_gate: object | None = None,
    sink: object | None = None,
    clock: object | None = None,
    diagnostic_sink: object | None = None,
) -> tuple[
    AnalysisScheduler,
    RecordingAudioLoader,
    RecordingExecutor,
    OutcomeCollector,
    ManualClock,
]:
    actual_loader = loader or RecordingAudioLoader()
    actual_executor = executor or RecordingExecutor()
    actual_start_gate = start_gate or PassThroughStartGate()
    actual_sink = sink or OutcomeCollector()
    actual_clock = clock or ManualClock()
    scheduler = AnalysisScheduler(
        config=config or AnalysisSchedulerConfig(),
        audio_loader=actual_loader,
        analysis_executor=actual_executor,
        start_gate=actual_start_gate,
        outcome_sink=actual_sink,
        clock=actual_clock,
        diagnostic_sink=diagnostic_sink,
    )
    return (
        scheduler,
        actual_loader,
        actual_executor,
        actual_sink,
        actual_clock,
    )


def test_constructor_and_public_method_signatures_are_exact():
    assert scheduler_contracts.__all__[0] == "AnalysisScheduler"
    assert scheduler_contracts.AnalysisScheduler is AnalysisScheduler
    assert str(inspect.signature(AnalysisScheduler)) == (
        "(*, config: 'AnalysisSchedulerConfig', audio_loader: 'AudioLoader', "
        "analysis_executor: 'AnalysisExecutor', start_gate: 'AnalysisStartGate', "
        "outcome_sink: 'OutcomeSink', "
        "clock: 'MonotonicClock', diagnostic_sink: 'DiagnosticSink | None' = None) "
        "-> 'None'"
    )
    assert str(inspect.signature(AnalysisScheduler.submit)) == (
        "(self, job: 'AnalysisJob') -> 'SubmitReceipt'"
    )
    assert str(inspect.signature(AnalysisScheduler.cancel)) == (
        "(self, run_id: 'str', analysis_attempt_id: 'str', "
        "origin: 'AnalysisCancelOrigin', reason: 'str') -> 'CancelReceipt'"
    )
    assert str(inspect.signature(AnalysisScheduler.shutdown)) == (
        "(self, timeout_seconds: 'float | None' = None) -> 'ShutdownReport'"
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda dependencies: AnalysisScheduler(*dependencies),
        lambda dependencies: AnalysisScheduler(
            config=dependencies[0],
            audio_loader=dependencies[1],
            analysis_executor=dependencies[2],
            start_gate=dependencies[3],
            outcome_sink=dependencies[4],
            clock=dependencies[5],
            epoch="forbidden",
        ),
        lambda dependencies: AnalysisScheduler(
            config=dependencies[0],
            audio_loader=dependencies[1],
            analysis_executor=dependencies[2],
            start_gate=dependencies[3],
            outcome_sink=dependencies[4],
            clock=dependencies[5],
            generation=1,
        ),
        lambda dependencies: AnalysisScheduler(
            config=dependencies[0],
            audio_loader=dependencies[1],
            analysis_executor=dependencies[2],
            start_gate=dependencies[3],
            outcome_sink=dependencies[4],
            clock=dependencies[5],
            database=object(),
        ),
    ],
)
def test_constructor_rejects_positional_and_extra_wiring_arguments(call):
    dependencies = (
        AnalysisSchedulerConfig(),
        RecordingAudioLoader(),
        RecordingExecutor(),
        PassThroughStartGate(),
        OutcomeCollector(),
        ManualClock(),
    )
    with pytest.raises(TypeError):
        call(dependencies)


def test_constructor_rejects_each_missing_required_dependency():
    valid = {
        "config": AnalysisSchedulerConfig(),
        "audio_loader": RecordingAudioLoader(),
        "analysis_executor": RecordingExecutor(),
        "start_gate": PassThroughStartGate(),
        "outcome_sink": OutcomeCollector(),
        "clock": ManualClock(),
    }
    for missing in tuple(valid):
        arguments = dict(valid)
        del arguments[missing]
        with pytest.raises(TypeError):
            AnalysisScheduler(**arguments)


def test_constructor_rechecks_bypassed_config_fields_before_starting_threads(
    monkeypatch,
):
    invalid = object.__new__(AnalysisSchedulerConfig)
    object.__setattr__(invalid, "worker_count", 0)
    object.__setattr__(invalid, "pending_capacity", 1)
    object.__setattr__(invalid, "terminal_receipt_capacity", 1)
    object.__setattr__(invalid, "shutdown_timeout_seconds", 1.0)
    started = []
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda thread: started.append(thread),
    )
    with pytest.raises(TypeError):
        AnalysisScheduler(
            config=invalid,
            audio_loader=RecordingAudioLoader(),
            analysis_executor=RecordingExecutor(),
            start_gate=PassThroughStartGate(),
            outcome_sink=OutcomeCollector(),
            clock=ManualClock(),
        )
    assert started == []


@pytest.mark.parametrize(
    ("dependency", "replacement"),
    [
        ("config", None),
        ("config", object()),
        ("audio_loader", None),
        ("audio_loader", object()),
        ("audio_loader", type("BadLoader", (), {"open": None})()),
        ("analysis_executor", None),
        ("analysis_executor", object()),
        ("analysis_executor", type("BadExecutor", (), {"execute": None})()),
        ("start_gate", None),
        ("start_gate", object()),
        ("start_gate", type("BadStartGate", (), {"before_audio_load": None})()),
        ("outcome_sink", None),
        ("outcome_sink", object()),
        ("clock", None),
        ("clock", object()),
        ("clock", type("BadClock", (), {"monotonic": None})()),
        ("diagnostic_sink", object()),
    ],
)
def test_constructor_dependency_failures_are_type_errors_before_threads_start(
    monkeypatch,
    dependency,
    replacement,
):
    starts = 0

    def unexpected_start(self):
        nonlocal starts
        starts += 1

    monkeypatch.setattr(threading.Thread, "start", unexpected_start)
    values = {
        "config": AnalysisSchedulerConfig(),
        "audio_loader": RecordingAudioLoader(),
        "analysis_executor": RecordingExecutor(),
        "start_gate": PassThroughStartGate(),
        "outcome_sink": OutcomeCollector(),
        "clock": ManualClock(),
        "diagnostic_sink": None,
    }
    values[dependency] = replacement
    with pytest.raises(TypeError):
        AnalysisScheduler(**values)
    assert starts == 0


def test_constructor_checks_ports_without_invoking_them_or_clock():
    port = NeverInvokePort()
    clock = ManualClock()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(),
        audio_loader=port,
        analysis_executor=port,
        start_gate=port,
        outcome_sink=port,
        clock=clock,
        diagnostic_sink=port,
    )
    try:
        assert port.calls == 0
        assert clock.call_count == 0
    finally:
        report = scheduler.shutdown()
        assert report.status is ShutdownStatus.CLOSED


def test_construction_starts_exact_fixed_worker_count_and_cleans_partial_start(
    monkeypatch,
):
    real_start = threading.Thread.start
    started_workers: list[threading.Thread] = []

    def fail_on_third_worker(thread):
        if "analysis-scheduler-worker-" in thread.name:
            started_workers.append(thread)
            if len(started_workers) == 3:
                raise RuntimeError("injected worker start failure")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_on_third_worker)
    with pytest.raises(RuntimeError, match="injected worker start failure"):
        make_scheduler(config=AnalysisSchedulerConfig(worker_count=4))
    assert len(started_workers) == 3
    assert all(not thread.is_alive() for thread in started_workers[:2])


@pytest.mark.parametrize("worker_count", range(1, 7))
def test_construction_creates_exact_configured_worker_count(
    monkeypatch,
    worker_count,
):
    real_start = threading.Thread.start
    worker_names: list[str] = []

    def record_start(thread):
        if "analysis-scheduler-worker-" in thread.name:
            worker_names.append(thread.name)
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", record_start)
    scheduler, _, _, _, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=worker_count)
    )
    try:
        assert len(worker_names) == worker_count
        assert len(set(worker_names)) == worker_count
    finally:
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_submit_signature_errors_do_not_mutate_sequence():
    scheduler, _, executor, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=4),
        executor=RecordingExecutor(gated=True),
    )
    first = make_job(1)
    second = make_job(2)
    try:
        with pytest.raises(TypeError):
            scheduler.submit()
        with pytest.raises(TypeError):
            scheduler.submit(first, second)
        with pytest.raises(TypeError):
            scheduler.submit(task=first)
        with pytest.raises(TypeError):
            scheduler.submit(job=first, extra=True)

        receipt = scheduler.submit(job=first)
        assert receipt.status is SubmitStatus.ACCEPTED
        assert receipt.submission_sequence == 1
        executor.wait_for_calls(1)
    finally:
        executor.release()
        sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_submit_is_nonblocking_and_does_not_call_ports_inline():
    loader = RecordingAudioLoader()
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler, _, _, _, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1),
        loader=loader,
        executor=executor,
        sink=sink,
    )
    completed = threading.Event()
    receipts = []
    submit_thread_ids = []

    def submit() -> None:
        submit_thread_ids.append(threading.get_ident())
        receipts.append(scheduler.submit(make_job(1)))
        completed.set()

    thread = threading.Thread(target=submit)
    try:
        thread.start()
        assert completed.wait(1.0), "submit waited for worker execution"
        thread.join(1.0)
        assert receipts[0].status is SubmitStatus.ACCEPTED
        assert sink.outcomes == []
        executor.wait_for_calls(1)
        assert submit_thread_ids[0] not in loader.call_thread_ids
        assert submit_thread_ids[0] not in executor.call_thread_ids
    finally:
        executor.release()
        sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_sequences_increase_and_config_id_is_not_an_identity_key():
    scheduler, _, executor, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=3),
        executor=RecordingExecutor(gated=True),
    )
    jobs = [
        make_job(number, config_snapshot_json='{"config_id":"same"}')
        for number in range(1, 4)
    ]
    try:
        receipts = [scheduler.submit(job) for job in jobs]
        assert [receipt.status for receipt in receipts] == [
            SubmitStatus.ACCEPTED,
        ] * 3
        assert [receipt.submission_sequence for receipt in receipts] == [1, 2, 3]
        executor.wait_for_calls(1)
    finally:
        executor.release()
        sink.wait_for_count(3)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def _bypassed_invalid_job():
    valid = make_job(99)
    invalid = object.__new__(type(valid))
    for field in (
        "run_id",
        "analysis_attempt_id",
        "kind",
        "audio",
        "snapshot",
        "execution_timeout_seconds",
        "queue_timeout_seconds",
    ):
        object.__setattr__(invalid, field, getattr(valid, field))
    object.__setattr__(invalid, "run_id", object())
    return invalid


def test_invalid_and_duplicate_active_jobs_return_exact_receipts():
    scheduler, _, executor, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        executor=RecordingExecutor(gated=True),
    )
    first = make_job(1)
    try:
        invalid = scheduler.submit(_bypassed_invalid_job())
        assert invalid.status is SubmitStatus.INVALID_JOB
        assert invalid.run_id == ""
        assert invalid.analysis_attempt_id == "attempt-99"
        assert invalid.submission_sequence is None
        assert invalid.error.category is AnalysisErrorCategory.INVALID_JOB
        assert invalid.error.stage is AnalysisStage.ADMISSION
        assert invalid.error.details == ()

        accepted = scheduler.submit(first)
        duplicate = scheduler.submit(first)
        assert accepted.submission_sequence == 1
        assert duplicate.status is SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID
        assert duplicate.submission_sequence is None
        assert (
            duplicate.error.category
            is AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID
        )
        assert duplicate.error.stage is AnalysisStage.ADMISSION
        executor.wait_for_calls(1)
    finally:
        executor.release()
        sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_queue_capacity_is_a_nonblocking_hard_bound():
    executor = RecordingExecutor(gated=True)
    scheduler, loader, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        executor=executor,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        assert scheduler.submit(make_job(3)).status is SubmitStatus.ACCEPTED
        full = scheduler.submit(make_job(4))
        assert full.status is SubmitStatus.QUEUE_FULL
        assert full.error.category is AnalysisErrorCategory.ANALYSIS_QUEUE_FULL
        assert full.error.stage is AnalysisStage.ADMISSION
        assert len(loader.calls) == 1
    finally:
        executor.release()
        outcomes = sink.wait_for_count(3)
        assert {outcome.analysis_attempt_id for outcome in outcomes} == {
            "attempt-1",
            "attempt-2",
            "attempt-3",
        }
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_concurrent_submitters_never_overflow_pending_capacity():
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        executor=executor,
    )
    barrier = threading.Barrier(9)
    receipts = []
    receipts_lock = threading.Lock()

    def submit(number: int) -> None:
        barrier.wait()
        receipt = scheduler.submit(make_job(number))
        with receipts_lock:
            receipts.append(receipt)

    threads = [
        threading.Thread(target=submit, args=(number,))
        for number in range(2, 10)
    ]
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1.0)
            assert not thread.is_alive()
        accepted_pending = [
            receipt
            for receipt in receipts
            if receipt.status is SubmitStatus.ACCEPTED
        ]
        assert len(accepted_pending) == 2
        assert len(receipts) == 8
        assert sum(
            receipt.status is SubmitStatus.QUEUE_FULL for receipt in receipts
        ) == 6
    finally:
        executor.release()
        sink.wait_for_count(3)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_queue_deadline_expires_atomically_and_releases_capacity():
    clock = ManualClock(0.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert (
            scheduler.submit(make_job(2, queue_timeout_seconds=5.0)).status
            is SubmitStatus.ACCEPTED
        )
        clock.set(5.0)
        wake = scheduler.submit(make_job(3))
        assert wake.status is SubmitStatus.QUEUE_FULL
        sink.wait_for_count(1)
        replacement = scheduler.submit(make_job(3))
        assert replacement.status is SubmitStatus.ACCEPTED
        assert replacement.submission_sequence == 3
    finally:
        executor.release()
        outcomes = sink.wait_for_count(3)
        by_id = {outcome.analysis_attempt_id: outcome for outcome in outcomes}
        timed_out = by_id["attempt-2"]
        assert timed_out.status is AnalysisStatus.FAILED
        assert (
            timed_out.error.category
            is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
        )
        assert timed_out.error.stage is AnalysisStage.QUEUE
        assert timed_out.started_monotonic is None
        assert timed_out.execution_ms is None
        assert sum(
            outcome.analysis_attempt_id == "attempt-2"
            for outcome in outcomes
        ) == 1
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_job_without_queue_deadline_does_not_expire():
    clock = ManualClock(0.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        clock.set(10_000.0)
        full = scheduler.submit(make_job(3))
        assert full.status is SubmitStatus.QUEUE_FULL
    finally:
        executor.release()
        outcomes = sink.wait_for_count(2)
        assert {outcome.analysis_attempt_id for outcome in outcomes} == {
            "attempt-1",
            "attempt-2",
        }
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


@pytest.mark.parametrize(
    ("bad_value", "exception"),
    [
        (True, TypeError),
        (1, TypeError),
        (-0.1, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (-math.inf, ValueError),
    ],
)
def test_checked_clock_rejects_invalid_runtime_values_without_mutation(
    bad_value,
    exception,
):
    clock = ManualClock(0.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=3),
        executor=executor,
        clock=clock,
    )
    try:
        first = scheduler.submit(make_job(1))
        assert first.submission_sequence == 1
        executor.wait_for_calls(1)
        clock.set(bad_value)
        with pytest.raises(exception):
            scheduler.submit(make_job(2))
        assert sink.outcomes == []
        clock.set(1.0)
        second = scheduler.submit(make_job(2))
        assert second.submission_sequence == 2
    finally:
        clock.set(2.0)
        executor.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_checked_clock_rejects_decrease_then_recovers_without_killing_threads():
    clock = ManualClock(1.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=3),
        executor=executor,
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).submission_sequence == 1
        executor.wait_for_calls(1)
        clock.set(0.5)
        with pytest.raises(ValueError):
            scheduler.submit(make_job(2))
        clock.set(1.0)
        assert scheduler.submit(make_job(2)).submission_sequence == 2
    finally:
        clock.set(2.0)
        executor.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_scheduler_has_one_checked_clock_call_site_and_no_bypass():
    path = Path(scheduler_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "monotonic"
    ]
    assert len(calls) == 1
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AnalysisScheduler"
    )
    checked_methods = [
        node
        for node in helper.body
        if isinstance(node, ast.FunctionDef) and node.name == "_read_clock"
    ]
    assert len(checked_methods) == 1
    assert calls[0] in list(ast.walk(checked_methods[0]))


def test_fully_drained_shutdown_closes_and_later_submit_is_not_accepting():
    scheduler, _, _, sink, _ = make_scheduler()
    try:
        accepted = scheduler.submit(make_job(1))
        assert accepted.status is SubmitStatus.ACCEPTED
        sink.wait_for_count(1)
        report = scheduler.shutdown()
        assert report.status is ShutdownStatus.CLOSED
        assert report.remaining_attempt_ids == ()
        rejected = scheduler.submit(make_job(2))
        assert rejected.status is SubmitStatus.NOT_ACCEPTING
        assert (
            rejected.error.category
            is AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING
        )
    finally:
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_empty_shutdown_closes_without_invoking_loader_executor_or_sink():
    loader = RecordingAudioLoader()
    executor = RecordingExecutor()
    sink = OutcomeCollector()
    scheduler, _, _, _, _ = make_scheduler(
        loader=loader,
        executor=executor,
        sink=sink,
    )
    report = scheduler.shutdown()
    assert report.status is ShutdownStatus.CLOSED
    assert loader.calls == []
    assert executor.calls == []
    assert sink.outcomes == []


def test_scheduler_source_has_no_forbidden_global_service_or_overflow_queue():
    source = Path(scheduler_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "TestManager",
        "TestSession",
        "ServiceContainer",
        "FactoryModeController",
        "AiService",
        "EventBus",
        "peewee",
        "database",
        "config_id",
        "overflow",
        "Future",
    ):
        assert forbidden not in source


def test_worker_transient_invalid_clock_is_contained_and_recovers_on_notification(
    monkeypatch,
):
    clock = ThreadFaultClock("analysis-scheduler-worker-", True)
    escaped: list[BaseException] = []
    uncaught = []
    original = AnalysisScheduler._execute_successful_attempt

    def retry_only_to_keep_the_red_scheduler_drainable(self, worker_id, command):
        while True:
            try:
                return original(self, worker_id, command)
            except (TypeError, ValueError) as error:
                escaped.append(error)

    monkeypatch.setattr(
        AnalysisScheduler,
        "_execute_successful_attempt",
        retry_only_to_keep_the_red_scheduler_drainable,
    )
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        assert clock.fault_observed.wait(1.0)
        calls_before_wake = clock.matching_call_count
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        outcomes = sink.wait_for_count(2)
        assert {outcome.analysis_attempt_id for outcome in outcomes} == {
            "attempt-1",
            "attempt-2",
        }
        assert calls_before_wake == 1
        assert escaped == []
        assert uncaught == []
    finally:
        clock.set(2.0)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_control_transient_invalid_clock_waits_for_notification_and_recovers(
    monkeypatch,
):
    clock = ThreadFaultClock("analysis-scheduler-control-", 1)
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        assert clock.fault_observed.wait(1.0)
        assert clock.matching_call_count == 1
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        sink.wait_for_count(2)
        assert clock.matching_call_count >= 2
        assert uncaught == []
    finally:
        clock.set(2.0)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_extreme_finite_queue_timing_cannot_corrupt_timeout_terminalization(
    monkeypatch,
):
    clock = ManualClock(0.0)
    escaped: list[BaseException] = []
    uncaught = []
    original = AnalysisScheduler._control_loop
    start_control = threading.Event()

    def retry_only_to_keep_the_red_scheduler_drainable(self):
        assert start_control.wait(1.0)
        while True:
            try:
                return original(self)
            except (OverflowError, TypeError, ValueError) as error:
                escaped.append(error)

    monkeypatch.setattr(AnalysisScheduler, "_control_loop", retry_only_to_keep_the_red_scheduler_drainable)
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        clock=clock,
    )
    try:
        assert (
            scheduler.submit(make_job(1, queue_timeout_seconds=1.0)).status
            is SubmitStatus.ACCEPTED
        )
        clock.set(1e308)
        start_control.set()
        outcomes = sink.wait_for_count(1, timeout_seconds=1.0)
        replacement = scheduler.submit(make_job(2))
        assert replacement.status is SubmitStatus.ACCEPTED
        assert replacement.submission_sequence == 2
        outcomes = sink.wait_for_count(2)
    finally:
        start_control.set()
        clock.set(1e308)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED
    by_id = {outcome.analysis_attempt_id: outcome for outcome in outcomes}
    assert by_id["attempt-1"].status is AnalysisStatus.FAILED
    assert (
        by_id["attempt-1"].error.category
        is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
    )
    assert sum(
        outcome.analysis_attempt_id == "attempt-1" for outcome in outcomes
    ) == 1
    assert escaped == []
    assert uncaught == []


def test_extreme_finite_submission_and_queue_timeout_do_not_overflow_deadline():
    clock = ManualClock(1e308)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert (
            scheduler.submit(
                make_job(2, queue_timeout_seconds=1e308)
            ).status
            is SubmitStatus.ACCEPTED
        )
    finally:
        executor.release()
        outcomes = sink.wait_for_count(2)
        assert all(outcome.status is AnalysisStatus.SUCCEEDED for outcome in outcomes)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_closed_rejection_precedes_invalid_clock_without_reading_port():
    clock = ManualClock(1.0)
    scheduler, _, _, _, _ = make_scheduler(clock=clock)
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED
    calls_before = clock.call_count
    clock.set(True)
    receipt = scheduler.submit(make_job(1))
    assert receipt.status is SubmitStatus.NOT_ACCEPTING
    assert receipt.submission_sequence is None
    assert clock.call_count == calls_before
    clock.set(2.0)
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_duplicate_rejection_precedes_invalid_clock_without_sequence_change():
    clock = ManualClock(1.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        executor=executor,
        clock=clock,
    )
    first = make_job(1)
    second_accepted = False
    try:
        assert scheduler.submit(first).submission_sequence == 1
        executor.wait_for_calls(1)
        calls_before = clock.call_count
        clock.set(True)
        duplicate = scheduler.submit(first)
        assert duplicate.status is SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID
        assert duplicate.submission_sequence is None
        assert clock.call_count == calls_before
        clock.set(1.0)
        assert scheduler.submit(make_job(2)).submission_sequence == 2
        second_accepted = True
    finally:
        clock.set(2.0)
        if not second_accepted:
            assert scheduler.submit(make_job(2)).submission_sequence == 2
        executor.release()
        try:
            sink.wait_for_count(2)
        finally:
            assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_queue_full_rejection_precedes_invalid_clock_without_sequence_change():
    clock = ManualClock(1.0)
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
        clock=clock,
    )
    try:
        assert scheduler.submit(make_job(1)).submission_sequence == 1
        executor.wait_for_calls(1)
        assert scheduler.submit(make_job(2)).submission_sequence == 2
        calls_before = clock.call_count
        clock.set(True)
        full = scheduler.submit(make_job(3))
        assert full.status is SubmitStatus.QUEUE_FULL
        assert full.submission_sequence is None
        assert clock.call_count == calls_before
    finally:
        clock.set(2.0)
        executor.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_stale_dispatch_identity_cannot_publish_or_free_worker_ownership(
    monkeypatch,
):
    original = AnalysisScheduler._execute_successful_attempt
    injected = threading.Event()

    def execute_stale_then_authoritative(self, worker_id, command):
        if not injected.is_set():
            injected.set()
            stale = dataclasses.replace(command, instance_epoch=object())
            original(self, worker_id, stale)
        return original(self, worker_id, command)

    monkeypatch.setattr(
        AnalysisScheduler,
        "_execute_successful_attempt",
        execute_stale_then_authoritative,
    )
    executor = RecordingExecutor(gated=True)
    scheduler, loader, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
    )
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        executor.release()
        outcomes = sink.wait_for_attempt("attempt-2")
        assert [outcome.analysis_attempt_id for outcome in outcomes].count(
            "attempt-1"
        ) == 1
        assert [outcome.analysis_attempt_id for outcome in outcomes].count(
            "attempt-2"
        ) == 1
        assert all(handle.close_count == 1 for handle in loader.handles)
    finally:
        executor.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def _wake_only_to_keep_the_red_scheduler_drainable(
    scheduler: AnalysisScheduler,
) -> None:
    with scheduler._condition:
        scheduler._wake_locked()


def test_queue_full_notification_recovers_control_clock_fault_at_full_capacity(
    monkeypatch,
):
    clock = BlockingThreadFaultClock("analysis-scheduler-control-", True)
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        clock=clock,
    )
    recovered_without_test_help = True
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        assert clock.entered.wait(1.0)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.QUEUE_FULL
        clock.release()
        try:
            sink.wait_for_attempt("attempt-1", timeout_seconds=0.5)
        except AssertionError:
            recovered_without_test_help = False
            _wake_only_to_keep_the_red_scheduler_drainable(scheduler)
            sink.wait_for_attempt("attempt-1")
        assert recovered_without_test_help
        assert clock.matching_call_count == 2
        assert uncaught == []
    finally:
        clock.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_queue_full_notification_recovers_worker_clock_fault_at_full_capacity(
    monkeypatch,
):
    clock = BlockingThreadFaultClock("analysis-scheduler-worker-", True)
    loader = GatedAudioLoader()
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        loader=loader,
        clock=clock,
    )
    recovered_without_test_help = True
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        assert loader.entered.wait(1.0)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        loader.release()
        assert clock.entered.wait(1.0)
        assert scheduler.submit(make_job(3)).status is SubmitStatus.QUEUE_FULL
        clock.release()
        try:
            sink.wait_for_attempt("attempt-1", timeout_seconds=0.5)
        except AssertionError:
            recovered_without_test_help = False
            _wake_only_to_keep_the_red_scheduler_drainable(scheduler)
            sink.wait_for_attempt("attempt-1")
        outcomes = sink.wait_for_count(2)
        assert {outcome.analysis_attempt_id for outcome in outcomes} == {
            "attempt-1",
            "attempt-2",
        }
        assert recovered_without_test_help
        assert clock.matching_call_count >= 2
        assert uncaught == []
    finally:
        loader.release()
        clock.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def _start_racing_submit(
    scheduler: AnalysisScheduler,
    job,
) -> tuple[threading.Thread, threading.Event, list, list]:
    completed = threading.Event()
    receipts = []
    errors = []

    def submit() -> None:
        try:
            receipts.append(scheduler.submit(job))
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=submit, name="racing-submit")
    thread.start()
    return thread, completed, receipts, errors


def _install_blocking_submit_clock_failure(monkeypatch, error):
    entered = threading.Event()
    release = threading.Event()
    original = AnalysisScheduler._read_clock
    remaining = [True]

    def read_clock(self):
        if threading.current_thread().name == "racing-submit" and remaining[0]:
            remaining[0] = False
            entered.set()
            assert release.wait(2.0), "submit clock gate was not released"
            raise error
        return original(self)

    monkeypatch.setattr(AnalysisScheduler, "_read_clock", read_clock)
    return entered, release


def test_clock_failure_race_rechecks_queue_full_before_reraising(monkeypatch):
    clock = ManualClock(1.0)
    clock_entered, release_clock = _install_blocking_submit_clock_failure(
        monkeypatch,
        TypeError("transient clock type"),
    )
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
        executor=executor,
        clock=clock,
    )
    thread = None
    try:
        assert scheduler.submit(make_job(1)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        thread, completed, receipts, errors = _start_racing_submit(
            scheduler,
            make_job(3),
        )
        assert clock_entered.wait(1.0)
        assert scheduler.submit(make_job(2)).status is SubmitStatus.ACCEPTED
        release_clock.set()
        assert completed.wait(1.0)
        thread.join(1.0)
        assert errors == []
        assert receipts[0].status is SubmitStatus.QUEUE_FULL
        assert receipts[0].submission_sequence is None
        assert (
            receipts[0].error.category
            is AnalysisErrorCategory.ANALYSIS_QUEUE_FULL
        )
    finally:
        release_clock.set()
        if thread is not None:
            thread.join(1.0)
        executor.release()
        try:
            sink.wait_for_count(2)
        finally:
            assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_clock_failure_race_rechecks_duplicate_before_reraising(monkeypatch):
    clock = ManualClock(1.0)
    clock_entered, release_clock = _install_blocking_submit_clock_failure(
        monkeypatch,
        TypeError("transient clock type"),
    )
    executor = RecordingExecutor(gated=True)
    scheduler, _, _, sink, _ = make_scheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=2),
        executor=executor,
        clock=clock,
    )
    job = make_job(1)
    thread = None
    try:
        thread, completed, receipts, errors = _start_racing_submit(scheduler, job)
        assert clock_entered.wait(1.0)
        accepted = scheduler.submit(job)
        assert accepted.status is SubmitStatus.ACCEPTED
        assert accepted.submission_sequence == 1
        release_clock.set()
        assert completed.wait(1.0)
        thread.join(1.0)
        assert errors == []
        assert (
            receipts[0].status
            is SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID
        )
        assert receipts[0].submission_sequence is None
        assert (
            receipts[0].error.category
            is AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID
        )
    finally:
        release_clock.set()
        if thread is not None:
            thread.join(1.0)
        executor.release()
        try:
            sink.wait_for_count(1)
        finally:
            assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_clock_failure_race_rechecks_shutdown_before_reraising(monkeypatch):
    clock = ManualClock(1.0)
    clock_entered, release_clock = _install_blocking_submit_clock_failure(
        monkeypatch,
        ValueError("transient clock value"),
    )
    scheduler, _, _, _, _ = make_scheduler(clock=clock)
    thread = None
    try:
        thread, completed, receipts, errors = _start_racing_submit(
            scheduler,
            make_job(1),
        )
        assert clock_entered.wait(1.0)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED
        release_clock.set()
        assert completed.wait(1.0)
        thread.join(1.0)
        assert errors == []
        assert receipts[0].status is SubmitStatus.NOT_ACCEPTING
        assert receipts[0].submission_sequence is None
        assert (
            receipts[0].error.category
            is AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING
        )
    finally:
        release_clock.set()
        if thread is not None:
            thread.join(1.0)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED
