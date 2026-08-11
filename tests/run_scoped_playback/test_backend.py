from __future__ import annotations

import math
import sys
import threading

import numpy
import pytest

from app.service.run_scoped_playback.backend import (
    DEFAULT_OUTPUT_LATENCY_SECONDS,
    SoundDevicePlaybackBackend,
)
from app.service.run_scoped_playback.contracts import (
    BackendJoinStatus,
    BackendStartStatus,
    PlaybackContractError,
)


class FakeCallbackStop(Exception):
    pass


class FakeCallbackAbort(Exception):
    pass


class FakeStream:
    def __init__(
        self,
        owner: "FakeSoundDevice",
        kwargs: dict[str, object],
    ) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.entered = threading.Event()
        self.exited = threading.Event()
        self.pending_output = False
        self.truncated_output = False
        self.operations: list[tuple[str, bool | None]] = []

    def __enter__(self) -> "FakeStream":
        self.start()
        return self

    def start(self) -> None:
        self.owner.start_calls += 1
        self.owner.enter_calls += 1
        self.operations.append(("start", None))
        self.entered.set()
        if self.owner.enter_release is not None:
            assert self.owner.enter_release.wait(2.0)
        if self.owner.open_error is not None:
            raise self.owner.open_error

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.owner.exit_calls += 1
        self.close()
        return False

    def stop(self, *, ignore_errors: bool = True) -> None:
        self.owner.stop_calls += 1
        self.owner.stop_ignore_errors.append(ignore_errors)
        self.operations.append(("stop", ignore_errors))
        self.owner.stop_entered.set()
        if self.owner.stop_release is not None:
            assert self.owner.stop_release.wait(2.0)
        if self.owner.stop_error is not None:
            raise self.owner.stop_error
        self.pending_output = False

    def close(self, *, ignore_errors: bool = True) -> None:
        self.owner.close_calls += 1
        self.owner.close_ignore_errors.append(ignore_errors)
        self.operations.append(("close", ignore_errors))
        if self.pending_output:
            self.truncated_output = True
        self.exited.set()
        if self.owner.exit_error is not None:
            raise self.owner.exit_error

    def invoke(self, frames: int, *, outdata: numpy.ndarray | None = None):
        if outdata is None:
            outdata = numpy.full((frames, 1), 99.0, dtype=numpy.float32)
        try:
            self.callback(outdata, frames, object(), None)
        finally:
            if outdata.size and numpy.any(outdata):
                self.pending_output = True
        return outdata


class FakeSoundDevice:
    CallbackStop = FakeCallbackStop
    CallbackAbort = FakeCallbackAbort

    def __init__(
        self,
        *,
        devices: list[dict[str, object]] | None = None,
        hostapis: list[dict[str, object]] | None = None,
        open_error: Exception | None = None,
        stop_error: Exception | None = None,
        stop_release: threading.Event | None = None,
        exit_error: Exception | None = None,
        enter_release: threading.Event | None = None,
    ) -> None:
        self.devices = (
            [
                {
                    "name": "Speaker",
                    "hostapi": 0,
                    "max_output_channels": 2,
                }
            ]
            if devices is None
            else devices
        )
        self.hostapis = (
            [{"name": "ASIO"}] if hostapis is None else hostapis
        )
        self.open_error = open_error
        self.stop_error = stop_error
        self.stop_release = stop_release
        self.exit_error = exit_error
        self.enter_release = enter_release
        self.query_devices_calls = 0
        self.query_hostapis_calls = 0
        self.output_stream_calls = 0
        self.start_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.stop_ignore_errors: list[bool] = []
        self.close_ignore_errors: list[bool] = []
        self.stop_entered = threading.Event()
        self.streams: list[FakeStream] = []
        self.stream_created = threading.Event()

    def query_devices(self):
        self.query_devices_calls += 1
        return self.devices

    def query_hostapis(self):
        self.query_hostapis_calls += 1
        return self.hostapis

    def OutputStream(self, **kwargs: object) -> FakeStream:
        self.output_stream_calls += 1
        stream = FakeStream(self, kwargs)
        self.streams.append(stream)
        self.stream_created.set()
        return stream


def segment(values: list[float] | None = None) -> numpy.ndarray:
    return numpy.array(
        [0.25, -0.25, 0.5] if values is None else values,
        dtype=numpy.float32,
        order="C",
    )


def start(
    backend: SoundDevicePlaybackBackend,
    samples: numpy.ndarray | None = None,
    **overrides: object,
):
    values = {
        "segment": segment() if samples is None else samples,
        "sample_rate_hz": 48_000,
        "output_device_name": "Speaker",
        "output_host_api": "ASIO",
        "gain_db": 0.0,
        "startup_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return backend.start(**values)


def install_sounddevice(monkeypatch, fake: FakeSoundDevice) -> None:
    monkeypatch.setattr(
        sys.modules["app.service.run_scoped_playback.backend"], "sd", fake
    )


def test_constructor_accepts_exact_defaults_and_custom_resolver():
    resolver = lambda device, host_api: 3
    backend = SoundDevicePlaybackBackend(device_resolver=resolver)

    assert DEFAULT_OUTPUT_LATENCY_SECONDS == 0.05
    assert backend._device_resolver is resolver
    assert backend._output_latency_seconds == 0.05


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"device_resolver": object()}, TypeError),
        ({"output_latency_seconds": 1}, PlaybackContractError),
        ({"output_latency_seconds": True}, PlaybackContractError),
        ({"output_latency_seconds": 0.0}, PlaybackContractError),
        ({"output_latency_seconds": -0.1}, PlaybackContractError),
        ({"output_latency_seconds": math.inf}, PlaybackContractError),
        ({"output_latency_seconds": math.nan}, PlaybackContractError),
    ],
)
def test_constructor_rejects_invalid_boundaries(kwargs, error):
    with pytest.raises(error):
        SoundDevicePlaybackBackend(**kwargs)


def invalid_segments() -> list[object]:
    owner = numpy.arange(6, dtype=numpy.float32)
    return [
        [0.0],
        numpy.array([], dtype=numpy.float32),
        numpy.array([[0.0]], dtype=numpy.float32),
        numpy.array([0.0], dtype=numpy.float64),
        owner[1:4],
        owner[::2],
        numpy.array([numpy.nan], dtype=numpy.float32),
        numpy.array([numpy.inf], dtype=numpy.float32),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("segment", value) for value in invalid_segments()),
        ("sample_rate_hz", True),
        ("sample_rate_hz", 48_000.0),
        ("sample_rate_hz", 0),
        ("sample_rate_hz", -1),
        ("output_device_name", 1),
        ("output_device_name", ""),
        ("output_device_name", "   "),
        ("output_host_api", None),
        ("output_host_api", ""),
        ("output_host_api", "\t"),
        ("gain_db", 0),
        ("gain_db", True),
        ("gain_db", -60.0001),
        ("gain_db", 35.0001),
        ("gain_db", math.inf),
        ("gain_db", math.nan),
        ("startup_timeout_seconds", 1),
        ("startup_timeout_seconds", True),
        ("startup_timeout_seconds", -0.1),
        ("startup_timeout_seconds", math.inf),
        ("startup_timeout_seconds", math.nan),
    ],
)
def test_start_rejects_invalid_exact_inputs_before_state_change(
    monkeypatch, field, value
):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    resolver_calls: list[tuple[str, str]] = []
    backend = SoundDevicePlaybackBackend(
        device_resolver=lambda device, host: resolver_calls.append(
            (device, host)
        )
        or 0
    )

    with pytest.raises(PlaybackContractError):
        start(backend, **{field: value})

    assert resolver_calls == []
    assert fake.output_stream_calls == 0
    assert backend.join(0.0).status is BackendJoinStatus.CLEAN


@pytest.mark.parametrize("timeout", [None, 0, True, -0.1, math.inf, math.nan])
def test_join_rejects_invalid_timeout_before_state_change(timeout):
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    with pytest.raises(PlaybackContractError):
        backend.join(timeout)

    assert backend.join(0.0).status is BackendJoinStatus.CLEAN


@pytest.mark.parametrize("gain", [-60.0, 35.0])
def test_start_accepts_inclusive_gain_bounds(monkeypatch, gain):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    report = start(backend, gain_db=gain)

    assert report.status is BackendStartStatus.STARTED
    backend.request_stop()
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


@pytest.mark.parametrize("resolved", [True, 0.0, -1, "0", None])
def test_custom_resolver_result_requires_exact_nonnegative_integer(
    monkeypatch, resolved
):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(
        device_resolver=lambda *_: resolved
    )

    with pytest.raises(PlaybackContractError):
        start(backend)

    assert fake.output_stream_calls == 0
    assert backend.join(0.0).status is BackendJoinStatus.CLEAN


def test_default_resolver_requires_one_unique_exact_output_match(monkeypatch):
    fake = FakeSoundDevice(
        devices=[
            {"name": "Speaker", "hostapi": 0, "max_output_channels": 0},
            {"name": "speaker", "hostapi": 0, "max_output_channels": 2},
            {"name": "Speaker", "hostapi": 1, "max_output_channels": 2},
            {"name": "Speaker", "hostapi": 0, "max_output_channels": 2},
        ],
        hostapis=[{"name": "ASIO"}, {"name": "WASAPI"}],
    )
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend()

    report = start(backend)

    assert report.status is BackendStartStatus.STARTED
    assert fake.query_devices_calls == 1
    assert fake.query_hostapis_calls == 1
    assert fake.streams[0].kwargs["device"] == 3
    backend.request_stop()
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


@pytest.mark.parametrize(
    "devices",
    [
        [],
        [{"name": "Other", "hostapi": 0, "max_output_channels": 2}],
        [{"name": "Speaker", "hostapi": 0, "max_output_channels": 0}],
        [
            {"name": "Speaker", "hostapi": 0, "max_output_channels": 1},
            {"name": "Speaker", "hostapi": 0, "max_output_channels": 2},
        ],
    ],
)
def test_default_resolver_rejects_zero_or_multiple_matches_without_fallback(
    monkeypatch, devices
):
    fake = FakeSoundDevice(devices=devices)
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend()

    report = start(backend)

    assert report.status is BackendStartStatus.FAILED
    assert report.generation_owned is False
    assert report.stop_requested is False
    assert fake.output_stream_calls == 0
    assert backend.join(0.0).status is BackendJoinStatus.CLEAN


def test_callback_applies_gain_clips_and_fills_mono_frames_in_order(monkeypatch):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(
        device_resolver=lambda *_: 7,
        output_latency_seconds=0.125,
    )
    original = segment([0.75, -0.75, 0.2])

    report = start(
        backend,
        original,
        sample_rate_hz=44_100,
        gain_db=6.020599913279624,
    )

    assert report.status is BackendStartStatus.STARTED
    stream = fake.streams[0]
    assert stream.kwargs == {
        "device": 7,
        "samplerate": 44_100,
        "channels": 1,
        "dtype": "float32",
        "latency": 0.125,
        "callback": stream.callback,
    }
    first = stream.invoke(2)
    numpy.testing.assert_array_equal(
        first,
        numpy.array([[1.0], [-1.0]], dtype=numpy.float32),
    )
    tail = numpy.full((3, 1), 99.0, dtype=numpy.float32)
    with pytest.raises(FakeCallbackStop):
        stream.invoke(3, outdata=tail)
    numpy.testing.assert_allclose(
        tail,
        numpy.array([[0.4], [0.0], [0.0]], dtype=numpy.float32),
        rtol=0.0,
        atol=1e-6,
    )
    original[:] = 0.0
    assert stream.exited.wait(2.0)
    joined = backend.join(1.0)
    assert joined.status is BackendJoinStatus.CLEAN
    assert joined.generation_owned is False


def test_start_waits_for_stream_open_start_handshake(monkeypatch):
    release = threading.Event()
    fake = FakeSoundDevice(enter_release=release)
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    reports: list[object] = []
    caller = threading.Thread(
        target=lambda: reports.append(start(backend)),
        daemon=True,
    )

    caller.start()
    assert fake.stream_created.wait(2.0)
    assert fake.streams[0].entered.wait(2.0)
    assert reports == []
    release.set()
    caller.join(2.0)

    assert not caller.is_alive()
    assert reports[0].status is BackendStartStatus.STARTED
    backend.request_stop()
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


def test_natural_exhaustion_retains_clean_terminal_until_join(monkeypatch):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    assert start(backend, segment([0.5])).status is BackendStartStatus.STARTED
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(2)
    assert fake.streams[0].exited.wait(2.0)

    joined = backend.join(1.0)
    assert joined.status is BackendJoinStatus.CLEAN
    assert joined.generation_owned is False
    assert backend.join(0.0).status is BackendJoinStatus.CLEAN
    assert fake.streams[0].operations == [
        ("start", None),
        ("stop", False),
        ("close", False),
    ]
    assert fake.streams[0].truncated_output is False


def test_request_stop_is_nonblocking_idempotent_and_terminal_is_retained(
    monkeypatch,
):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend).status is BackendStartStatus.STARTED

    backend.request_stop()
    backend.request_stop()
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(2)
    assert fake.streams[0].exited.wait(2.0)

    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


def test_request_stop_and_join_without_generation_are_exact_noops(monkeypatch):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    backend.request_stop()
    report = backend.join(0.0)

    assert report.status is BackendJoinStatus.CLEAN
    assert report.generation_owned is False
    assert fake.output_stream_calls == 0


def test_stream_open_failure_retains_owned_signaled_non_clean_terminal(
    monkeypatch,
):
    fake = FakeSoundDevice(open_error=RuntimeError("open failed"))
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    started = start(backend)

    assert started.status is BackendStartStatus.FAILED
    assert started.generation_owned is True
    assert started.stop_requested is True
    joined = backend.join(1.0)
    assert joined.status is BackendJoinStatus.NON_CLEAN
    assert joined.generation_owned is False


def test_constructed_stream_is_closed_once_when_start_handshake_fails(
    monkeypatch,
):
    fake = FakeSoundDevice(open_error=RuntimeError("start failed"))
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    started = start(backend)

    assert started.status is BackendStartStatus.FAILED
    assert started.generation_owned is True
    assert started.stop_requested is True
    assert fake.output_stream_calls == 1
    assert fake.start_calls == 1
    assert fake.stop_calls == 0
    assert fake.close_calls == 1
    assert fake.close_ignore_errors == [False]
    joined = backend.join(1.0)
    assert joined.status is BackendJoinStatus.NON_CLEAN
    assert joined.generation_owned is False


def test_callback_failure_retains_non_clean_terminal_until_join(monkeypatch):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend).status is BackendStartStatus.STARTED
    invalid_outdata = numpy.zeros((2, 0), dtype=numpy.float32)

    with pytest.raises(FakeCallbackAbort):
        fake.streams[0].invoke(2, outdata=invalid_outdata)
    assert fake.streams[0].exited.wait(2.0)

    assert backend.join(1.0).status is BackendJoinStatus.NON_CLEAN
    assert fake.streams[0].operations == [
        ("start", None),
        ("stop", False),
        ("close", False),
    ]
    assert fake.stop_calls == 1
    assert fake.stop_ignore_errors == [False]
    assert fake.close_calls == 1
    assert fake.close_ignore_errors == [False]


def test_stop_cleanup_failure_still_closes_and_retains_non_clean_terminal(
    monkeypatch,
):
    fake = FakeSoundDevice(stop_error=RuntimeError("drain failed"))
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend, segment([0.5])).status is BackendStartStatus.STARTED

    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert fake.streams[0].exited.wait(2.0)

    assert backend.join(1.0).status is BackendJoinStatus.NON_CLEAN
    assert fake.streams[0].operations == [
        ("start", None),
        ("stop", False),
        ("close", False),
    ]
    assert fake.stop_calls == 1
    assert fake.close_calls == 1


def test_worker_exit_failure_retains_non_clean_terminal_until_join(monkeypatch):
    fake = FakeSoundDevice(exit_error=RuntimeError("close failed"))
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend, segment([0.5])).status is BackendStartStatus.STARTED

    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert fake.streams[0].exited.wait(2.0)

    assert backend.join(1.0).status is BackendJoinStatus.NON_CLEAN
    assert fake.streams[0].operations == [
        ("start", None),
        ("stop", False),
        ("close", False),
    ]
    assert fake.stop_ignore_errors == [False]
    assert fake.close_ignore_errors == [False]


def test_startup_timeout_signals_once_never_joins_and_retains_terminal(
    monkeypatch,
):
    release = threading.Event()
    fake = FakeSoundDevice(enter_release=release)
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)

    started = start(backend, startup_timeout_seconds=0.0)

    assert started.status is BackendStartStatus.TIMED_OUT
    assert started.generation_owned is True
    assert started.stop_requested is True
    assert fake.streams[0].exited.is_set() is False
    assert backend.join(0.0).status is BackendJoinStatus.TIMED_OUT
    release.set()
    assert fake.streams[0].exited.wait(2.0)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN
    assert fake.streams[0].operations == [
        ("start", None),
        ("stop", False),
        ("close", False),
    ]


def test_oversized_finite_startup_timeout_waits_without_overflow(monkeypatch):
    release = threading.Event()
    fake = FakeSoundDevice(enter_release=release)
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    reports: list[object] = []
    errors: list[BaseException] = []
    finished = threading.Event()

    def invoke_start() -> None:
        try:
            reports.append(
                start(
                    backend,
                    startup_timeout_seconds=sys.float_info.max,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=invoke_start, daemon=True)
    caller.start()
    assert fake.stream_created.wait(2.0)
    assert fake.streams[0].entered.wait(2.0)
    completed_before_handshake = finished.wait(0.1)
    release.set()
    caller.join(2.0)

    assert completed_before_handshake is False
    assert caller.is_alive() is False
    assert errors == []
    assert reports[0].status is BackendStartStatus.STARTED
    backend.request_stop()
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


def test_join_timeout_retains_generation_then_terminal_join_consumes_it(
    monkeypatch,
):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend).status is BackendStartStatus.STARTED

    timed_out = backend.join(0.0)
    assert timed_out.status is BackendJoinStatus.TIMED_OUT
    assert timed_out.generation_owned is True

    backend.request_stop()
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert fake.streams[0].exited.wait(2.0)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


def test_oversized_finite_join_timeout_waits_without_overflow(monkeypatch):
    stop_release = threading.Event()
    fake = FakeSoundDevice(stop_release=stop_release)
    install_sounddevice(monkeypatch, fake)
    backend = SoundDevicePlaybackBackend(device_resolver=lambda *_: 0)
    assert start(backend).status is BackendStartStatus.STARTED
    backend.request_stop()
    assert fake.stop_entered.wait(2.0)
    reports: list[object] = []
    errors: list[BaseException] = []
    finished = threading.Event()

    def invoke_join() -> None:
        try:
            reports.append(backend.join(sys.float_info.max))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=invoke_join, daemon=True)
    caller.start()
    completed_while_worker_owned = finished.wait(0.1)
    stop_release.set()
    caller.join(2.0)

    assert completed_while_worker_owned is False
    assert caller.is_alive() is False
    assert errors == []
    assert reports[0].status is BackendJoinStatus.CLEAN
    assert reports[0].generation_owned is False


def test_start_while_owned_signals_existing_and_creates_no_second_generation(
    monkeypatch,
):
    fake = FakeSoundDevice()
    install_sounddevice(monkeypatch, fake)
    resolver_calls: list[tuple[str, str]] = []
    backend = SoundDevicePlaybackBackend(
        device_resolver=lambda device, host: resolver_calls.append(
            (device, host)
        )
        or 0
    )
    assert start(backend).status is BackendStartStatus.STARTED

    refused = start(backend, output_device_name="Other")

    assert refused.status is BackendStartStatus.FAILED
    assert refused.generation_owned is True
    assert refused.stop_requested is True
    assert fake.output_stream_calls == 1
    assert resolver_calls == [("Speaker", "ASIO")]
    with pytest.raises(FakeCallbackStop):
        fake.streams[0].invoke(1)
    assert backend.join(1.0).status is BackendJoinStatus.CLEAN


def test_resolver_and_stream_are_not_retried_or_given_fallback_values(
    monkeypatch,
):
    fake = FakeSoundDevice(open_error=RuntimeError("device unavailable"))
    install_sounddevice(monkeypatch, fake)
    calls: list[tuple[str, str]] = []

    def resolver(device: str, host_api: str) -> int:
        calls.append((device, host_api))
        return 4

    backend = SoundDevicePlaybackBackend(device_resolver=resolver)
    started = start(
        backend,
        sample_rate_hz=22_050,
        output_device_name="Exact Device",
        output_host_api="Exact API",
    )

    assert started.status is BackendStartStatus.FAILED
    assert calls == [("Exact Device", "Exact API")]
    assert fake.output_stream_calls == 1
    assert fake.streams[0].kwargs["samplerate"] == 22_050
    assert fake.streams[0].kwargs["device"] == 4
    assert backend.join(1.0).status is BackendJoinStatus.NON_CLEAN


def test_backend_source_has_no_legacy_owner_retry_resampling_or_fallback():
    source = (
        __import__(
            "app.service.run_scoped_playback.backend",
            fromlist=["SoundDevicePlaybackBackend"],
        )
        .__loader__.get_source("app.service.run_scoped_playback.backend")
        .lower()
    )

    assert "oneshotreplayplayer" not in source
    assert "resample" not in source
    assert "default.device" not in source
    assert "retry" not in source
