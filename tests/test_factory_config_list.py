from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

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
