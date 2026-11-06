"""Real DLL tests; the harness invokes the production C callback on a C thread."""
import ctypes as ct
import json
import time
from pathlib import Path

import numpy as np
import pytest

from app.service.native_capture_buffer import NativeCaptureBuffer, NativeCaptureError, _load_library


class Timing(ct.Structure):
    _fields_ = [(name, ct.c_double) for name in ("adc", "current", "dac")]


CALLBACK = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_void_p, ct.c_ulong,
                      ct.POINTER(Timing), ct.c_ulong, ct.c_void_p)


def invoke(buffer, values, *, status=0, frames=None):
    values = np.asarray(values, dtype=np.float32)
    timing = Timing(1.25, 2.5, 3.75)
    return CALLBACK(buffer.callback_address)(values.ctypes.data, None,
        len(values) if frames is None else frames, ct.byref(timing), status,
        buffer.userdata_address)


def finish(buffer):
    buffer.request_stop()
    buffer.producer_stopped()  # All synchronous test callbacks have returned.
    if buffer.peek() is not None:
        buffer.release()
    buffer.close()


def test_exact_wrap_status_variable_frames_and_backpressure():
    buffer = NativeCaptureBuffer(sample_rate=8, channels=2, block_frames=4, capacity_seconds=1)
    buffer.arm()
    try:
        for cycle in range(10):
            expected = np.arange(8, dtype=np.float32).reshape(4, 2) + cycle * 100
            before = time.monotonic_ns()
            assert invoke(buffer, expected, status=2) == 0
            after = time.monotonic_ns()
            block = buffer.peek()
            assert block.sequence == cycle * 2 + 1
            assert before - buffer.clock_uncertainty_ns <= block.entry_ns <= after + buffer.clock_uncertainty_ns
            assert block.status == 2
            assert (block.adc_time, block.current_time, block.dac_time) == (1.25, 2.5, 3.75)
            np.testing.assert_array_equal(block.samples, expected)
            assert buffer.peek() is block
            with pytest.raises(NativeCaptureError):
                buffer.close()
            assert invoke(buffer, expected[:3]) == 0
            np.testing.assert_array_equal(buffer.peek().samples, expected)
            buffer.release()
            np.testing.assert_array_equal(buffer.peek().samples, expected[:3])
            buffer.release()
            assert buffer.peek() is None
    finally:
        finish(buffer)


def test_capacity_fault_does_not_overwrite_and_stop_is_explicit():
    buffer = NativeCaptureBuffer(sample_rate=4, channels=1, block_frames=4, capacity_seconds=1)
    buffer.arm()
    assert invoke(buffer, [[1], [2], [3], [4]]) == 0
    assert invoke(buffer, [[5], [6], [7], [8]]) == 2
    assert buffer.snapshot().error == 1  # Full
    np.testing.assert_array_equal(buffer.peek().samples[:, 0], [1, 2, 3, 4])
    buffer.release()
    buffer.request_stop()
    assert invoke(buffer, [[9]]) == 2
    buffer.producer_stopped()
    buffer.close()
    buffer.close()


def test_native_stop_visibility_and_copied_read_lifetime():
    buffer = NativeCaptureBuffer(sample_rate=4, channels=1, block_frames=4)
    buffer.arm()
    assert invoke(buffer, [[1], [2], [3], [4]]) == 0
    block = buffer.peek()
    buffer.request_stop()
    assert buffer.snapshot().stop_requested == 1
    buffer.producer_stopped()
    with pytest.raises(NativeCaptureError):
        buffer.close()
    buffer.release()
    buffer.close()
    np.testing.assert_array_equal(block.samples[:, 0], [1, 2, 3, 4])
    with pytest.raises(NativeCaptureError):
        buffer.arm()


def test_cffi_expert_callback_is_the_native_address():
    import sounddevice as sd
    buffer = NativeCaptureBuffer(sample_rate=48000, channels=2, block_frames=512)
    try:
        callback, userdata = buffer.cffi_callback(sd._ffi)
        assert int(sd._ffi.cast("uintptr_t", callback)) == buffer.callback_address
        assert int(sd._ffi.cast("uintptr_t", userdata)) == buffer.userdata_address
    finally:
        buffer.close()


def test_float32_bits_and_native_size_validation():
    buffer = NativeCaptureBuffer(sample_rate=4, channels=2, block_frames=4)
    assert not buffer._dll.cb_create(0, 4, 1)
    assert not buffer._dll.cb_create(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    buffer.arm()
    try:
        bits = np.array([0, 0x80000000, 0x7FC01234, 0x7F800000,
                         0xFF800000, 0x00000001, 0x3F800000, 0xBF800000], dtype=np.uint32).reshape(4, 2)
        assert invoke(buffer, bits.view(np.float32)) == 0
        np.testing.assert_array_equal(buffer.peek().samples.view(np.uint32), bits)
        buffer.release()
    finally:
        finish(buffer)


@pytest.mark.parametrize("kwargs", [dict(sample_rate=0), dict(channels=0),
    dict(block_frames=0), dict(capacity_seconds=float('nan')),
    dict(channels=2**32), dict(sample_rate=2**32), dict(capacity_seconds=1000000)])
def test_reject_invalid_or_excessive_allocation(kwargs):
    params = dict(sample_rate=48000, channels=2, block_frames=512)
    params.update(kwargs)
    with pytest.raises((ValueError, NativeCaptureError)):
        NativeCaptureBuffer(**params)


def test_oversize_and_null_input_are_explicit_faults():
    for null_input in (False, True):
        buffer = NativeCaptureBuffer(sample_rate=8, channels=1, block_frames=4)
        buffer.arm()
        try:
            if null_input:
                result = CALLBACK(buffer.callback_address)(None, None, 4, None, 0, buffer.userdata_address)
            else:
                result = invoke(buffer, np.zeros((5, 1)))
            assert result == 2
            assert buffer.snapshot().error == 2
            assert buffer.peek() is None
        finally:
            finish(buffer)


@pytest.fixture
def harness():
    # Resolve the driver's import even when no earlier test loaded the DLL.
    production_dll = _load_library()
    dll = ct.CDLL(str(Path(__file__).with_name('capture_test_driver.dll')))
    dll._production_dll = production_dll  # Retain it for the fixture's lifetime.
    dll.driver_start.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.c_uint32]
    dll.driver_start.restype = ct.c_void_p
    dll.driver_wait.argtypes = [ct.c_void_p, ct.c_uint32]
    dll.driver_wait.restype = ct.c_int
    dll.driver_entry.argtypes = [ct.c_void_p, ct.c_uint32]
    dll.driver_entry.restype = ct.c_uint64
    dll.driver_result.argtypes = [ct.c_void_p]
    dll.driver_result.restype = ct.c_int
    dll.driver_priority.argtypes = [ct.c_void_p]
    dll.driver_priority.restype = ct.c_int
    dll.driver_destroy.argtypes = [ct.c_void_p]
    return dll


@pytest.mark.parametrize("hold_gil", [True, False])
def test_native_thread_exact_samples_and_timestamps_under_contention(harness, hold_gil):
    count, frames, channels = (320, 128, 3) if hold_gil else (600, 31, 2)
    buffer = NativeCaptureBuffer(sample_rate=48000, channels=channels,
        block_frames=frames, capacity_seconds=10 if hold_gil else .02)
    buffer.arm()
    driver = harness.driver_start(buffer.userdata_address, count, frames, channels, 8 if hold_gil else 1, 1)
    assert driver
    blocks = []
    try:
        if hold_gil:
            sleep = ct.PyDLL('kernel32').Sleep  # PyDLL deliberately retains the GIL.
            sleep.argtypes = [ct.c_ulong]
            started = time.monotonic()
            sleep(2300)
            assert time.monotonic() - started >= 2.2
            assert buffer.snapshot().sequence >= 100
        deadline = time.monotonic() + 12
        while len(blocks) < count and time.monotonic() < deadline:
            block = buffer.peek()
            if block is None:
                time.sleep(.0005)
                continue
            blocks.append(block)
            buffer.release()
            if not hold_gil:
                json.dumps({str(i): list(range(100)) for i in range(20)})
        assert harness.driver_wait(driver, 2000) == 1
        assert harness.driver_result(driver) == 0
        assert harness.driver_priority(driver) == 2  # THREAD_PRIORITY_HIGHEST
        assert len(blocks) == count
        assert buffer.snapshot().priority_applied == 1
        assert buffer.snapshot().heartbeat_ns >= blocks[-1].entry_ns
        for index, block in enumerate(blocks):
            actual_frames = frames - (index % 3)
            expected = (np.arange(actual_frames * channels, dtype=np.float32) + index * 10000).reshape(actual_frames, channels)
            np.testing.assert_array_equal(block.samples, expected)
            assert block.entry_ns == harness.driver_entry(driver, index)
            assert block.sequence == index + 1
        # Two directly adjacent windows across backlog are sample exact.
        joined = np.concatenate([b.samples for b in blocks])
        reference = np.concatenate([(np.arange((frames - i % 3) * channels,
            dtype=np.float32) + i * 10000).reshape(frames - i % 3, channels) for i in range(count)])
        split = len(joined) // 2 + 7
        np.testing.assert_array_equal(joined[:split], reference[:split])
        np.testing.assert_array_equal(joined[split:], reference[split:])
    finally:
        buffer.request_stop()
        assert harness.driver_wait(driver, 3000) == 1
        harness.driver_destroy(driver)
        buffer.producer_stopped()
        if buffer.peek() is not None:
            buffer.release()
        buffer.close()
