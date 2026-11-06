"""Production integration with real native memory and synthetic device input."""
import ctypes as ct
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp.ring import _ResidentRing, _MappingStatus
from app.service.plc_current_history import PlcCurrentHistory, CurrentHistoryUnavailable
from app.service.managed_input_sounddevice_backend import (
    MonotonicSoundDeviceAudioBackend, PythonSystemMonotonicAuthority,
)
from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective, BackendCallbackStatus, BackendCloseStatus, CaptureOutcomeStatus, LifecycleStatus, ShutdownStatus,
)
from app.service.analysis.resident_audio_mvp.pool import ResidentAudioPool
from app.service.analysis.resident_audio_mvp.stream import _ResidentStreamSlot
from tests.resident_audio_mvp.test_ring_capture import stream_spec
from tests.native_capture.test_native_buffer import invoke, harness


def wait_until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(.001)


@pytest.fixture(autouse=True)
def no_integration_thread_leaks():
    before = set(threading.enumerate())
    yield
    def remaining():
        return [thread for thread in threading.enumerate() if thread not in before
            and thread.name.startswith(('native-audio-pump-', 'resident-audio-', 'resident-sounddevice-'))]
    wait_until(lambda: not remaining())


class ExpertStream:
    opened = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.latency = .1
        self.active = False
        self.closed = False
        self.opened.append(self)

    def start(self):
        self.active = True

    def stop(self, ignore_errors=True):
        self.stop_ignore_errors = ignore_errors
        self.active = False

    def close(self, ignore_errors=True):
        self.close_ignore_errors = ignore_errors
        self.active = False
        self.closed = True


def native_handle(monkeypatch, callback, faults=None, *, expert=ExpertStream):
    import sounddevice as sd
    monkeypatch.setattr(sd, '_StreamBase', expert)
    def python_stream(**kwargs):
        raise AssertionError('production capture must register the expert C callback')
    monkeypatch.setattr(sd, 'InputStream', python_stream)
    backend = MonotonicSoundDeviceAudioBackend(sd, PythonSystemMonotonicAuthority())
    return backend.create_input(stream_spec(sample_rate_hz=1000, frames_per_buffer=10,
        capture_channel_indices=(0, 1)), callback, (faults if faults is not None else []).append)


def test_production_registers_cdata_pump_backpressure_and_safe_restart(monkeypatch):
    import sounddevice as sd
    received, faults = [], []
    ready = threading.Event()
    def callback(samples, frames, timestamp, status):
        received.append((samples.copy(), timestamp))
        return BackendCallbackDirective.CONTINUE
    callback.native_ready = ready.is_set
    handle = native_handle(monkeypatch, callback, faults)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    try:
        native = handle._native
        args = native._stream.kwargs
        assert isinstance(args['callback'], sd._ffi.CData)
        assert args['wrap_callback'] is None and args['kind'] == 'input'
        assert int(sd._ffi.cast('uintptr_t', args['userdata'])) == native._buffer.userdata_address
        values = np.arange(20, dtype=np.float32).reshape(10, 2)
        assert invoke(native._buffer, values) == 0
        time.sleep(.03)
        assert native.producer_snapshot().sequence == 1
        assert native.producer_snapshot().consumed == 0
        assert received == []
        ready.set()
        wait_until(lambda: len(received) == 1)
        np.testing.assert_array_equal(received[0][0], values)
        previous = native._buffer
        assert handle.restart_native_once(time.monotonic_ns() + 1_000_000_000)
        assert native._buffer is not previous
        assert native.producer_snapshot().sequence == 0
        assert invoke(native._buffer, values + 20) == 0
        wait_until(lambda: len(received) == 2)
        assert not faults
    finally:
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_wasapi_native_timing_ignores_adc_and_delayed_delivery_and_fast_clock(monkeypatch):
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE)
    handle._native_input_latency = .1
    def stamp(entry, adc, uncertainty=0):
        return handle._audio_block_end_ns(SimpleNamespace(inputBufferAdcTime=adc,
            currentTime=adc + 20, native_entry_ns=entry,
            clock_uncertainty_ns=uncertainty), 10, entry)
    origin = stamp(2_000_000_000, 19.0, 16_000_000)
    assert origin == 1_894_000_000
    # Python arrival does not enter this calculation; arbitrary raw ADC drift
    # cannot alter the already-established trigger/current relative offset.
    previous = origin
    for i in range(1, 200000):
        entry = 2_000_000_000 + i * 9_999_000
        end = stamp(entry, 1000.0 if i % 2 else -1000.0)
        assert previous <= end <= entry
        previous = end
        if i < 100:
            assert end == origin + i * 10_000_000
    assert end == entry  # Fast-clock drift eventually requires correction.
    handle._before_native_restart()
    assert stamp(300_000_000_000, -500.0) == 299_910_000_000


@pytest.mark.parametrize('hostapi, native_timing, continues', [
    ('Windows DirectSound', True, True),
    ('WASAPI', True, True),
    ('ASIO', True, False),
    ('MME', True, False),
    ('Windows DirectSound', False, False),
    ('WASAPI', False, False),
])
def test_directsound_reported_44100_adc_jump_preserves_samples_and_host_policy(
    monkeypatch, hostapi, native_timing, continues,
):
    received, faults = [], []
    def callback(samples, frames, timestamp, status):
        received.append((samples.copy(), timestamp, status))
        return BackendCallbackDirective.CONTINUE
    handle = native_handle(monkeypatch, callback, faults)
    handle._spec = replace(handle._spec, hardware_id='5', hostapi_name=hostapi,
        sample_rate_hz=44100, frames_per_buffer=512)
    handle._set_native_latency(.1)
    duration = round(512 * 1e9 / 44100)
    entry = 2_465_589_048_279_600
    expected_raw_end = 2_465_589_137_607_250
    raw_end = 2_465_589_111_525_477
    uncertainty = 16_000_000
    # Replay occurs five seconds later, and must not become the audio origin.
    handle._monotonic_authority = SimpleNamespace(monotonic_ns=lambda: entry + 5_000_000_000)
    values = np.arange(1024 * 2, dtype=np.float32).reshape(1024, 2)
    directives = []
    for index, raw in enumerate((expected_raw_end - duration, raw_end)):
        block_entry = entry - duration if index == 0 else entry
        timing = SimpleNamespace(inputBufferAdcTime=(raw - duration) / 1e9,
            currentTime=block_entry / 1e9)
        assert round(timing.inputBufferAdcTime * 1e9) + duration == raw
        if native_timing:
            timing.native_entry_ns = block_entry
            timing.clock_uncertainty_ns = uncertainty
        directives.append(handle._native_callback(values[index*512:(index+1)*512],
            512, timing, BackendCallbackStatus.OK))
    if continues:
        assert directives == [BackendCallbackDirective.CONTINUE] * 2, faults
        assert faults == []
        np.testing.assert_array_equal(np.concatenate([block[0] for block in received]), values)
        origin = entry - 100_000_000 - uncertainty
        assert [block[1] for block in received] == [origin, origin + duration]
        assert all(block[2] is BackendCallbackStatus.OK for block in received)
        # Explicit driver overflow still overrides the callback's CONTINUE.
        assert handle._native_callback(values[:512], 512, timing,
            BackendCallbackStatus.INPUT_OVERFLOW) is BackendCallbackDirective.STOP
        assert len(faults) == 1
        assert faults[0].status is BackendCallbackStatus.INPUT_OVERFLOW
    else:
        assert directives == [BackendCallbackDirective.CONTINUE, BackendCallbackDirective.STOP]
        assert len(received) == 1 and len(faults) == 1
        np.testing.assert_array_equal(received[0][0], values[:512])
        assert faults[0].status is BackendCallbackStatus.BACKEND_FAULT
        assert 'raw_end_ns=2465589111525477' in faults[0].summary
        assert 'expected_end_ns=2465589137607250' in faults[0].summary
        assert 'delta_ns=-26081773 tolerance_ns=23219954' in faults[0].summary


@pytest.mark.parametrize('hostapi', ['WASAPI', 'Windows DirectSound'])
def test_native_estimated_clock_correction_keeps_uncertainty_once(monkeypatch, hostapi):
    received, faults = [], []
    handle = native_handle(monkeypatch,
        lambda samples, frames, stamp, status: received.append(stamp) or BackendCallbackDirective.CONTINUE,
        faults)
    handle._spec = replace(handle._spec, hostapi_name=hostapi,
        sample_rate_hz=44100, frames_per_buffer=512)
    # Zero latency starts at the ceiling, so an early next block needs correction.
    handle._set_native_latency(0)
    handle._monotonic_authority = SimpleNamespace(monotonic_ns=lambda: 90_000_000_000)
    for index in range(3):
        entry = 2_000_000_000 + index * 10_000_000
        info = SimpleNamespace(native_entry_ns=entry, clock_uncertainty_ns=16_000_000,
            inputBufferAdcTime=1.25 + index * .1, currentTime=2.0)
        assert handle._native_callback(np.zeros((512, 2), np.float32), 512,
            info, BackendCallbackStatus.OK) is BackendCallbackDirective.CONTINUE
        assert received[-1] == entry - 16_000_000
    assert faults == []


def test_directsound_actual_pool_44100_native_jitter_stall_and_exact_captures(monkeypatch, harness):
    import sounddevice as sd

    rate, block_frames, count = 44100, 512, 500
    drivers, timings = [], []
    jitter_enabled, replay_gate, drain_gate = threading.Event(), threading.Event(), threading.Event()
    replay_gate.set()
    drain_gate.set()

    class ProducingStream(ExpertStream):
        def start(self):
            super().start()
            self.driver = harness.driver_start(int(sd._ffi.cast('uintptr_t', self.kwargs['userdata'])),
                count, block_frames, 2, 10, 0)
            assert self.driver
            drivers.append(self.driver)

        def stop(self, ignore_errors=True):
            assert harness.driver_wait(self.driver, 1000)
            super().stop(ignore_errors=ignore_errors)

    class EstimatedAdcBackend(MonotonicSoundDeviceAudioBackend):
        def create_input(self, spec, callback, on_fault):
            handle = super().create_input(spec, callback, on_fault)
            original_callback = handle._sounddevice_callback
            cursor = 0
            def estimated_adc_callback(samples, frames, timing, status):
                nonlocal cursor
                # The existing C driver supplies real native memory/entry times
                # and 512/511/510-frame PCM. Change only estimated ADC metadata
                # at the pump boundary; never replace samples or native entry.
                jitter = (-.026081773, .075, -.09)[len(timings) % 3] if jitter_enabled.is_set() else 0
                timing.inputBufferAdcTime = 1.25 + cursor / rate + jitter
                timing.currentTime = 1.35 + cursor / rate
                timings.append((frames, timing.native_entry_ns, timing.clock_uncertainty_ns,
                    timing.inputBufferAdcTime, time.monotonic_ns()))
                cursor += frames
                return original_callback(samples, frames, timing, status)
            handle._sounddevice_callback = estimated_adc_callback
            return handle

    monkeypatch.setattr(sd, '_StreamBase', ProducingStream)
    backend = EstimatedAdcBackend(sd, PythonSystemMonotonicAuthority())
    def observer(*args):
        assert drain_gate.wait(4)
    spec = replace(stream_spec(sample_rate_hz=rate, frames_per_buffer=block_frames,
        capture_channel_indices=(0, 1)), hostapi_name='Windows DirectSound')
    result = ResidentAudioPool.open([spec], backend=backend, max_capture_frames=rate,
        startup_stream_timeout_s=1, fault_close_timeout_s=1, shutdown_timeout_s=2,
        drain_observer=observer)
    pool = result.pool
    try:
        assert result.ready_count == 1
        slot = pool._slots['mic-1']
        native = slot._handle._native
        generation = pool._generation_ids['mic-1']
        real_ready = native._ready
        native._ready = lambda: replay_gate.is_set() and real_ready()
        replay_gate.clear()
        wait_until(lambda: bool(slot._ring.block_metadata))
        origin = slot._ring.block_metadata[0].estimated_start_ns
        first_frames, first_entry, uncertainty, _, _ = timings[0]
        assert first_frames == 512
        assert origin == first_entry - 100_000_000 - uncertainty
        before = native.producer_snapshot().sequence
        sleep = ct.PyDLL('kernel32').Sleep
        sleep.argtypes, sleep.restype = [ct.c_uint32], None
        sleep(2300)
        assert native.producer_snapshot().sequence - before >= 100
        wait_until(lambda: native.producer_snapshot().sequence - native.producer_snapshot().consumed
            > slot._ingress.capacity_blocks + 20)
        ticket = pool.begin_readiness('mic-1', time.monotonic_ns() + 2_000_000_000)
        outcome = ticket.future.wait(2)
        assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
        assert outcome.generation_id == generation
        replay_start = len(timings)
        jitter_enabled.set()
        drain_gate.clear()
        replay_gate.set()
        wait_until(lambda: not native._ready() or slot.fault_details_under_decision_lock()[0] is not None)
        assert slot.fault_details_under_decision_lock()[0] is None, slot.fault_details_under_decision_lock()
        assert slot._ingress.overflow_count == 0
        assert native.producer_snapshot().sequence > native.producer_snapshot().consumed
        drain_gate.set()
        expected = np.concatenate([np.arange((block_frames-i%3)*2, dtype=np.float32).reshape(-1, 2)
            + i*10000 for i in range(count)])
        for window in range(2):
            window_start = window * rate
            wait_until(lambda: slot.ring_bounds()[1] > window_start)
            metadata = next(item for item in slot._ring.block_metadata
                if item.start_cursor <= window_start < item.end_cursor)
            # Ring triggers select with ceil. Floor the within-block duration
            # to avoid asking for the next sample after nanosecond rounding.
            trigger = metadata.estimated_start_ns + (window_start - metadata.start_cursor) * 1_000_000_000 // rate
            assert abs(trigger - (origin + window * 1_000_000_000)) <= 1
            binding = pool.reserve_capture(f'directsound-window-{window}', 'mic-1')
            completion = pool.start_capture(binding, trigger_monotonic_ns=trigger,
                target_frame_count=rate)
            captured = completion.wait(2)
            assert captured.status is CaptureOutcomeStatus.COMPLETED
            assert captured.audio.start_frame_cursor == window*rate
            np.testing.assert_array_equal(captured.audio.samples, expected[window*rate:(window+1)*rate])
        assert pool._generation_ids['mic-1'] == generation
        assert pool._slots['mic-1'] is slot
        assert slot.fault_details_under_decision_lock()[0] is None
        assert len(timings) > replay_start + 3
        assert timings[replay_start][4] - timings[replay_start][1] > 2_000_000_000
        assert first_entry == harness.driver_entry(drivers[0], 0)
        assert slot._handle._audio_origin_end_ns == origin + round(block_frames * 1e9 / rate)
        assert native.producer_snapshot().error == 0
    finally:
        replay_gate.set()
        drain_gate.set()
        assert pool.shutdown().status is ShutdownStatus.CLOSED
        for driver in drivers:
            assert harness.driver_wait(driver, 1000)
            harness.driver_destroy(driver)


@pytest.mark.parametrize('failure', ['input_overflow', 'native_full', 'native_invalid'])
def test_directsound_actual_native_fault_still_stops(monkeypatch, failure):
    received, faults = [], []
    def callback(samples, frames, timestamp, status):
        received.append(status)
        return BackendCallbackDirective.CONTINUE
    callback.native_ready = lambda: failure == 'input_overflow'
    handle = native_handle(monkeypatch, callback, faults)
    handle._spec = replace(handle._spec, hostapi_name='Windows DirectSound',
        sample_rate_hz=44100, frames_per_buffer=512)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    try:
        buffer = handle._native._buffer
        samples = np.zeros((512, 2), np.float32)
        if failure == 'input_overflow':
            assert invoke(buffer, samples, status=2) == 0  # paInputOverflow
        elif failure == 'native_full':
            for _ in range(buffer.snapshot().capacity):
                assert invoke(buffer, samples) == 0
            assert invoke(buffer, samples) == 2
        else:
            assert invoke(buffer, np.zeros((513, 2), np.float32)) == 2
        # Fault retirement can already have closed native memory by this point.
        wait_until(lambda: bool(faults))
        assert len(faults) == 1
        if failure == 'input_overflow':
            assert received == [BackendCallbackStatus.INPUT_OVERFLOW]
            assert faults[0].status is BackendCallbackStatus.INPUT_OVERFLOW
        else:
            assert received == []
            assert faults[0].status is BackendCallbackStatus.BACKEND_FAULT
            code = 1 if failure == 'native_full' else 2
            assert f'Native capture fault code={code}' in faults[0].summary
        assert handle._retired
    finally:
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


@pytest.mark.parametrize('force_recovery_observation', [False, True])
def test_actual_pool_native_producer_survives_gil_stall_and_replays_adjoining_windows(monkeypatch, harness, force_recovery_observation):
    import sounddevice as sd
    drivers = []
    class ProducingStream(ExpertStream):
        def start(self):
            super().start()
            self.driver = harness.driver_start(int(sd._ffi.cast('uintptr_t', self.kwargs['userdata'])),
                600, 16, 2, 10, 0)
            assert self.driver
            drivers.append(self.driver)
        def stop(self, ignore_errors=True):
            assert harness.driver_wait(self.driver, 1000)
            super().stop(ignore_errors=ignore_errors)
    monkeypatch.setattr(sd, '_StreamBase', ProducingStream)
    backend = MonotonicSoundDeviceAudioBackend(sd, PythonSystemMonotonicAuthority())
    observed = []
    drain_gate = threading.Event()
    drain_gate.set()
    def observer(hardware, samples, frames, timestamp, sample_rate):
        observed.append(samples.copy())
        assert drain_gate.wait(4)
    result = ResidentAudioPool.open([stream_spec(sample_rate_hz=1000, frames_per_buffer=16,
        capture_channel_indices=(0, 1))], backend=backend, max_capture_frames=100,
        startup_stream_timeout_s=1, fault_close_timeout_s=1, shutdown_timeout_s=2,
        drain_observer=observer)
    pool = result.pool
    assert result.ready_count == 1
    slot = pool._slots['mic-1']
    native = slot._handle._native
    generation = pool._generation_ids['mic-1']
    gate = threading.Event()
    real_ready = native._ready
    native._ready = lambda: gate.is_set() and real_ready()
    signal = SimpleNamespace(connect=lambda *args: None, disconnect=lambda *args: None)
    history = PlcCurrentHistory(bus=SimpleNamespace(plc_current_sampled=signal), plc_ids=(1,))
    try:
        wait_until(lambda: bool(slot._ring.block_metadata))
        origin = slot._ring.block_metadata[0].estimated_start_ns
        # These readings were actually observed before the stall. Replay must
        # preserve their offsets to the same native audio origin.
        history._on_sample(1, origin / 1e9, 7.0)
        history._on_sample(1, origin / 1e9 + .05, 8.0)
        sleep = ct.PyDLL('kernel32').Sleep
        sleep.argtypes, sleep.restype = [ct.c_uint32], None
        sleep(2300)  # Native production continues while every Python thread is blocked.
        assert native.producer_snapshot().sequence > 125
        if force_recovery_observation:
            original_snapshot = slot._handle.native_producer_snapshot
            stale_once = [True]
            def snapshot():
                value = original_snapshot()
                if stale_once and stale_once.pop():
                    value.heartbeat_ns = 0
                return value
            slot._handle.native_producer_snapshot = snapshot
        ticket = pool.begin_readiness('mic-1', time.monotonic_ns() + 2_000_000_000)
        outcome = ticket.future.wait(2)
        assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
        assert outcome.generation_id == generation
        assert pool._slots['mic-1'] is slot
        current = []
        subscription = history.subscribe(1, origin / 1e9,
            lambda plc, stamp, value: current.append((stamp-origin/1e9, value)))
        subscription.drain()
        subscription.close()
        assert [stamp for stamp, value in current] == pytest.approx([0, .05])
        assert [value for stamp, value in current] == [7.0, 8.0]
        # Hold Resident drain while the pump replays >2 seconds into its smaller
        # ingress: it must retain the excess in native memory without faulting.
        drain_gate.clear()
        gate.set()
        wait_until(lambda: slot._ingress.accepted_sequence >= slot._ingress.capacity_blocks)
        time.sleep(.04)
        assert slot._ingress.overflow_count == 0
        assert slot.fault_details_under_decision_lock()[0] is None
        assert native.producer_snapshot().sequence > native.producer_snapshot().consumed
        drain_gate.set()
        expected = np.concatenate([np.arange((16-i%3)*2, dtype=np.float32).reshape(-1, 2)
            + i*10000 for i in range(30)])
        for window in range(2):
            binding = pool.reserve_capture(f'window-{window}', 'mic-1')
            completion = pool.start_capture(binding, trigger_monotonic_ns=origin+window*100_000_000,
                target_frame_count=100)
            captured = completion.wait(2)
            assert captured.status is CaptureOutcomeStatus.COMPLETED
            assert captured.audio.start_frame_cursor == window*100
            np.testing.assert_array_equal(captured.audio.samples, expected[window*100:(window+1)*100])
        assert pool._generation_ids['mic-1'] == generation
    finally:
        gate.set()
        drain_gate.set()
        assert pool.shutdown().status is ShutdownStatus.CLOSED
        history.close()
        for driver in drivers:
            assert harness.driver_wait(driver, 1000)
            harness.driver_destroy(driver)


def test_two_station_48khz_adjoining_eight_second_captures_survive_handoff_gil_pause(
    monkeypatch, harness,
):
    import sounddevice as sd

    rate, block_frames, channels = 48000, 512, 2
    window_frames = rate * 8
    count = (window_frames * 2) // (block_frames - 1) + 2
    drivers = []

    class ProducingStream(ExpertStream):
        def start(self):
            super().start()
            self.driver = harness.driver_start(
                int(sd._ffi.cast('uintptr_t', self.kwargs['userdata'])),
                count, block_frames, channels, 10, 0,
            )
            assert self.driver
            drivers.append(self.driver)

        def stop(self, ignore_errors=True):
            assert harness.driver_wait(self.driver, 1000)
            super().stop(ignore_errors=ignore_errors)

    monkeypatch.setattr(sd, '_StreamBase', ProducingStream)
    backend = MonotonicSoundDeviceAudioBackend(sd, PythonSystemMonotonicAuthority())
    spec = stream_spec(sample_rate_hz=rate, frames_per_buffer=block_frames,
        capture_channel_indices=(0, 1))
    specs = [replace(spec, hardware_id=f'station-{i}', device_index=i) for i in (1, 2)]
    result = ResidentAudioPool.open(specs, backend=backend,
        max_capture_frames=window_frames, startup_stream_timeout_s=2,
        fault_close_timeout_s=1, shutdown_timeout_s=2)
    pool = result.pool
    try:
        assert result.ready_count == 2
        origins, generations, first = {}, {}, {}
        for spec in specs:
            hardware = spec.hardware_id
            slot = pool._slots[hardware]
            wait_until(lambda: bool(slot._ring.block_metadata))
            origins[hardware] = slot._ring.block_metadata[0].estimated_start_ns
            generations[hardware] = pool._generation_ids[hardware]
            binding = pool.reserve_capture(f'{hardware}-window-1', hardware)
            first[hardware] = pool.start_capture(binding,
                trigger_monotonic_ns=origins[hardware], target_frame_count=window_frames)

        # Driver Sleep(10) may be ~15.6 ms: this is full 48 kHz PCM volume,
        # not a claim that synthetic wall cadence matches a hardware sample clock.
        wait_until(lambda: all(slot.ring_bounds()[1] >= rate * 7.5
            for slot in pool._slots.values()), seconds=25)
        before = {hardware: slot._handle.native_producer_snapshot().sequence
            for hardware, slot in pool._slots.items()}
        sleep = ct.PyDLL('kernel32').Sleep
        sleep.argtypes, sleep.restype = [ct.c_uint32], None
        started = time.monotonic()
        sleep(2300)
        assert time.monotonic() - started >= 2.2
        for hardware, slot in pool._slots.items():
            snapshot = slot._handle.native_producer_snapshot()
            assert snapshot.sequence >= before[hardware] + 100
            assert snapshot.error == 0

        second = {}
        for spec in specs:
            hardware = spec.hardware_id
            binding = pool.reserve_capture(f'{hardware}-window-2', hardware)
            second[hardware] = pool.start_capture(binding,
                trigger_monotonic_ns=origins[hardware] + 8_000_000_000,
                target_frame_count=window_frames)

        reference = np.concatenate([
            (np.arange((block_frames-i%3)*channels, dtype=np.float32) + i*10000)
                .reshape(-1, channels)
            for i in range(count)
        ])
        for hardware in origins:
            for window, completion in enumerate((first[hardware], second[hardware])):
                captured = completion.wait(20)
                assert captured.status is CaptureOutcomeStatus.COMPLETED
                assert captured.audio.frame_count == window_frames
                assert captured.audio.start_frame_cursor == window * window_frames
                expected = reference[window*window_frames:(window+1)*window_frames]
                np.testing.assert_array_equal(captured.audio.samples, expected)
                # All four 2-second phases, including each exact join, survive.
                for phase in range(4):
                    begin, end = phase*rate*2, (phase+1)*rate*2
                    np.testing.assert_array_equal(
                        captured.audio.samples[begin:end], expected[begin:end])
            slot = pool._slots[hardware]
            assert pool._generation_ids[hardware] == generations[hardware]
            assert slot._ingress.overflow_count == 0
            assert slot.fault_details_under_decision_lock()[0] is None
        for driver in drivers:
            assert harness.driver_wait(driver, 1000)
            assert harness.driver_result(driver) == 0
            assert harness.driver_priority(driver) == 2
    finally:
        assert pool.shutdown().status is ShutdownStatus.CLOSED
        for driver in drivers:
            assert harness.driver_wait(driver, 1000)
            harness.driver_destroy(driver)


def test_resident_explicit_readiness_preserves_legacy_overflow_contract():
    spec = stream_spec(sample_rate_hz=1000, frames_per_buffer=10)
    slot = _ResidentStreamSlot(spec, ring_capacity_frames=10010, monotonic_ns=time.monotonic_ns)
    callback, _ = slot.backend_entrypoints()
    assert callback.native_ready()
    values = np.zeros((10, 2), dtype=np.float32)
    for _ in range(slot._ingress.capacity_blocks):
        assert slot._ingress.try_push(values, 10, 1, 1)
    assert not callback.native_ready()
    assert not slot._ingress.try_push(values, 10, 1, 1)
    assert slot._ingress.overflow_count == 1


def test_delayed_pump_close_is_bounded_retains_memory_and_can_retry(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def callback(*args):
        entered.set()
        assert release.wait(2)
        return BackendCallbackDirective.CONTINUE
    handle = native_handle(monkeypatch, callback)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    native = handle._native
    assert invoke(native._buffer, np.zeros((10, 2), np.float32)) == 0
    assert entered.wait(1)
    try:
        started = time.monotonic()
        result = handle.close(time.monotonic_ns() + 30_000_000)
        assert result.status is not BackendCloseStatus.CLOSED
        assert time.monotonic() - started < .3
        assert not native.closed
        assert native._buffer.userdata_address
        assert native._buffer.snapshot().consumed == 0
    finally:
        release.set()
        assert handle.wait_for_lifecycle_settle(1)
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED
        assert native.closed


@pytest.mark.parametrize('stage', ['open', 'start', 'close'])
def test_native_lifecycle_errors_retain_retryable_owners(monkeypatch, stage):
    class FailingStream(ExpertStream):
        failed = False
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            if stage == 'open':
                raise RuntimeError('open failed after allocation')
        def start(self):
            if stage == 'start':
                raise RuntimeError('start failed')
            super().start()
        def close(self, ignore_errors=True):
            if stage == 'close' and not self.failed:
                self.failed = True
                raise RuntimeError('close failed once')
            super().close(ignore_errors=ignore_errors)
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE, expert=FailingStream)
    try:
        if stage in ('open', 'start'):
            with pytest.raises(RuntimeError, match=f'{stage} failed'):
                handle.start(time.monotonic_ns() + 1_000_000_000)
            assert handle.start_failure_stage == stage
        else:
            handle.start(time.monotonic_ns() + 1_000_000_000)
            assert handle.close(time.monotonic_ns() + 1_000_000_000).status is not BackendCloseStatus.CLOSED
    finally:
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_missing_native_dll_fails_clearly_without_python_fallback(monkeypatch):
    from app.service import native_capture_buffer as binding
    def missing():
        raise binding.NativeCaptureError('Required Windows x64 capture DLL unavailable')
    monkeypatch.setattr(binding, '_load_library', missing)
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE)
    with pytest.raises(binding.NativeCaptureError, match='Required Windows x64 capture DLL unavailable'):
        handle.start(time.monotonic_ns() + 1_000_000_000)
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_package_includes_production_native_dll():
    from pathlib import Path
    spec = Path('main.spec').read_text(encoding='utf-8')
    assert '("app/native/capture_buffer.dll", "app/native")' in spec


def test_non_wasapi_native_timestamp_allows_fixed_clock_domain_uncertainty(monkeypatch):
    from dataclasses import replace
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE)
    handle._spec = replace(handle._spec, hostapi_name='ASIO')
    received = []
    handle._callback = lambda samples, frames, stamp, status: received.append(stamp) or BackendCallbackDirective.CONTINUE
    handle._monotonic_authority = SimpleNamespace(monotonic_ns=lambda: 2_000_000_000)
    result = handle._native_callback(np.zeros((10, 2), np.float32), 10,
        SimpleNamespace(native_entry_ns=2_010_000_000, clock_uncertainty_ns=16_000_000), 0)
    assert result is BackendCallbackDirective.CONTINUE
    assert received == [1_994_000_000]


def test_native_restart_timeout_does_not_rearm_retired_buffer(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class BlockingStop(ExpertStream):
        starts = 0
        def start(self):
            self.starts += 1
            super().start()
        def stop(self, ignore_errors=True):
            entered.set()
            assert release.wait(2)
            super().stop(ignore_errors=ignore_errors)
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE, expert=BlockingStop)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    native = handle._native
    buffer = native._buffer
    try:
        assert not handle.restart_native_once(time.monotonic_ns() + 30_000_000)
        assert entered.is_set()
        assert buffer.snapshot().armed
        assert native._stream.starts == 1
    finally:
        release.set()
        assert handle.wait_for_lifecycle_settle(1)
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED
    assert native._buffer is buffer


def test_pump_and_resident_drain_apply_above_normal_windows_priority(monkeypatch):
    from app.service.analysis.resident_audio_mvp.stream import _DrainOwner
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentThread.argtypes, kernel.GetCurrentThread.restype = [], ct.c_void_p
    kernel.GetThreadPriority.argtypes, kernel.GetThreadPriority.restype = [ct.c_void_p], ct.c_int
    def priority():
        return kernel.GetThreadPriority(kernel.GetCurrentThread())
    pump_values, drain_values = [], []
    def callback(*args):
        pump_values.append(priority())
        return BackendCallbackDirective.CONTINUE
    handle = native_handle(monkeypatch, callback)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    owner = _DrainOwner.create('priority-test', threading.Event(), threading.Event(),
        lambda: drain_values.append(priority()) or False)
    try:
        assert invoke(handle._native._buffer, np.zeros((10, 2), np.float32)) == 0
        owner.control_event.set()
        wait_until(lambda: pump_values and drain_values)
        assert pump_values == [1]
        assert drain_values == [1]
    finally:
        owner.stop_event.set()
        owner.control_event.set()
        owner.thread.join(1)
        assert not owner.thread.is_alive()
        assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_native_stream_requires_stop_close_error_reporting(monkeypatch):
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    stream = handle._native._stream
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED
    assert stream.stop_ignore_errors is False
    assert stream.close_ignore_errors is False


@pytest.mark.parametrize('failure', ['host', 'generic', 'missing_code'])
def test_failed_stop_flags_cannot_free_or_restart_unconfirmed_generation(failure):
    # A failed stop deliberately quarantines its owner until process shutdown.
    # Keep that real lifecycle owner in a child process; never manufacture a
    # successful cleanup to appease the test runner's thread-leak fixture.
    result = subprocess.run([sys.executable, '-c', r'''
import sys
import time
import pytest
import sounddevice as sd
from app.service.native_capture_buffer import NativeCaptureError
from tests.native_capture.test_native_integration import (
    ExpertStream, native_handle, wait_until, BackendCallbackDirective, BackendCloseStatus,
)

class FailedStop(ExpertStream):
    stop_calls = 0
    start_calls = 0
    def start(self):
        self.start_calls += 1
        super().start()
    def stop(self, ignore_errors=True):
        assert ignore_errors is False
        self.stop_calls += 1
        # ASIO's failure path can set these before callback completion is proven.
        self.stopped, self.active = True, False
        if self.stop_calls == 1:
            if sys.argv[1] == 'host':
                raise sd.PortAudioError('Error stopping stream',
                    sd._lib.paUnanticipatedHostError, (2, 7, 'ASIOStop failed'))
            if sys.argv[1] == 'generic':
                raise RuntimeError('already stopped', sd._lib.paStreamIsStopped)
            raise sd.PortAudioError('Stream is stopped')
        raise sd.PortAudioError('Error stopping stream', sd._lib.paStreamIsStopped)

with pytest.MonkeyPatch.context() as monkeypatch:
    faults = []
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE,
        faults, expert=FailedStop)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    native = handle._native
    buffer, stream = native._buffer, native._stream
    address = buffer.userdata_address
    for _ in range(3):
        started = time.monotonic()
        closed = handle.close(time.monotonic_ns() + 100_000_000)
        assert closed.status is not BackendCloseStatus.CLOSED
        assert time.monotonic() - started < .5
        assert handle.wait_for_lifecycle_settle(1)
        assert buffer.userdata_address == address
        assert buffer.snapshot().armed
        assert native.active and not native.closed and not stream.closed
    assert faults
    assert not handle.restart_native_once(time.monotonic_ns() + 100_000_000)
    with pytest.raises(NativeCaptureError):
        native.start()
    with pytest.raises(NativeCaptureError):
        native.stop()
    assert stream.start_calls == 1
    assert stream.stop_calls == 1  # Cached flags cannot establish later quiescence.
    assert native._buffer is buffer
    wait_until(lambda: not native._pump.is_alive())
''', failure], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_callback_abort_stream_is_stopped_error_allows_close(monkeypatch):
    import sounddevice as sd
    class AbortedStream(ExpertStream):
        def stop(self, ignore_errors=True):
            assert ignore_errors is False
            self.stopped, self.active = True, False
            raise sd.PortAudioError('Error stopping stream', sd._lib.paStreamIsStopped)
    faults = []
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE,
        faults, expert=AbortedStream)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    native = handle._native
    native._buffer.request_stop()
    assert invoke(native._buffer, np.zeros((10, 2), np.float32)) == 2
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED
    assert native.closed
    assert not faults


def test_failed_portaudio_open_with_null_output_pointer_cleans_native_buffer(monkeypatch):
    import sounddevice as sd
    class FailedOpen:
        def __init__(self, **kwargs):
            self._ptr = sd._ffi.new('PaStream **')
            raise RuntimeError('Pa_OpenStream failed')
        def close(self, **kwargs):
            raise AssertionError('there is no PaStream to close')
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE, expert=FailedOpen)
    with pytest.raises(RuntimeError, match='Pa_OpenStream failed'):
        handle.start(time.monotonic_ns() + 1_000_000_000)
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_actual_expert_parameter_failure_before_pointer_allocation_cleans_buffer(monkeypatch):
    import sounddevice as sd
    actual_expert = sd._StreamBase
    def parameters(*args):
        raise RuntimeError('device unavailable before pointer allocation')
    monkeypatch.setattr(sd, '_get_stream_parameters', parameters)
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE, expert=actual_expert)
    with pytest.raises(RuntimeError, match='before pointer allocation'):
        handle.start(time.monotonic_ns() + 1_000_000_000)
    assert '_ptr' not in handle._native._stream.__dict__
    assert handle._native._stream._ptr == sd._ffi.NULL
    assert handle.start_failure_stage == 'open'
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED
    assert handle._native.closed


def test_failed_close_keeps_original_pointer_for_explicit_retry(monkeypatch):
    import sounddevice as sd
    class FailedClose(ExpertStream):
        failed = False
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._ptr = sd._ffi.cast('PaStream *', 1234)
        def close(self, **kwargs):
            previous = self._ptr
            self._ptr = sd._ffi.NULL  # sounddevice does this before checking err.
            if not self.failed:
                self.failed = True
                raise RuntimeError('Pa_CloseStream failed')
            assert int(sd._ffi.cast('uintptr_t', previous)) == 1234
            super().close(**kwargs)
    handle = native_handle(monkeypatch, lambda *args: BackendCallbackDirective.CONTINUE, expert=FailedClose)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is not BackendCloseStatus.CLOSED
    assert handle.close(time.monotonic_ns() + 1_000_000_000).status is BackendCloseStatus.CLOSED


def test_ten_second_audio_history_replays_delayed_trigger_and_expires_exactly():
    ring = _ResidentRing(1010, 2)
    ring.configure_timeline(sample_rate_hz=100, frames_per_buffer=10)
    reference = np.arange(2200, dtype=np.float32).reshape(1100, 2)
    for i in range(110):
        ring.append_ingress(reference[i*10:(i+1)*10], callback_monotonic_ns=(i+1)*100_000_000)
    assert ring.map_trigger(999_999_999).status is _MappingStatus.EXPIRED
    assert ring.map_trigger(1_000_000_000).cursor == 100
    assert ring.map_trigger(3_000_000_000).cursor == 300
    np.testing.assert_array_equal(ring.read(300, 200), reference[300:500])


def test_current_history_replays_observed_readings_after_two_seconds():
    signal = SimpleNamespace(connect=lambda *a: None, disconnect=lambda *a: None)
    clock = [1.0]
    history = PlcCurrentHistory(bus=SimpleNamespace(plc_current_sampled=signal), plc_ids=(1,), monotonic=lambda: clock[0])
    try:
        history._on_sample(1, 1.0, 7.0)
        history._on_sample(1, 2.0, 8.0)
        clock[0] = 11.0
        received = []
        sub = history.subscribe(1, 1.0, lambda *args: received.append(args))
        sub.drain()
        assert received == [(1, 1.0, 7.0), (1, 2.0, 8.0)]
        sub.close()
        clock[0] = 11.000001
        with pytest.raises(CurrentHistoryUnavailable):
            history.subscribe(1, 1.0, lambda *args: None)
    finally:
        history.close()
