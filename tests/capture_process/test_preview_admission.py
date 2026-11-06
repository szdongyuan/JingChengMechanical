from types import SimpleNamespace

import pytest

from app.service.multi_run_integration import production_composition as composition
from tests.multi_run_coordinator_mvp._support import captured_request, canonical_payload


def production_freezer(tmp_path, **kwargs):
    config = SimpleNamespace(
        config_id=7, input_hardware_device_id=1, selected_channels=(0,),
        values={"config_name": "test", "fault_types_json": "[]", "analysis_duration": .1,
                "analysis_window_offset_ms": 0},
        channels=(SimpleNamespace(values={"channel_index": 0, "fft_band_limits": [],
                                          "oct_band_limits": [], "ai_config": {}}),),
        managed_input=SimpleNamespace(descriptor_sample_rate_hz=8000, descriptor_input_channels=1,
                                      calibration_revision=1, calibration=((0, True, 1.0),),
                                      values={"hardware_id": 1}))
    return composition._FrozenRunFreezer(SimpleNamespace(configs=(config,)), tmp_path, **kwargs)


def request():
    from dataclasses import replace
    return replace(captured_request(), ingress_payload=canonical_payload({"config": {"config_id": 7}}))


def test_freezing_before_admission_does_not_start_optional_preview(tmp_path):
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    try:
        freezer = production_freezer(tmp_path)
        freezer.freeze(request())
        # Reserve rejection or abort immediately after freeze has no preview owner.
        assert not manager.has_active_runs()
        assert manager.wait_until_setup_idle(.1)
    finally:
        manager.close()


@pytest.mark.parametrize("admission", ["committed", "rejected", "exception"])
def test_only_committed_active_admission_registers_preview(tmp_path, admission):
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    persistence = composition._CoordinatorPersistence(object(), live_display=manager)
    def insert(_fact):
        if admission == "exception":
            raise RuntimeError("admission failed")
        return SimpleNamespace(state=composition.TransactionState.COMMITTED,
                               value=admission == "committed")
    persistence._runs = SimpleNamespace(insert_active=insert)
    try:
        frozen = production_freezer(tmp_path).freeze(request())
        if admission == "exception":
            with pytest.raises(RuntimeError, match="admission failed"):
                persistence.create_active(frozen)
        else:
            persistence.create_active(frozen)
        assert manager.wait_until_setup_idle(1)
        assert manager.has_active_runs() is (admission == "committed")
    finally:
        manager.close()


@pytest.mark.parametrize("failure", ["reserve", "active_rejected", "active_exception",
                                    "register", "start", "abort_freeze", "abort_commit"])
def test_coordinator_aborted_admission_leaves_no_optional_preview(tmp_path, failure):
    from app.service.multi_run_integration.capture_preview import PreviewCapturePort
    from app.service.multi_run_coordinator_mvp.contracts import StartStatus
    from tests.multi_run_coordinator_mvp._support import Harness
    harness = Harness()
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    freezer = production_freezer(tmp_path)
    harness.captured_freezer = freezer
    delegate = harness.resident
    harness.resident = PreviewCapturePort(delegate, manager)
    persistence = composition._CoordinatorPersistence(object(), live_display=manager)
    commits = []
    coordinator = None
    def insert(fact):
        commits.append(fact)
        if failure == "active_exception":
            raise RuntimeError("active insert failed")
        if failure == "abort_commit":
            coordinator.begin_shutdown()
        return SimpleNamespace(state=composition.TransactionState.COMMITTED,
                               value=failure != "active_rejected")
    persistence._runs = SimpleNamespace(
        insert_active=insert,
        finish_active=lambda *_args: SimpleNamespace(state=composition.TransactionState.COMMITTED, value=True))
    harness.persistence = persistence
    if failure in ("reserve", "register", "start"):
        setattr(delegate, failure + "_exception", RuntimeError(failure + " failed"))
    if failure == "abort_freeze":
        freeze = freezer.freeze
        def aborted_freeze(value):
            result = freeze(value)
            coordinator.begin_shutdown()
            return result
        freezer.freeze = aborted_freeze
    coordinator = harness.coordinator()
    try:
        result = coordinator.start_captured(request())
        assert result.status is not StartStatus.ACCEPTED_ACTIVE
        assert manager.wait_until_setup_idle(1)
        assert not manager.has_active_runs()
        if failure in ("reserve", "abort_freeze"):
            assert not commits
    finally:
        manager.close()


def test_terminal_persistence_failure_still_finishes_preview(tmp_path):
    from datetime import datetime, timezone
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    persistence = composition._CoordinatorPersistence(object(), live_display=manager)
    def failed_terminal(*args):
        raise RuntimeError("terminal persistence failed")
    persistence._runs = SimpleNamespace(
        insert_active=lambda _fact: SimpleNamespace(state=composition.TransactionState.COMMITTED, value=True),
        finish_active=failed_terminal)
    try:
        frozen = production_freezer(tmp_path).freeze(request())
        persistence.create_active(frozen)
        assert manager.wait_until_setup_idle(1)
        with pytest.raises(RuntimeError, match="terminal persistence failed"):
            persistence.commit_initial_terminal(SimpleNamespace(
                run_id=frozen.run_id, conclusion=composition.TerminalConclusion.MANUAL_STOPPED,
                error_category=None, reason="aborted", terminal_at=datetime.now(timezone.utc)))
        assert not manager.has_active_runs()
    finally:
        manager.close()
