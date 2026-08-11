from __future__ import annotations

import threading

from app.service.multi_run_integration.captured_start_dispatcher import (
    CapturedStartDispatcher,
)


def test_two_start_intents_are_admitted_without_waiting_and_processed_fifo():
    first_entered = threading.Event()
    release_first = threading.Event()
    completed = threading.Event()
    calls = []

    def first() -> None:
        calls.append("first")
        first_entered.set()
        assert release_first.wait(timeout=2)

    def second() -> None:
        calls.append("second")
        completed.set()

    dispatcher = CapturedStartDispatcher(capacity=2)
    try:
        assert dispatcher.try_submit(first) is True
        assert first_entered.wait(timeout=2)
        assert dispatcher.try_submit(second) is True
        assert dispatcher.try_submit(lambda: calls.append("third")) is False
        assert calls == ["first"]
        release_first.set()
        assert completed.wait(timeout=2)
    finally:
        release_first.set()
        assert dispatcher.close(timeout_seconds=2)

    assert calls == ["first", "second"]


def test_failed_start_intent_releases_capacity_and_keeps_worker_alive():
    failed = threading.Event()
    succeeded = threading.Event()

    def raises() -> None:
        failed.set()
        raise RuntimeError("start failed")

    dispatcher = CapturedStartDispatcher(capacity=2)
    try:
        assert dispatcher.try_submit(raises) is True
        assert failed.wait(timeout=2)
        assert dispatcher.try_submit(succeeded.set) is True
        assert succeeded.wait(timeout=2)
    finally:
        assert dispatcher.close(timeout_seconds=2)


def test_two_distinct_lanes_can_enter_start_admission_concurrently():
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        first_entered.set()
        assert release.wait(timeout=2)

    dispatcher = CapturedStartDispatcher(capacity=2, worker_count=2)
    try:
        assert dispatcher.try_submit(first, lane_key=("plc", 7))
        assert first_entered.wait(timeout=2)
        assert dispatcher.try_submit(
            second_entered.set,
            lane_key=("plc", 8),
        )
        assert second_entered.wait(timeout=2)
    finally:
        release.set()
        assert dispatcher.close(timeout_seconds=2)
