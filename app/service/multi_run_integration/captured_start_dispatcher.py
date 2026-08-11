from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_ABOVE_NORMAL,
    set_current_thread_priority,
)


class CapturedStartDispatcher:
    """Bounded FIFO admission lane for latency-sensitive captured starts."""

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
        work: Callable[[], None],
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
            self._queues[lane_index].put(work)
            return True

    def close(self, *, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            threads = self._threads
            if not threads:
                return True
            if self._accepting:
                self._accepting = False
                for lane_queue in self._queues:
                    lane_queue.put(self._STOP)
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
            work = lane_queue.get()
            if work is self._STOP:
                return
            try:
                work()
            except Exception:
                self._logger.exception("captured start dispatch failed")
            finally:
                self._capacity.release()
