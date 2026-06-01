from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui.modules.today_statistics import TodayStatisticsModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _services():
    return SimpleNamespace(
        bus=SimpleNamespace(
            result_changed=_SignalStub(),
            test_finished=_SignalStub(),
            factory_display_reset=_SignalStub(),
        ),
        audio_data_service=SimpleNamespace(
            today_statistics=MagicMock(
                return_value={"total": 8, "ok": 6, "ng": 2},
            ),
        ),
    )


def test_today_statistics_uses_product_quality_labels(qapp):
    module = TodayStatisticsModule(_services())
    try:
        labels = {
            label.text()
            for label in module.findChildren(QLabel)
            if label.text()
        }

        assert "良品数：" in labels
        assert "不良品数：" in labels
        assert "OK数：" not in labels
        assert "NG数：" not in labels
        assert module.lb_ok.text() == "6"
        assert module.lb_ng.text() == "2"
    finally:
        module.close()
