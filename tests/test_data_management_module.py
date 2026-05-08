from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.modules.data_management import DataManagementModule


class _AudioDataServiceStub:
    def list_column_names(self):
        return [
            "audio_data_id",
            "file_path",
            "product_model",
            "sample_rate",
            "record_date",
            "labels",
            "label_source",
            "barcode",
            "analyse_config",
            "config_content",
        ]

    def list_records(self, _filters=None):
        return [
            {
                "audio_data_id": "a1",
                "file_path": "a.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:00:00+08:00",
                "labels": "OK",
                "label_source": "auto",
                "barcode": "SN-A",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a2",
                "file_path": "b.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:01:00+08:00",
                "labels": "NG",
                "label_source": "manual",
                "barcode": "SN-B",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a3",
                "file_path": "c.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:02:00+08:00",
                "labels": "",
                "label_source": "",
                "barcode": "SN-C",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a4",
                "file_path": "d.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:03:00+08:00",
                "labels": "",
                "label_source": None,
                "barcode": "SN-D",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a5",
                "file_path": "e.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:04:00+08:00",
                "labels": "OK",
                "label_source": "operator",
                "barcode": "SN-E",
                "analyse_config": "Config",
            },
        ]

    def list_product_models(self):
        return ["M"]

    def record_date_bounds(self):
        return None, None


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_data_management_displays_label_source_column(qapp):
    services = SimpleNamespace(audio_data_service=_AudioDataServiceStub())
    module = DataManagementModule(services)

    headers = [
        module.table.horizontalHeaderItem(index).text()
        for index in range(module.table.columnCount())
    ]

    assert "判定来源" in headers
    source_column = headers.index("判定来源")
    assert module.table.item(0, source_column).text() == "自动"
    assert module.table.item(1, source_column).text() == "人工"
    assert module.table.item(2, source_column).text() == ""
    assert module.table.item(3, source_column).text() == ""
    assert module.table.item(4, source_column).text() == "operator"
