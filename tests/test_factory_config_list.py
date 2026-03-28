from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication, QListWidget

from ui.widgets.factory_config_list import FactoryConfigListWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _row(config_id: int, config_name: str) -> dict:
    return {
        "config_id": config_id,
        "config_name": config_name,
    }


class _DropEventStub:
    def __init__(self, position: QPointF):
        self._position = position
        self.accepted = False
        self.ignored = False
        self.drop_action = None

    def position(self) -> QPointF:
        return self._position

    def setDropAction(self, action) -> None:
        self.drop_action = action

    def accept(self) -> None:
        self.accepted = True

    def acceptProposedAction(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


def test_factory_config_list_keeps_checked_rows_first_and_appends_new_checks_to_end(qapp):
    widget = FactoryConfigListWidget()
    emissions: list[tuple[int, ...]] = []
    widget.enabled_order_changed.connect(lambda config_ids: emissions.append(config_ids))

    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B"), _row(303, "Config C")],
        enabled_config_ids=(202,),
    )

    widget.set_checked(101, True)

    assert widget.enabled_config_ids() == (202, 101)
    assert widget.visual_config_order() == (202, 101, 303)
    assert emissions[-1] == (202, 101)


def test_factory_config_list_drag_reorders_checked_priority_only(qapp):
    widget = FactoryConfigListWidget()
    emissions: list[tuple[int, ...]] = []
    widget.enabled_order_changed.connect(lambda config_ids: emissions.append(config_ids))

    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B"), _row(303, "Config C")],
        enabled_config_ids=(101, 202, 303),
    )

    widget.move_checked_config(303, before_config_id=101)

    assert widget.enabled_config_ids() == (303, 101, 202)
    assert widget.visual_config_order() == (303, 101, 202)
    assert emissions[-1] == (303, 101, 202)


def test_factory_config_list_ignores_unchecked_drag_reorder_attempts(qapp):
    widget = FactoryConfigListWidget()

    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B"), _row(303, "Config C")],
        enabled_config_ids=(101, 202),
    )

    widget.move_checked_config(303, before_config_id=101)

    assert widget.enabled_config_ids() == (101, 202)
    assert widget.visual_config_order() == (101, 202, 303)


def test_factory_config_list_visually_marks_top_checked_row_as_primary(qapp):
    widget = FactoryConfigListWidget()

    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B"), _row(303, "Config C")],
        enabled_config_ids=(202, 101),
    )

    assert widget.primary_config_id() == 202
    assert "Primary" in widget.primary_row_badge_text()


def test_factory_config_list_drop_event_preserves_all_checked_configs_when_moving_to_top(
    qapp,
    monkeypatch,
):
    widget = FactoryConfigListWidget()
    emissions: list[tuple[int, ...]] = []
    widget.enabled_order_changed.connect(lambda config_ids: emissions.append(config_ids))
    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B"), _row(303, "Config C")],
        enabled_config_ids=(101, 202, 303),
    )
    widget.resize(320, 200)
    widget.show()
    qapp.processEvents()
    widget.setCurrentItem(widget.item(2))

    def _fake_drop_event(self, event):
        model = self.model()
        mime = model.mimeData([model.index(2, 0)])
        model.dropMimeData(mime, self.defaultDropAction(), 0, 0, self.rootIndex())

    monkeypatch.setattr(QListWidget, "dropEvent", _fake_drop_event)

    first_row_rect = widget.visualItemRect(widget.item(0))
    drop_event = _DropEventStub(
        QPointF(first_row_rect.left() + 4, first_row_rect.top() + 4)
    )

    widget.dropEvent(drop_event)

    assert widget.enabled_config_ids() == (303, 101, 202)
    assert widget.visual_config_order() == (303, 101, 202)
    assert widget.primary_config_id() == 303
    assert widget.primary_row_badge_text() == "Primary"
    assert emissions[-1] == (303, 101, 202)
    assert drop_event.accepted is True
    assert drop_event.ignored is False
