from __future__ import annotations

import threading
import time

from app.service.analysis.resident_audio import (
    CaptureErrorCode,
    CaptureOwnerState,
)
from tests.resident_audio.test_capture_owner import (
    PausingWriter,
    activate,
    block,
    make_owner,
    make_ring,
    make_store,
    wait_until,
)
from tests.resident_audio.test_resident_pool import (
    desired,
    device,
    make_pool,
)


class ParallelCallbackClock:
    def __init__(self) -> None:
        self._armed = False
        self._barrier = threading.Barrier(6)
        self._lock = threading.Lock()
        self._active = 0
        self.maximum_active = 0

    def arm(self) -> None:
        self._armed = True

    def __call__(self) -> float:
        if not self._armed or not threading.current_thread().name.startswith(
            "stress-callback-"
        ):
            return 10.0
        with self._lock:
            self._active += 1
            self.maximum_active = max(self.maximum_active, self._active)
        try:
            self._barrier.wait(1.0)
            time.sleep(0.01)
            return 10.0
        finally:
            with self._lock:
                self._active -= 1


def test_six_pool_stream_callbacks_overlap_without_pool_wide_lock(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    callback_clock = ParallelCallbackClock()
    pool._clock = callback_clock
    hardware_ids = [f"mic-{index}" for index in range(6)]
    resolver.devices.update(
        {
            hardware_id: device(hardware_id)
            for hardware_id in hardware_ids
        }
    )
    assert pool.reconcile([desired(item) for item in hardware_ids]).accepted
    callback_clock.arm()

    workers = [
        threading.Thread(
            target=lambda handle=backend.handles[hardware_id][0]: handle.emit(
                frames=5,
                channels=1,
                callback_monotonic=10.0,
                adc_time=9.99,
                current_time=10.0,
            ),
            name=f"stress-callback-{index}",
        )
        for index, hardware_id in enumerate(hardware_ids)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2.0)
        assert not worker.is_alive()

    assert callback_clock.maximum_active == 6
    for hardware_id in hardware_ids:
        assert pool.synchronize_stream(hardware_id, timeout=1.0)
    pool.shutdown()


def test_six_overlapping_owners_isolate_ninth_reference_to_stalled_owner(
    tmp_path,
):
    ring = make_ring(block(0, 1))
    owners = []
    events = []
    stalled_writer = None
    stalled_store = make_store(tmp_path / "stalled-store")
    stalled_delegate = stalled_store.allocate_writer(
        run_id="owner-0",
        target_frames=20,
        channels=2,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
    )
    stalled_writer = PausingWriter(stalled_delegate, pause_start=1)

    for index in range(6):
        owner_events = []
        owner, _, _, _ = make_owner(
            tmp_path / f"owner-{index}",
            ring,
            run_id=f"owner-{index}",
            start=0,
            target_frames=20,
            writer=stalled_writer if index == 0 else None,
            terminal_events=owner_events,
        )
        activate(owner, 0)
        owners.append(owner)
        events.append(owner_events)
    for owner in owners:
        wait_until(lambda owner=owner: owner.snapshot().state is CaptureOwnerState.LIVE)

    assert owners[0].offer_live(block(1, 1))
    assert stalled_writer.entered.wait(1.0)
    for start in range(1, 10):
        for owner in owners[1:]:
            assert owner.offer_live(block(start, 1))
        for owner in owners[1:]:
            wait_until(lambda owner=owner: owner.snapshot().credits_occupied == 0)
        if start >= 2 and start <= 8:
            assert owners[0].offer_live(block(start, 1))

    assert owners[0].snapshot().credits_occupied == 8
    assert owners[0].offer_live(block(9, 1)) is False
    assert all(owner.snapshot().state is CaptureOwnerState.LIVE for owner in owners[1:])
    assert all(not owner_events for owner_events in events[1:])

    stalled_writer.release.set()
    wait_until(lambda: len(events[0]) == 1)
    assert events[0][0].error_code is CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG
    for owner in owners[1:]:
        owner.cancel("stress cleanup")
    for owner in owners:
        owner.join(1.0)
