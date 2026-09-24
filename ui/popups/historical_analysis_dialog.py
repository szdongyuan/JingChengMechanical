"""Independent, asynchronous viewer of a run's durable saved analysis."""
from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout,
)

from ui.widgets.historical_analysis_view import HistoricalAnalysisView
from ui.popups.run_audio_replay_dialog import RunAudioReplayControls
from model.malfunction_type_table import GOOD_MALFUNCTION_TYPE_NAME, BAD_MALFUNCTION_TYPE_NAME


# Worker lifetimes are independent of the dialog and of stale request delivery.
_ACTIVE_READERS = set()


def historical_playback(services):
    """Resolve hardware-independent playback, retaining older embedding fakes."""
    if hasattr(services, "historical_playback"):
        return services.historical_playback
    return getattr(getattr(services, "final_composition", None), "playback", None)


class _ReadWorker(QThread):
    completed = pyqtSignal(int, object, object)

    def __init__(self, generation, reader, run_id):
        super().__init__()
        self.generation, self.reader, self.run_id = generation, reader, run_id
        self.finished.connect(self._release)

    def run(self):
        try:
            snapshot = self.reader.read(self.run_id)
        except Exception as exc:
            self.completed.emit(self.generation, None, exc)
        else:
            self.completed.emit(self.generation, snapshot, None)

    @pyqtSlot()
    def _release(self):
        _ACTIVE_READERS.discard(self)
        self.deleteLater()


class HistoricalAnalysisDialog(QDialog):
    label_saved = pyqtSignal(str)

    def __init__(self, *, run_id, services, parent=None):
        super().__init__(parent)
        self._run_id = run_id
        self.services = services
        self.snapshot = None
        self.result_view = None
        self.replay_panel = None
        self._generation = 0
        self._closed = False
        self._pending_saved = False
        self._save_error = None
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setWindowTitle("历史分析结果")
        self.setModal(False)
        # Qt 6.10.2 on Windows adds maximize for an unrestricted maximum size,
        # even with custom flags. Stay one pixel below that limit to keep resizing.
        self.setMaximumSize(self.maximumWidth() - 1, self.maximumHeight() - 1)
        self.resize(1100, 820)
        root = QVBoxLayout(self)
        self.metadata_label = QLabel()
        self.metadata_label.setWordWrap(True)
        root.addWidget(self.metadata_label)
        self.current_label = QLabel()
        root.addWidget(self.current_label)
        self.status_label = QLabel("正在加载历史分析结果…")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        self.label_combo = QComboBox()
        self.label_combo.setMinimumWidth(160)
        self.label_combo.setEnabled(False)
        controls.addWidget(self.label_combo)
        self.save_label_button = QPushButton("保存标记")
        self.save_label_button.clicked.connect(self.save_label)
        self.save_label_button.setEnabled(False)
        controls.addWidget(self.save_label_button)
        controls.addStretch()
        root.addLayout(controls)
        self.audio_layout = QVBoxLayout()
        root.addLayout(self.audio_layout)
        self.original_label = QLabel("原始已保存分析结果")
        root.addWidget(self.original_label)
        self.result_message_label = QLabel()
        self.result_message_label.setWordWrap(True)
        root.addWidget(self.result_message_label)
        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        root.addWidget(self.result_scroll, 1)
        self.refresh()

    @property
    def run_id(self):
        return self._run_id

    def refresh_enrichment(self):
        self.refresh(_preserve_audio=True)

    def refresh(self, *, _save_error=None, _preserve_audio=False):
        if self._closed:
            return
        self._generation += 1
        if not _preserve_audio:
            self._clear_audio()
        self._save_error = _save_error
        self.status_label.setText("正在加载历史分析结果…")
        self.save_label_button.setEnabled(False)
        self.label_combo.setEnabled(False)
        worker = _ReadWorker(self._generation, self.services.historical_analysis_reader, self.run_id)
        _ACTIVE_READERS.add(worker)
        worker.completed.connect(self._read_completed, Qt.ConnectionType.QueuedConnection)
        worker.start()

    @pyqtSlot(int, object, object)
    def _read_completed(self, generation, snapshot, error):
        if self._closed or generation != self._generation:
            return
        if error is not None:
            self._clear_audio()
            self.snapshot = None
            self._clear_result()
            self.current_label.setText("当前记录不可用")
            self.status_label.setText(f"历史记录不可用：{error}")
            self._emit_saved()
            return
        previous_audio = self.snapshot.audio if self.snapshot is not None else None
        self.snapshot = snapshot
        run = snapshot.run
        workorder = snapshot.workorder
        self.metadata_label.setText(
            f"SN：{workorder.get('sn', workorder.get('sn_code', ''))}    "
            f"型号：{workorder.get('model', workorder.get('product_model', ''))}    "
            f"配置：{snapshot.config.get('name', snapshot.config.get('config_name', run.config_id))}\n"
            f"记录时间：{run.accepted_at}    记录编号：{run.run_id}")
        if run.ingress_kind == "IMPORTED":
            self.metadata_label.setText(self.metadata_label.text() + "    来源：导入 · 无电流数据")
        self.current_label.setText(f"当前标记：{run.judgment or '未判定'}    判定来源：{run.judgment_source}    故障类型：{run.fault_type or '—'}")
        self.original_label.setText(f"原始已保存分析结果 · 原始判定：{snapshot.original_analysis_judgment or '未保存'}")
        self.result_message_label.setText("\n".join(filter(None, [snapshot.result_message, snapshot.charts_message])))
        self._clear_result()
        self.result_view = HistoricalAnalysisView(snapshot) if snapshot.review is not None else None
        if self.result_view is not None:
            self.result_scroll.setWidget(self.result_view)
        if self.replay_panel is None or previous_audio != snapshot.audio:
            self._clear_audio()
            self.replay_panel = RunAudioReplayControls(record=snapshot.audio, playback=historical_playback(self.services),
                parent=self, unavailable_message=snapshot.audio_message)
            self.audio_layout.addWidget(self.replay_panel)
        self._populate_labels()
        editable = run.state != "ACTIVE"
        self.label_combo.setEnabled(editable)
        self.save_label_button.setEnabled(editable)
        self.status_label.setText(self._save_error or ("活动记录不能修改标记" if not editable else ""))
        self._emit_saved()

    def _clear_result(self):
        old = self.result_scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.result_view = None

    def _clear_audio(self):
        if self.replay_panel is not None:
            self.replay_panel.shutdown()
            self.audio_layout.removeWidget(self.replay_panel)
            self.replay_panel.hide()
            self.replay_panel.deleteLater()
            self.replay_panel = None

    def _populate_labels(self):
        self.label_combo.clear()
        self.label_combo.addItem("请选择", None)
        self.label_combo.addItem("合格（OK）", GOOD_MALFUNCTION_TYPE_NAME)
        seen = {GOOD_MALFUNCTION_TYPE_NAME}
        for name in self.snapshot.accepted_fault_types or (BAD_MALFUNCTION_TYPE_NAME,):
            if name not in seen:
                self.label_combo.addItem(name, name)
                seen.add(name)
        run = self.snapshot.run
        selected = (GOOD_MALFUNCTION_TYPE_NAME if run.judgment == "PASS" else
                    run.fault_type or (BAD_MALFUNCTION_TYPE_NAME if run.judgment == "FAIL" else None))
        self.label_combo.setCurrentIndex(max(0, self.label_combo.findData(selected)))

    def save_label(self):
        if self._closed or self.snapshot is None or not self.save_label_button.isEnabled():
            return
        label = self.label_combo.currentData()
        if not label:
            self.status_label.setText("请选择要保存的标记")
            return
        try:
            self.services.run_data_management_service.update_fault_type(self.run_id, str(label))
        except Exception as exc:
            self._populate_labels()
            message = f"标记保存失败：{exc}"
            self.refresh(_save_error=message)
            self.status_label.setText(message)
            return
        self._pending_saved = True
        self.refresh()

    def _emit_saved(self):
        if self._pending_saved:
            self._pending_saved = False
            self.label_saved.emit(self.run_id)

    def _shutdown(self):
        self._closed = True
        self._generation += 1
        self._clear_audio()
        # A committed save must still notify the list if its reload is interrupted.
        self._emit_saved()

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)

    def reject(self):
        self._shutdown()
        super().reject()

    def done(self, result):
        self._shutdown()
        super().done(result)

    def deleteLater(self):
        self._shutdown()
        super().deleteLater()
