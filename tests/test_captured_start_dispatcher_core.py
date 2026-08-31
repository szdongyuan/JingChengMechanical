from __future__ import annotations

import threading
import time

from app.service.captured_start_dispatcher import (
    CapturedStartDispatcher,
    CapturedStartWorkResult,
)


def _wait(event: threading.Event, message: str) -> None:
    assert event.wait(2.0), message


def test_plain_none_returning_work_remains_compatible() -> None:
    called = threading.Event()
    dispatcher = CapturedStartDispatcher(capacity=1, worker_count=1)
    try:
        assert dispatcher.try_submit(lambda: called.set(), lane_key="hardware-1")
        _wait(called, "ordinary captured-start work did not run")
    finally:
        assert dispatcher.close(timeout_seconds=2.0)


def test_parked_work_keeps_permit_but_releases_worker_for_healthy_lane() -> None:
    wait_release = threading.Event()
    initial_ran = threading.Event()
    resumed = threading.Event()
    healthy = threading.Event()
    dispatcher = CapturedStartDispatcher(capacity=2, worker_count=2)

    def parked_work() -> CapturedStartWorkResult:
        initial_ran.set()
        return CapturedStartWorkResult.parked(
            wait=wait_release.wait,
            continuation=lambda: resumed.set(),
        )

    try:
        assert dispatcher.try_submit(parked_work, lane_key="parked-hardware")
        _wait(initial_ran, "parked work did not enter")
        assert dispatcher.try_submit(
            lambda: healthy.set(), lane_key="healthy-hardware"
        )
        assert not dispatcher.try_submit(lambda: None, lane_key="third-hardware")
        _wait(healthy, "parked work consumed a captured-start worker")

        wait_release.set()
        _wait(resumed, "settled parked work was not resumed")

        deadline = time.monotonic() + 2.0
        while not dispatcher.try_submit(lambda: None, lane_key="third-hardware"):
            assert time.monotonic() < deadline, "completion did not release permit"
            time.sleep(0.005)
    finally:
        wait_release.set()
        assert dispatcher.close(timeout_seconds=2.0)


def test_parked_continuation_requeues_on_the_same_lane_worker() -> None:
    wait_release = threading.Event()
    completed = threading.Event()
    thread_names: list[str] = []
    dispatcher = CapturedStartDispatcher(capacity=1, worker_count=1)

    def initial() -> CapturedStartWorkResult:
        thread_names.append(threading.current_thread().name)

        def continuation() -> None:
            thread_names.append(threading.current_thread().name)
            completed.set()

        return CapturedStartWorkResult.parked(
            wait=wait_release.wait,
            continuation=continuation,
        )

    try:
        assert dispatcher.try_submit(initial, lane_key="hardware-1")
        wait_release.set()
        _wait(completed, "continuation did not run")
        assert len(thread_names) == 2
        assert thread_names[0] == thread_names[1]
        assert thread_names[0].startswith("captured-start-dispatch-")
    finally:
        assert dispatcher.close(timeout_seconds=2.0)


def test_close_before_wait_settlement_cancels_once_and_never_resumes() -> None:
    wait_release = threading.Event()
    parked = threading.Event()
    canceled: list[str] = []
    resumed: list[str] = []
    dispatcher = CapturedStartDispatcher(capacity=1, worker_count=1)

    def work() -> CapturedStartWorkResult:
        parked.set()
        return CapturedStartWorkResult.parked(
            wait=wait_release.wait,
            continuation=lambda: resumed.append("resume"),
            cancel=lambda: canceled.append("cancel"),
        )

    assert dispatcher.try_submit(work, lane_key="hardware-1")
    _wait(parked, "work did not park")
    assert dispatcher.close(timeout_seconds=2.0)
    assert canceled == ["cancel"]
    wait_release.set()
    time.sleep(0.05)
    assert resumed == []


def test_wait_settlement_wins_close_race_and_releases_once() -> None:
    wait_release = threading.Event()
    resumed = threading.Event()
    cancel_count = 0
    dispatcher = CapturedStartDispatcher(capacity=1, worker_count=1)

    def cancel() -> None:
        nonlocal cancel_count
        cancel_count += 1

    def work() -> CapturedStartWorkResult:
        return CapturedStartWorkResult.parked(
            wait=wait_release.wait,
            continuation=lambda: resumed.set(),
            cancel=cancel,
        )

    assert dispatcher.try_submit(work, lane_key="hardware-1")
    wait_release.set()
    _wait(resumed, "settled continuation did not win the race")
    assert dispatcher.close(timeout_seconds=2.0)
    assert cancel_count == 0
