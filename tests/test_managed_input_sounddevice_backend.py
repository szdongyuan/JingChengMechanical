from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

from app.service.analysis.resident_audio_mvp import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    ResidentStreamSpec,
)
from app.service.managed_input_contracts import (
    BackendScanId,
    StableUidCapability,
    StableUidContract,
)
from app.service.managed_input_sounddevice_backend import (
    ManagedInputSoundDeviceBackend,
    MonotonicSoundDeviceAudioBackend,
)


class _SoundDevice:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def query_devices(self):
        self.calls.append("devices")
        return (
            {
                "name": "Speaker",
                "hostapi": 0,
                "default_samplerate": 48_000.0,
                "max_input_channels": 0,
            },
            {
                "name": "Exact Input",
                "hostapi": 1,
                "default_samplerate": 96_000.0,
                "max_input_channels": 4,
            },
        )

    def query_hostapis(self):
        self.calls.append("hostapis")
        return ({"name": "MME"}, {"name": "ASIO"})

    @staticmethod
    def InputStream(**_kwargs):
        raise AssertionError("native stream need not open in this unit test")

    def _terminate(self):
        raise AssertionError("must not terminate")

    def _initialize(self):
        raise AssertionError("must not initialize")


def test_raw_sounddevice_scan_maps_exact_coordinates_without_uid_invention():
    sounddevice = _SoundDevice()
    backend = ManagedInputSoundDeviceBackend(sounddevice)
    scan_id = BackendScanId("production-process", 7)

    scan = backend.query_raw_input_endpoints(scan_id)

    assert sounddevice.calls == ["devices", "hostapis"]
    assert scan.scan_id is scan_id
    assert scan.complete is True
    assert scan.stable_uid_capability is StableUidCapability.NONE
    assert scan.stable_uid_contract is StableUidContract.ABSENT
    assert tuple((item.hostapi_index, item.hostapi_name) for item in scan.hostapis) == (
        (0, "MME"),
        (1, "ASIO"),
    )
    assert len(scan.observations) == 1
    observation = scan.observations[0]
    assert observation.device_index == 1
    assert observation.hostapi_index == 1
    assert observation.stable_backend_uid is None
    assert observation.descriptor.device_name == "Exact Input"
    assert observation.descriptor.hostapi_name == "ASIO"
    assert observation.descriptor.sample_rate_hz == 96_000.0
    assert observation.descriptor.input_channels == 4


def test_production_callback_timestamps_use_python_system_monotonic_domain_without_rebasing(
    monkeypatch,
):
    from app.service.managed_input_sounddevice_backend import (
        PythonSystemMonotonicAuthority,
    )

    exact_timestamp = 1_234_567_890_123
    monkeypatch.setattr(
        "app.service.managed_input_sounddevice_backend.time.monotonic_ns",
        lambda: exact_timestamp,
    )
    timestamps = []
    backend = MonotonicSoundDeviceAudioBackend(
        _SoundDevice(),
        PythonSystemMonotonicAuthority(),
    )
    handle = backend.create_input(
        ResidentStreamSpec(
            hardware_id="managed-input-1",
            device_index=1,
            device_name="Exact Input",
            hostapi_index=1,
            hostapi_name="ASIO",
            sample_rate_hz=48_000,
            input_channel_count=1,
            capture_channel_indices=(0,),
            sample_format="float32",
            frames_per_buffer=512,
        ),
        lambda _samples, frames, timestamp, _status: (
            timestamps.append((frames, timestamp))
            or BackendCallbackDirective.CONTINUE
        ),
        lambda _fault: None,
    )

    directive = handle._native_callback(
        np.zeros((512, 1), dtype=np.float32),
        512,
        object(),
        BackendCallbackStatus.OK,
    )

    assert directive is BackendCallbackDirective.CONTINUE
    assert timestamps == [(512, exact_timestamp)]


def test_production_callback_keeps_adc_time_when_python_callback_is_late():
    class Clock:
        monotonic_domain = "python/system-monotonic"

        def __init__(self):
            self.now_ns = 1_000_000_000

        def monotonic(self):
            return self.now_ns / 1_000_000_000

        def monotonic_ns(self):
            return self.now_ns

    clock = Clock()
    timestamps = []
    backend = MonotonicSoundDeviceAudioBackend(_SoundDevice(), clock)
    handle = backend.create_input(
        ResidentStreamSpec(
            hardware_id="managed-input-1",
            device_index=1,
            device_name="Exact Input",
            hostapi_index=1,
            hostapi_name="ASIO",
            sample_rate_hz=48_000,
            input_channel_count=1,
            capture_channel_indices=(0,),
            sample_format="float32",
            frames_per_buffer=480,
        ),
        lambda _samples, _frames, timestamp, _status: (
            timestamps.append(timestamp)
            or BackendCallbackDirective.CONTINUE
        ),
        lambda _fault: None,
    )
    block = np.zeros((480, 1), dtype=np.float32)

    handle._native_callback(
        block,
        480,
        SimpleNamespace(currentTime=1.0, inputBufferAdcTime=0.99),
        BackendCallbackStatus.OK,
    )
    clock.now_ns = 1_500_000_000
    handle._native_callback(
        block,
        480,
        SimpleNamespace(currentTime=1.01, inputBufferAdcTime=1.0),
        BackendCallbackStatus.OK,
    )

    assert timestamps == [1_000_000_000, 1_010_000_000]


def test_production_callback_accepts_300_and_500_ms_scheduling_delays():
    class Clock:
        monotonic_domain = "python/system-monotonic"

        def __init__(self):
            self.values = iter(
                (1_000_000_000, 1_300_000_000, 1_800_000_000)
            )

        @staticmethod
        def monotonic():
            return 1.0

        def monotonic_ns(self):
            return next(self.values)

    timestamps = []
    backend = MonotonicSoundDeviceAudioBackend(_SoundDevice(), Clock())
    handle = backend.create_input(
        ResidentStreamSpec(
            hardware_id="managed-input-1",
            device_index=1,
            device_name="Exact Input",
            hostapi_index=1,
            hostapi_name="ASIO",
            sample_rate_hz=48_000,
            input_channel_count=1,
            capture_channel_indices=(0,),
            sample_format="float32",
            frames_per_buffer=512,
        ),
        lambda _samples, _frames, timestamp, _status: (
            timestamps.append(timestamp)
            or BackendCallbackDirective.CONTINUE
        ),
        lambda _fault: None,
    )
    block = np.zeros((512, 1), dtype=np.float32)

    assert handle._native_callback(
        block,
        512,
        object(),
        BackendCallbackStatus.OK,
    ) is BackendCallbackDirective.CONTINUE
    assert handle._native_callback(
        block,
        512,
        object(),
        BackendCallbackStatus.OK,
    ) is BackendCallbackDirective.CONTINUE
    assert handle._native_callback(
        block,
        512,
        object(),
        BackendCallbackStatus.OK,
    ) is BackendCallbackDirective.CONTINUE

    assert timestamps == [
        1_000_000_000,
        1_300_000_000,
        1_800_000_000,
    ]


def test_restart_resets_production_callback_timeline_to_new_observed_time():
    class Clock:
        monotonic_domain = "python/system-monotonic"

        def __init__(self):
            self.now_ns = 1_000_000_000

        def monotonic(self):
            return self.now_ns / 1_000_000_000

        def monotonic_ns(self):
            return self.now_ns

    class Native:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    class SoundDevice:
        CallbackStop = RuntimeError

        def __init__(self):
            self.native = Native()

        def InputStream(self, **_kwargs):
            return self.native

    clock = Clock()
    timestamps = []
    backend = MonotonicSoundDeviceAudioBackend(SoundDevice(), clock)
    handle = backend.create_input(
        ResidentStreamSpec(
            hardware_id="managed-input-1",
            device_index=1,
            device_name="Exact Input",
            hostapi_index=1,
            hostapi_name="ASIO",
            sample_rate_hz=48_000,
            input_channel_count=1,
            capture_channel_indices=(0,),
            sample_format="float32",
            frames_per_buffer=512,
        ),
        lambda _samples, _frames, timestamp, _status: (
            timestamps.append(timestamp)
            or BackendCallbackDirective.CONTINUE
        ),
        lambda _fault: None,
    )
    block = np.zeros((512, 1), dtype=np.float32)
    handle.start(clock.now_ns + 1_000_000_000)
    handle._native_callback(
        block,
        512,
        object(),
        BackendCallbackStatus.OK,
    )

    clock.now_ns = 5_000_000_000
    assert handle.restart_native_once(clock.now_ns + 1_000_000_000) is True
    handle._native_callback(
        block,
        512,
        object(),
        BackendCallbackStatus.OK,
    )

    assert timestamps == [1_000_000_000, 5_000_000_000]
    handle.close(clock.now_ns + 1_000_000_000)
