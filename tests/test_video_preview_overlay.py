from __future__ import annotations

import os
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSettings, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from ui.widgets.video_preview_overlay import VideoPreviewOverlay


class ProgrammableSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []
        self.fail_next_value = False
        self.fail_next_set = False
        self.fail_set_keys = set()
        self.fail_next_remove = False
        self.fail_next_group_remove = False
        self.sync_outcomes = []
        self._status = QSettings.Status.NoError
        self._group = ""

    def value(self, key):
        self.calls.append(("value", key))
        if self.fail_next_value:
            self.fail_next_value = False
            raise RuntimeError("value failed")
        return self.values.get(key)

    def setValue(self, key, value):
        self.calls.append(("setValue", key, value))
        if self.fail_next_set or key in self.fail_set_keys:
            self.fail_next_set = False
            self.fail_set_keys.discard(key)
            raise RuntimeError("setValue failed")
        self.values[key] = value

    def remove(self, key):
        self.calls.append(("remove", self._group, key))
        is_group_clear = bool(self._group) and key == ""
        if (is_group_clear and self.fail_next_group_remove) or (
            not is_group_clear and self.fail_next_remove
        ):
            self.fail_next_group_remove = False
            self.fail_next_remove = False
            raise RuntimeError("remove failed")
        full_key = "/".join(part for part in (self._group, key) if part)
        if key == "":
            prefix = f"{self._group}/"
            for candidate in list(self.values):
                if candidate == self._group or candidate.startswith(prefix):
                    del self.values[candidate]
        else:
            self.values.pop(full_key, None)

    def beginGroup(self, group):
        self.calls.append(("beginGroup", group))
        self._group = group

    def endGroup(self):
        self.calls.append(("endGroup",))
        self._group = ""

    def sync(self):
        self.calls.append(("sync",))
        outcome = (
            self.sync_outcomes.pop(0)
            if self.sync_outcomes
            else QSettings.Status.NoError
        )
        if isinstance(outcome, Exception):
            raise outcome
        self._status = outcome

    def status(self):
        self.calls.append(("status",))
        return self._status


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


@pytest.fixture
def settings(tmp_path):
    value = QSettings(
        str(tmp_path / "video-preview.ini"), QSettings.Format.IniFormat
    )
    value.clear()
    return value


def _build_overlay(qapp, settings):
    page = QWidget()
    page.resize(1000, 700)
    anchor = QWidget(page)
    anchor.setGeometry(0, 430, 420, 270)
    body = QWidget()
    overlay = VideoPreviewOverlay(page, body, anchor, settings=settings)
    page.show()
    qapp.processEvents()
    overlay.sync_geometry()
    return page, anchor, body, overlay


def _drag(widget, start, end, qapp):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    qapp.processEvents()


def _is_overlay_or_descendant(widget, overlay):
    while widget is not None:
        if widget is overlay:
            return True
        widget = widget.parentWidget()
    return False


def test_default_geometry_follows_fixed_anchor(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)

    assert overlay.geometry() == QRect(0, 430, 420, 270)
    assert body.parentWidget() is overlay
    assert overlay.is_customized is False


def test_default_geometry_tracks_anchor_move_and_resize(qapp, settings):
    page, anchor, _, overlay = _build_overlay(qapp, settings)

    anchor.setGeometry(35, 300, 510, 285)
    overlay.sync_geometry()

    assert overlay.geometry() == QRect(35, 300, 510, 285)


def test_reset_control_uses_approved_label(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)

    assert overlay.reset_button.text() == "恢复默认"


def test_valid_custom_geometry_is_restored(qapp, settings):
    requested = QRect(480, 260, 400, 310)
    settings.setValue(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, True)
    settings.setValue(VideoPreviewOverlay.SETTINGS_GEOMETRY, requested)
    settings.sync()

    page, anchor, body, overlay = _build_overlay(qapp, settings)

    assert overlay.is_customized is True
    assert overlay.requested_geometry == requested
    assert overlay.geometry() == requested


def test_custom_geometry_survives_settings_process_restart(qapp, tmp_path):
    settings_path = str(tmp_path / "restart.ini")
    subprocess.run(
        [sys.executable, "-c", """
import sys
from PyQt6.QtCore import QRect, QSettings
settings = QSettings(sys.argv[1], QSettings.Format.IniFormat)
settings.setValue('ui/video_preview_overlay/customized', True)
settings.setValue('ui/video_preview_overlay/geometry', QRect(480, 260, 400, 310))
settings.sync()
assert settings.status() == QSettings.Status.NoError
""", settings_path],
        check=True,
    )
    settings = QSettings(settings_path, QSettings.Format.IniFormat)

    page, _, _, overlay = _build_overlay(qapp, settings)

    assert overlay.is_customized is True
    assert overlay.requested_geometry == QRect(480, 260, 400, 310)
    assert overlay.geometry() == QRect(480, 260, 400, 310)


@pytest.mark.parametrize(
    ("customized", "geometry"),
    [
        (None, None),
        (True, None),
        (None, QRect(20, 20, 400, 300)),
        ("yes", QRect(20, 20, 400, 300)),
        (True, "20,20,400,300"),
        (True, QRect()),
        (True, QRect(1, 2, 0, 20)),
        (True, QRect(1, 2, -10, 20)),
    ],
)
def test_invalid_or_partial_state_falls_back_and_clears_group(
    qapp, settings, customized, geometry
):
    if customized is not None:
        settings.setValue(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, customized)
    if geometry is not None:
        settings.setValue(VideoPreviewOverlay.SETTINGS_GEOMETRY, geometry)
    settings.sync()

    page, anchor, body, overlay = _build_overlay(qapp, settings)

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert overlay.geometry() == QRect(0, 430, 420, 270)
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_CUSTOMIZED)
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_GEOMETRY)


def test_persisted_default_tombstone_is_a_valid_default_state(qapp):
    fake = ProgrammableSettings(
        {
            VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False,
            VideoPreviewOverlay.SETTINGS_GEOMETRY: QRect(500, 200, 350, 300),
        }
    )

    page, anchor, body, overlay = _build_overlay(qapp, fake)

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert overlay.geometry() == QRect(0, 430, 420, 270)


def test_parent_clamping_does_not_replace_requested_geometry(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    requested = QRect(500, 300, 420, 300)
    overlay._commit_user_geometry(requested)

    page.resize(350, 280)
    overlay.sync_geometry()
    assert overlay.geometry() == QRect(0, 0, 350, 280)
    assert overlay.requested_geometry == requested

    page.resize(1000, 700)
    overlay.sync_geometry()
    assert overlay.geometry() == requested
    assert overlay.requested_geometry == requested


@pytest.mark.parametrize("small_size", [(150, 70), (150, 100), (250, 70)])
def test_extreme_parent_size_fills_parent_then_restores_request(
    qapp, settings, small_size
):
    page, _, _, overlay = _build_overlay(qapp, settings)
    requested = QRect(500, 300, 420, 300)
    overlay._commit_user_geometry(requested)

    page.resize(*small_size)
    overlay.sync_geometry()
    assert overlay.geometry() == page.rect()
    assert overlay.requested_geometry == requested

    page.resize(1000, 700)
    overlay.sync_geometry()
    assert overlay.geometry() == requested


def test_extreme_parent_size_restores_default_anchor_geometry(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)

    page.resize(150, 70)
    overlay.sync_geometry()
    assert overlay.geometry() == page.rect()

    page.resize(1000, 700)
    overlay.sync_geometry()
    assert overlay.geometry() == QRect(0, 430, 420, 270)


@pytest.mark.parametrize("reset", [False, True])
def test_small_page_default_uses_anchor_intersection_then_expands(
    qapp, settings, reset
):
    page, anchor, _, overlay = _build_overlay(qapp, settings)
    anchor.setGeometry(30, 50, 420, 270)
    if reset:
        overlay._commit_user_geometry(QRect(500, 200, 350, 300))
    page.resize(250, 170)
    if reset:
        overlay.reset_geometry()
    qapp.processEvents()

    assert overlay.geometry() == QRect(30, 50, 220, 120)
    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_GEOMETRY)

    page.resize(1000, 700)
    qapp.processEvents()
    assert overlay.geometry() == anchor.geometry()


@pytest.mark.parametrize("anchor_rect", [
    QRect(0, 430, 420, 270),  # Entirely outside the shrunken page.
    QRect(180, 60, 420, 270),  # Intersection cannot contain title controls.
])
def test_small_page_unusable_anchor_keeps_header_operable(
    qapp, settings, anchor_rect
):
    page, anchor, _, overlay = _build_overlay(qapp, settings)
    anchor.setGeometry(anchor_rect)
    page.resize(200, 80)
    overlay.reset_geometry()
    qapp.processEvents()

    assert page.rect().contains(overlay.geometry())
    for control in (overlay.title_bar, overlay.reset_button, overlay.resize_handle):
        rect = QRect(control.mapTo(page, QPoint()), control.size())
        assert page.rect().contains(rect)
        assert _is_overlay_or_descendant(page.childAt(rect.center()), overlay)
    assert overlay.is_customized is False

    page.resize(1000, 700)
    qapp.processEvents()
    assert overlay.geometry() == anchor_rect


def test_reset_immediately_follows_anchor_and_persists_default(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(500, 200, 350, 300))
    anchor.setGeometry(30, 390, 440, 270)

    overlay.reset_geometry()

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert overlay.geometry() == QRect(30, 390, 440, 270)
    assert (
        settings.value(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, type=bool)
        is False
    )
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_GEOMETRY)


@pytest.mark.parametrize("failure_kind", ["value", "sync_exception", "status"])
def test_settings_read_failure_falls_back_to_usable_default(qapp, failure_kind):
    fake = ProgrammableSettings(
        {
            VideoPreviewOverlay.SETTINGS_CUSTOMIZED: True,
            VideoPreviewOverlay.SETTINGS_GEOMETRY: QRect(500, 200, 350, 300),
        }
    )
    if failure_kind == "value":
        fake.fail_next_value = True
    elif failure_kind == "sync_exception":
        fake.sync_outcomes.append(RuntimeError("sync failed"))
    else:
        fake.sync_outcomes.append(QSettings.Status.AccessError)

    page, anchor, body, overlay = _build_overlay(qapp, fake)

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert overlay.geometry() == QRect(0, 430, 420, 270)


@pytest.mark.parametrize("failure_kind", ["set", "sync_exception", "status"])
def test_custom_save_failure_does_not_disable_later_session_geometry_changes(
    qapp, failure_kind
):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, anchor, body, overlay = _build_overlay(qapp, fake)
    if failure_kind == "set":
        fake.fail_next_set = True
    elif failure_kind == "sync_exception":
        fake.sync_outcomes.append(RuntimeError("sync failed"))
    else:
        fake.sync_outcomes.append(QSettings.Status.AccessError)

    first = QRect(400, 240, 350, 300)
    overlay._commit_user_geometry(first)
    assert overlay.requested_geometry == first

    second = QRect(300, 180, 360, 310)
    overlay._commit_user_geometry(second)

    assert overlay.is_customized is True
    assert overlay.requested_geometry == second
    assert overlay.geometry() == second


def test_geometry_write_failure_cannot_activate_stale_geometry_on_restart(qapp):
    stale = QRect(500, 200, 350, 300)
    fake = ProgrammableSettings(
        {
            VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False,
            VideoPreviewOverlay.SETTINGS_GEOMETRY: stale,
        }
    )
    page, anchor, body, overlay = _build_overlay(qapp, fake)
    fake.fail_set_keys.add(VideoPreviewOverlay.SETTINGS_GEOMETRY)

    requested = QRect(300, 180, 360, 310)
    overlay._commit_user_geometry(requested)
    restart_page, restart_anchor, restart_body, restarted = _build_overlay(
        qapp, fake
    )

    assert overlay.requested_geometry == requested
    assert restarted.is_customized is False
    assert restarted.requested_geometry is None
    assert restarted.geometry() == QRect(0, 430, 420, 270)


def test_invalid_state_cleanup_failure_does_not_break_construction(qapp):
    fake = ProgrammableSettings(
        {
            VideoPreviewOverlay.SETTINGS_CUSTOMIZED: "bad",
            VideoPreviewOverlay.SETTINGS_GEOMETRY: "bad",
        }
    )
    fake.fail_next_group_remove = True

    page, anchor, body, overlay = _build_overlay(qapp, fake)

    assert overlay.is_customized is False
    assert overlay.geometry() == QRect(0, 430, 420, 270)


def test_reset_write_failure_keeps_current_session_in_default_mode(qapp):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, anchor, body, overlay = _build_overlay(qapp, fake)
    overlay._commit_user_geometry(QRect(500, 200, 350, 300))
    fake.fail_next_set = True

    overlay.reset_geometry()

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert overlay.geometry() == QRect(0, 430, 420, 270)


@pytest.mark.parametrize("cleanup_failure", ["remove", "status"])
def test_reset_tombstone_prevents_restore_when_geometry_cleanup_fails(
    qapp, cleanup_failure
):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, anchor, body, overlay = _build_overlay(qapp, fake)
    stale = QRect(500, 200, 350, 300)
    overlay._commit_user_geometry(stale)
    if cleanup_failure == "remove":
        fake.fail_next_remove = True
    else:
        fake.sync_outcomes.extend(
            [QSettings.Status.NoError, QSettings.Status.AccessError]
        )

    overlay.reset_geometry()
    restart_page, restart_anchor, restart_body, restarted = _build_overlay(
        qapp, fake
    )

    assert fake.values[VideoPreviewOverlay.SETTINGS_CUSTOMIZED] is False
    assert restarted.is_customized is False
    assert restarted.geometry() == QRect(0, 430, 420, 270)


def test_successful_reset_removes_geometry_and_survives_restart(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(500, 200, 350, 300))

    overlay.reset_geometry()
    restart_page, restart_anchor, restart_body, restarted = _build_overlay(
        qapp, settings
    )

    assert settings.value(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, type=bool) is False
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_GEOMETRY)
    assert restarted.is_customized is False


def test_anchor_event_coalesces_geometry_synchronization(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)

    anchor.setGeometry(45, 320, 500, 280)
    qapp.processEvents()

    assert overlay.geometry() == QRect(45, 320, 500, 280)


def test_destroying_anchor_immediately_hides_default_overlay(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)

    anchor.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()

    assert overlay.isHidden()
    assert overlay.geometry().isEmpty()

    overlay.sync_geometry()
    assert overlay.isHidden()
    assert overlay.geometry().isEmpty()


def test_destroying_anchor_preserves_valid_custom_geometry(qapp, settings):
    page, anchor, body, overlay = _build_overlay(qapp, settings)
    requested = QRect(300, 180, 360, 310)
    overlay._commit_user_geometry(requested)

    anchor.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()

    assert overlay.isVisible()
    assert overlay.geometry() == requested
    assert overlay.requested_geometry == requested


def test_hidden_parent_resize_synchronizes_when_shown(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    requested = QRect(500, 300, 420, 300)
    overlay._commit_user_geometry(requested)

    page.hide()
    page.resize(350, 280)
    page.show()
    qapp.processEvents()

    assert overlay.geometry() == QRect(0, 0, 350, 280)
    assert overlay.requested_geometry == requested


def test_dragging_title_moves_and_persists_overlay(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    start = overlay.pos()

    _drag(overlay.title_bar, QPoint(80, 12), QPoint(150, 52), qapp)

    assert overlay.pos() != start
    assert overlay.is_customized is True
    assert overlay.requested_geometry == overlay.geometry()
    assert (
        settings.value(VideoPreviewOverlay.SETTINGS_CUSTOMIZED, type=bool)
        is True
    )
    assert settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == overlay.geometry()


@pytest.mark.parametrize(
    "end",
    [QPoint(-900, 12), QPoint(1400, 12), QPoint(80, -900), QPoint(80, 900)],
)
def test_dragging_title_keeps_full_overlay_inside_page(
    qapp, settings, end
):
    page, _, _, overlay = _build_overlay(qapp, settings)

    _drag(overlay.title_bar, QPoint(80, 12), end, qapp)

    assert page.rect().contains(overlay.geometry())


def test_header_controls_do_not_begin_title_movement_and_reset_uses_anchor(
    qapp, settings
):
    page, anchor, _, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(450, 240, 360, 300))
    before_button_drag = QRect(overlay.geometry())

    _drag(overlay.reset_button, QPoint(5, 5), QPoint(-100, -100), qapp)

    assert overlay.geometry() == before_button_drag
    assert overlay.is_customized is True

    before_resize = QRect(overlay.geometry())
    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(29, 29), qapp)
    assert overlay.geometry().left() == before_resize.left()
    assert overlay.geometry().bottom() == before_resize.bottom()

    anchor.setGeometry(25, 410, 440, 270)
    QTest.mouseClick(overlay.reset_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert overlay.geometry() == QRect(25, 410, 440, 270)
    assert overlay.is_customized is False
    assert overlay.requested_geometry is None


def test_top_right_resize_keeps_left_and_bottom_fixed(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(200, 250, 420, 300))
    before = QRect(overlay.geometry())

    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(129, -71), qapp)

    after = overlay.geometry()
    assert after.left() == before.left()
    assert after.bottom() == before.bottom()
    assert after.width() == before.width() + 120
    assert after.height() == before.height() + 80
    assert overlay.requested_geometry == after
    assert settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == after


def test_top_right_resize_from_small_default_keeps_fixed_edges_on_release(
    qapp, settings
):
    page, anchor, _, overlay = _build_overlay(qapp, settings)
    anchor.setGeometry(30, 50, 420, 270)
    page.resize(250, 170)
    qapp.processEvents()
    before = QRect(overlay.geometry())
    assert before == QRect(30, 50, 220, 120)

    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(19, -11), qapp)

    assert overlay.geometry().left() == before.left()
    assert overlay.geometry().bottom() == before.bottom()
    assert page.rect().contains(overlay.geometry())
    assert overlay.requested_geometry == overlay.geometry()


@pytest.mark.parametrize(
    "page_size, anchor_rect, before, delta, expected, expanded",
    [
        ((400, 170), QRect(150, 50, 420, 270), QRect(150, 50, 250, 120),
         QPoint(0, -50), QRect(150, 0, 250, 170), QRect(150, 0, 320, 270)),
        ((250, 400), QRect(-200, -100, 420, 270), QRect(0, 0, 220, 170),
         QPoint(20, -50), QRect(0, 0, 250, 170), QRect(0, 0, 320, 270)),
        ((400, 170), QRect(220, 50, 420, 270), QRect(220, 50, 180, 120),
         QPoint(0, -50), QRect(220, 0, 180, 170), QRect(220, 0, 320, 270)),
        ((250, 400), QRect(-200, -210, 420, 270), QRect(0, 0, 220, 60),
         QPoint(20, -50), QRect(0, 0, 250, 60), QRect(0, 0, 320, 270)),
    ],
    ids=["fixed-left", "fixed-bottom", "narrow-header", "short-header"],
)
def test_clipped_default_resize_preserves_fixed_edges_through_restart(
    qapp, settings, page_size, anchor_rect, before, delta, expected, expanded
):
    page, anchor, _, overlay = _build_overlay(qapp, settings)
    anchor.setGeometry(anchor_rect)
    page.resize(*page_size)
    qapp.processEvents()
    assert overlay.geometry() == before

    handle = overlay.resize_handle
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=QPoint(9, 9))
    QTest.mouseMove(handle, QPoint(9, 9) + delta)
    assert overlay.geometry() == expected
    overlay.sync_geometry()
    qapp.processEvents()
    assert overlay.geometry() == expected
    assert overlay.requested_geometry is None
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=QPoint(9, 9) + delta)
    assert overlay.geometry() == expected
    for _ in range(3):
        overlay.sync_geometry()
        qapp.processEvents()
        assert overlay.geometry() == expected
    assert expected.left() == before.left()
    assert expected.bottom() == before.bottom()
    assert overlay.requested_geometry == expected
    assert settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == expected

    # Load the actual saved INI in a new process on the same size page, then
    # expand and shrink: normalization must never replace the saved request.
    subprocess.run(
        [sys.executable, "-c", """
import sys
from PyQt6.QtCore import QRect, QSettings
from PyQt6.QtWidgets import QApplication, QWidget
from ui.widgets.video_preview_overlay import VideoPreviewOverlay
app = QApplication([])
settings = QSettings(sys.argv[1], QSettings.Format.IniFormat)
values = list(map(int, sys.argv[2:]))
page = QWidget()
page.resize(*values[:2])
expected = QRect(*values[2:6])
expanded = QRect(*values[6:10])
anchor = QWidget(page)
anchor.setGeometry(0, 430, 420, 270)
overlay = VideoPreviewOverlay(page, QWidget(), anchor, settings=settings)
page.show()
app.processEvents()
assert overlay.is_customized
assert overlay.geometry() == expected, (overlay.geometry(), expected)
page.resize(1000, 700)
app.processEvents()
assert overlay.geometry() == expanded, (overlay.geometry(), expanded)
page.resize(*values[:2])
app.processEvents()
assert overlay.geometry() == expected
assert overlay.requested_geometry == expected
assert settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == expected
""", settings.fileName(), *map(str, (*page_size, *expected.getRect(), *expanded.getRect()))],
        check=True,
    )


@pytest.mark.parametrize("interaction", ["move", "resize"])
def test_active_pointer_sync_applies_parent_bounds_without_saving(qapp, settings, interaction):
    page, _, _, overlay = _build_overlay(qapp, settings)
    target = overlay.title_bar if interaction == "move" else overlay.resize_handle
    QTest.mousePress(target, Qt.MouseButton.LeftButton, pos=QPoint(9, 9))
    QTest.mouseMove(target, QPoint(159, -71))
    active = QRect(overlay.geometry())

    page.resize(600, 500)
    qapp.processEvents()
    expected = QRect(active)
    expected.moveBottom(499)
    assert overlay.geometry() == expected
    assert page.rect().contains(overlay.geometry())
    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert not settings.contains(VideoPreviewOverlay.SETTINGS_GEOMETRY)

    QTest.mouseRelease(target, Qt.MouseButton.LeftButton, pos=QPoint(159, -71))
    qapp.processEvents()
    assert overlay.requested_geometry == expected
    assert settings.value(VideoPreviewOverlay.SETTINGS_GEOMETRY) == expected


def test_small_custom_request_keeps_header_minimum_on_supported_page(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    page.resize(250, 170)

    overlay._commit_user_geometry(QRect(0, 0, 10, 10))
    qapp.processEvents()

    assert overlay.width() >= overlay.SUPPORTED_PAGE_MINIMUM.width()
    assert overlay.height() >= overlay.SUPPORTED_PAGE_MINIMUM.height()
    assert page.rect().contains(overlay.geometry())


def test_top_right_resize_enforces_normal_minimum(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(200, 250, 420, 300))
    before = QRect(overlay.geometry())

    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(-991, 1009), qapp)

    after = overlay.geometry()
    assert after.size() == VideoPreviewOverlay.NORMAL_MINIMUM
    assert after.left() == before.left()
    assert after.bottom() == before.bottom()


def test_top_right_resize_caps_right_and_top_at_parent_edges(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(200, 250, 420, 300))
    before = QRect(overlay.geometry())

    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(2000, -2000), qapp)

    after = overlay.geometry()
    assert after.left() == before.left()
    assert after.bottom() == before.bottom()
    assert after.right() == page.rect().right()
    assert after.top() == page.rect().top()


def test_resize_while_parent_clamped_replaces_old_request(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    old_request = QRect(500, 300, 420, 300)
    overlay._commit_user_geometry(old_request)
    page.resize(350, 280)
    overlay.sync_geometry()
    assert overlay.geometry() == QRect(0, 0, 350, 280)

    _drag(overlay.resize_handle, QPoint(9, 9), QPoint(-1, 19), qapp)

    assert overlay.requested_geometry == overlay.geometry()
    assert overlay.requested_geometry == QRect(0, 10, 340, 270)
    assert overlay.requested_geometry != old_request
    page.resize(1000, 700)
    overlay.sync_geometry()
    assert overlay.geometry() == overlay.requested_geometry


def test_first_show_raises_overlay_above_later_sibling(qapp, settings):
    page = QWidget()
    page.resize(1000, 700)
    anchor = QWidget(page)
    anchor.setGeometry(0, 430, 420, 270)
    overlay = VideoPreviewOverlay(
        page, QWidget(), anchor, settings=settings
    )
    cover = QWidget(page)
    cover.setGeometry(anchor.geometry())
    cover.show()
    cover.raise_()

    page.show()
    qapp.processEvents()

    probe = overlay.geometry().center()
    assert _is_overlay_or_descendant(page.childAt(probe), overlay)


@pytest.mark.parametrize(
    "trigger", ["show", "sync", "resize", "move_press", "resize_press"]
)
def test_overlay_is_raised_after_lifecycle_and_interaction_triggers(
    qapp, settings, trigger
):
    page, _, _, overlay = _build_overlay(qapp, settings)
    cover = QWidget(page)
    cover.setGeometry(overlay.geometry())
    cover.show()
    cover.raise_()
    probe = overlay.geometry().center()
    assert not _is_overlay_or_descendant(page.childAt(probe), overlay)

    if trigger == "show":
        overlay.hide()
        overlay.show()
    elif trigger == "sync":
        overlay.sync_geometry()
    elif trigger == "resize":
        page.resize(1001, 701)
        qapp.processEvents()
    elif trigger == "move_press":
        QTest.mousePress(
            overlay.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(80, 12)
        )
    else:
        QTest.mousePress(
            overlay.resize_handle, Qt.MouseButton.LeftButton, pos=QPoint(9, 9)
        )
    qapp.processEvents()

    assert _is_overlay_or_descendant(page.childAt(probe), overlay)

    if trigger == "move_press":
        QTest.mouseRelease(
            overlay.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(80, 12)
        )
    elif trigger == "resize_press":
        QTest.mouseRelease(
            overlay.resize_handle, Qt.MouseButton.LeftButton, pos=QPoint(9, 9)
        )


def test_hidden_page_resize_show_resynchronizes_and_raises(qapp, settings):
    page, _, _, overlay = _build_overlay(qapp, settings)
    overlay._commit_user_geometry(QRect(500, 300, 420, 300))
    page.hide()
    page.resize(350, 280)
    cover = QWidget(page)
    cover.setGeometry(page.rect())
    cover.show()
    cover.raise_()

    page.show()
    qapp.processEvents()

    assert overlay.geometry() == QRect(0, 0, 350, 280)
    assert _is_overlay_or_descendant(page.childAt(QPoint(175, 140)), overlay)


@pytest.mark.parametrize("interaction", ["move", "resize"])
def test_pointer_interaction_saves_exactly_once_on_release(qapp, interaction):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, _, _, overlay = _build_overlay(qapp, fake)
    target = overlay.title_bar if interaction == "move" else overlay.resize_handle
    start = QPoint(80, 12) if interaction == "move" else QPoint(9, 9)
    fake.calls.clear()

    QTest.mousePress(target, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(target, start + QPoint(10, -5))
    QTest.mouseMove(target, start + QPoint(20, -10))

    assert fake.calls == []

    QTest.mouseRelease(
        target, Qt.MouseButton.LeftButton, pos=start + QPoint(20, -10)
    )
    qapp.processEvents()

    writes = [call for call in fake.calls if call[0] == "setValue"]
    assert [call[1] for call in writes] == [
        VideoPreviewOverlay.SETTINGS_GEOMETRY,
        VideoPreviewOverlay.SETTINGS_CUSTOMIZED,
    ]
    assert sum(call[0] == "sync" for call in fake.calls) == 1


def test_save_failure_does_not_disable_second_pointer_interaction(qapp):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, _, _, overlay = _build_overlay(qapp, fake)
    fake.fail_next_set = True

    _drag(overlay.title_bar, QPoint(80, 12), QPoint(120, 32), qapp)
    after_failed_save = QRect(overlay.geometry())
    _drag(overlay.title_bar, QPoint(80, 12), QPoint(110, 22), qapp)

    assert overlay.geometry() != after_failed_save
    assert overlay.requested_geometry == overlay.geometry()


@pytest.mark.parametrize("interaction", ["move", "resize"])
def test_no_motion_pointer_click_preserves_default_without_saving(
    qapp, interaction
):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, anchor, _, overlay = _build_overlay(qapp, fake)
    target = overlay.title_bar if interaction == "move" else overlay.resize_handle
    click_pos = QPoint(80, 12) if interaction == "move" else QPoint(9, 9)
    fake.calls.clear()

    QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=click_pos)
    qapp.processEvents()

    assert overlay.is_customized is False
    assert overlay.requested_geometry is None
    assert fake.calls == []

    anchor.setGeometry(30, 390, 440, 270)
    qapp.processEvents()
    assert overlay.geometry() == QRect(30, 390, 440, 270)


@pytest.mark.parametrize("interaction", ["move", "resize"])
def test_no_motion_pointer_click_preserves_custom_state_without_saving(
    qapp, interaction
):
    fake = ProgrammableSettings(
        {VideoPreviewOverlay.SETTINGS_CUSTOMIZED: False}
    )
    page, _, _, overlay = _build_overlay(qapp, fake)
    requested = QRect(400, 240, 350, 300)
    overlay._commit_user_geometry(requested)
    target = overlay.title_bar if interaction == "move" else overlay.resize_handle
    click_pos = QPoint(80, 12) if interaction == "move" else QPoint(9, 9)
    fake.calls.clear()

    QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=click_pos)
    qapp.processEvents()

    assert overlay.is_customized is True
    assert overlay.requested_geometry == requested
    assert overlay.geometry() == requested
    assert fake.calls == []


def test_default_height_fits_body_minimum_and_vertical_margins(qapp, settings):
    page = QWidget()
    page.resize(1000, 700)
    anchor = QWidget(page)
    anchor.setGeometry(0, 430, 420, 270)
    body = QWidget()
    body.setMinimumHeight(220)
    overlay = VideoPreviewOverlay(page, body, anchor, settings=settings)
    page.show()
    qapp.processEvents()
    overlay.sync_geometry()

    contents = overlay.contentsRect()
    assert overlay.height() == 270
    assert overlay.minimumSizeHint().height() <= 270
    assert body.height() >= 220
    assert overlay.title_bar.geometry().top() - contents.top() >= 8
    assert contents.bottom() - body.geometry().bottom() >= 8
