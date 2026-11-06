import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.service.multi_run_integration.capture_preview import PreviewCapturePort
from app.service.multi_run_integration.production_composition import _ProductionLiveDisplayManager
from tests.capture_process.test_preview import frozen


def test_terminal_sink_is_not_blocked_by_monitor_cleanup_and_keeps_outcome_identity():
    entered, release, delivered = (threading.Event() for _ in range(3))
    manager = _ProductionLiveDisplayManager(bus=SimpleNamespace())
    class Monitor:
        def stop(self):
            entered.set()
            assert release.wait(3)
        def wait(self, _timeout):
            return True
    sinks, received = [], []
    port = PreviewCapturePort(SimpleNamespace(
        register_completion=lambda reservation, sink: sinks.append(sink)), manager)
    from app.service.multi_run_coordinator_mvp.contracts import CaptureReservation
    run_id = str(uuid4())
    reservation = CaptureReservation("reservation", run_id, "1")
    outcome = object()
    caller = None
    try:
        manager.register_frozen(frozen(run_id))
        assert manager.wait_until_setup_idle(1)
        manager._runs_by_hardware["managed-input-1"][run_id].monitor = Monitor()
        port.register_completion(reservation, lambda actual: (received.append(actual), delivered.set()))
        caller = threading.Thread(target=lambda: sinks[0](outcome))
        caller.start()
        assert entered.wait(1)
        assert delivered.wait(.2)
        assert received == [outcome] and received[0] is outcome
        assert not manager.has_active_runs()
    finally:
        release.set()
        if caller is not None:
            caller.join(1)
        manager.close()


@pytest.mark.parametrize("invalid", ["sink", "cancel_reservation", "start_reservation", "register_reservation"])
def test_wrapper_preserves_managed_port_argument_validation(invalid):
    from app.service.managed_input_contracts import ManagedHardwareId, ResidentStreamGenerationStamp
    from app.service.multi_run_coordinator_mvp.contracts import CaptureReservation
    from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
        ManagedResidentCapturePort, ManagedResidentCaptureRoute)
    hardware = ManagedHardwareId(1)
    delegate = ManagedResidentCapturePort(SimpleNamespace(), (ManagedResidentCaptureRoute(
        "1", hardware, ResidentStreamGenerationStamp(hardware, 1, 1)),))
    port = PreviewCapturePort(delegate, SimpleNamespace(finish=lambda _run: None, finish_all=lambda: None))
    try:
        with pytest.raises(ValueError, match="sink must be callable" if invalid == "sink" else "exact CaptureReservation"):
            if invalid == "sink":
                port.register_completion(CaptureReservation("reservation", str(uuid4()), "1"), None)
            elif invalid == "cancel_reservation":
                port.cancel(None)
            elif invalid == "start_reservation":
                port.start(None, 1, 1)
            else:
                port.register_completion(None, lambda _outcome: None)
    finally:
        port.close_and_join(1.0)


def test_preview_cleanup_error_does_not_replace_capture_close_failure():
    def close(_timeout):
        raise RuntimeError("capture close failure")
    def finish_all():
        raise ValueError("preview cleanup failure")
    port = PreviewCapturePort(SimpleNamespace(close_and_join=close), SimpleNamespace(finish_all=finish_all))
    with pytest.raises(RuntimeError, match="capture close failure"):
        port.close_and_join(.1)
