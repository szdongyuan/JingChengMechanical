"""Bounded PLC current lookback with an atomic replay/live subscription."""
from __future__ import annotations

from collections import deque
import math
import threading
import time

from PyQt6.QtCore import Qt


class CurrentHistoryUnavailable(ValueError):
    """The requested trace cannot be reconstructed from retained readings."""


class _Subscription:
    def __init__(self, owner, plc_id, origin, callback):
        self.owner = owner
        self.plc_id = plc_id
        self.origin = origin
        self.callback = callback
        self.pending = deque()
        self.closed = False
        self.delivery_lock = threading.RLock()
        self.delivering = False

    def drain(self):
        # Only a subscriber's delivery is serialized, never another station's.
        with self.delivery_lock:
            if self.delivering:
                return
            self.delivering = True
            try:
                while True:
                    with self.owner._lock:
                        if self.closed or not self.pending:
                            return
                        sample = self.pending.popleft()
                    self.callback(self.plc_id, *sample)
            finally:
                self.delivering = False

    def close(self):
        with self.owner._lock:
            self.closed = True
            self.pending.clear()
            self.owner._subscribers[self.plc_id].discard(self)
        # Wait for an in-flight callback without holding the history lock.
        with self.delivery_lock:
            pass


class PlcCurrentHistory:
    """Own one direct bus connection for the armed production composition.

    subscribe() atomically queues retained data and registers live delivery.
    The caller installs the returned handle before calling drain(), allowing
    stop to cancel even during replay. No user callback runs under _lock.
    """
    def __init__(self, *, bus, plc_ids, monotonic=None):
        self._clock = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._samples = {plc_id: deque() for plc_id in plc_ids}
        self._subscribers = {plc_id: set() for plc_id in self._samples}
        self._evicted_through = {}
        self._closed = False
        self._signal = getattr(bus, "plc_current_sampled", None)
        self._handler = self._on_sample
        if self._samples:
            if self._signal is None:
                raise ValueError("PLC current history signal unavailable")
            self._signal.connect(self._handler, Qt.ConnectionType.DirectConnection)

    def _prune(self, plc_id, now):
        samples = self._samples[plc_id]
        while samples and (samples[0][0] < now - 10.0 or len(samples) > 4096):
            stamp, _value = samples.popleft()
            self._evicted_through[plc_id] = max(
                stamp, self._evicted_through.get(plc_id, -math.inf),
            )

    def _on_sample(self, plc_id, monotonic_time, current_a):
        try:
            stamp, value = float(monotonic_time), float(current_a)
        except (TypeError, ValueError):
            return
        if not math.isfinite(stamp) or not math.isfinite(value):
            return
        with self._lock:
            if self._closed or plc_id not in self._samples:
                return
            self._samples[plc_id].append((stamp, value))
            self._prune(plc_id, max(stamp, self._clock()))
            subscribers = tuple(self._subscribers[plc_id])
            for subscriber in subscribers:
                if stamp >= subscriber.origin:
                    subscriber.pending.append((stamp, value))
        for subscriber in subscribers:
            subscriber.drain()

    def subscribe(self, plc_id, origin, callback):
        if not math.isfinite(origin):
            raise CurrentHistoryUnavailable("current history origin must be finite")
        with self._lock:
            if self._closed or plc_id not in self._samples:
                raise CurrentHistoryUnavailable("PLC current history is not armed")
            self._prune(plc_id, self._clock())
            if origin <= self._evicted_through.get(plc_id, -math.inf):
                raise CurrentHistoryUnavailable("current trigger precedes evicted history")
            subscriber = _Subscription(self, plc_id, origin, callback)
            subscriber.pending.extend(
                sample for sample in self._samples[plc_id] if sample[0] >= origin
            )
            self._subscribers[plc_id].add(subscriber)
            return subscriber

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = tuple(s for group in self._subscribers.values() for s in group)
            for samples in self._samples.values():
                samples.clear()
            self._evicted_through.clear()
        if self._samples:
            try:
                self._signal.disconnect(self._handler)
            except (TypeError, RuntimeError):
                pass
        for subscriber in subscribers:
            subscriber.close()
