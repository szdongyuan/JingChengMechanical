from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

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


class _FakeCameraThread:
    def __init__(self, *_args, **_kwargs):
        self.frame_ready = _SignalStub()
        self.opened = _SignalStub()
        self.failed = _SignalStub()
        self.disconnected = _SignalStub()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self):
        return True


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.mark.parametrize("reason", ["manual_judgment_ok", "selection_changed"])
def test_video_module_clears_pixmap_on_display_reset(qapp, monkeypatch, reason):
    monkeypatch.setattr("ui.modules.video.CameraThread", _FakeCameraThread)
    monkeypatch.setattr(
        "ui.modules.video.cv2",
        SimpleNamespace(COLOR_BGR2RGB=1, cvtColor=lambda frame, _code: frame),
    )
    bus = SimpleNamespace(factory_display_reset=_SignalStub(), log=_SignalStub())
    module = VideoModule(SimpleNamespace(bus=bus))
    camera_thread = module.camera_thread
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    module.update_frame(frame)

    assert module.lb.pixmap() is not None

    bus.factory_display_reset.emit(reason)

    assert module.lb.pixmap().isNull()
    assert module.lb.text() == "暂无画面"
    assert module.camera_thread is camera_thread
    assert camera_thread.started is True
    assert camera_thread.stopped is False
