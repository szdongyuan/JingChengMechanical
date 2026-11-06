"""Owning, Python-version-independent binding for the Windows native audio ring.

One producer and one serialized consumer only. A peek copies samples but retains
the ring lease until release(), allowing downstream readiness/backpressure checks.
Never call producer_stopped() until the stream has definitively stopped calling C.
"""
from __future__ import annotations

import ctypes as ct
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time

import numpy as np


class NativeCaptureError(RuntimeError):
    pass


class _Block(ct.Structure):
    _fields_ = [("sequence", ct.c_uint64), ("entry_ns", ct.c_uint64),
                ("frames", ct.c_uint32), ("channels", ct.c_uint32),
                ("status", ct.c_uint32), ("reserved", ct.c_uint32),
                ("adc_time", ct.c_double), ("current_time", ct.c_double),
                ("dac_time", ct.c_double), ("samples", ct.POINTER(ct.c_float))]


class NativeCaptureStats(ct.Structure):
    _fields_ = [("sequence", ct.c_uint64), ("consumed", ct.c_uint64),
                ("heartbeat_ns", ct.c_uint64), ("error", ct.c_uint32),
                ("priority_applied", ct.c_uint32), ("capacity", ct.c_uint32),
                ("armed", ct.c_uint32), ("stop_requested", ct.c_uint32)]


@dataclass(frozen=True)
class NativeCaptureBlock:
    sequence: int
    entry_ns: int
    frames: int
    channels: int
    status: int
    adc_time: float
    current_time: float
    dac_time: float
    samples: np.ndarray


def _load_library():
    path = Path(__file__).parents[1] / "native" / "capture_buffer.dll"
    try:
        dll = ct.CDLL(str(path))
    except OSError as exc:
        raise NativeCaptureError(f"Required Windows x64 capture DLL unavailable: {path}. "
                                 "Run scripts/build_capture_buffer.ps1.") from exc
    signatures = {
        "cb_abi_version": ([], ct.c_uint32),
        "cb_create": ([ct.c_uint32] * 3, ct.c_void_p),
        "cb_callback_address": ([], ct.c_void_p),
        "cb_arm": ([ct.c_void_p], ct.c_int),
        "cb_set_clock_offset": ([ct.c_void_p, ct.c_int64], ct.c_int),
        "cb_request_stop": ([ct.c_void_p], None),
        "cb_producer_stopped": ([ct.c_void_p], ct.c_int),
        "cb_destroy": ([ct.c_void_p], ct.c_int),
        "cb_peek": ([ct.c_void_p, ct.POINTER(_Block)], ct.c_int),
        "cb_release": ([ct.c_void_p], ct.c_int),
        "cb_snapshot": ([ct.c_void_p, ct.POINTER(NativeCaptureStats)], None),
    }
    for name, (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    if dll.cb_abi_version() != 1:
        raise NativeCaptureError("Unsupported native capture ABI")
    return dll


class NativeCaptureBuffer:
    def __init__(self, *, sample_rate: float, channels: int, block_frames: int,
                 capacity_seconds: float = 10):
        for name, value in (("channels", channels), ("block_frames", block_frames)):
            if not isinstance(value, int) or not 0 < value <= 0xFFFFFFFF:
                raise ValueError(f"{name} must be a positive uint32")
        if not math.isfinite(sample_rate) or not 0 < sample_rate <= 0xFFFFFFFF:
            raise ValueError("sample_rate must be finite and positive")
        if not math.isfinite(capacity_seconds) or capacity_seconds <= 0:
            raise ValueError("capacity_seconds must be finite and positive")
        requested = sample_rate * capacity_seconds / block_frames
        if not math.isfinite(requested) or requested > 0xFFFFFFFF:
            raise ValueError("native capacity is too large")
        slots = math.ceil(requested)
        self._dll = _load_library()
        self._lock = threading.RLock()
        self._peeked = None
        self._handle = self._dll.cb_create(channels, block_frames, slots)
        if not self._handle:
            raise NativeCaptureError("Native buffer allocation rejected (512 MiB maximum) or failed")
        # Python 3.13+ Windows monotonic already uses QPC; older Python may use
        # GetTickCount64. Anchor once at creation, never at delayed delivery.
        monotonic_clock = time.get_clock_info("monotonic")
        perf_clock = time.get_clock_info("perf_counter")
        self.clock_offset_ns = 0
        self.clock_uncertainty_ns = 0
        if monotonic_clock.implementation != perf_clock.implementation:
            before = time.perf_counter_ns()
            monotonic = time.monotonic_ns()
            after = time.perf_counter_ns()
            self.clock_offset_ns = monotonic - (before + after) // 2
            self.clock_uncertainty_ns = math.ceil(monotonic_clock.resolution * 1e9) + (after - before)
        self._dll.cb_set_clock_offset(self._handle, self.clock_offset_ns)

    def _require_open(self):
        if not self._handle:
            raise NativeCaptureError("Native capture buffer is closed")
        return self._handle

    @property
    def callback_address(self):
        with self._lock:
            self._require_open()
            return self._dll.cb_callback_address()

    @property
    def userdata_address(self):
        with self._lock:
            return self._require_open()

    def cffi_callback(self, ffi):
        """Return expert sounddevice callback/userdata CData; keep this owner alive."""
        return (ffi.cast("PaStreamCallback *", self.callback_address),
                ffi.cast("void *", self.userdata_address))

    def arm(self):
        with self._lock:
            if not self._dll.cb_arm(self._require_open()):
                raise NativeCaptureError("Buffer already armed or stopped; use a new generation")

    def request_stop(self):
        with self._lock:
            self._dll.cb_request_stop(self._require_open())

    def producer_stopped(self):
        """Caller guarantees stream closed/stopped; active callbacks also veto this."""
        with self._lock:
            if not self._dll.cb_producer_stopped(self._require_open()):
                raise NativeCaptureError("Native producer still active")

    def snapshot(self):
        with self._lock:
            stats = NativeCaptureStats()
            self._dll.cb_snapshot(self._require_open(), ct.byref(stats))
            return stats

    def peek(self):
        with self._lock:
            self._require_open()
            if self._peeked is not None:
                return self._peeked
            block = _Block()
            result = self._dll.cb_peek(self._handle, ct.byref(block))
            if result < 0:
                raise NativeCaptureError("Outstanding native read lease")
            if not result:
                return None
            try:
                samples = np.ctypeslib.as_array(block.samples,
                    shape=(block.frames * block.channels,)).copy().reshape(block.frames, block.channels)
                self._peeked = NativeCaptureBlock(block.sequence, block.entry_ns,
                    block.frames, block.channels, block.status, block.adc_time,
                    block.current_time, block.dac_time, samples)
                return self._peeked
            except BaseException:
                self._dll.cb_release(self._handle)
                raise

    def release(self):
        with self._lock:
            if self._peeked is None or not self._dll.cb_release(self._require_open()):
                raise NativeCaptureError("No outstanding read lease")
            self._peeked = None

    def close(self):
        with self._lock:
            if self._handle and not self._dll.cb_destroy(self._handle):
                raise NativeCaptureError("Cannot free armed producer or outstanding read lease")
            self._handle = None
