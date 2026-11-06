"""Hardware-free WASAPI COM ownership tests on the lifecycle thread."""

import ctypes
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCloseStatus,
)
from app.service.analysis.resident_audio_mvp.sounddevice_backend import (
    SoundDeviceAudioBackend,
)
from tests.resident_audio_mvp.test_sounddevice_backend import stream_spec


class FakeOle32:
    def __init__(self, hresult=0):
        self.hresult = hresult
        self.local = threading.local()
        self.events = []
        self.loads = []
        self.uninitialize_entered = threading.Event()
        self.uninitialize_release = None

        def initialize(reserved, flags):
            assert reserved is None and flags == 0
            self.events.append(("initialize", threading.get_ident()))
            self.local.ready = self.hresult in (0, 1, -2147417850, 0x80010106)
            return self.hresult

        def uninitialize():
            self.record("uninitialize")
            self.uninitialize_entered.set()
            if self.uninitialize_release is not None:
                assert self.uninitialize_release.wait(2)
            self.local.ready = False

        self.CoInitializeEx = initialize
        self.CoUninitialize = uninitialize

    def record(self, operation):
        assert getattr(self.local, "ready", False), "WASAPI owner COM is not initialized"
        self.events.append((operation, threading.get_ident()))


@pytest.fixture
def ole32(monkeypatch):
    library = FakeOle32()
    install_ole32(monkeypatch, library)
    return library


def install_ole32(monkeypatch, library, platform="win32"):
    original_loader = getattr(ctypes, "WinDLL", None)

    def load(name, *args, **kwargs):
        if name != "ole32":
            assert original_loader is not None
            return original_loader(name, *args, **kwargs)
        library.loads.append(threading.get_ident())
        return library

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)


def deadline():
    return time.monotonic_ns() + 1_000_000_000


def wasapi_handle(ole32, *, fail=None):
    class Stream:
        closed = False
        active = False

        def __init__(self, **kwargs):
            ole32.record("construct")
            if fail == "construct":
                raise OSError("native construct rejected")

        def start(self):
            ole32.record("start")
            self.active = True

        def stop(self):
            ole32.record("stop")
            self.active = False

        def close(self):
            ole32.record("close")
            self.closed = True

    handle = SoundDeviceAudioBackend(SimpleNamespace(InputStream=Stream)).create_input(
        replace(stream_spec(), hostapi_name="Windows WASAPI"),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )

    def open_stream(native):
        ole32.record("open")
        if fail == "open":
            raise OSError("native open rejected")

    handle._open_stream = open_stream
    return handle


def test_wasapi_owner_initializes_before_construct_and_holds_com_through_restart(ole32):
    handle = wasapi_handle(ole32)
    assert ole32.events == []
    try:
        handle.start(deadline())
        assert handle.restart_native_once(deadline())
    finally:
        result = handle.close(deadline())
    assert result.status is BackendCloseStatus.CLOSED
    assert [event for event, _thread in ole32.events] == [
        "initialize", "construct", "open", "start", "stop", "start",
        "stop", "close", "uninitialize",
    ]
    owner_ids = {owner for _event, owner in ole32.events}
    assert len(owner_ids) == 1
    assert threading.get_ident() not in owner_ids
    assert handle._control_done.is_set()
    assert handle.close(deadline()).status is BackendCloseStatus.CLOSED
    assert sum(event == "uninitialize" for event, _thread in ole32.events) == 1


@pytest.mark.parametrize("stage", ["construct", "open"])
def test_wasapi_failed_open_releases_owner_com_once_after_native_cleanup(ole32, stage):
    handle = wasapi_handle(ole32, fail=stage)
    try:
        with pytest.raises(OSError, match=f"native {stage} rejected"):
            handle.start(deadline())
        assert handle.start_failure_stage == "open"
    finally:
        result = handle.close(deadline())
    assert result.status is BackendCloseStatus.CLOSED
    expected = ["initialize", "construct"]
    if stage == "open":
        expected.extend(["open", "stop", "close"])
    assert [event for event, _thread in ole32.events] == expected + ["uninitialize"]
    assert len({owner for _event, owner in ole32.events}) == 1
    assert handle._control_done.is_set()


@pytest.mark.parametrize("hresult, releases", [(0, 1), (1, 1), (0x80010106, 0), (-2147417850, 0)])
def test_com_hresult_balances_only_acquired_references(ole32, hresult, releases):
    ole32.hresult = hresult
    handle = wasapi_handle(ole32)
    try:
        handle.start(deadline())
    finally:
        result = handle.close(deadline())
    assert result.status is BackendCloseStatus.CLOSED
    assert sum(event == "initialize" for event, _thread in ole32.events) == 1
    assert sum(event == "uninitialize" for event, _thread in ole32.events) == releases
    assert ole32.CoInitializeEx.argtypes == [ctypes.c_void_p, ctypes.c_uint32]
    assert ole32.CoInitializeEx.restype is ctypes.c_int32
    assert ole32.CoUninitialize.argtypes == []
    assert ole32.CoUninitialize.restype is None


@pytest.mark.parametrize("hresult", [0x8007000E, -2147024882])
def test_com_initialization_failure_surfaces_without_open_or_hanging_close(ole32, hresult):
    ole32.hresult = hresult
    handle = wasapi_handle(ole32)
    try:
        with pytest.raises(OSError, match="CoInitializeEx.*0x8007000E"):
            handle.start(deadline())
        assert handle.start_failure_stage == "open"
    finally:
        result = handle.close(deadline())
    assert result.status is BackendCloseStatus.CLOSED
    assert [event for event, _thread in ole32.events] == ["initialize"]
    assert handle._control_done.is_set()


@pytest.mark.parametrize("platform, hostapi", [
    ("win32", "ASIO"), ("win32", "Windows DirectSound"),
    ("linux", "Windows WASAPI"),
])
def test_other_platforms_and_host_apis_do_not_load_com(monkeypatch, platform, hostapi):
    from tests.resident_audio_mvp.test_sounddevice_backend import FakeSoundDevice

    library = FakeOle32()
    install_ole32(monkeypatch, library, platform)
    handle = SoundDeviceAudioBackend(FakeSoundDevice()).create_input(
        replace(stream_spec(), hostapi_name=hostapi),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )
    try:
        handle.start(deadline())
    finally:
        assert handle.close(deadline()).status is BackendCloseStatus.CLOSED
    assert library.loads == []
    assert library.events == []


def test_blocked_native_close_retains_com_until_failed_close_is_retried(ole32):
    entered, release = threading.Event(), threading.Event()
    handle = wasapi_handle(ole32)
    try:
        handle.start(deadline())
        native_close = handle._native.close

        def close():
            if not entered.is_set():
                ole32.record("blocked-close")
                entered.set()
                assert release.wait(2)
                raise OSError("native close needs retry")
            native_close()

        handle._native.close = close
        result = handle.close(time.monotonic_ns() + 30_000_000)
        assert result.status is BackendCloseStatus.NOT_CONFIRMED
        assert entered.is_set()
        assert not ole32.uninitialize_entered.is_set()
        release.set()
        assert handle.wait_for_lifecycle_settle(1)
        assert not ole32.uninitialize_entered.is_set()
        assert not handle._control_done.is_set()
    finally:
        release.set()
        assert handle.close(deadline()).status is BackendCloseStatus.CLOSED
    assert [event for event, _thread in ole32.events] == [
        "initialize", "construct", "open", "start", "stop", "blocked-close",
        "close", "uninitialize",
    ]
    assert len({owner for _event, owner in ole32.events}) == 1


def test_successful_close_waits_for_owner_com_cleanup(ole32):
    release = threading.Event()
    ole32.uninitialize_release = release
    handle = wasapi_handle(ole32)
    try:
        handle.start(deadline())
        result = handle.close(time.monotonic_ns() + 30_000_000)
        assert result.status is BackendCloseStatus.NOT_CONFIRMED
        assert ole32.uninitialize_entered.wait(1)
        assert not handle._control_done.is_set()
        assert not handle.wait_for_lifecycle_settle(0)
        assert handle.close(time.monotonic_ns() + 30_000_000).status is BackendCloseStatus.NOT_CONFIRMED
    finally:
        release.set()
        assert handle.close(deadline()).status is BackendCloseStatus.CLOSED
    assert handle._control_done.is_set()
    assert [event for event, _thread in ole32.events].count("uninitialize") == 1
