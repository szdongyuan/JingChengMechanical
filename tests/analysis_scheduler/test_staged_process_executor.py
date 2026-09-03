from __future__ import annotations

import json
import queue
import threading
import time
import zlib
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.service.analysis import process_executor as process_executor_module
from app.service.analysis import staged_result_payload
from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisBundleIdentity,
    AnalysisBundleToken,
    AnalysisDecisionEnvelope,
    AnalysisEnrichmentReport,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStageTimings,
    CancelStatus,
    SubmitStatus,
)
from tests.analysis_scheduler.fakes import (
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    RecordingAudioLoader,
    make_job,
)
from tests.analysis_scheduler.test_staged_production_executor import (
    _Audio as _RealAudio,
    _Token as _RealToken,
    _job as _real_job,
)
from tests.test_ai_resident_runtime import (
    _entrypoint as _resident_entrypoint,
    _toolchain_snapshot,
    _write_fake_resident_script,
)


def _identity(attempt_id: str) -> AnalysisBundleIdentity:
    return AnalysisBundleIdentity(
        run_id=f"run-{attempt_id}",
        analysis_attempt_id=attempt_id,
        content_revision=f"content-{attempt_id}",
        algorithm_revision="algorithm-1",
        config_identity="config-1",
        calibration_identity="calibration-1",
        channel_mapping_identity="mapping-1",
        window_start_seconds=0.0,
        window_end_seconds=1.0,
    )


def _canonical(*, charts: bool) -> str:
    result = {
        "legacy_overall": "合格",
        "limit_pass": True,
        "ai_pass": None,
        "ai_score": None,
        "analysis_window": [0.0, 1.0],
        "channel_results": [],
        "review": {},
        "displayed_config_id": 1,
        "config_payload": {},
        "captured_duration_sec": 1.0,
        "default_window": [0.0, 1.0],
        "fallback_window": False,
        "manual_reanalysis_allowed": True,
        "judgment_payload": {},
        "summary_payload": {},
        "chart_payload": {"0": {"spl": [1.0]}} if charts else {},
    }
    return json.dumps(
        {"judgment": "PASS", "result": result},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decision(attempt_id: str, slot_id: int) -> AnalysisDecisionEnvelope:
    identity = _identity(attempt_id)
    token = AnalysisBundleToken(slot_id, f"bundle-{attempt_id}", identity)
    return AnalysisDecisionEnvelope(
        identity=identity,
        judgment="PASS",
        canonical_result_json=_canonical(charts=False),
        summary_payload_json="{}",
        bundle_token=token,
        timings=AnalysisStageTimings(()),
    )


def _enrichment(token: AnalysisBundleToken) -> AnalysisEnrichmentReport:
    canonical = _canonical(charts=True)
    return AnalysisEnrichmentReport(
        identity=token.identity,
        judgment="PASS",
        canonical_result_json=canonical,
        result_payload_zlib=zlib.compress(canonical.encode("utf-8")),
        timings=AnalysisStageTimings(()),
        chart_manifest=((0, ("spl",)),),
    )


def test_parent_decodes_enrichment_transport_without_parsing_chart_json(
    monkeypatch,
):
    token = AnalysisBundleToken(0, "bundle-a", _identity("a"))
    report = _enrichment(token)
    transport = process_executor_module._CompressedEnrichmentTransport(
        identity=report.identity,
        judgment=report.judgment,
        result_payload_zlib=report.result_payload_zlib,
        chart_manifest=report.chart_manifest,
        timings=report.timings,
    )
    monkeypatch.setattr(
        staged_result_payload.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("parent parsed full chart JSON")
        ),
    )

    decoded = ProcessIsolatedAnalysisExecutor._decode_enrichment_response(
        ("enrich", transport)
    )

    assert decoded.canonical_result_json == report.canonical_result_json
    assert decoded.chart_manifest == report.chart_manifest


class _Connection:
    def __init__(self, slot_id: int) -> None:
        self.slot_id = slot_id
        self.responses = deque([("ready", ())])
        self.sent = []
        self.send_threads = []
        self.closed = False
        self.pending_enrichment = None

    def send(self, message) -> None:
        self.sent.append(message)
        self.send_threads.append(threading.current_thread().name)
        kind = message[0]
        if kind == "decision":
            attempt_id = message[1].analysis_attempt_id
            self.responses.append(("decision", _decision(attempt_id, self.slot_id)))
        elif kind == "enrich":
            self.pending_enrichment = message[1]
            self.responses.append(("enrich_ready", message[1].identity))
        elif kind == "enrich_transport":
            assert self.pending_enrichment == message[1]
            self.responses.append(
                ("enrich", _enrichment(self.pending_enrichment))
            )
            self.pending_enrichment = None
        elif kind == "discard":
            self.responses.append(("discard", message[1]))
        elif kind == "warm":
            self.responses.append(
                ("warm", process_executor_module._toolchain_key(message[1]))
            )

    def poll(self, _timeout: float) -> bool:
        return bool(self.responses)

    def recv(self):
        return self.responses.popleft()

    def close(self) -> None:
        self.closed = True


class _ChildConnection:
    def close(self) -> None:
        pass


class _Process:
    def __init__(self) -> None:
        self.alive = True

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive

    def join(self, _timeout: float) -> None:
        self.alive = False

    def terminate(self) -> None:
        self.alive = False


class _CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds=None) -> bool:
        return self._event.wait(timeout_seconds)

    def cancel(self, reason="canceled") -> None:
        self.reason = reason
        self._event.set()


class _Context:
    def __init__(self) -> None:
        self.connections = []
        self.control_connections = []

    def Pipe(self, *, duplex: bool):
        if not duplex:
            control = _ControlConnection()
            self.control_connections.append(control)
            return control, _ChildConnection()
        connection = _Connection(len(self.connections))
        self.connections.append(connection)
        return connection, _ChildConnection()

    def Process(self, **_kwargs):
        return _Process()


class _FailingEnrichmentConnection(_Connection):
    def send(self, message) -> None:
        self.sent.append(message)
        self.send_threads.append(threading.current_thread().name)
        kind = message[0]
        if kind == "decision":
            attempt_id = message[1].analysis_attempt_id
            self.responses.append(("decision", _decision(attempt_id, self.slot_id)))
        elif kind == "enrich":
            self.responses.append(("unexpected_error", "enrichment failed"))
        elif kind == "discard":
            self.responses.append(("discard", message[1]))


class _FailingEnrichmentContext(_Context):
    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        connection = _FailingEnrichmentConnection(len(self.connections))
        self.connections.append(connection)
        return connection, _ChildConnection()


class _FailingDecisionConnection(_Connection):
    def send(self, message) -> None:
        self.sent.append(message)
        self.send_threads.append(threading.current_thread().name)
        if message[0] == "decision":
            self.responses.append(("unexpected_error", "decision failed"))


class _FailingDecisionContext(_Context):
    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        connection = _FailingDecisionConnection(len(self.connections))
        self.connections.append(connection)
        return connection, _ChildConnection()


class _BlockingDecisionConnection(_Connection):
    def __init__(self, slot_id: int) -> None:
        super().__init__(slot_id)
        self.decision_entered = threading.Event()
        self.decision_release = threading.Event()
        self._pending_attempt_id = None

    def send(self, message) -> None:
        self.sent.append(message)
        self.send_threads.append(threading.current_thread().name)
        kind = message[0]
        if kind == "decision":
            self._pending_attempt_id = message[1].analysis_attempt_id
            self.decision_entered.set()
        elif kind == "discard":
            self.responses.append(("discard", message[1]))
        elif kind == "enrich":
            self.pending_enrichment = message[1]
            self.responses.append(("enrich_ready", message[1].identity))
        elif kind == "enrich_transport":
            assert self.pending_enrichment == message[1]
            self.responses.append(
                ("enrich", _enrichment(self.pending_enrichment))
            )
            self.pending_enrichment = None

    def poll(self, timeout: float) -> bool:
        if self.responses:
            return True
        if self._pending_attempt_id is not None:
            if self.decision_release.wait(timeout):
                self.responses.append(
                    (
                        "decision",
                        _decision(self._pending_attempt_id, self.slot_id),
                    )
                )
                self._pending_attempt_id = None
                return True
        return False


class _BlockingDecisionContext(_Context):
    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        connection = _BlockingDecisionConnection(len(self.connections))
        self.connections.append(connection)
        return connection, _ChildConnection()


class _SecondSlotBlockingContext(_Context):
    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        slot_id = len(self.connections)
        connection = (
            _Connection(slot_id)
            if slot_id == 0
            else _BlockingDecisionConnection(slot_id)
        )
        self.connections.append(connection)
        return connection, _ChildConnection()


class _FirstSlotBlockingContext(_Context):
    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        slot_id = len(self.connections)
        connection = (
            _BlockingDecisionConnection(slot_id)
            if slot_id == 0
            else _Connection(slot_id)
        )
        self.connections.append(connection)
        return connection, _ChildConnection()


class _FakeCaptureClock:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = True
        self._now = 100.0

    def capture_active(self) -> bool:
        with self._condition:
            return self._active

    def monotonic(self) -> float:
        with self._condition:
            return self._now

    def wait(self, _timeout: float) -> None:
        with self._condition:
            self._condition.wait(0.01)

    def set_active(self, active: bool) -> None:
        with self._condition:
            self._active = active
            self._condition.notify_all()

    def advance(self, seconds: float) -> None:
        with self._condition:
            self._now += seconds
            self._condition.notify_all()


class _ReplacementConnection(_Connection):
    def __init__(self, slot_id: int, ready_keys=()) -> None:
        super().__init__(slot_id)
        self.responses.clear()
        self.ready_keys = ready_keys
        self.ready_release = threading.Event()
        self._startup_ready = False

    def poll(self, timeout: float) -> bool:
        if not self._startup_ready:
            if self.ready_release.wait(timeout):
                self.responses.append(("ready", self.ready_keys))
                self._startup_ready = True
                return True
            return False
        return super().poll(timeout)


class _ReplacingContext(_Context):
    def __init__(self, ready_keys=()) -> None:
        super().__init__()
        self.ready_keys = ready_keys
        self.processes = []
        self.process_specs = []

    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        slot_id = len(self.connections)
        connection = (
            _Connection(slot_id)
            if slot_id < 2
            else _ReplacementConnection(0, self.ready_keys)
        )
        if slot_id < 2:
            connection.responses.clear()
            connection.responses.append(("ready", self.ready_keys))
        self.connections.append(connection)
        return connection, _ChildConnection()

    def Process(self, **kwargs):
        process = _Process()
        self.processes.append(process)
        self.process_specs.append(kwargs)
        return process


class _StartupErrorConnection(_Connection):
    def __init__(self, slot_id: int) -> None:
        super().__init__(slot_id)
        self.responses.clear()
        self.responses.append(("startup_error", "replacement failed"))


class _RetryReplacementContext(_Context):
    def __init__(self, *, fail_replacements: int) -> None:
        super().__init__()
        self.fail_replacements = fail_replacements
        self.processes = []

    def Pipe(self, *, duplex: bool):
        if not duplex:
            return super().Pipe(duplex=duplex)
        connection_index = len(self.connections)
        replacement_number = connection_index
        connection = (
            _StartupErrorConnection(0)
            if 1 <= replacement_number <= self.fail_replacements
            else _Connection(0)
        )
        self.connections.append(connection)
        return connection, _ChildConnection()

    def Process(self, **_kwargs):
        process = _Process()
        self.processes.append(process)
        return process


class _ControlConnection:
    def __init__(self) -> None:
        self.messages = deque()
        self.closed = False

    def emit(self, message) -> None:
        self.messages.append(message)

    def poll(self, timeout: float) -> bool:
        if self.messages:
            return True
        time.sleep(min(timeout, 0.01))
        return bool(self.messages)

    def recv(self):
        return self.messages.popleft()

    def close(self) -> None:
        self.closed = True


class _ReadinessContext(_Context):
    pass


class _StartupFalseContext(_Context):
    def __init__(self, key) -> None:
        super().__init__()
        self.key = key

    def Pipe(self, *, duplex: bool):
        if not duplex:
            control = _ControlConnection()
            control.emit(("readiness", self.key, False))
            self.control_connections.append(control)
            return control, _ChildConnection()
        connection = _Connection(len(self.connections))
        connection.responses.clear()
        connection.responses.append(("ready", (self.key,)))
        self.connections.append(connection)
        return connection, _ChildConnection()


class _ChildLoopConnection:
    def __init__(self) -> None:
        self.sent = []
        self._commands = deque([("stop",)])

    def send(self, message) -> None:
        self.sent.append(message)

    def recv(self):
        return self._commands.popleft()

    def close(self) -> None:
        pass


class _ChildControlSender:
    def __init__(self) -> None:
        self.sent = []

    def send(self, message) -> None:
        self.sent.append(message)

    def close(self) -> None:
        pass


def _job(attempt_id: str):
    return SimpleNamespace(analysis_attempt_id=attempt_id)


def _audio():
    return SimpleNamespace(
        samples=(0.0,),
        sample_rate_hz=48_000,
        channel_count=1,
        frame_count=1,
    )


def _commit_decision(
    executor: ProcessIsolatedAnalysisExecutor,
    attempt_id: str,
) -> None:
    assert executor.mark_decision_committed(attempt_id) is True


def _commit_for_direct_enrichment(
    executor: ProcessIsolatedAnalysisExecutor,
    attempt_id: str,
) -> None:
    with executor._state_lock:
        state = executor._attempts[attempt_id]
        state.decision_committed = True
        state.checkpoint = "decision_committed"


def test_decision_is_admitted_during_capture_and_enrichment_uses_owner_fence(
    monkeypatch,
):
    context = _Context()
    capture = {"active": True}
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        capture_active=lambda: capture["active"],
    )
    try:
        started = time.monotonic()
        decision = executor.execute_decision(_job("a"), _audio(), object())
        _commit_for_direct_enrichment(executor, "a")
        assert time.monotonic() - started < 0.15
        assert context.connections[0].sent[0][0] == "decision"

        result = []
        thread = threading.Thread(
            target=lambda: result.append(
                executor.execute_enrichment(decision.bundle_token)
            )
        )
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()
        assert not any(
            message[0] == "enrich" for message in context.connections[0].sent
        )

        released_at = time.monotonic()
        capture["active"] = False
        thread.join(1.0)

        assert result and result[0].identity == decision.identity
        assert time.monotonic() - released_at >= 0.24
        assert context.connections[0].sent[-2:] == [
            ("enrich", decision.bundle_token),
            ("enrich_transport", decision.bundle_token),
        ]
    finally:
        executor.close()


def test_quiet_epoch_scans_six_attempts_including_completing_decision_once(
    monkeypatch,
):
    context = _FirstSlotBlockingContext()
    clock = _FakeCaptureClock()
    reports = []
    delivery_threads = []

    def deliver(report):
        reports.append(report)
        delivery_threads.append(threading.current_thread().name)

    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        capture_active=clock.capture_active,
        monotonic=clock.monotonic,
        quiet_wait=clock.wait,
        enrichment_sink=deliver,
    )
    def complete_slow_decision() -> None:
        executor.execute(_job("slow"), _audio(), object())
        _commit_decision(executor, "slow")

    completing = threading.Thread(target=complete_slow_decision)
    try:
        completing.start()
        blocked = context.connections[0]
        assert blocked.decision_entered.wait(1.0)
        for attempt_id in ("a", "b", "c", "d", "e"):
            executor.execute(_job(attempt_id), _audio(), object())
            _commit_decision(executor, attempt_id)

        states = executor.attempt_state_snapshot()
        assert set(states) == {"slow", "a", "b", "c", "d", "e"}
        assert states["slow"].audio_frozen is True
        assert states["slow"].decision_committed is False
        assert not any(
            message[0] == "enrich"
            for connection in context.connections
            for message in connection.sent
        )

        clock.set_active(False)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and executor.quiet_epoch_id() == 0:
            threading.Event().wait(0.01)
        assert executor.quiet_epoch_id() == 1
        assert executor.attempt_state_snapshot()["slow"].quiet_epoch_id == 1

        clock.advance(0.249)
        threading.Event().wait(0.05)
        assert reports == []

        blocked.decision_release.set()
        completing.join(1.0)
        assert not completing.is_alive()
        clock.advance(0.001)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(reports) < 6:
            threading.Event().wait(0.01)

        assert {report.identity.analysis_attempt_id for report in reports} == {
            "slow",
            "a",
            "b",
            "c",
            "d",
            "e",
        }
        commands = [
            message[0]
            for connection in context.connections
            for message in connection.sent
        ]
        assert commands.count("enrich") == 6
        assert len(delivery_threads) == 6
        assert all(
            name.startswith("analysis-enrichment-dispatcher-")
            for name in delivery_threads
        )
        assert all(
            not name.startswith("resident-audio-drain-")
            for name in delivery_threads
        )
    finally:
        blocked.decision_release.set()
        completing.join(1.0)
        executor.close()


def test_scheduler_acceptance_registers_all_six_before_two_workers_start_decision(
    monkeypatch,
):
    context = _BlockingDecisionContext()
    capture_clock = _FakeCaptureClock()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        capture_active=capture_clock.capture_active,
        monotonic=capture_clock.monotonic,
        quiet_wait=capture_clock.wait,
    )
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=2, pending_capacity=6),
        audio_loader=RecordingAudioLoader(),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=OutcomeCollector(),
        clock=ManualClock(1.0),
    )
    try:
        jobs = tuple(make_job(number) for number in range(1, 7))
        assert [scheduler.submit(job).status for job in jobs] == [
            SubmitStatus.ACCEPTED
        ] * 6
        assert scheduler.submit(make_job(7)).status is SubmitStatus.QUEUE_FULL
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(context.connections) < 2:
            threading.Event().wait(0.01)
        assert len(context.connections) == 2
        assert all(connection.decision_entered.wait(1.0) for connection in context.connections)
        decision_threads = [
            connection.send_threads[-1] for connection in context.connections
        ]
        assert all(
            name.startswith("analysis-scheduler-worker-")
            for name in decision_threads
        )
        assert all(
            not name.startswith("resident-audio-drain-")
            for name in decision_threads
        )

        states = executor.attempt_state_snapshot()
        assert set(states) == {job.analysis_attempt_id for job in jobs}
        assert sum(state.checkpoint != "audio_frozen" for state in states.values()) == 0

        capture_clock.set_active(False)
        assert executor.quiet_epoch_id() == 1
        quiet_states = executor.attempt_state_snapshot()
        assert all(state.quiet_epoch_id == 1 for state in quiet_states.values())
        assert sum(
            connection.decision_entered.is_set()
            for connection in context.connections
        ) == 2
        canceled = scheduler.cancel(
            jobs[-1].run_id,
            jobs[-1].analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "cancel pending sixth",
        )
        assert canceled.status is CancelStatus.CANCELED_PENDING
        assert jobs[-1].analysis_attempt_id not in executor.attempt_state_snapshot()
    finally:
        for connection in context.connections:
            connection.decision_release.set()
        scheduler.shutdown(5.0)
        executor.close()


def test_enrichment_waits_for_explicit_durable_decision_commit(monkeypatch):
    context = _Context()
    clock = _FakeCaptureClock()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        capture_active=clock.capture_active,
        monotonic=clock.monotonic,
        quiet_wait=clock.wait,
        enrichment_sink=reports.append,
    )
    job = _job("durable")
    try:
        assert executor.register_staged_attempt(job) is True
        executor.execute(job, _audio(), object())
        clock.set_active(False)
        assert executor.quiet_epoch_id() == 1
        clock.advance(1.0)
        threading.Event().wait(0.05)

        assert reports == []
        assert not any(
            message[0] == "enrich" for message in context.connections[0].sent
        )
        assert executor.attempt_state_snapshot()["durable"].decision_committed is False

        assert executor.mark_decision_committed("durable") is True
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not reports:
            threading.Event().wait(0.01)

        assert len(reports) == 1
        assert [
            message[0] for message in context.connections[0].sent
        ].count("enrich") == 1
        assert executor.mark_decision_committed("durable") is False
    finally:
        executor.close()


def test_pending_timeout_retires_admission_state_before_decision_starts(
    monkeypatch,
):
    context = _BlockingDecisionContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    outcomes = OutcomeCollector()
    clock = ManualClock(1.0)
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=6),
        audio_loader=RecordingAudioLoader(),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=clock,
    )
    blocker = make_job(21)
    timeout = make_job(22, queue_timeout_seconds=0.01)
    try:
        assert scheduler.submit(blocker).status is SubmitStatus.ACCEPTED
        while not context.connections:
            threading.Event().wait(0.01)
        assert context.connections[0].decision_entered.wait(1.0)
        assert scheduler.submit(timeout).status is SubmitStatus.ACCEPTED
        assert timeout.analysis_attempt_id in executor.attempt_state_snapshot()

        clock.set(2.0)
        outcomes.wait_for_attempt(timeout.analysis_attempt_id)

        assert timeout.analysis_attempt_id not in executor.attempt_state_snapshot()
        assert [
            message[1].analysis_attempt_id
            for message in context.connections[0].sent
            if message[0] == "decision"
        ] == [blocker.analysis_attempt_id]
    finally:
        for connection in context.connections:
            connection.decision_release.set()
        scheduler.shutdown(5.0)
        executor.close()


def test_failed_decision_retires_admission_state_without_enrichment_retry(
    monkeypatch,
):
    context = _FailingDecisionContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    outcomes = OutcomeCollector()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=1),
        audio_loader=RecordingAudioLoader(),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=ManualClock(1.0),
    )
    failed = make_job(23)
    try:
        assert scheduler.submit(failed).status is SubmitStatus.ACCEPTED
        outcomes.wait_for_attempt(failed.analysis_attempt_id)

        assert failed.analysis_attempt_id not in executor.attempt_state_snapshot()
        commands = [message[0] for message in context.connections[0].sent]
        assert commands.count("decision") == 1
        assert commands.count("enrich") == 0
    finally:
        scheduler.shutdown(5.0)
        executor.close()


@pytest.mark.parametrize(
    ("checkpoint", "parked_checkpoint"),
    [
        ("child_compressed_transport", "computed"),
        ("parent_decode_validation", "transported"),
        ("artifact_publication", "decoded"),
    ],
)
def test_capture_restart_parks_each_enrichment_boundary_without_duplicate_dsp(
    monkeypatch,
    checkpoint,
    parked_checkpoint,
):
    context = _Context()
    clock = _FakeCaptureClock()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        capture_active=clock.capture_active,
        monotonic=clock.monotonic,
        quiet_wait=clock.wait,
    )
    try:
        decision = executor.execute_decision(_job(checkpoint), _audio(), object())
        _commit_for_direct_enrichment(executor, checkpoint)
        clock.set_active(False)
        assert executor.quiet_epoch_id() == 1
        clock.advance(0.25)

        original_guard = executor.require_quiet_epoch
        gate_reached = threading.Event()

        def restart_at_checkpoint(epoch_id=None, name="enrichment", cancel_token=None):
            if name == checkpoint and not gate_reached.is_set():
                clock.set_active(True)
                gate_reached.set()
            return original_guard(epoch_id, name, cancel_token)

        monkeypatch.setattr(executor, "require_quiet_epoch", restart_at_checkpoint)
        reports = []
        worker = threading.Thread(
            target=lambda: reports.append(
                executor.execute_enrichment(decision.bundle_token)
            )
        )
        worker.start()
        assert gate_reached.wait(1.0)
        assert worker.is_alive()
        state = executor.attempt_state_snapshot()[checkpoint]
        assert state.checkpoint == parked_checkpoint

        commands = [message[0] for message in context.connections[0].sent]
        assert commands.count("enrich") == 1
        assert commands.count("enrich_transport") == (
            0 if checkpoint == "child_compressed_transport" else 1
        )

        clock.set_active(False)
        assert executor.quiet_epoch_id() == 2
        clock.advance(0.25)
        worker.join(1.0)

        assert not worker.is_alive()
        assert reports[0].identity == decision.identity
        commands = [message[0] for message in context.connections[0].sent]
        assert commands.count("enrich") == 1
        assert commands.count("enrich_transport") == 1
    finally:
        executor.close()


def test_revision_is_published_only_after_every_slot_acknowledges_warm(
    monkeypatch,
    tmp_path,
):
    context = _Context()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    snapshot = _toolchain_snapshot(tmp_path, "python resident.py")
    executor = ProcessIsolatedAnalysisExecutor(process_count=2)
    try:
        executor.prewarm(snapshot)

        assert executor.revision_ready(snapshot) is True
        assert all(
            [message[0] for message in connection.sent] == ["warm"]
            for connection in context.connections
        )
    finally:
        executor.close()


def test_dedicated_control_channel_revokes_and_republishes_exact_readiness(
    monkeypatch,
    tmp_path,
):
    context = _ReadinessContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    snapshot = _toolchain_snapshot(tmp_path, "python resident.py")
    key = (
        snapshot.toolchain_id,
        snapshot.execution_revision,
        snapshot.entrypoint,
        snapshot.package_dir,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        executor.prewarm(snapshot)
        assert executor.revision_ready(snapshot) is True
        control = context.control_connections[0]

        control.emit(("readiness", key, False))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and executor.revision_ready(snapshot):
            time.sleep(0.01)
        assert executor.revision_ready(snapshot) is False

        control.emit(("readiness", key, True))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not executor.revision_ready(snapshot):
            time.sleep(0.01)
        assert executor.revision_ready(snapshot) is True
    finally:
        executor.close()


def test_startup_ready_keys_install_before_queued_false_control_event(
    monkeypatch,
    tmp_path,
):
    snapshot = _toolchain_snapshot(tmp_path, "python resident.py")
    key = process_executor_module._toolchain_key(snapshot)
    context = _StartupFalseContext(key)
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        selected_toolchains=(snapshot,),
    )
    try:
        executor.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and executor.revision_ready(snapshot):
            time.sleep(0.01)

        assert executor.revision_ready(snapshot) is False
        assert executor.binding_ready(
            {
                "toolchain_id": snapshot.toolchain_id,
                "execution_revision": snapshot.execution_revision,
                "entrypoint": snapshot.entrypoint,
                "package_dir": snapshot.package_dir,
            }
        ) is False
    finally:
        executor.close()


@pytest.mark.parametrize(
    ("required", "expected_kind"),
    ((False, "ready"), (True, "startup_error")),
)
def test_child_startup_only_requires_loudness_backend_for_selected_limits(
    monkeypatch,
    required,
    expected_kind,
):
    connection = _ChildLoopConnection()
    monkeypatch.setattr(
        process_executor_module.dsp,
        "preload_loudness_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("backend unavailable")),
    )
    monkeypatch.setattr(
        process_executor_module,
        "set_current_process_priority",
        lambda _priority: None,
    )
    monkeypatch.setattr(
        process_executor_module,
        "set_current_thread_priority",
        lambda _priority: None,
    )

    process_executor_module._analysis_process_main(
        connection,
        {},
        (),
        0,
        required,
    )

    assert connection.sent[0][0] == expected_kind


def test_child_resident_readiness_uses_control_channel_not_command_response(
    monkeypatch,
    tmp_path,
):
    connection = _ChildLoopConnection()
    control = _ChildControlSender()
    script = _write_fake_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _resident_entrypoint(script))
    monkeypatch.setattr(
        process_executor_module,
        "set_current_process_priority",
        lambda _priority: None,
    )
    monkeypatch.setattr(
        process_executor_module,
        "set_current_thread_priority",
        lambda _priority: None,
    )

    process_executor_module._analysis_process_main(
        connection,
        {},
        (snapshot,),
        0,
        False,
        control,
    )

    key = process_executor_module._toolchain_key(snapshot)
    assert connection.sent[0] == ("ready", (key,))
    assert ("readiness", key, True) in control.sent


def test_one_slot_keeps_multiple_attempt_bundles_and_discards_exact_owner(
    monkeypatch,
):
    context = _Context()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        first = executor.execute_decision(_job("a"), _audio(), object())
        second = executor.execute_decision(_job("b"), _audio(), object())

        assert set(executor.owned_attempt_ids()) == {"a", "b"}
        executor.discard_attempt("a")
        assert executor.owned_attempt_ids() == ("b",)
        _commit_for_direct_enrichment(executor, "b")
        enriched = executor.execute_enrichment(second.bundle_token)
        assert enriched.identity.analysis_attempt_id == "b"
        assert executor.owned_attempt_ids() == ()

        commands = [message[0] for message in context.connections[0].sent]
        assert commands == [
            "decision",
            "decision",
            "discard",
            "enrich",
            "enrich_transport",
        ]
    finally:
        executor.close()


def test_bounded_dispatcher_delivers_compressed_report_once(monkeypatch):
    context = _Context()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_sink=reports.append,
    )
    try:
        executor.execute(_job("a"), _audio(), object())
        _commit_decision(executor, "a")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not reports:
            time.sleep(0.01)

        assert len(reports) == 1
        assert zlib.decompress(reports[0].result_payload_zlib).decode("utf-8") == (
            reports[0].canonical_result_json
        )
        assert reports[0].chart_manifest == ((0, ("spl",)),)
        assert executor.owned_attempt_ids() == ()
        assert [
            message[0] for message in context.connections[0].sent
        ].count("enrich") == 1
    finally:
        executor.close()


def test_enrichment_backlog_uses_both_existing_process_slots(monkeypatch):
    context = _Context()
    reports = []
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        enrichment_sink=reports.append,
    )
    try:
        first = executor.execute_decision(_job("a"), _audio(), object())
        second = executor.execute_decision(_job("b"), _audio(), object())
        assert {first.bundle_token.slot_id, second.bundle_token.slot_id} == {0, 1}

        def blocked_enrichment(token):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            release.wait(1.0)
            with lock:
                active -= 1
            return _enrichment(token)

        monkeypatch.setattr(executor, "execute_enrichment", blocked_enrichment)
        _commit_decision(executor, "a")
        _commit_decision(executor, "b")

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and max_active < 2:
            time.sleep(0.01)

        assert max_active == 2
        release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(reports) < 2:
            time.sleep(0.01)
        assert {report.identity.analysis_attempt_id for report in reports} == {
            "a",
            "b",
        }
    finally:
        release.set()
        executor.close()


def test_enrichment_sink_transient_failure_retries_delivery_without_dsp(
    monkeypatch,
):
    context = _Context()
    delivered = []

    def transient_sink(report):
        delivered.append(report)
        if len(delivered) == 1:
            raise RuntimeError("transient projection failure")

    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_sink=transient_sink,
    )
    try:
        executor.execute(_job("sink-retry"), _audio(), object())
        _commit_decision(executor, "sink-retry")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(delivered) < 2:
            time.sleep(0.01)

        commands = [message[0] for message in context.connections[0].sent]
        assert len(delivered) == 2
        assert delivered[0] is delivered[1]
        assert commands.count("enrich") == 1
        assert executor.pending_delivery_attempt_ids() == ()
        assert executor.enrichment_delivery_failures() == ()
    finally:
        executor.close()


def test_enrichment_sink_permanent_failure_records_bounded_diagnostic(
    monkeypatch,
):
    context = _Context()
    delivered = []
    diagnostics = []
    failures = []

    def failing_sink(report):
        delivered.append(report)
        raise RuntimeError("permanent projection failure")

    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_sink=failing_sink,
        enrichment_diagnostic=lambda attempt_id, summary: diagnostics.append(
            (attempt_id, summary)
        ),
        enrichment_failure_sink=lambda identity, code: failures.append(
            (identity.analysis_attempt_id, code)
        ),
    )
    try:
        executor.execute(_job("sink-failed"), _audio(), object())
        _commit_decision(executor, "sink-failed")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not diagnostics:
            time.sleep(0.01)

        commands = [message[0] for message in context.connections[0].sent]
        assert len(delivered) == 2
        assert delivered[0] is delivered[1]
        assert commands.count("enrich") == 1
        assert executor.pending_delivery_attempt_ids() == ()
        assert executor.enrichment_delivery_failures() == ("sink-failed",)
        assert diagnostics == [
            ("sink-failed", "enrichment sink delivery failed twice")
        ]
        assert failures == [("sink-failed", "ENRICHMENT_SINK_FAILED")]
    finally:
        executor.close()


def test_idle_slot_death_does_not_drop_pending_sink_delivery_diagnostic(
    monkeypatch,
):
    context = _ReplacingContext()
    entered = threading.Event()
    release = threading.Event()
    deliveries = []
    diagnostics = []

    def blocked_failing_sink(report):
        deliveries.append(report)
        if len(deliveries) == 1:
            entered.set()
            release.wait(1.0)
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        enrichment_sink=blocked_failing_sink,
        enrichment_diagnostic=lambda attempt_id, summary: diagnostics.append(
            (attempt_id, summary)
        ),
    )
    try:
        executor.execute(_job("pending-on-death"), _audio(), object())
        _commit_decision(executor, "pending-on-death")
        assert entered.wait(1.0)
        assert executor.pending_delivery_attempt_ids() == ("pending-on-death",)
        context.processes[0].alive = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(context.connections) < 3:
            time.sleep(0.01)
        assert len(context.connections) == 3

        release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not diagnostics:
            time.sleep(0.01)

        assert len(deliveries) == 2
        assert deliveries[0] is deliveries[1]
        assert executor.pending_delivery_attempt_ids() == ()
        assert executor.enrichment_delivery_failures() == (
            "pending-on-death",
        )
        assert diagnostics == [
            ("pending-on-death", "enrichment sink delivery failed twice")
        ]
        context.connections[2].ready_release.set()
    finally:
        release.set()
        if len(context.connections) >= 3:
            context.connections[2].ready_release.set()
        executor.close()


def test_wrong_slot_or_stale_token_is_rejected_without_consuming_owner(
    monkeypatch,
):
    context = _Context()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        decision = executor.execute_decision(_job("a"), _audio(), object())
        wrong = replace(decision.bundle_token, slot_id=1)

        with pytest.raises(Exception, match="owner|slot|token"):
            executor.execute_enrichment(wrong)

        assert executor.owned_attempt_ids() == ("a",)
    finally:
        executor.close()


def test_six_bundle_capacity_is_bounded_and_seventh_is_discarded(monkeypatch):
    context = _Context()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        decisions = [
            executor.execute_decision(_job(str(index)), _audio(), object())
            for index in range(6)
        ]

        with pytest.raises(Exception, match="capacity"):
            executor.execute_decision(_job("seventh"), _audio(), object())

        assert len(executor.owned_attempt_ids()) == 6
        assert [message[0] for message in context.connections[0].sent].count(
            "decision"
        ) == 6
        for decision in decisions:
            assert executor.discard_attempt(
                decision.identity.analysis_attempt_id
            ) is True
        assert executor.owned_attempt_ids() == ()
    finally:
        executor.close()


def test_dispatcher_retries_once_then_discards_failed_bundle(monkeypatch):
    context = _FailingEnrichmentContext()
    failures = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_failure_sink=lambda identity, code: failures.append(
            (identity.analysis_attempt_id, code)
        ),
    )
    try:
        executor.execute(_job("retry"), _audio(), object())
        _commit_decision(executor, "retry")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and executor.owned_attempt_ids():
            time.sleep(0.01)

        commands = [message[0] for message in context.connections[0].sent]
        assert commands.count("enrich") == 2
        assert commands.count("discard") == 1
        assert executor.owned_attempt_ids() == ()
        assert failures == [("retry", "ENRICHMENT_COMPUTE_FAILED")]
    finally:
        executor.close()


def test_enrichment_queue_full_notifies_final_identity_once(monkeypatch):
    context = _Context()
    failures = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_failure_sink=lambda identity, code: failures.append(
            (identity.analysis_attempt_id, code)
        ),
    )
    try:
        decision = executor.execute_decision(_job("queue-full"), _audio(), object())
        monkeypatch.setattr(
            executor._enrichment_queue,
            "put_nowait",
            lambda _token: (_ for _ in ()).throw(queue.Full()),
        )

        with pytest.raises(Exception, match="capacity"):
            executor.mark_decision_committed("queue-full")

        assert executor.owned_attempt_ids() == ()
        assert failures == [("queue-full", "ENRICHMENT_QUEUE_FULL")]
    finally:
        executor.close()


def test_cancel_before_decision_send_creates_no_bundle_or_enrichment(monkeypatch):
    context = _Context()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    token = _CancelToken()
    token.cancel("operator canceled")
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_sink=reports.append,
    )
    try:
        with pytest.raises(Exception, match="canceled"):
            executor.execute(_job("cancel-before"), _audio(), token)

        assert context.connections[0].sent == []
        assert executor.owned_attempt_ids() == ()
        assert reports == []
    finally:
        executor.close()


def test_cancel_during_decision_discards_returned_bundle_without_enrichment(
    monkeypatch,
):
    context = _BlockingDecisionContext()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    token = _CancelToken()
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_sink=reports.append,
    )
    errors = []

    def capture_error():
        try:
            executor.execute(_job("cancel-during"), _audio(), token)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=capture_error)
    try:
        thread.start()
        connection = context.connections[0]
        assert connection.decision_entered.wait(1.0)
        token.cancel("operator canceled during decision")
        connection.decision_release.set()
        thread.join(1.0)

        assert not thread.is_alive()
        assert errors and "canceled" in str(errors[0]).lower()
        assert [message[0] for message in connection.sent] == [
            "decision",
            "discard",
        ]
        assert executor.owned_attempt_ids() == ()
        assert reports == []
    finally:
        executor.close()


def test_queued_enrichment_cancellation_bypasses_permanent_capture_fence(
    monkeypatch,
):
    context = _Context()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    token = _CancelToken()
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        capture_active=lambda: True,
        enrichment_sink=reports.append,
    )
    try:
        executor.execute(_job("cancel-queued"), _audio(), token)
        _commit_decision(executor, "cancel-queued")
        token.cancel("operator canceled queued enrichment")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and executor.owned_attempt_ids():
            time.sleep(0.01)

        commands = [message[0] for message in context.connections[0].sent]
        assert executor.owned_attempt_ids() == ()
        assert commands == ["decision", "discard"]
        assert reports == []
    finally:
        executor.close()


def test_canceled_bundle_retirement_ignores_other_slot_decision_priority(
    monkeypatch,
):
    context = _SecondSlotBlockingContext()
    reports = []
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    token = _CancelToken()
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        capture_active=lambda: True,
        enrichment_sink=reports.append,
    )
    slow_results = []

    def run_slow_decision():
        slow_results.append(
            executor.execute_decision(_job("slow-decision"), _audio(), object())
        )

    try:
        executor.execute(_job("cancel-priority"), _audio(), token)
        _commit_decision(executor, "cancel-priority")
        with executor._slot_condition:
            executor._slots[0].busy = True
        slow_thread = threading.Thread(target=run_slow_decision)
        slow_thread.start()
        slow_connection = context.connections[1]
        assert slow_connection.decision_entered.wait(1.0)
        with executor._slot_condition:
            executor._slots[0].busy = False
            executor._slot_condition.notify_all()

        token.cancel("cancel while another slot decides")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and executor.owned_attempt_ids() == (
            "cancel-priority",
        ):
            time.sleep(0.01)

        assert "cancel-priority" not in executor.owned_attempt_ids()
        assert [message[0] for message in context.connections[0].sent] == [
            "decision",
            "discard",
        ]
        assert reports == []

        slow_connection.decision_release.set()
        slow_thread.join(1.0)
        assert len(slow_results) == 1
    finally:
        if len(context.connections) > 1:
            context.connections[1].decision_release.set()
        executor.close()


def test_child_death_releases_every_bundle_owned_by_that_slot(monkeypatch):
    context = _Context()
    processes = []
    failures = []

    def process_factory(**_kwargs):
        process = _Process()
        processes.append(process)
        return process

    context.Process = process_factory
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        enrichment_failure_sink=lambda identity, code: failures.append(
            (identity.analysis_attempt_id, code)
        ),
    )
    try:
        first = executor.execute_decision(_job("dead-a"), _audio(), object())
        executor.execute_decision(_job("dead-b"), _audio(), object())
        _commit_for_direct_enrichment(executor, "dead-a")
        with executor._state_lock:
            executor._attempts["dead-a"].parked_transport = object()
            executor._attempts["dead-a"].parked_report = _enrichment(
                first.bundle_token
            )
            executor._enrichment_retries.update({"dead-a": 1, "dead-b": 1})
        connection = context.connections[0]
        connection.send = lambda message: connection.sent.append(message)
        processes[0].alive = False

        with pytest.raises(Exception, match="ended"):
            executor.execute_enrichment(first.bundle_token)

        assert executor.owned_attempt_ids() == ()
        assert executor.attempt_state_snapshot() == {}
        assert executor._enrichment_retries == {}
        assert sorted(failures) == [
            ("dead-a", "ENRICHMENT_SLOT_LOST"),
            ("dead-b", "ENRICHMENT_SLOT_LOST"),
        ]
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and (
            len(context.connections) < 2
            or executor._slots[0].process is processes[0]
        ):
            time.sleep(0.01)
        assert len(context.connections) == 2
        context.connections[1].slot_id = 0
        fresh = [
            executor.execute_decision(
                _job(f"fresh-{index}"),
                _audio(),
                object(),
            )
            for index in range(6)
        ]
        assert len(executor.attempt_state_snapshot()) == 6
        for decision in fresh:
            assert executor.discard_attempt(
                decision.identity.analysis_attempt_id
            ) is True
        assert executor.attempt_state_snapshot() == {}
    finally:
        executor.close()


def test_dead_slot_is_quarantined_while_healthy_slot_serves_and_replacement_warms(
    monkeypatch,
):
    context = _ReplacingContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=2)
    try:
        dead_owner = executor.execute_decision(
            _job("dead-owner"), _audio(), object()
        )
        _commit_for_direct_enrichment(executor, "dead-owner")
        dead_connection = context.connections[0]
        dead_connection.send = lambda message: dead_connection.sent.append(message)
        context.processes[0].alive = False

        with pytest.raises(Exception, match="ended"):
            executor.execute_enrichment(dead_owner.bundle_token)
        assert "dead-owner" not in executor.owned_attempt_ids()

        healthy = executor.execute_decision(
            _job("healthy-next"), _audio(), object()
        )
        assert healthy.bundle_token.slot_id == 1
        assert [message[0] for message in context.connections[1].sent] == [
            "decision"
        ]

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(context.connections) < 3:
            time.sleep(0.01)
        replacement = context.connections[2]
        replacement.ready_release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with executor._state_lock:
                if executor._slots[0].process is context.processes[2]:
                    break
            time.sleep(0.01)

        replaced = executor.execute_decision(
            _job("replacement-next"), _audio(), object()
        )
        assert replaced.bundle_token.slot_id == 0
        assert [message[0] for message in replacement.sent] == ["decision"]
    finally:
        if len(context.connections) >= 3:
            context.connections[2].ready_release.set()
        executor.close()


def test_idle_dead_empty_slot_restarts_with_selected_readiness(
    monkeypatch,
    tmp_path,
):
    snapshot = _toolchain_snapshot(tmp_path, "python resident.py")
    key = process_executor_module._toolchain_key(snapshot)
    context = _ReplacingContext((key,))
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        selected_toolchains=(snapshot,),
        require_loudness_backend=True,
    )
    try:
        executor.start()
        assert executor.toolchains_ready() is True
        context.processes[0].alive = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(context.connections) < 3:
            time.sleep(0.01)

        assert len(context.connections) == 3
        replacement = context.connections[2]
        replacement.ready_release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not executor.toolchains_ready():
            time.sleep(0.01)

        assert executor.toolchains_ready() is True
        assert executor._slots[0].process is context.processes[2]
        replacement_args = context.process_specs[2]["args"]
        assert replacement_args[2] == (snapshot,)
        assert replacement_args[4] is True
    finally:
        if len(context.connections) >= 3:
            context.connections[2].ready_release.set()
        executor.close()


def test_idle_dead_slot_releases_retained_decision_bundle(monkeypatch):
    context = _ReplacingContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=2)
    try:
        executor.execute_decision(_job("idle-owned"), _audio(), object())
        assert executor.owned_attempt_ids() == ("idle-owned",)
        context.processes[0].alive = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and (
            executor.owned_attempt_ids() or len(context.connections) < 3
        ):
            time.sleep(0.01)

        assert executor.owned_attempt_ids() == ()
        assert len(context.connections) == 3
        context.connections[2].ready_release.set()
    finally:
        if len(context.connections) >= 3:
            context.connections[2].ready_release.set()
        executor.close()


def test_monitor_and_command_dead_detection_coalesce_one_replacement(monkeypatch):
    context = _ReplacingContext()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=2)
    errors = []
    try:
        decision = executor.execute_decision(
            _job("concurrent-death"), _audio(), object()
        )
        _commit_for_direct_enrichment(executor, "concurrent-death")
        dead_connection = context.connections[0]
        dead_connection.send = lambda message: dead_connection.sent.append(message)
        context.processes[0].alive = False

        def observe_command_death():
            try:
                executor.execute_enrichment(decision.bundle_token)
            except BaseException as exc:
                errors.append(exc)

        command_thread = threading.Thread(target=observe_command_death)
        command_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(context.connections) < 3:
            time.sleep(0.01)
        time.sleep(0.1)

        assert len(context.connections) == 3
        assert executor.owned_attempt_ids() == ()
        assert executor._replacing_slot_ids == {0}
        context.connections[2].ready_release.set()
        command_thread.join(1.0)
        assert not command_thread.is_alive()
        assert len(errors) == 1
    finally:
        if len(context.connections) >= 3:
            context.connections[2].ready_release.set()
        executor.close()


def test_replacement_retries_after_first_startup_failure_then_recovers(
    monkeypatch,
):
    context = _RetryReplacementContext(fail_replacements=1)
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        executor.start()
        context.processes[0].alive = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with executor._state_lock:
                recovered = (
                    len(context.processes) >= 3
                    and executor._slots[0].process is context.processes[2]
                )
            if recovered:
                break
            time.sleep(0.01)

        assert recovered is True
        assert executor.pool_failure() is None
        decision = executor.execute_decision(
            _job("after-replacement-retry"), _audio(), object()
        )
        assert decision.bundle_token.slot_id == 0
    finally:
        executor.close()


def test_exhausted_replacement_sets_pool_failure_and_decisions_fail_promptly(
    monkeypatch,
):
    context = _RetryReplacementContext(fail_replacements=3)
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        executor.start()
        context.processes[0].alive = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(context.processes) < 4:
            time.sleep(0.01)

        assert len(context.processes) == 4
        assert executor.pool_failure() == (
            "analysis process slot 0 failed to restart"
        )
        started = time.monotonic()
        with pytest.raises(Exception, match="failed to restart"):
            executor.execute_decision(
                _job("after-replacement-exhausted"), _audio(), object()
            )
        assert time.monotonic() - started < 0.2
    finally:
        executor.close()


def test_shutdown_cancels_slot_replacement_retries(monkeypatch):
    context = _RetryReplacementContext(fail_replacements=99)
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    executor.start()
    context.processes[0].alive = False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(context.processes) < 2:
        time.sleep(0.01)

    executor.close()
    process_count_after_close = len(context.processes)
    time.sleep(0.2)

    assert len(context.processes) == process_count_after_close


def test_shutdown_releases_parent_registry_and_stops_child(monkeypatch):
    context = _Context()
    monkeypatch.setattr(
        process_executor_module.multiprocessing,
        "get_context",
        lambda method: context if method == "spawn" else None,
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    executor.execute_decision(_job("shutdown"), _audio(), object())

    executor.close()

    assert executor.owned_attempt_ids() == ()
    assert context.connections[0].sent[-1] == ("stop",)
    time.sleep(0.1)
    assert len(context.connections) == 1


def test_real_child_loop_retains_multiple_bundles_until_enrich_or_discard(tmp_path):
    audio = _RealAudio(sample_rate=8_000, channel_count=1)
    first_job = _real_job(tmp_path, audio)
    second_job = replace(
        first_job,
        run_id="run-second",
        analysis_attempt_id="attempt-second",
    )
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        first = executor.execute_decision(first_job, audio, _RealToken())
        second = executor.execute_decision(second_job, audio, _RealToken())
        _commit_for_direct_enrichment(
            executor,
            second.identity.analysis_attempt_id,
        )

        assert set(executor.owned_attempt_ids()) == {
            first.identity.analysis_attempt_id,
            second.identity.analysis_attempt_id,
        }
        enriched = executor.execute_enrichment(second.bundle_token)
        assert enriched.identity == second.identity
        assert executor.discard_attempt(first.identity.analysis_attempt_id) is True
        assert executor.owned_attempt_ids() == ()
    finally:
        executor.close()


def test_every_real_slot_prewarms_selected_revision_before_decision(tmp_path):
    script = _write_fake_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _resident_entrypoint(script))
    audio = _RealAudio(sample_rate=8_000, channel_count=1)
    base_job = _real_job(tmp_path, audio)
    job = replace(
        base_job,
        snapshot=replace(base_job.snapshot, ai_toolchain=snapshot),
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=2,
        selected_toolchains=(snapshot,),
    )
    try:
        executor.start()
        assert executor.toolchains_ready() is True
        first = executor.execute_decision(job, audio, _RealToken())
        second_job = replace(
            job,
            run_id="run-prewarmed-second",
            analysis_attempt_id="attempt-prewarmed-second",
        )
        second = executor.execute_decision(second_job, audio, _RealToken())

        assert first.judgment in {"PASS", "FAIL"}
        assert second.judgment in {"PASS", "FAIL"}
        assert {first.bundle_token.slot_id, second.bundle_token.slot_id}.issubset(
            {0, 1}
        )
    finally:
        executor.close()


def test_frozen_decision_never_substitutes_an_unwarmed_revision(tmp_path):
    script = _write_fake_resident_script(tmp_path)
    ready_snapshot = _toolchain_snapshot(tmp_path, _resident_entrypoint(script))
    stale_snapshot = replace(
        ready_snapshot,
        execution_revision="revision-not-prewarmed",
    )
    audio = _RealAudio(sample_rate=8_000, channel_count=1)
    base_job = _real_job(tmp_path, audio)
    job = replace(
        base_job,
        snapshot=replace(base_job.snapshot, ai_toolchain=stale_snapshot),
    )
    executor = ProcessIsolatedAnalysisExecutor(
        process_count=1,
        selected_toolchains=(ready_snapshot,),
    )
    try:
        with pytest.raises(Exception, match="analysis|Resident|revision"):
            executor.execute_decision(job, audio, _RealToken())
    finally:
        executor.close()


def test_pinned_runtime_recovers_readiness_through_existing_invalidation_path(
    tmp_path,
):
    script = _write_fake_resident_script(tmp_path)
    snapshot = _toolchain_snapshot(tmp_path, _resident_entrypoint(script))
    events = []
    runtime = process_executor_module.ResidentToolchainRuntime(
        readiness_changed=lambda _snapshot, ready: events.append(ready)
    )
    pinned = process_executor_module._PinnedResidentRuntime(runtime)
    try:
        first_instance = pinned.prewarm(snapshot)
        pinned.pin(snapshot)
        cached = next(iter(runtime._processes.values()))

        cached.process.kill()
        cached.process.wait(timeout=2.0)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if False in events and events.count(True) >= 2:
                break
            time.sleep(0.01)

        pinned.pin(snapshot)

        assert False in events
        assert events.count(True) == 2
        assert runtime.process_instance_id(snapshot) != first_instance
    finally:
        runtime.shutdown_all()
