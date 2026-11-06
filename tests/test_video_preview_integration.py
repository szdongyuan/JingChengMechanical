from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.modules.setdefault("cv2", SimpleNamespace())
if "snap7" not in sys.modules:
    snap7_module = ModuleType("snap7")
    snap7_client_module = ModuleType("snap7.client")
    snap7_error_module = ModuleType("snap7.error")
    snap7_util_module = ModuleType("snap7.util")

    class _Client:
        def connect(self, *_args, **_kwargs):
            return None

        def disconnect(self):
            return None

    snap7_client_module.Client = _Client
    snap7_error_module.S7Error = RuntimeError
    snap7_util_module.get_bool = lambda *_args, **_kwargs: False
    snap7_util_module.set_bool = lambda *_args, **_kwargs: None
    snap7_module.client = snap7_client_module
    snap7_module.util = snap7_util_module
    sys.modules["snap7"] = snap7_module
    sys.modules["snap7.client"] = snap7_client_module
    sys.modules["snap7.error"] = snap7_error_module
    sys.modules["snap7.util"] = snap7_util_module

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QStyle, QStyleOptionButton, QWidget,
    QStackedWidget, QHBoxLayout, QLabel, QVBoxLayout,
)

from ui.widgets.video_preview_overlay import VideoPreviewOverlay
from consts.style import APP_QSS
from ui.modules import video as video_module


class _SignalStub:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class _FakeCamera(QObject):
    frame_ready = pyqtSignal(object)
    opened = pyqtSignal()
    failed = pyqtSignal(str)
    disconnected = pyqtSignal(str)

    def __init__(self, camera_index=0):
        super().__init__()
        self.start_count = 0
        self.stop_count = 0
        self.wait_count = 0

    def start(self):
        self.start_count += 1

    def stop(self):
        self.stop_count += 1

    def wait(self):
        self.wait_count += 1


@pytest.fixture
def real_video_overlay(qapp, monkeypatch, tmp_path):
    cameras = []

    def make_camera(camera_index):
        camera = _FakeCamera(camera_index)
        cameras.append(camera)
        return camera

    monkeypatch.setattr(video_module, "CameraThread", make_camera)
    # Other test modules stub cv2 at collection time. Keep this boundary local
    # while exercising VideoModule's actual QImage/QPixmap rendering path.
    monkeypatch.setattr(video_module, "cv2", SimpleNamespace(
        COLOR_BGR2RGB=1,
        cvtColor=lambda frame, _code: np.ascontiguousarray(frame[..., ::-1]),
    ))
    settings = QSettings(str(tmp_path / "real-video.ini"), QSettings.Format.IniFormat)
    page = QWidget()
    page.setStyleSheet(APP_QSS)
    page.resize(1000, 700)
    anchor = QWidget(page)
    anchor.setGeometry(0, 430, 420, 270)
    video = video_module.VideoModule(SimpleNamespace(bus=SimpleNamespace(log=_SignalStub())))
    overlay = VideoPreviewOverlay(page, video, anchor, settings=settings)
    page.show()
    qapp.processEvents()
    overlay.sync_geometry()
    yield SimpleNamespace(
        page=page, anchor=anchor, video=video, overlay=overlay,
        cameras=cameras, settings=settings,
    )
    video.stop_camera()
    page.close()


class _WidgetStub(QWidget):
    def __init__(self, services=None, *_args) -> None:
        super().__init__()
        self.services = services


class _WorkOrderStub(_WidgetStub):
    pass


class _ControlStub(_WidgetStub):
    def __init__(self, services=None) -> None:
        super().__init__(services)
        self.btn_stop = QPushButton()
        self.restart_intent = _SignalStub()
        self.manual_judgment_widget = QWidget()
        self.manual_judgment_intent = _SignalStub()
        self.stop_intent = _SignalStub()

    def handle_manual_judgment_result(self, _result) -> None:
        return None

    def set_manual_judgment_target(self, _target) -> None:
        return None


class _TodayStatisticsStub(_WidgetStub):
    def on_committed_history_event(self, _event) -> None:
        return None


class _VideoStub(_WidgetStub):
    def stop_camera(self) -> None:
        return None


class _ResultDashboardStub(_WidgetStub):
    def __init__(self, services, _history_store, _manual_widget) -> None:
        super().__init__(services)
        self.manual_judgment_target_changed = _SignalStub()
        self.manual_judgment_requested = _SignalStub()
        self.reanalysis_requested = _SignalStub()
        self.reanalysis_cancel_requested = _SignalStub()

    def request_manual_judgment(self, _request) -> None:
        return None


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def close_test_windows(qapp):
    yield
    for widget in qapp.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


def test_test_page_uses_overlay_without_rearranging_layout(
    qapp,
    monkeypatch,
    tmp_path,
):
    import app.main_window as main_window

    settings = QSettings(
        str(tmp_path / "video-preview.ini"),
        QSettings.Format.IniFormat,
    )
    settings.clear()
    registered_shutdown_callbacks = []
    services = SimpleNamespace(
        result_history_store=object(),
        register_independent_shutdown_resource=(
            registered_shutdown_callbacks.append
        ),
        attach_committed_history_observer=lambda _observer: None,
        attach_result_dashboard=lambda _dashboard: object(),
    )

    monkeypatch.setattr(main_window, "WorkOrderModule", _WorkOrderStub)
    monkeypatch.setattr(main_window, "ControlModule", _ControlStub)
    monkeypatch.setattr(
        main_window,
        "TodayStatisticsModule",
        _TodayStatisticsStub,
    )
    monkeypatch.setattr(main_window, "VideoModule", _VideoStub)
    monkeypatch.setattr(
        main_window,
        "ResultDashboardModule",
        _ResultDashboardStub,
    )
    monkeypatch.setattr(
        main_window,
        "VideoPreviewOverlay",
        lambda parent, body, anchor: VideoPreviewOverlay(
            parent,
            body,
            anchor,
            settings=settings,
        ),
        raising=False,
    )

    window = main_window.MainWindow.__new__(main_window.MainWindow)
    QMainWindow.__init__(window)
    window.services = services
    window._restart_application = lambda: None
    window._wire_run_scoped_result_operations = lambda: None

    page = window.build_test_page()
    page.resize(1100, 720)
    page.show()
    qapp.processEvents()
    window.video_card.sync_geometry()

    assert window.video_placeholder.height() == 270
    assert window.video_placeholder.parentWidget() is not page
    assert window.video_card.parentWidget() is page
    assert window.video_card.body is window.video
    assert registered_shutdown_callbacks == [window.video.stop_camera]

    placeholder_top_left = window.video_placeholder.mapTo(
        page,
        window.video_placeholder.rect().topLeft(),
    )
    assert window.video_card.geometry() == QRect(
        placeholder_top_left,
        window.video_placeholder.size(),
    )

    left_column = window.video_placeholder.parentWidget()
    right_column = window.result_dashboard.parentWidget()
    before_placeholder = QRect(window.video_placeholder.geometry())
    before_left = QRect(left_column.geometry())
    before_right = QRect(right_column.geometry())

    window.video_card._commit_user_geometry(QRect(100, 50, 700, 500))
    qapp.processEvents()

    assert window.video_card.geometry() == QRect(100, 50, 700, 500)
    assert window.video_placeholder.geometry() == before_placeholder
    assert left_column.geometry() == before_left
    assert right_column.geometry() == before_right
    covered_right = window.video_card.geometry().intersected(QRect(
        right_column.mapTo(page, QPoint()), right_column.size()
    ))
    assert not covered_right.isEmpty()
    hit = page.childAt(covered_right.center())
    assert hit is window.video_card or window.video_card.isAncestorOf(hit)


@pytest.mark.parametrize("size", [(200, 80), (150, 70), (150, 100), (250, 70)])
@pytest.mark.parametrize("customized", [False, True])
def test_real_video_minimum_cannot_expand_overlay_beyond_page(
    qapp, real_video_overlay, size, customized
):
    scene = real_video_overlay
    requested = QRect(400, 200, 500, 400)
    if customized:
        scene.overlay._commit_user_geometry(requested)
    scene.page.resize(*size)
    qapp.processEvents()

    assert scene.page.size().width() == size[0]
    assert scene.page.size().height() == size[1]
    assert scene.overlay.geometry() == scene.page.rect()
    assert scene.video.lb.minimumHeight() == 220
    assert scene.video.lb.height() >= 220
    assert scene.overlay.requested_geometry == (requested if customized else None)

    scene.page.resize(1000, 700)
    qapp.processEvents()
    assert scene.overlay.geometry() == (requested if customized else scene.anchor.geometry())
    assert len(scene.cameras) == 1
    assert scene.cameras[0].start_count == 1
    assert scene.cameras[0].stop_count == scene.cameras[0].wait_count == 0


def test_styled_header_controls_remain_readable_and_operable_at_supported_minimum(
    qapp, real_video_overlay
):
    scene = real_video_overlay
    overlay = scene.overlay
    overlay._commit_user_geometry(QRect(400, 200, 500, 400))
    scene.page.resize(200, 80)
    qapp.processEvents()

    for control in (overlay.title_label, overlay.reset_button, overlay.resize_handle):
        rect = QRect(control.mapTo(scene.page, QPoint()), control.size())
        assert scene.page.rect().contains(rect)
        hit = scene.page.childAt(rect.center())
        expected = overlay.title_bar if control is overlay.title_label else control
        assert hit is expected
    title_bounds = overlay.title_label.fontMetrics().boundingRect(overlay.title_label.text())
    assert overlay.title_label.contentsRect().width() >= title_bounds.width()
    assert overlay.title_label.contentsRect().height() >= title_bounds.height()
    button = overlay.reset_button
    option = QStyleOptionButton()
    button.initStyleOption(option)
    text_rect = button.style().subElementRect(
        QStyle.SubElement.SE_PushButtonContents, option, button
    )
    text_bounds = button.fontMetrics().boundingRect(button.text())
    assert text_rect.width() >= text_bounds.width()
    assert text_rect.height() >= text_bounds.height()
    body_visible = scene.video.visibleRegion().boundingRect()
    if not body_visible.isEmpty():
        assert scene.video.mapTo(overlay, body_visible.topLeft()).y() > overlay.title_bar.geometry().bottom()

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert overlay.is_customized is False
    assert overlay.geometry() == scene.page.rect()
    scene.page.resize(1000, 700)
    qapp.processEvents()
    assert overlay.geometry() == scene.anchor.geometry()


@pytest.mark.parametrize("interaction", ["move", "resize"])
@pytest.mark.parametrize("customized", [False, True])
def test_real_video_frames_preserve_active_pointer_geometry_until_release(
    qapp, real_video_overlay, monkeypatch, interaction, customized
):
    scene = real_video_overlay
    overlay = scene.overlay
    layout = QHBoxLayout(scene.page)
    layout.setContentsMargins(20, 20, 20, 20)
    left = QWidget()
    column = QVBoxLayout(left)
    column.setContentsMargins(0, 0, 0, 0)
    column.addWidget(QLabel("Other page content"), 1)
    scene.anchor.setFixedHeight(270)
    column.addWidget(scene.anchor)
    layout.addWidget(left, 2)
    layout.addWidget(QWidget(), 3)
    scene.page.resize(1100, 720)
    for _ in range(4):
        qapp.processEvents()
    if customized:
        overlay._commit_user_geometry(QRect(100, 300, 450, 350))
    before = QRect(overlay.geometry())
    prior_request = overlay.requested_geometry
    prior_settings = {key: scene.settings.value(key) for key in scene.settings.allKeys()}
    saves = []
    save = overlay._save_custom_geometry

    def record_save():
        saves.append(overlay.requested_geometry)
        return save()

    monkeypatch.setattr(overlay, "_save_custom_geometry", record_save)
    target = overlay.title_bar if interaction == "move" else overlay.resize_handle
    origin = target.mapToGlobal(QPoint(9, 9))

    def send_pointer(kind, global_pos):
        release = kind == QEvent.Type.MouseButtonRelease
        event = QMouseEvent(
            kind, QPointF(target.mapFromGlobal(global_pos)), QPointF(global_pos),
            Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton if release else Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(target, event)

    send_pointer(QEvent.Type.MouseButtonPress, origin)
    for delta in (QPoint(150, -80), QPoint(180, -100)):
        send_pointer(QEvent.Type.MouseMove, origin + delta)
        expected = (
            before.translated(delta) if interaction == "move" else
            QRect(before.x(), before.y() + delta.y(),
                  before.width() + delta.x(), before.height() - delta.y())
        )
        assert overlay.geometry() == expected
        for _ in range(4):
            scene.video.update_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
            qapp.processEvents()
        assert overlay.geometry() == expected
        assert overlay.is_customized is customized
        assert overlay.requested_geometry == prior_request
        assert saves == []
        assert {key: scene.settings.value(key) for key in scene.settings.allKeys()} == prior_settings

    send_pointer(QEvent.Type.MouseButtonRelease, origin + delta)
    qapp.processEvents()
    assert overlay.geometry() == expected
    assert overlay.is_customized is True
    assert overlay.requested_geometry == expected
    assert saves == [expected]
    assert scene.settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == expected
    assert len(scene.cameras) == 1
    assert scene.cameras[0].start_count == 1
    assert scene.cameras[0].stop_count == scene.cameras[0].wait_count == 0


def test_real_video_resizes_frames_and_switches_pages_without_restarting_camera(
    qapp, real_video_overlay
):
    scene = real_video_overlay
    stack = QStackedWidget()
    scene.stack = stack  # Keep the page owner alive through fixture cleanup.
    stack.addWidget(scene.page)
    other_page = QWidget()
    stack.addWidget(other_page)
    stack.resize(1000, 700)
    stack.show()
    qapp.processEvents()
    camera = scene.cameras[0]
    frame = np.zeros((360, 640, 3), dtype=np.uint8)

    def deliver_frame():
        camera.frame_ready.emit(frame)
        pixmap = scene.video.lb.pixmap()
        assert pixmap is not None and not pixmap.isNull()
        assert pixmap.size() == QSize(640, 360).scaled(
            scene.video.lb.size(), Qt.AspectRatioMode.KeepAspectRatio
        )
        assert abs(pixmap.width() / pixmap.height() - 640 / 360) < 0.02
        return pixmap.size()

    before = deliver_frame()
    requested = QRect(300, 150, 600, 450)
    scene.overlay._commit_user_geometry(requested)
    qapp.processEvents()
    assert deliver_frame() != before

    stack.setCurrentWidget(other_page)
    assert not scene.overlay.isVisible()
    stack.resize(350, 280)
    qapp.processEvents()
    stack.setCurrentWidget(scene.page)
    qapp.processEvents()
    assert scene.overlay.isVisible()
    assert scene.page.rect().contains(scene.overlay.geometry())
    assert scene.overlay.requested_geometry == requested
    stack.resize(1000, 700)
    qapp.processEvents()
    assert scene.overlay.geometry() == requested
    deliver_frame()
    scene.overlay.reset_geometry()
    qapp.processEvents()
    assert scene.overlay.geometry() == scene.anchor.geometry()
    assert scene.video.camera_thread is camera
    assert scene.cameras == [camera]
    assert camera.start_count == 1
    assert camera.stop_count == camera.wait_count == 0


@pytest.mark.parametrize("signal", ["failed", "disconnected"])
def test_real_video_error_retry_and_stop_preserve_camera_lifecycle(
    qapp, real_video_overlay, signal
):
    scene = real_video_overlay
    original = scene.cameras[0]
    getattr(original, signal).emit("camera unavailable")
    qapp.processEvents()
    assert "camera unavailable" in scene.video.lb.text()
    assert 'href="retry"' in scene.video.lb.text()

    scene.overlay._commit_user_geometry(QRect(300, 150, 600, 450))
    scene.overlay.reset_geometry()
    assert scene.video.camera_thread is original
    assert original.start_count == 1
    assert original.stop_count == original.wait_count == 0

    scene.video.lb.linkActivated.emit("retry")
    qapp.processEvents()
    assert len(scene.cameras) == 2
    replacement = scene.cameras[1]
    assert scene.video.camera_thread is replacement
    assert original.stop_count == original.wait_count == 1
    assert replacement.start_count == 1
    assert replacement.stop_count == replacement.wait_count == 0
    replacement.frame_ready.emit(np.zeros((90, 160, 3), dtype=np.uint8))
    assert not scene.video.lb.pixmap().isNull()
    assert scene.video.lb.text() == ""

    scene.video.stop_camera()
    assert scene.video.camera_thread is None
    assert original.stop_count == original.wait_count == 1
    assert replacement.stop_count == replacement.wait_count == 1
    scene.video.stop_camera()
    assert replacement.stop_count == replacement.wait_count == 1
