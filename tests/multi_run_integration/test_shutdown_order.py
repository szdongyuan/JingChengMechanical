from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QThread

from app.service.analysis.scheduler.models import (
    ShutdownReport as SchedulerShutdownReport,
    ShutdownStatus as SchedulerShutdownStatus,
)
from app.service.managed_input_contracts import (
    ManagedHardwareId,
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
    ManagedInputError,
    ManagedInputErrorCode,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    CoordinatorShutdownReport,
    CoordinatorShutdownStatus,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
)
from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
    ManagedResidentCaptureCloseReport,
    ManagedResidentCaptureCloseStatus,
)
from app.service.multi_run_integration.shutdown import (
    ApplicationShutdownOwner,
    ApplicationShutdownResources,
    PlcServiceThreadShutdownOwner,
)
from app.service.plc_service import PlcService
from app.service.plc_result_sequencer import PlcShutdownReport
from app.services_container import _invoke_plc_thread_slot
from app.service.run_persistence.run_persistence_scheduler_bridge import (
    BridgeDatabaseCloseResult,
    BridgeDatabaseCloseStatus,
    BridgeSchedulerShutdownResult,
    BridgeSchedulerShutdownStatus,
)
from app.service.run_scoped_playback.contracts import (
    PlaybackLifecycleCode,
    PlaybackLifecycleReport,
    PlaybackLifecycleStatus,
)


class _Clock:
    def __init__(self):
        self.value = 100.0
        self.samples = []

    def __call__(self):
        self.samples.append(self.value)
        self.value += 0.25
        return self.samples[-1]


class _Call:
    def __init__(self, trace, name, result=None):
        self.trace = trace
        self.name = name
        self.result = result
        self.arguments = []

    def invoke(self, *args):
        self.trace.append(self.name)
        self.arguments.append(args)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _scheduler_closed():
    return SchedulerShutdownReport(
        status=SchedulerShutdownStatus.CLOSED,
        started_monotonic=1.0,
        finished_monotonic=1.0,
        canceled_pending=0,
        cancel_requested_running=0,
        remaining_attempt_ids=(),
        remaining_worker_ids=(),
        remaining_run_lanes=(),
        remaining_toolchain_lanes=(),
        control_thread_alive=False,
    )


def _coordinator_closed():
    dispatcher = SaveDispatcherShutdownReport(
        status=SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=(),
        live_worker_ids=(),
        errors=(),
    )
    return CoordinatorShutdownReport(
        status=CoordinatorShutdownStatus.CLOSED,
        remaining_ingress_run_ids=(),
        remaining_run_ids=(),
        remaining_attempt_ids=(),
        remaining_save_job_ids=(),
        pending_publication_run_ids=(),
        save_dispatcher_report=dispatcher,
        errors=(),
    )


def _bridge_closed():
    return BridgeSchedulerShutdownResult(
        status=BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED,
        scheduler_report=_scheduler_closed(),
        code=None,
    )


def _capture_closed():
    return ManagedResidentCaptureCloseReport(
        status=ManagedResidentCaptureCloseStatus.CLOSED,
        remaining_registration_ids=(),
        remaining_reservation_ids=(),
        remaining_waiter_ids=(),
    )


def _managed_closed():
    return (
        ManagedInputCloseResult(
            hardware_id=ManagedHardwareId(1),
            status=ManagedInputCloseStatus.CLOSED,
            error=None,
        ),
    )


def _playback_closed():
    return PlaybackLifecycleReport(
        status=PlaybackLifecycleStatus.CLEAN,
        code=PlaybackLifecycleCode.CLEAN,
        summary="Playback lifecycle is clean.",
    )


def _plc_closed():
    return PlcShutdownReport(
        clean=True,
        clear_returned_plc_ids=(),
        clear_failures=(),
        skipped_busy_plc_ids=(),
        dropped_tickets=(),
    )


def _database_closed():
    return BridgeDatabaseCloseResult(
        status=BridgeDatabaseCloseStatus.DATABASE_CLOSED,
        database_closed=True,
        restart_required=False,
        code=None,
    )


def _non_clean_result(stage):
    if stage == "bridge_a":
        return BridgeSchedulerShutdownResult(
            status=BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
            scheduler_report=replace(
                _scheduler_closed(),
                status=SchedulerShutdownStatus.IN_PROGRESS,
            ),
            code="SCHEDULER_IN_PROGRESS",
        )
    if stage == "finish":
        return replace(
            _coordinator_closed(),
            status=CoordinatorShutdownStatus.INCOMPLETE,
            errors=("coordinator incomplete",),
            restart_required=True,
        )
    if stage == "plc":
        return replace(_plc_closed(), clean=False)
    if stage == "capture":
        return ManagedResidentCaptureCloseReport(
            status=ManagedResidentCaptureCloseStatus.NON_CLEAN,
            remaining_registration_ids=(),
            remaining_reservation_ids=("reservation-1",),
            remaining_waiter_ids=(),
        )
    if stage == "resident":
        return (
            ManagedInputCloseResult(
                hardware_id=ManagedHardwareId(1),
                status=ManagedInputCloseStatus.FAILED,
                error=ManagedInputError(
                    ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
                    "resident_close",
                    "Resident close failed.",
                ),
            ),
        )
    if stage == "playback":
        return PlaybackLifecycleReport(
            status=PlaybackLifecycleStatus.NON_CLEAN,
            code=PlaybackLifecycleCode.BUSY,
            summary="Playback is busy with another operation.",
        )
    if stage == "bridge_b":
        return BridgeDatabaseCloseResult(
            status=BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED,
            database_closed=True,
            restart_required=True,
            code="DATABASE_CLOSED_UNEXPECTED",
        )
    # These public boundaries are void. A non-None return is their exact
    # non-clean/fail-closed observation; they have no separate report type.
    return False


def _harness(*, overrides=None):
    trace = []
    calls = {
        "begin": _Call(trace, "coordinator.begin"),
        "bridge_a": _Call(trace, "bridge.phase_a", _bridge_closed()),
        "finish": _Call(trace, "coordinator.finish", _coordinator_closed()),
        "producer": _Call(trace, "producer.close"),
        "plc": _Call(trace, "plc.shutdown", _plc_closed()),
        "plc_owner": _Call(trace, "plc_owner.close"),
        "capture": _Call(trace, "capture.close", _capture_closed()),
        "resident": _Call(trace, "resident.close", _managed_closed()),
        "playback": _Call(trace, "playback.shutdown", _playback_closed()),
        "writer": _Call(trace, "writer.close"),
        "config_writer": _Call(trace, "config_writer.close"),
        "config_db": _Call(trace, "config_db.close"),
        "backend": _Call(trace, "backend.close"),
        "bridge_b": _Call(trace, "bridge.phase_b", _database_closed()),
    }
    for name, result in (overrides or {}).items():
        calls[name].result = result
    resources = ApplicationShutdownResources(
        coordinator=SimpleNamespace(
            begin_shutdown=calls["begin"].invoke,
            finish_shutdown=calls["finish"].invoke,
        ),
        scheduler_bridge=SimpleNamespace(
            stop_and_join_scheduler=calls["bridge_a"].invoke,
            close_persistence_after_upstream_stopped=calls["bridge_b"].invoke,
        ),
        external_producers=(
            SimpleNamespace(close=calls["producer"].invoke),
        ),
        plc_sequencer=SimpleNamespace(shutdown=calls["plc"].invoke),
        plc_owners=(SimpleNamespace(close=calls["plc_owner"].invoke),),
        resident_capture=SimpleNamespace(
            close_and_join=calls["capture"].invoke,
        ),
        managed_resident=SimpleNamespace(close_all=calls["resident"].invoke),
        playback=SimpleNamespace(shutdown=calls["playback"].invoke),
        independent_writers=(SimpleNamespace(close=calls["writer"].invoke),),
        application_config_writers=(
            SimpleNamespace(close=calls["config_writer"].invoke),
        ),
        application_config_database=SimpleNamespace(
            close=calls["config_db"].invoke,
        ),
        shared_audio_backend=SimpleNamespace(close=calls["backend"].invoke),
    )
    return trace, calls, resources


def test_global_shutdown_uses_one_deadline_and_exact_run_skip_matrix():
    trace, calls, resources = _harness()
    clock = _Clock()
    notices = []
    owner = ApplicationShutdownOwner(
        resources,
        monotonic=clock,
        timeout_seconds=5.0,
        aggregate_notice=notices.append,
    )

    report = owner.shutdown()
    repeated = owner.shutdown()

    assert report.clean is True
    assert report is repeated
    assert notices == [report]
    assert trace == [
        "coordinator.begin",
        "bridge.phase_a",
        "coordinator.finish",
        "producer.close",
        "plc.shutdown",
        "plc_owner.close",
        "capture.close",
        "resident.close",
        "playback.shutdown",
        "writer.close",
        "config_writer.close",
        "config_db.close",
        "backend.close",
        "bridge.phase_b",
    ]
    assert calls["finish"].arguments == [(105.0,)]
    duration_arguments = [
        args[0]
        for name in ("bridge_a", "plc", "capture", "resident", "playback")
        for args in calls[name].arguments
    ]
    assert duration_arguments
    assert all(value >= 0.0 for value in duration_arguments)
    assert duration_arguments == sorted(duration_arguments, reverse=True)


def test_registered_process_plc_stop_is_cached_once_before_config_db_backend_and_phase_b():
    trace, _calls, resources = _harness()

    def stop_process_param_service():
        trace.append("process-plc.stop")

    resources = replace(
        resources,
        independent_writers=(stop_process_param_service,),
    )
    owner = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    )

    report = owner.shutdown()
    repeated = owner.shutdown()

    assert report.clean is True
    assert repeated is report
    assert trace.count("process-plc.stop") == 1
    assert trace.index("process-plc.stop") < trace.index("config_db.close")
    assert trace.index("process-plc.stop") < trace.index("backend.close")
    assert trace.index("process-plc.stop") < trace.index("bridge.phase_b")


def test_registered_strict_process_plc_failure_is_nonclean_once_without_retry():
    trace, _calls, resources = _harness()
    invocations = []

    def strict_process_plc_stop():
        invocations.append("process-plc.stop")
        raise TimeoutError("process PLC thread did not stop")

    resources = replace(
        resources,
        independent_writers=(strict_process_plc_stop,),
    )
    owner = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    )

    report = owner.shutdown()
    repeated = owner.shutdown()

    assert report.clean is False
    assert repeated is report
    assert invocations == ["process-plc.stop"]
    assert "backend.close" in trace
    assert "bridge.phase_b" not in trace


@pytest.mark.parametrize(
    ("stage", "result", "expected_trace"),
    [
        ("begin", RuntimeError("begin"), ("coordinator.begin",)),
        (
            "bridge_a",
            object(),
            ("coordinator.begin", "bridge.phase_a"),
        ),
        (
            "finish",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
            ),
        ),
        (
            "producer",
            RuntimeError("producer"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "plc",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "plc_owner",
            RuntimeError("plc owner"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "capture",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
            ),
        ),
        (
            "resident",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
            ),
        ),
        (
            "playback",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
            ),
        ),
        (
            "writer",
            RuntimeError("writer"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "config_writer",
            RuntimeError("writer"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "backend.close",
            ),
        ),
        (
            "config_db",
            RuntimeError("db"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "backend",
            RuntimeError("backend"),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
            ),
        ),
        (
            "bridge_b",
            object(),
            (
                "coordinator.begin",
                "bridge.phase_a",
                "coordinator.finish",
                "producer.close",
                "plc.shutdown",
                "plc_owner.close",
                "capture.close",
                "resident.close",
                "playback.shutdown",
                "writer.close",
                "config_writer.close",
                "config_db.close",
                "backend.close",
                "bridge.phase_b",
            ),
        ),
    ],
)
def test_every_exception_timeout_malformed_or_non_clean_stage_is_once_and_fail_closed(
    stage,
    result,
    expected_trace,
):
    trace, calls, resources = _harness(overrides={stage: result})
    owner = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    )

    report = owner.shutdown()

    assert report.clean is False
    assert report.restart_required is True
    assert owner.shutdown() is report
    assert all(len(call.arguments) <= 1 for call in calls.values())
    assert tuple(trace) == expected_trace


@pytest.mark.parametrize(
    "stage",
    (
        "begin",
        "bridge_a",
        "finish",
        "producer",
        "plc",
        "plc_owner",
        "capture",
        "resident",
        "playback",
        "writer",
        "config_writer",
        "config_db",
        "backend",
        "bridge_b",
    ),
)
@pytest.mark.parametrize(
    "outcome_kind",
    ("exception", "timeout", "malformed", "exact_non_clean"),
)
def test_each_shutdown_stage_covers_exception_timeout_malformed_and_exact_non_clean(
    stage,
    outcome_kind,
):
    result = {
        "exception": RuntimeError(f"{stage} exception"),
        "timeout": TimeoutError(f"{stage} timeout"),
        "malformed": object(),
        "exact_non_clean": _non_clean_result(stage),
    }[outcome_kind]
    _trace, calls, resources = _harness(overrides={stage: result})
    notices = []
    owner = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
        aggregate_notice=notices.append,
    )

    report = owner.shutdown()

    assert report.clean is False
    assert report.restart_required is True
    assert owner.shutdown() is report
    assert notices == [report]
    assert len(calls[stage].arguments) == 1
    assert all(len(call.arguments) <= 1 for call in calls.values())


def test_prepared_only_shutdown_closes_owned_graph_lease_and_run_database_last():
    trace = []
    prepared = SimpleNamespace(close=lambda: trace.append("prepared.close"))
    writer = SimpleNamespace(shutdown=lambda: trace.append("writer.close"))
    config_db = SimpleNamespace(close=lambda: trace.append("config_db.close"))
    run_db = SimpleNamespace(
        close_after_upstream_stopped=lambda: trace.append("run_db.close")
    )
    owner = ApplicationShutdownOwner(
        ApplicationShutdownResources(
            prepared_composition=prepared,
            independent_writers=(writer,),
            application_config_database=config_db,
            run_persistence_database=run_db,
        ),
        monotonic=_Clock(),
        timeout_seconds=5.0,
    )

    report = owner.shutdown()

    assert report.clean is True
    assert trace == [
        "prepared.close",
        "writer.close",
        "config_db.close",
        "run_db.close",
    ]


def test_missing_required_active_owner_is_non_clean_and_never_closes_run_database():
    _trace, calls, resources = _harness()
    resources = replace(resources, playback=None)

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is False
    assert any(
        stage.name == "playback"
        and stage.status.value == "NON_CLEAN"
        for stage in report.stages
    )
    assert calls["bridge_b"].arguments == []


def test_failed_producer_blocks_only_its_plc_owner_and_not_the_sequencer_or_other_owner():
    trace, calls, resources = _harness()
    producer_2 = _Call(trace, "producer-2.close")
    owner_2 = _Call(trace, "plc-owner-2.close")
    calls["producer"].result = RuntimeError("producer 1 failed")
    resources = replace(
        resources,
        external_producers=(
            resources.external_producers[0],
            SimpleNamespace(close=producer_2.invoke),
        ),
        plc_owners=(
            resources.plc_owners[0],
            SimpleNamespace(close=owner_2.invoke),
        ),
    )

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is False
    assert calls["plc"].arguments
    assert calls["plc_owner"].arguments == []
    assert owner_2.arguments == [()]
    assert calls["capture"].arguments
    assert calls["bridge_b"].arguments == []


def test_plc_thread_owner_quits_then_joins_with_remaining_budget_once():
    trace, _calls, resources = _harness()

    class ThreadOwner:
        def __init__(self):
            self.wait_arguments = []

        def quit(self):
            trace.append("thread.quit")

        def wait(self, milliseconds):
            trace.append("thread.wait")
            self.wait_arguments.append(milliseconds)
            return True

    thread = ThreadOwner()
    resources = replace(
        resources,
        plc_owners=(thread,),
        external_producers=(),
    )

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is True
    assert thread.wait_arguments
    assert 0 <= thread.wait_arguments[0] <= 5000
    assert trace.index("plc.shutdown") < trace.index("thread.quit")
    assert trace.index("thread.quit") < trace.index("thread.wait")
    assert trace.index("thread.wait") < trace.index("capture.close")


def test_plc_split_stop_preserves_connection_until_sequencer_then_disconnects_before_thread():
    trace, calls, resources = _harness()

    class Service:
        def stop_ingress_polling(self):
            raise AssertionError("PLC slot called outside its owning thread")

        def disconnect_plc_service(self):
            raise AssertionError("PLC slot called outside its owning thread")

    class Thread:
        running = True

        def isRunning(self):
            trace.append("thread.running")
            return self.running

        def quit(self):
            trace.append("thread.quit")
            self.running = False

        def wait(self, _milliseconds):
            trace.append("thread.wait")
            return True

    service = Service()
    thread = Thread()
    invocations = []

    def invoke_slot(target, target_thread, method_name):
        assert target is service
        assert target_thread is thread
        assert target_thread.running is True
        invocations.append(method_name)
        trace.append(f"invoke.{method_name}")

    binding = PlcServiceThreadShutdownOwner(
        service,
        thread,
        invoke_slot,
    )
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is True
    assert invocations == [
        "stop_ingress_polling",
        "disconnect_plc_service",
    ]
    assert trace.index("invoke.stop_ingress_polling") < trace.index("plc.shutdown")
    assert trace.index("plc.shutdown") < trace.index("invoke.disconnect_plc_service")
    assert trace.index("invoke.disconnect_plc_service") < trace.index("thread.quit")
    assert trace.index("thread.quit") < trace.index("thread.wait")


def test_real_plc_service_completes_thread_affine_application_shutdown():
    app = QCoreApplication.instance() or QCoreApplication([])
    _trace, _calls, resources = _harness()
    bus = SimpleNamespace(log=SimpleNamespace(emit=lambda *_args: None))
    client = MagicMock()
    service = PlcService(
        bus,
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"start_signal_offset": "0.0"},
        plc_id=7,
    )
    service.plc_client = client
    thread = QThread()
    service.moveToThread(thread)
    thread.finished.connect(service.deleteLater)
    thread.start()
    binding = PlcServiceThreadShutdownOwner(
        service,
        thread,
        _invoke_plc_thread_slot,
    )
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    try:
        report = ApplicationShutdownOwner(
            resources,
            monotonic=_Clock(),
            timeout_seconds=5.0,
        ).shutdown()

        assert report.clean is True
        assert report.restart_required is False
        client.disconnect.assert_called_once_with()
        assert thread.isRunning() is False
    finally:
        if thread.isRunning():
            thread.quit()
            thread.wait(1000)
        del app


def test_real_plc_disconnect_failure_is_nonclean_across_thread_boundary():
    app = QCoreApplication.instance() or QCoreApplication([])
    _trace, _calls, resources = _harness()
    bus = SimpleNamespace(log=SimpleNamespace(emit=lambda *_args: None))
    client = MagicMock()
    client.disconnect.side_effect = RuntimeError("disconnect failed")
    service = PlcService(
        bus,
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"start_signal_offset": "0.0"},
        plc_id=7,
    )
    service.plc_client = client
    thread = QThread()
    service.moveToThread(thread)
    thread.finished.connect(service.deleteLater)
    thread.start()
    binding = PlcServiceThreadShutdownOwner(
        service,
        thread,
        _invoke_plc_thread_slot,
    )
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    try:
        report = ApplicationShutdownOwner(
            resources,
            monotonic=_Clock(),
            timeout_seconds=5.0,
        ).shutdown()

        assert report.clean is False
        assert report.restart_required is True
        owner_stage = next(
            stage for stage in report.stages if stage.name == "plc_owners[0]"
        )
        assert "disconnect failed" in (owner_stage.detail or "")
        client.disconnect.assert_called_once_with()
    finally:
        if thread.isRunning():
            thread.quit()
            thread.wait(1000)
        del app


def test_plc_thread_affine_invocation_failure_is_nonclean_once_without_disconnect_retry():
    trace, calls, resources = _harness()

    class Service:
        def stop_ingress_polling(self):
            raise AssertionError("PLC slot called outside its owning thread")

        def disconnect_plc_service(self):
            raise AssertionError("PLC slot called outside its owning thread")

    class Thread:
        def isRunning(self):
            return True

        def quit(self):
            trace.append("thread.quit")

        def wait(self, _milliseconds):
            trace.append("thread.wait")
            return True

    invocations = []

    def invoke_slot(_service, thread, method_name):
        assert thread.isRunning() is True
        invocations.append(method_name)
        raise RuntimeError("queued invocation failed")

    binding = PlcServiceThreadShutdownOwner(Service(), Thread(), invoke_slot)
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is False
    assert invocations == ["stop_ingress_polling"]
    assert len(calls["plc"].arguments) == 1
    assert "thread.quit" not in trace
    assert "thread.wait" not in trace


@pytest.mark.parametrize("thread", [None, SimpleNamespace(isRunning=lambda: False)])
def test_plc_thread_affine_invocation_rejects_absent_or_stopped_thread(thread):
    _trace, calls, resources = _harness()
    invocations = []
    binding = PlcServiceThreadShutdownOwner(
        SimpleNamespace(),
        thread,
        lambda *_args: invocations.append(_args),
    )
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is False
    assert invocations == []
    assert len(calls["plc"].arguments) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (PlaybackLifecycleStatus.CLEAN, PlaybackLifecycleCode.BUSY),
        (PlaybackLifecycleStatus.NON_CLEAN, PlaybackLifecycleCode.CLEAN),
    ],
)
def test_playback_requires_exact_clean_status_and_code_before_backend_and_phase_b(
    status,
    code,
):
    _trace, calls, resources = _harness()
    malformed = object.__new__(PlaybackLifecycleReport)
    object.__setattr__(malformed, "status", status)
    object.__setattr__(malformed, "code", code)
    object.__setattr__(malformed, "summary", "malformed")
    calls["playback"].result = malformed

    report = ApplicationShutdownOwner(
        resources,
        monotonic=_Clock(),
        timeout_seconds=5.0,
    ).shutdown()

    assert report.clean is False
    assert calls["backend"].arguments == []
    assert calls["bridge_b"].arguments == []
