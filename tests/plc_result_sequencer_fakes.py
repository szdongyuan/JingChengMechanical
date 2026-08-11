from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any


class RecordingPorts:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.result_calls: list[tuple[int, dict[str, object]]] = []
        self.testing_calls: list[tuple[int, bool]] = []
        self.reports: list[object] = []

    def write_result(self, plc_id: int, payload: Mapping[str, object]) -> None:
        with self._lock:
            self.result_calls.append((plc_id, dict(payload)))

    def write_testing_state(self, plc_id: int, testing: bool) -> None:
        with self._lock:
            self.testing_calls.append((plc_id, testing))

    def report_attempt(self, report: object) -> None:
        with self._lock:
            self.reports.append(report)


class BlockingTestingStatePort:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, plc_id: int, testing: bool) -> None:
        with self._lock:
            self.calls.append((plc_id, testing))
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release testing-state boundary")


class BlockingFalseTestingStatePort:
    def __init__(self) -> None:
        self.false_entered = threading.Event()
        self.release_false = threading.Event()
        self._lock = threading.Lock()
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, plc_id: int, testing: bool) -> None:
        with self._lock:
            self.calls.append((plc_id, testing))
        if not testing:
            self.false_entered.set()
            if not self.release_false.wait(timeout=5):
                raise TimeoutError("test did not release false boundary")


class BlockingResultPort:
    def __init__(self, *, blocked_plc_id: int | None = None) -> None:
        self.blocked_plc_id = blocked_plc_id
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls: list[tuple[int, dict[str, object]]] = []

    def __call__(self, plc_id: int, payload: Mapping[str, object]) -> None:
        with self._lock:
            self.calls.append((plc_id, dict(payload)))
        if self.blocked_plc_id is None or plc_id == self.blocked_plc_id:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release result boundary")


class RaisingResultPort:
    def __init__(self, failures: dict[object, Exception]) -> None:
        self.failures = failures
        self.calls: list[tuple[int, dict[str, object]]] = []

    def __call__(self, plc_id: int, payload: Mapping[str, object]) -> None:
        copied = dict(payload)
        self.calls.append((plc_id, copied))
        error = self.failures.get(copied.get("value"))
        if error is not None:
            raise error


class ObservableLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._acquire_count = 0
        self.second_acquire_entered = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._count_lock:
            self._acquire_count += 1
            if self._acquire_count == 2:
                self.second_acquire_entered.set()
        if timeout == -1:
            return self._lock.acquire(blocking)
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> ObservableLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


class RaisingTestingStatePort:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, plc_id: int, testing: bool) -> None:
        self.calls.append((plc_id, testing))
        raise self.error


class SelectiveRaisingTestingStatePort:
    def __init__(self, failures: dict[int, Exception]) -> None:
        self.failures = failures
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, plc_id: int, testing: bool) -> None:
        self.calls.append((plc_id, testing))
        error = self.failures.get(plc_id)
        if error is not None:
            raise error


class ImmediatelyBusyLock:
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if blocking and timeout < 0:
            raise AssertionError("test lock only supports bounded acquisition")
        return False

    def release(self) -> None:
        raise AssertionError("an unacquired test lock cannot be released")


class RaisingReportSink:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[Any] = []

    def __call__(self, report: object) -> None:
        self.calls.append(report)
        raise self.error
