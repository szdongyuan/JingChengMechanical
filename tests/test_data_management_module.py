from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

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

    def count_records(self, _filters=None):
        return len(self.list_records(_filters))

    def list_records_page(self, filters=None, page=1, page_size=50):
        start = (page - 1) * page_size
        end = start + page_size
        return self.list_records(filters)[start:end]

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


def test_data_management_shows_statistics_report_button(qapp):
    services = SimpleNamespace(
        audio_data_service=_AudioDataServiceStub(),
        historical_metric_report_service=object(),
    )
    module = DataManagementModule(services)

    assert module.btn_statistics_report.text() == "统计报告"


def test_play_saved_wav_keeps_file_frame_zero_and_full_length(
    qapp,
    monkeypatch,
    tmp_path,
):
    source = np.asarray(
        [123.25, -8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0],
        dtype=np.float32,
    )
    wav_path = tmp_path / "canonical-session.wav"
    wavfile.write(wav_path, 100, source)
    services = SimpleNamespace(audio_data_service=_AudioDataServiceStub())
    module = DataManagementModule(services)
    played = []
    stopped = MagicMock()
    capture_window = MagicMock(
        side_effect=AssertionError("saved WAV playback must not trim again")
    )
    monkeypatch.setattr("ui.modules.data_management.sd.stop", stopped)
    monkeypatch.setattr(
        "ui.modules.data_management.sd.play",
        lambda data, sample_rate: played.append(
            (np.asarray(data).copy(), sample_rate)
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.test_session.SessionCaptureWindow",
        capture_window,
    )

    module._play_file(str(wav_path))

    stopped.assert_called_once_with()
    assert len(played) == 1
    played_data, played_sample_rate = played[0]
    assert played_sample_rate == 100
    assert played_data.shape == source.shape
    np.testing.assert_array_equal(played_data, source)
    assert played_data[0] == source[0]
    capture_window.assert_not_called()


def test_statistics_report_opens_without_selected_rows(qapp, monkeypatch):
    opened = []

    class FakeDialog:
        def __init__(self, services, parent=None):
            self.destroyed = SimpleNamespace(connect=lambda callback: None)
            opened.append((services, parent))

        def show(self):
            opened.append("show")

        def raise_(self):
            opened.append("raise")

        def activateWindow(self):
            opened.append("activate")

    monkeypatch.setattr(
        "ui.modules.data_management.HistoricalMetricReportDialog",
        FakeDialog,
    )
    services = SimpleNamespace(
        audio_data_service=_AudioDataServiceStub(),
        historical_metric_report_service=object(),
    )
    module = DataManagementModule(services)

    module.btn_statistics_report.click()

    assert opened[0][0] is services
    assert "show" in opened
