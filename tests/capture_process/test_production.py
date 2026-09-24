from types import SimpleNamespace

import pytest

from app.service.multi_run_integration.shutdown import ApplicationShutdownOwner
from tests.multi_run_integration.test_shutdown_order import _harness
from tests.capture_process.test_worker import make_adapter
from tests.capture_process.fake_backend import endpoint


@pytest.mark.parametrize("failed_stage", ["bridge_a", "capture"])
def test_shutdown_failure_reaps_actual_owned_capture_child(failed_stage):
    from dataclasses import replace
    adapter = make_adapter()
    try:
        adapter.open_startup_streams((endpoint(),))
        _, _, resources = _harness(overrides={failed_stage: RuntimeError("shutdown failure")})
        resources = replace(resources, managed_resident=adapter)
        report = ApplicationShutdownOwner(resources, timeout_seconds=2.0).shutdown()
        assert report.clean is False
        assert not adapter.client.process.is_alive()
    finally:
        adapter.close_all(2.0)


def test_exhausted_application_deadline_still_reaps_owned_capture_child():
    from dataclasses import replace
    from itertools import chain, repeat
    import time
    adapter = make_adapter()
    try:
        adapter.open_startup_streams((endpoint(),))
        _, _, resources = _harness(overrides={"bridge_a": RuntimeError("earlier shutdown failure")})
        resources = replace(resources, managed_resident=adapter)
        clock = chain((100.0,), repeat(102.0))
        before = time.monotonic()
        report = ApplicationShutdownOwner(
            resources, monotonic=lambda: next(clock), timeout_seconds=1.0).shutdown()
        assert time.monotonic() - before < .8
        assert report.clean is False
        assert any("earlier shutdown failure" in error for error in report.errors)
        assert not adapter.client.process.is_alive()
        assert not any("must be positive" in error for error in report.errors)
    finally:
        adapter.close_all(2.0)


def test_actual_production_factory_connects_one_child_and_realtime_preview(monkeypatch, tmp_path):
    import os
    import threading
    import time
    from types import ModuleType
    from uuid import uuid4
    import numpy as np
    from app.service.capture_process.adapter import CaptureProcessManagedInputAdapter
    from tests.multi_run_integration.test_production_composition import _isolated_stream_prepare
    from tests.capture_process.test_preview import frozen
    factory, prerequisites, _, _ = _isolated_stream_prepare(
        monkeypatch, tmp_path, observed_hardware_ids=(1, 2), ready_hardware_ids={1, 2})
    stft = threading.Event()
    factory._sounddevice_module = ModuleType("sounddevice")
    factory._event_bus = SimpleNamespace(stft_updated=SimpleNamespace(emit=lambda *_args: stft.set()))
    factory._capture_process_factory = lambda **kwargs: CaptureProcessManagedInputAdapter(
        **kwargs, backend_factory="tests.capture_process.fake_backend:create_backend")
    upstream = factory.prepare_upstream(prerequisites)
    try:
        assert isinstance(upstream.managed_resident, CaptureProcessManagedInputAdapter)
        assert len(upstream.streams) == 2
        assert upstream.managed_resident.client.process.pid != os.getpid()
        assert prerequisites.adapter_calls == []
        for hardware_id in ("1", "2"):
            run_id = str(uuid4())
            trigger = time.monotonic_ns()
            if hardware_id == "1":
                upstream.live_display.register_frozen(frozen(run_id, trigger=trigger, frames=800, rate=8000))
            reservation = upstream.resident_capture.reserve(run_id, hardware_id).reservation
            received, completed = [], threading.Event()
            upstream.resident_capture.register_completion(
                reservation, lambda outcome: (received.append(outcome), completed.set()))
            upstream.resident_capture.start(reservation, trigger, 800)
            assert completed.wait(2)
            pcm = np.frombuffer(received[0].audio.interleaved_bytes, dtype="<f4")
            assert len(pcm) == 800
            np.testing.assert_array_equal(np.diff(pcm), np.ones(799))
        assert stft.wait(1), "production wiring did not forward child previews to realtime STFT"
    finally:
        upstream.close()
    assert not upstream.managed_resident.client.process.is_alive()
    assert not upstream.live_display._preview_pump.thread.is_alive()


@pytest.mark.parametrize("terminal", ["complete", "cancel", "child_failure", "start_failure"])
def test_production_capture_terminal_cancels_blocked_preview_setup(monkeypatch, tmp_path, terminal):
    from dataclasses import replace
    import threading
    import time
    from types import ModuleType
    from uuid import uuid4
    from app.service.capture_process.adapter import CaptureProcessManagedInputAdapter
    from app.service.multi_run_integration import production_composition as composition
    from tests.multi_run_integration.test_production_composition import _isolated_stream_prepare
    from tests.capture_process.test_preview_admission import production_freezer, request
    entered, release, completed = (threading.Event() for _ in range(3))
    original = composition.AnalysisPipeline
    def blocked_setup(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(composition, "AnalysisPipeline", blocked_setup)
    factory, prerequisites, _, _ = _isolated_stream_prepare(
        monkeypatch, tmp_path, observed_hardware_ids=(1,), ready_hardware_ids={1})
    factory._sounddevice_module = ModuleType("sounddevice")
    factory._event_bus = SimpleNamespace()
    factory._capture_process_factory = lambda **kwargs: CaptureProcessManagedInputAdapter(
        **kwargs, backend_factory="tests.capture_process.fake_backend:create_backend")
    upstream = factory.prepare_upstream(prerequisites)
    manager = upstream.live_display
    received = []
    try:
        # No future preview may be needed to settle terminal capture ownership.
        assert manager._preview_pump.close(1.0)
        run_id = str(uuid4())
        trigger = time.monotonic_ns()
        reservation = upstream.resident_capture.reserve(run_id, "1").reservation
        value = production_freezer(tmp_path).freeze(replace(
            request(), run_id=run_id, trigger_monotonic_ns=trigger))
        persistence = composition._CoordinatorPersistence(object(), live_display=manager)
        persistence._runs = SimpleNamespace(insert_active=lambda _fact: SimpleNamespace(
            state=composition.TransactionState.COMMITTED, value=True))
        assert persistence.create_active(value) is composition.CommitResult.COMMITTED
        assert entered.wait(1)
        if terminal == "cancel":
            upstream.resident_capture.cancel(reservation)
        else:
            upstream.resident_capture.register_completion(
                reservation, lambda outcome: (received.append(outcome), completed.set()))
            if terminal == "start_failure":
                target = upstream.resident_capture._reservations[reservation.reservation_id].target
                def fail_start(*args, **kwargs):
                    raise RuntimeError("start failed")
                monkeypatch.setattr(target.pool, "start_capture", fail_start)
                with pytest.raises(RuntimeError, match="start failed"):
                    upstream.resident_capture.start(reservation, trigger, 800)
            elif terminal == "child_failure":
                upstream.managed_resident.client.process.terminate()
                assert completed.wait(2)
            else:
                upstream.resident_capture.start(reservation, trigger, 800)
                assert completed.wait(2)
            if received:
                assert received[0].reservation is reservation
        release.set()
        assert manager.wait_until_setup_idle(2)
        assert not manager.has_active_runs(), "capture terminal left optional preview alive"
    finally:
        release.set()
        upstream.close()
