"""Independent completed PCM ownership and best-effort realtime preview transport."""

from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass, field, replace
import math
from multiprocessing import shared_memory
import struct
import threading
import time
from uuid import uuid4

from .protocol import (
    PROTOCOL_VERSION,
    FLOAT32_BYTES,
    MAX_CAPTURES,
    MAX_CAPTURE_BUFFER_BYTES,
    MAX_COUNTER,
    MAX_PREVIEW_SLOTS,
    MAX_TEXT_CHARS,
    CaptureLimits,
    CaptureReservation,
    CompletedAudioDescriptor,
    PreviewBlock,
    PreviewBootstrap,
    PreviewStreamConfig,
    PreviewStreamHandle,
    TransportCapacityError,
    bounded_int,
    bounded_text,
    resource_id,
)


def _validate_pcm(pcm, frames, channels):
    import numpy as np

    if (
        not isinstance(pcm, np.ndarray)
        or pcm.dtype != np.dtype(np.float32)
        or pcm.shape != (frames, channels)
    ):
        raise ValueError("PCM must be a float32 array with the reserved shape")


def _write_pcm(mapping, pcm):
    # Do not retain an exported view across mapping close/unlink.
    import numpy as np

    target = np.ndarray(pcm.shape, dtype=np.float32, buffer=mapping.buf)
    try:
        np.copyto(target, pcm, casting="no")
    finally:
        del target


def _dispose(mapping):
    try:
        mapping.close()
    finally:
        try:
            mapping.unlink()
        except FileNotFoundError:
            pass


@dataclass
class _RetainedCapture:
    reservation: CaptureReservation
    mapping: shared_memory.SharedMemory | None = None
    descriptor: CompletedAudioDescriptor | None = None
    publishing: bool = False
    release_requested: bool = False


class CompletedAudioStore:
    """Child owner. Credit includes active reservations and published PCM until receipt.

    No mapping views escape this class. A publication lease prevents cleanup during
    a local write without holding the admission lock for the bulk copy. Parent
    readers only close their own handles; they never unlink child mappings.
    """

    def __init__(self, incarnation: str, limits: CaptureLimits):
        resource_id(incarnation, "incarnation")
        if type(limits) is not CaptureLimits:
            raise ValueError("limits must be CaptureLimits")
        limits.__post_init__()
        self.incarnation = incarnation
        self.owner_id = uuid4().hex
        self.limits = limits
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._captures: dict[str, _RetainedCapture] = {}
        self._reserved_bytes = 0
        self._closed = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._captures)

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    def reserve(
        self, capture_id: str, *, frames: int, channels: int, sample_rate_hz: int,
    ) -> CaptureReservation:
        bounded_text(capture_id, "capture_id")
        size = self.limits.validate_pcm(frames, channels, sample_rate_hz)
        with self._lock:
            if self._closed:
                raise RuntimeError("completed-audio store is closed")
            if any(
                item.reservation.capture_id == capture_id
                for item in self._captures.values()
            ):
                raise ValueError("capture_id already has an active reservation")
            if (
                len(self._captures) >= self.limits.max_captures
                or self._reserved_bytes + size > self.limits.max_retained_bytes
            ):
                raise TransportCapacityError("completed-audio retention capacity exhausted")
            reservation = CaptureReservation(
                PROTOCOL_VERSION, self.incarnation, self.owner_id, capture_id,
                uuid4().hex, frames, channels, sample_rate_hz, size,
            )
            self._captures[reservation.resource_id] = _RetainedCapture(reservation)
            self._reserved_bytes += size
            return reservation

    def _lookup(self, value):
        if type(value) not in (CaptureReservation, CompletedAudioDescriptor):
            raise ValueError("invalid capture resource")
        value.validate()
        if value.incarnation != self.incarnation or value.owner_id != self.owner_id:
            raise ValueError("capture resource belongs to another owner or incarnation")
        item = self._captures.get(value.resource_id)
        if item is not None:
            reservation_values = asdict(item.reservation)
            if any(
                getattr(value, key) != field
                for key, field in reservation_values.items()
            ):
                raise ValueError("capture resource does not match its reservation")
        return item

    def shrink(self, reservation: CaptureReservation, *, frames: int) -> CaptureReservation:
        """Freeze actual length after conservative admission, without a credit gap."""
        with self._lock:
            item = self._lookup(reservation)
            bounded_int(frames, "frames", reservation.frames)
            if self._closed or item is None or item.publishing or item.descriptor is not None:
                raise ValueError("capture reservation cannot be resized")
            smaller = replace(reservation, frames=frames,
                              nbytes=frames * reservation.channels * FLOAT32_BYTES)
            self._reserved_bytes -= reservation.nbytes - smaller.nbytes
            item.reservation = smaller
            return smaller

    def publish(
        self, reservation: CaptureReservation, pcm: object,
    ) -> CompletedAudioDescriptor:
        with self._lock:
            item = self._lookup(reservation)
            if self._closed or item is None:
                raise ValueError("capture reservation is no longer active")
            if item.descriptor is not None or item.publishing:
                raise ValueError("capture reservation already published")
            _validate_pcm(pcm, reservation.frames, reservation.channels)
            item.publishing = True
        name = f"capture_pcm_{self.incarnation}_{self.owner_id}_{reservation.resource_id}"
        try:
            item.mapping = shared_memory.SharedMemory(
                name=name, create=True, size=reservation.nbytes
            )
            _write_pcm(item.mapping, pcm)
            descriptor = CompletedAudioDescriptor(**asdict(item.reservation), shm_name=name)
        except BaseException:
            with self._changed:
                item.publishing = False
                try:
                    self._release(item)
                finally:
                    self._changed.notify_all()
            raise
        with self._changed:
            item.descriptor = descriptor
            item.publishing = False
            try:
                if item.release_requested:
                    self._release(item)
                    raise CancelledError("capture publication was cancelled during cleanup")
            finally:
                self._changed.notify_all()
            return descriptor

    def _release(self, item):
        if item.mapping is not None:
            _dispose(item.mapping)
        del self._captures[item.reservation.resource_id]
        self._reserved_bytes -= item.reservation.nbytes

    def receipt(self, descriptor: CompletedAudioDescriptor) -> bool:
        if type(descriptor) is not CompletedAudioDescriptor:
            raise ValueError("receipt requires a completed-audio descriptor")
        with self._changed:
            item = self._lookup(descriptor)
            if item is None:
                return False
            if item.descriptor != descriptor:
                raise ValueError("receipt requires the published resource")
            self._release(item)
            return True

    def cancel(self, reservation: CaptureReservation) -> bool:
        """Request disposal without waiting on an active publication lease.

        A cancelled in-flight publish raises concurrent.futures.CancelledError.
        Its credit remains charged until its local mapping has been disposed.
        """
        with self._changed:
            item = self._lookup(reservation)
            if item is None or item.release_requested:
                return False
            if item.publishing:
                item.release_requested = True
            else:
                self._release(item)
            return True

    def close(self, timeout_s: float = 1.0) -> bool:
        """Stop admission and release ownership; False reports outstanding leases.

        A timed-out publication cleans itself up when its lease ends. No mapping is
        unlinked under a local writer. The process owner can impose its shutdown
        deadline and report non-clean cleanup if a writer never returns.
        """
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 <= timeout_s <= 300
        ):
            raise ValueError("timeout_s must be finite and between zero and 300 seconds")
        deadline = time.monotonic() + timeout_s
        with self._changed:
            self._closed = True
            for item in tuple(self._captures.values()):
                if item.publishing:
                    item.release_requested = True
                else:
                    self._release(item)
            while self._captures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def copy_completed_audio(
    descriptor: CompletedAudioDescriptor, *, incarnation: str,
    limits: CaptureLimits, owner_id: str | None = None,
):
    """Validate, attach, then make immutable parent-owned PCM before caller receipt.

    The caller must only acknowledge after this returns successfully. Failure leaves
    the child resource retained for retry or explicit cancellation. The bytes-backed
    ndarray cannot be made writable, and outlives both shared-memory handles.
    """
    import numpy as np

    resource_id(incarnation, "incarnation")
    if type(descriptor) is not CompletedAudioDescriptor:
        raise ValueError("invalid completed-audio descriptor")
    descriptor.validate()
    if descriptor.incarnation != incarnation:
        raise ValueError("stale child incarnation")
    if owner_id is not None and descriptor.owner_id != owner_id:
        raise ValueError("unexpected completed-audio owner")
    if type(limits) is not CaptureLimits:
        raise ValueError("limits must be CaptureLimits")
    limits.__post_init__()
    limits.validate_pcm(
        descriptor.frames, descriptor.channels, descriptor.sample_rate_hz
    )
    if descriptor.nbytes > limits.max_retained_bytes:
        raise ValueError("PCM exceeds configured retained byte budget")
    mapping = shared_memory.SharedMemory(name=descriptor.shm_name)
    try:
        # Windows reports the page-rounded mapping size; exact size equality would
        # reject valid small recordings. Metadata supplies the validated copy bound.
        if mapping.size < descriptor.nbytes:
            raise ValueError("shared-memory mapping is smaller than its PCM descriptor")
        owned = bytes(mapping.buf[:descriptor.nbytes])
    finally:
        mapping.close()
    return np.frombuffer(owned, dtype=np.float32).reshape(
        descriptor.frames, descriptor.channels
    )


_RING_HEADER = struct.Struct("<II")  # next write index, pending slots
_BLOCK_HEADER = struct.Struct("<QQQIH")  # sequence, sample, time, frames, UTF-8 length
_GENERATION_BYTES = MAX_TEXT_CHARS * 4
_PCM_OFFSET = _BLOCK_HEADER.size + _GENERATION_BYTES


def _preview_slot_bytes(config):
    return _PCM_OFFSET + config.max_frames * config.channels * FLOAT32_BYTES


def _preview_mapping_bytes(config, slots):
    size = _RING_HEADER.size + slots * _preview_slot_bytes(config)
    bounded_int(size, "preview stream bytes", MAX_CAPTURE_BUFFER_BYTES)
    return size


@dataclass
class _PreviewStream:
    handle: PreviewStreamHandle
    mapping: shared_memory.SharedMemory
    # Exclusive data-view lease, separate from ring shutdown admission state.
    local_lock: object = field(default_factory=threading.Lock)
    closed: bool = False


class PreviewRing:
    """At most four latest blocks per stream, independent of completed audio.

    Both offer and read try their stream's lock without waiting. A paused parent
    holding a shared lock can only drop previews from that stream. Closing never
    acquires shared locks and there are no sender threads or pipe drains to join.
    Local leases protect mapping views from concurrent close in the same process.
    Closing stops admission independently and waits only until its deadline; a
    stalled lease disposes its own mapping when it eventually finishes.
    """

    def __init__(self, bootstrap, mappings, *, owner):
        self.bootstrap = bootstrap
        self._owner = owner
        self._closing = threading.Event()
        self._streams = {
            handle.config.hardware_id: _PreviewStream(handle, mapping)
            for handle, mapping in zip(bootstrap.streams, mappings, strict=True)
        }
        self.allocated_bytes = sum(
            _preview_mapping_bytes(handle.config, bootstrap.slots_per_stream)
            for handle in bootstrap.streams
        )

    @classmethod
    def create(
        cls, context, incarnation: str, streams: tuple[PreviewStreamConfig, ...],
        *, slots_per_stream: int = MAX_PREVIEW_SLOTS,
    ) -> PreviewRing:
        resource_id(incarnation, "incarnation")
        bounded_int(slots_per_stream, "slots_per_stream", MAX_PREVIEW_SLOTS)
        if type(streams) is not tuple or not 1 <= len(streams) <= MAX_CAPTURES:
            raise ValueError("preview requires one through six stream configurations")
        hardware_ids = set()
        for config in streams:
            if type(config) is not PreviewStreamConfig:
                raise ValueError("invalid preview stream configuration")
            config.__post_init__()
            _preview_mapping_bytes(config, slots_per_stream)
            if config.hardware_id in hardware_ids:
                raise ValueError("duplicate preview hardware_id")
            hardware_ids.add(config.hardware_id)
        owner_id = uuid4().hex
        mappings, handles = [], []
        try:
            for index, config in enumerate(streams):
                name = f"capture_preview_{incarnation}_{owner_id}_{index}"
                lock = context.Lock()
                mapping = shared_memory.SharedMemory(
                    name=name, create=True,
                    size=_preview_mapping_bytes(config, slots_per_stream),
                )
                mappings.append(mapping)
                _RING_HEADER.pack_into(mapping.buf, 0, 0, 0)
                handles.append(PreviewStreamHandle(config, name, lock))
            bootstrap = PreviewBootstrap(
                PROTOCOL_VERSION, incarnation, owner_id, slots_per_stream, tuple(handles)
            )
            return cls(bootstrap, mappings, owner=True)
        except BaseException:
            for mapping in mappings:
                _dispose(mapping)
            raise

    @classmethod
    def attach(cls, bootstrap: PreviewBootstrap, *, incarnation: str) -> PreviewRing:
        resource_id(incarnation, "incarnation")
        if type(bootstrap) is not PreviewBootstrap:
            raise ValueError("invalid preview bootstrap")
        bootstrap.validate()
        if bootstrap.incarnation != incarnation:
            raise ValueError("stale preview child incarnation")
        sizes = [
            _preview_mapping_bytes(handle.config, bootstrap.slots_per_stream)
            for handle in bootstrap.streams
        ]
        mappings = []
        try:
            for handle, size in zip(bootstrap.streams, sizes, strict=True):
                mapping = shared_memory.SharedMemory(name=handle.shm_name)
                mappings.append(mapping)
                if mapping.size < size:
                    raise ValueError("preview mapping is smaller than its configured buffer")
            return cls(bootstrap, mappings, owner=False)
        except BaseException:
            for mapping in mappings:
                mapping.close()
            raise

    def _stream(self, hardware_id):
        bounded_text(hardware_id, "hardware_id")
        try:
            return self._streams[hardware_id]
        except KeyError:
            raise ValueError("unknown preview hardware_id") from None

    def _indices(self, stream):
        next_write, pending = _RING_HEADER.unpack_from(stream.mapping.buf, 0)
        slots = self.bootstrap.slots_per_stream
        bounded_int(next_write, "preview write index", slots - 1, 0)
        bounded_int(pending, "preview pending count", slots, 0)
        return next_write, pending

    def offer(
        self, hardware_id: str, pcm: object, *, sequence: int,
        first_sample: int, monotonic_ns: int, generation_id: str,
    ) -> bool:
        """Offer a block whose time/sample metadata refer to its first frame.

        Sequence is supplied by the producer and advances even on dropped offers.
        Together with generation and sample position it exposes gaps to STFT.
        """
        import numpy as np

        stream = self._stream(hardware_id)
        config = stream.handle.config
        bounded_int(sequence, "sequence", MAX_COUNTER, 0)
        bounded_int(first_sample, "first_sample", MAX_COUNTER, 0)
        bounded_int(monotonic_ns, "monotonic_ns", MAX_COUNTER, 0)
        generation = bounded_text(generation_id, "generation_id").encode("utf-8")
        if not isinstance(pcm, np.ndarray) or pcm.ndim != 2:
            raise ValueError("preview PCM must be a two-dimensional array")
        frames = pcm.shape[0]
        bounded_int(frames, "preview frames", config.max_frames)
        _validate_pcm(pcm, frames, config.channels)
        if self._closing.is_set() or not stream.local_lock.acquire(blocking=False):
            return False
        try:
            if (
                self._closing.is_set() or stream.closed
                or not stream.handle.lock.acquire(block=False)
            ):
                return False
            try:
                next_write, pending = self._indices(stream)
                offset = _RING_HEADER.size + next_write * _preview_slot_bytes(config)
                target = np.ndarray(
                    pcm.shape, dtype=np.float32,
                    buffer=stream.mapping.buf, offset=offset + _PCM_OFFSET,
                )
                try:
                    np.copyto(target, pcm, casting="no")
                finally:
                    del target
                _BLOCK_HEADER.pack_into(
                    stream.mapping.buf, offset,
                    sequence, first_sample, monotonic_ns, frames, len(generation),
                )
                text_offset = offset + _BLOCK_HEADER.size
                stream.mapping.buf[text_offset:text_offset + len(generation)] = generation
                slots = self.bootstrap.slots_per_stream
                _RING_HEADER.pack_into(
                    stream.mapping.buf, 0, (next_write + 1) % slots, min(slots, pending + 1)
                )
                return True
            finally:
                stream.handle.lock.release()
        finally:
            self._end_lease(stream)

    def read(self, hardware_id: str) -> tuple[PreviewBlock, ...]:
        import numpy as np

        stream = self._stream(hardware_id)
        config = stream.handle.config
        if self._closing.is_set() or not stream.local_lock.acquire(blocking=False):
            return ()
        try:
            if (
                self._closing.is_set() or stream.closed
                or not stream.handle.lock.acquire(block=False)
            ):
                return ()
            try:
                next_write, pending = self._indices(stream)
                slots = self.bootstrap.slots_per_stream
                blocks = []
                for index in range(pending):
                    slot = (next_write - pending + index) % slots
                    offset = _RING_HEADER.size + slot * _preview_slot_bytes(config)
                    sequence, sample, timestamp, frames, length = (
                        _BLOCK_HEADER.unpack_from(stream.mapping.buf, offset)
                    )
                    bounded_int(sequence, "preview sequence", MAX_COUNTER, 0)
                    bounded_int(sample, "preview sample", MAX_COUNTER, 0)
                    bounded_int(timestamp, "preview time", MAX_COUNTER, 0)
                    bounded_int(frames, "preview frames", config.max_frames)
                    bounded_int(length, "preview generation bytes", _GENERATION_BYTES)
                    text_offset = offset + _BLOCK_HEADER.size
                    generation = bytes(
                        stream.mapping.buf[text_offset:text_offset + length]
                    ).decode("utf-8")
                    bounded_text(generation, "preview generation")
                    pcm_offset = offset + _PCM_OFFSET
                    size = frames * config.channels * FLOAT32_BYTES
                    owned = bytes(stream.mapping.buf[pcm_offset:pcm_offset + size])
                    pcm = np.frombuffer(owned, dtype=np.float32).reshape(
                        frames, config.channels
                    )
                    blocks.append(PreviewBlock(
                        self.bootstrap.incarnation, hardware_id, generation, sequence,
                        sample, timestamp, config.sample_rate_hz, pcm,
                    ))
                _RING_HEADER.pack_into(stream.mapping.buf, 0, next_write, 0)
                return tuple(blocks)
            finally:
                stream.handle.lock.release()
        finally:
            self._end_lease(stream)

    def _close_stream(self, stream):
        # Caller exclusively holds the local data lease; no view can still be live.
        if not stream.closed:
            if self._owner:
                _dispose(stream.mapping)
            else:
                stream.mapping.close()
            stream.closed = True

    def _end_lease(self, stream):
        # Check shutdown AFTER release: otherwise a zero-timeout close could miss
        # this lease between the closing-state check and the lock release.
        stream.local_lock.release()
        if self._closing.is_set() and stream.local_lock.acquire(blocking=False):
            try:
                self._close_stream(stream)
            finally:
                stream.local_lock.release()

    def close(self, timeout_s: float = 1.0) -> bool:
        """Stop local operations and dispose mappings within one shared deadline.

        False reports a still-active local lease. Its offer/read disposes the
        mapping when it finishes. Repeated close is safe, including after timeout.
        Shared interprocess locks are never acquired by cleanup.
        """
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 <= timeout_s <= 300
        ):
            raise ValueError("timeout_s must be finite and between zero and 300 seconds")
        deadline = time.monotonic() + timeout_s
        self._closing.set()
        for stream in self._streams.values():
            remaining = max(0.0, deadline - time.monotonic())
            if stream.local_lock.acquire(timeout=remaining):
                try:
                    self._close_stream(stream)
                finally:
                    stream.local_lock.release()
        return all(stream.closed for stream in self._streams.values())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
