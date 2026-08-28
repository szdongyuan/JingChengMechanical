from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_ABOVE_NORMAL,
    set_current_thread_priority,
)

__all__ = [
    "CapturedStartDispatcher",
    "CapturedStartWorkDisposition",
    "CapturedStartWorkResult",
]


class CapturedStartWorkDisposition(Enum):
    COMPLETE = auto()
    PARKED = auto()


@dataclass(frozen=True, slots=True)
class CapturedStartWorkResult:
    disposition: CapturedStartWorkDisposition
    wait: Callable[[], object] | None = None
    continuation: Callable[[], object] | None = None
    cancel: Callable[[], object] | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not CapturedStartWorkDisposition:
            raise ValueError("disposition must be a CapturedStartWorkDisposition")
        if self.disposition is CapturedStartWorkDisposition.COMPLETE:
            if any(
                callback is not None
                for callback in (self.wait, self.continuation, self.cancel)
            ):
                raise ValueError("COMPLETE work must not carry parked callbacks")
            return
        if not callable(self.wait) or not callable(self.continuation):
            raise ValueError("PARKED work requires wait and continuation callbacks")
        if self.cancel is not None and not callable(self.cancel):
            raise ValueError("parked cancel callback must be callable or None")

    @classmethod
    def complete(cls) -> CapturedStartWorkResult:
        return cls(CapturedStartWorkDisposition.COMPLETE)

    @classmethod
    def parked(
        cls,
        *,
        wait: Callable[[], object],
        continuation: Callable[[], object],
        cancel: Callable[[], object] | None = None,
    ) -> CapturedStartWorkResult:
        return cls(
            CapturedStartWorkDisposition.PARKED,
            wait,
            continuation,
            cancel,
        )


@dataclass(frozen=True, slots=True)
class _QueuedWork:
    work: Callable[[], object]
    lane_index: int


@dataclass(frozen=True, slots=True, eq=False)
class _ParkedTicket:
    result: CapturedStartWorkResult
    lane_index: int


class CapturedStartDispatcher:
    """Bounded lane dispatcher whose parked work retains admission capacity."""

    _STOP = object()

    def __init__(self, *, capacity: int = 2, worker_count: int = 2) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("captured start capacity must be positive")
        if type(worker_count) is not int or not 1 <= worker_count <= capacity:
            raise ValueError(
                "captured start worker count must be between one and capacity"
            )
        self._logger = logging.getLogger(__name__)
        self._capacity = threading.BoundedSemaphore(capacity)
        self._queues = tuple(queue.Queue() for _ in range(worker_count))
        self._lane_indexes: dict[object, int] = {}
        self._next_lane_index = 0
        self._lock = threading.Lock()
        self._accepting = True
        self._parked: set[_ParkedTicket] = set()
        self._threads: tuple[threading.Thread, ...] = tuple(
            threading.Thread(
                target=self._run,
                args=(lane_queue,),
                name=f"captured-start-dispatch-{index + 1}",
                daemon=True,
            )
            for index, lane_queue in enumerate(self._queues)
        )
        for thread in self._threads:
            thread.start()

    def try_submit(
        self,
        work: Callable[[], object],
        *,
        lane_key: object = None,
    ) -> bool:
        if not callable(work):
            raise TypeError("captured start work must be callable")
        try:
            hash(lane_key)
        except Exception:
            raise TypeError("captured start lane key must be hashable") from None
        if not self._capacity.acquire(blocking=False):
            return False
        with self._lock:
            if not self._accepting:
                self._capacity.release()
                return False
            lane_index = self._lane_indexes.get(lane_key)
            if lane_index is None:
                lane_index = self._next_lane_index
                self._lane_indexes[lane_key] = lane_index
                self._next_lane_index = (
                    self._next_lane_index + 1
                ) % len(self._queues)
            self._queues[lane_index].put(_QueuedWork(work, lane_index))
            return True

    def close(self, *, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            threads = self._threads
            if not threads:
                return True
            if self._accepting:
                self._accepting = False
                parked = tuple(self._parked)
                self._parked.clear()
                for lane_queue in self._queues:
                    lane_queue.put(self._STOP)
            else:
                parked = ()
        for ticket in parked:
            self._cancel_parked(ticket)
            self._capacity.release()
        if threading.current_thread() in threads:
            return False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = not any(thread.is_alive() for thread in threads)
        if stopped:
            with self._lock:
                self._threads = ()
        return stopped

    def _run(self, lane_queue: queue.Queue[object]) -> None:
        set_current_thread_priority(THREAD_PRIORITY_ABOVE_NORMAL)
        while True:
            queued = lane_queue.get()
            if queued is self._STOP:
                return
            assert type(queued) is _QueuedWork
            release_permit = True
            try:
                result = queued.work()
                if (
                    type(result) is CapturedStartWorkResult
                    and result.disposition
                    is CapturedStartWorkDisposition.PARKED
                ):
                    release_permit = not self._park(queued.lane_index, result)
            except Exception:
                self._logger.exception("captured start dispatch failed")
            finally:
                if release_permit:
                    self._capacity.release()

    def _park(
        self,
        lane_index: int,
        result: CapturedStartWorkResult,
    ) -> bool:
        ticket = _ParkedTicket(result, lane_index)
        with self._lock:
            if not self._accepting:
                accepted = False
            else:
                self._parked.add(ticket)
                accepted = True
        if not accepted:
            self._cancel_parked(ticket)
            return False
        threading.Thread(
            target=self._wait_for_parked,
            args=(ticket,),
            name=f"captured-start-parked-{lane_index + 1}",
            daemon=True,
        ).start()
        return True

    def _wait_for_parked(self, ticket: _ParkedTicket) -> None:
        assert ticket.result.wait is not None
        try:
            ticket.result.wait()
        except Exception:
            self._logger.exception("captured start parked wait failed")
        with self._lock:
            if ticket not in self._parked:
                return
            self._parked.remove(ticket)
            assert ticket.result.continuation is not None
            self._queues[ticket.lane_index].put(
                _QueuedWork(ticket.result.continuation, ticket.lane_index)
            )

    def _cancel_parked(self, ticket: _ParkedTicket) -> None:
        cancel = ticket.result.cancel
        if cancel is None:
            return
        try:
            cancel()
        except Exception:
            self._logger.exception("captured start parked cancel failed")
