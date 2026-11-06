from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.modules import video as video_module
from ui.modules.video import VideoModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.factory_display_reset = _SignalStub()
        self.log = _SignalStub()


class _FakeCameraThread:
    instances = []

    def __init__(self, camera_index=0):
        self.camera_index = camera_index
        self.frame_ready = _SignalStub()
        self.opened = _SignalStub()
        self.failed = _SignalStub()
        self.disconnected = _SignalStub()
        self.start_count = 0
        self.stop_count = 0
        self.wait_count = 0
        _FakeCameraThread.instances.append(self)

    def start(self):
        self.start_count += 1

    def stop(self):
        self.stop_count += 1

    def wait(self):
        self.wait_count += 1


class _FakeCv2:
    COLOR_BGR2RGB = 1

    @staticmethod
    def cvtColor(frame, code):
        assert code == _FakeCv2.COLOR_BGR2RGB
        return np.ascontiguousarray(frame[..., ::-1])


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def fake_video_dependencies(monkeypatch):
    _FakeCameraThread.instances = []
    monkeypatch.setattr(video_module, "CameraThread", _FakeCameraThread)
    monkeypatch.setattr(video_module, "cv2", _FakeCv2)
    return _FakeCameraThread


@pytest.mark.parametrize("reason", ["manual_judgment_ok", "manual_judgment_ng"])
def test_manual_judgment_display_reset_preserves_live_preview_and_camera(
    qapp,
    fake_video_dependencies,
    reason,
):
    services = SimpleNamespace(bus=_BusStub())
    module = VideoModule(services)
    module.lb.resize(320, 240)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[:, :, 0] = 255

    module.update_frame(frame)
    qapp.processEvents()

    assert module.lb.pixmap() is not None
    assert module.lb.pixmap().isNull() is False
    assert module.lb.text() == ""
    original_camera_thread = module.camera_thread
    assert original_camera_thread is fake_video_dependencies.instances[0]
    assert original_camera_thread.start_count == 1

    services.bus.factory_display_reset.emit(reason)
    qapp.processEvents()

    assert module.lb.pixmap() is not None
    assert module.lb.pixmap().isNull() is False
    assert module.lb.text() != "暂无画面"
    assert module.camera_thread is original_camera_thread
    assert original_camera_thread.stop_count == 0
    assert len(fake_video_dependencies.instances) == 1
    assert fake_video_dependencies.instances[0].start_count == 1

    module.deleteLater()
    qapp.processEvents()


def test_camera_preview_failure_remains_local_and_keeps_module_usable(
    qapp,
    fake_video_dependencies,
):
    services = SimpleNamespace(bus=_BusStub())
    module = VideoModule(services)
    camera = fake_video_dependencies.instances[0]

    camera.failed.emit("摄像头不可用")
    qapp.processEvents()

    assert "摄像头不可用" in module.lb.text()
    assert camera.stop_count == 0
    assert camera.start_count == 1
    assert services.bus.log.emissions

    module.deleteLater()
    qapp.processEvents()
