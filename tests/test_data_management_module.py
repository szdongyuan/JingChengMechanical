from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

import ui.modules.data_management as data_management_mod
from ui.modules.data_management import DataManagementModule


class _AudioDataServiceStub:
    def __init__(self):
        self.deleted_ids: list[object] = []

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

    def delete_record(self, audio_data_id):
        self.deleted_ids.append(audio_data_id)


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
    assert headers[-2:] == ["操作", "删除"]
    source_column = headers.index("判定来源")
    assert module.table.item(0, source_column).text() == "自动"
    assert module.table.item(1, source_column).text() == "人工"
    assert module.table.item(2, source_column).text() == ""
    assert module.table.item(3, source_column).text() == ""
    assert module.table.item(4, source_column).text() == "operator"
    assert module.table.cellWidget(0, module.table.columnCount() - 1).objectName() == (
        "TableDeleteButton"
    )


def test_data_management_delete_button_calls_service_and_refreshes(qapp, monkeypatch):
    service = _AudioDataServiceStub()
    services = SimpleNamespace(audio_data_service=service)
    module = DataManagementModule(services)
    refresh_count = {"value": 0}

    monkeypatch.setattr(data_management_mod.sd, "stop", lambda: None)
    monkeypatch.setattr(
        data_management_mod.QMessageBox,
        "question",
        lambda *args, **kwargs: data_management_mod.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        module,
        "refresh_table",
        lambda: refresh_count.__setitem__("value", refresh_count["value"] + 1),
    )

    module._delete_record(module._records[0])

    assert service.deleted_ids == ["a1"]
    assert refresh_count["value"] == 1


def test_data_management_export_runs_on_background_thread(qapp, monkeypatch, tmp_path):
    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _ProgressDialog:
        instances = []

        def __init__(self, *args):
            self.args = args
            self.canceled = _Signal()
            self.values = []
            _ProgressDialog.instances.append(self)

        def setWindowTitle(self, title):
            self.title = title

        def setWindowModality(self, modality):
            self.modality = modality

        def setWindowFlag(self, flag, enabled):
            self.window_flag = (flag, enabled)

        def setValue(self, value):
            self.values.append(value)

        def setLabelText(self, text):
            self.label_text = text

        def setCancelButton(self, button):
            self.cancel_button = button

        def blockSignals(self, blocked):
            self.signals_blocked = blocked

        def close(self):
            self.closed = True

    class _ExportThread:
        instances = []

        def __init__(self, audio_data_service, target_path, records):
            self.audio_data_service = audio_data_service
            self.target_path = target_path
            self.records = records
            self.progress_changed = _Signal()
            self.succeeded = _Signal()
            self.failed = _Signal()
            self.canceled = _Signal()
            self.finished = _Signal()
            self.started = False
            _ExportThread.instances.append(self)

        def start(self):
            self.started = True

        def isRunning(self):
            return self.started

        def requestInterruption(self):
            self.interruption_requested = True

        def deleteLater(self):
            self.deleted = True

    services = SimpleNamespace(audio_data_service=_AudioDataServiceStub())
    module = DataManagementModule(services)
    target_path = tmp_path / "export.zip"
    monkeypatch.setattr(
        data_management_mod.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "Zip 文件 (*.zip)"),
    )
    monkeypatch.setattr(data_management_mod, "QProgressDialog", _ProgressDialog)
    monkeypatch.setattr(data_management_mod, "AudioDataExportThread", _ExportThread)
    module.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

    module._export_selected_rows()

    assert module.btn_export.isEnabled() is False
    assert len(_ProgressDialog.instances) == 1
    assert _ProgressDialog.instances[0].args[3] == 2
    assert len(_ExportThread.instances) == 1
    assert _ExportThread.instances[0].audio_data_service is services.audio_data_service
    assert _ExportThread.instances[0].target_path == str(target_path)
    assert _ExportThread.instances[0].records == [module._records[0]]
    assert _ExportThread.instances[0].started is True
