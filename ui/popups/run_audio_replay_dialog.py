from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from scipy.io import wavfile

from app.service.analysis import dsp
from app.service.run_scoped_playback import (
    PlaybackCode,
    PlaybackLifecycleCode,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)


@dataclass(frozen=True)
class ReplayPlotData:
    wave_times: np.ndarray
    wave_values: np.ndarray
    selection: tuple[float, float]
    magnitude_db: np.ndarray | None
    max_frequency: float


@dataclass(frozen=True)
class PreparedReplayAudio:
    sample_rate: float
    samples: np.ndarray
    plots: ReplayPlotData


def prepare_replay_plots(samples, sample_rate, columns, selection) -> ReplayPlotData:
    """Pure audio audition preparation; no saved analysis or GUI state is touched."""
    mono = np.mean(samples[:, columns or (0,)], axis=1, dtype=np.float32)
    step = max(1, math.ceil(mono.size / 6000))
    indexes = np.arange(0, mono.size, step)
    start, end = selection
    segment = mono[max(0, int(start * sample_rate)):min(mono.size, int(end * sample_rate))]
    magnitude_db, max_frequency = None, 0.0
    if segment.size >= 2:
        times, frequencies, magnitude = dsp.compute_stft(
            segment, sample_rate, min(1024, int(segment.size)), 0.5,
        )
        if times.size and frequencies.size:
            magnitude_db, max_frequency = magnitude, float(frequencies[-1])
    return ReplayPlotData(indexes / sample_rate, mono[indexes], selection, magnitude_db, max_frequency)


def prepare_replay_audio(path=None, *, preloaded_audio=None) -> PreparedReplayAudio:
    """Load/normalize audio and prepare its initial channel/range off the GUI thread.

    preloaded_audio is (sample_rate, normalized float samples), in stored-column order.
    """
    if preloaded_audio is None:
        sample_rate, samples = RunAudioReplayPanel._load_audio(path)
    else:
        sample_rate, samples = preloaded_audio
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        if samples.ndim != 2 or not samples.shape[0] or not samples.shape[1] or sample_rate <= 0:
            raise ValueError("音频数据为空")
    duration = samples.shape[0] / sample_rate
    return PreparedReplayAudio(float(sample_rate), samples, prepare_replay_plots(samples, sample_rate, (0,), (0.0, duration)))


# Strong references outlive the host widget. Workers never capture widget methods.
_ACTIVE_WORKERS: set[QThread] = set()


class _ReplayWorker(QThread):
    completed = pyqtSignal(str, int, object, object)

    def __init__(self, kind, generation, work):
        super().__init__()
        self.kind, self.generation, self.work = kind, generation, work
        self.finished.connect(self._release)

    def run(self):
        try:
            value = self.work()
        except Exception as exc:
            self.completed.emit(self.kind, self.generation, None, exc)
        else:
            self.completed.emit(self.kind, self.generation, value, None)

    @pyqtSlot()
    def _release(self):
        _ACTIVE_WORKERS.discard(self)
        self.deleteLater()


def _start_worker(kind, generation, work, receiver=None):
    worker = _ReplayWorker(kind, generation, work)
    _ACTIVE_WORKERS.add(worker)
    if receiver is not None:
        worker.completed.connect(receiver, Qt.ConnectionType.QueuedConnection)
    worker.start()


class _RangeSelectionViewBox(pg.ViewBox):
    """Left-drag creates a new time range; the view itself never pans."""

    range_dragging = pyqtSignal(float, float)
    range_finished = pyqtSignal(float, float)

    def mouseDragEvent(self, event, axis=None) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        event.accept()
        start = float(
            self.mapSceneToView(event.buttonDownScenePos()).x()
        )
        current = float(self.mapSceneToView(event.scenePos()).x())
        lower, upper = sorted((start, current))
        self.range_dragging.emit(lower, upper)
        if event.isFinish():
            self.range_finished.emit(lower, upper)


class RunAudioReplayPanel(QWidget):
    """Embeddable audition controls. Hosts must call shutdown before replacing/closing.

    Loading, plot preparation and playback operations are asynchronous by default.
    prepared_audio may be produced by prepare_replay_audio in a host's worker.
    """

    def __init__(self, *, record, playback, parent=None, preloaded_audio=None,
                 prepared_audio=None, _synchronous=False) -> None:
        super().__init__(parent)
        self._record = record
        self._playback = playback
        self._synchronous = _synchronous
        self._owner = object()
        self._closed = threading.Event()
        self._plot_generation = 0
        self._ready = False
        self._operation_pending = False
        self._applied_plot_range = None
        self._sample_rate = float(record.sample_rate_hz)
        self._samples = np.empty((0, len(record.channel_order)), dtype=np.float32)
        self._duration = float(record.frame_count) / self._sample_rate
        self._selected_range = (0.0, self._duration)
        self._build_ui()
        self._update_buttons()
        if prepared_audio is not None:
            self._apply_audio(prepared_audio)
        elif _synchronous:
            self._apply_audio(prepare_replay_audio(record.source_wav_path, preloaded_audio=preloaded_audio))
        else:
            path = record.source_wav_path
            _start_worker("audio", 0, lambda: prepare_replay_audio(path, preloaded_audio=preloaded_audio), self._worker_completed)

    @staticmethod
    def _load_audio(path: str) -> tuple[float, np.ndarray]:
        sample_rate, raw = wavfile.read(path)
        array = np.asarray(raw)
        if array.ndim == 1:
            array = array.reshape(-1, 1)
        if array.ndim != 2 or array.shape[0] == 0:
            raise ValueError("音频数据为空")
        if np.issubdtype(array.dtype, np.integer):
            scale = float(max(abs(np.iinfo(array.dtype).min), np.iinfo(array.dtype).max))
            samples = array.astype(np.float32) / scale
        else:
            samples = array.astype(np.float32)
        return float(sample_rate), samples

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.status_label = QLabel("正在加载试听音频…", self)
        root.addWidget(self.status_label)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("通道"))
        self.channel_list = QListWidget(self)
        self.channel_list.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self.channel_list.setMaximumHeight(82)
        controls.addWidget(self.channel_list, 1)

        controls.addWidget(QLabel("增益(dB)"))
        self.gain_spin = QDoubleSpinBox(self)
        self.gain_spin.setRange(-60.0, 35.0)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setValue(20.0)
        controls.addWidget(self.gain_spin)
        root.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._wave_view_box = _RangeSelectionViewBox()
        self._wave_view_box.setMenuEnabled(False)
        self.wave_plot = pg.PlotWidget(
            self,
            viewBox=self._wave_view_box,
        )
        self.wave_plot.setLabel("bottom", "时间", units="s")
        self.wave_plot.setLabel("left", "幅值")
        self.wave_plot.setTitle("试听波形")
        self.wave_curve = self.wave_plot.plot(pen=pg.mkPen("#2563eb", width=1))
        self.region = pg.LinearRegionItem(
            values=self._selected_range,
            bounds=(0.0, self._duration),
            movable=False,
            brush=pg.mkBrush(37, 99, 235, 35),
            pen=pg.mkPen("#1d4ed8", width=1),
        )
        self.wave_plot.addItem(self.region)
        splitter.addWidget(self.wave_plot)

        self.stft_plot = pg.PlotWidget(self)
        self.stft_plot.setLabel("bottom", "时间", units="s")
        self.stft_plot.setLabel("left", "频率", units="Hz")
        self.stft_plot.setTitle("试听时频谱")
        self.stft_image = pg.ImageItem(axisOrder="row-major")
        self.stft_image.setLookupTable(
            pg.colormap.get("CET-L9").getLookupTable()
        )
        self.stft_plot.addItem(self.stft_image)
        splitter.addWidget(self.stft_plot)
        splitter.setSizes([300, 300])
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.play_button = QPushButton("播放所选范围", self)
        self.stop_button = QPushButton("停止", self)
        self.close_button = QPushButton("关闭", self)
        actions.addWidget(self.play_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.close_button)
        root.addLayout(actions)

        self.channel_list.itemSelectionChanged.connect(self._refresh_plots)
        self._wave_view_box.range_dragging.connect(
            self._selection_dragging
        )
        self._wave_view_box.range_finished.connect(
            self._selection_finished
        )
        self.play_button.clicked.connect(self._play)
        self.stop_button.clicked.connect(self._stop)
        self.close_button.clicked.connect(self.close)

    def _selected_channels(self) -> tuple[tuple[int, int], ...]:
        selected = []
        for item in self.channel_list.selectedItems():
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                selected.append((int(data[0]), int(data[1])))
        return tuple(selected)

    def _range(self) -> tuple[float, float]:
        return self._selected_range

    def _set_selection_range(
        self,
        start: float,
        end: float,
    ) -> bool:
        lower = max(0.0, min(float(start), self._duration))
        upper = max(0.0, min(float(end), self._duration))
        lower, upper = sorted((lower, upper))
        if upper - lower < 1.0 / self._sample_rate:
            return False
        self._selected_range = (lower, upper)
        self.region.setRegion(self._selected_range)
        return True

    def _selection_dragging(self, start: float, end: float) -> None:
        self._set_selection_range(start, end)

    def _selection_finished(self, start: float, end: float) -> None:
        if self._set_selection_range(start, end):
            self._refresh_stft()

    def _refresh_plots(self) -> None:
        self._refresh_stft()

    def _refresh_stft(self) -> None:
        if not self._ready or self._closed.is_set():
            return
        self._plot_generation += 1
        samples, sample_rate = self._samples, self._sample_rate
        columns = tuple(stored for _physical, stored in self._selected_channels())
        selection = self._range()
        work = lambda: prepare_replay_plots(samples, sample_rate, columns, selection)
        if self._synchronous:
            self._apply_plots(work())
        else:
            _start_worker("plots", self._plot_generation, work, self._worker_completed)

    def _apply_plots(self, plots) -> None:
        self._applied_plot_range = plots.selection
        self.wave_curve.setData(plots.wave_times, plots.wave_values)
        self.wave_plot.setXRange(0.0, self._duration, padding=0.0)
        if plots.magnitude_db is None:
            self.stft_image.clear()
            return
        start, end = plots.selection
        self.stft_image.setImage(
            plots.magnitude_db,
            autoLevels=True,
        )
        self.stft_image.setRect(
            QRectF(
                start,
                0.0,
                max(end - start, 1.0 / self._sample_rate),
                plots.max_frequency,
            )
        )
        self.stft_plot.setXRange(start, end, padding=0.0)
        self.stft_plot.setYRange(
            0.0,
            plots.max_frequency,
            padding=0.0,
        )

    def _apply_audio(self, audio) -> None:
        self._sample_rate, self._samples = audio.sample_rate, audio.samples
        self._duration = self._samples.shape[0] / self._sample_rate
        self._selected_range = (0.0, self._duration)
        self.region.setBounds((0.0, self._duration))
        self.region.setRegion(self._selected_range)
        self.channel_list.blockSignals(True)
        self.channel_list.clear()
        for column in range(self._samples.shape[1]):
            order = self._record.channel_order
            physical = order[column] if column < len(order) else column
            item = QListWidgetItem(f"CH{physical + 1}")
            item.setData(Qt.ItemDataRole.UserRole, (physical, column))
            self.channel_list.addItem(item)
        self.channel_list.item(0).setSelected(True)
        self.channel_list.blockSignals(False)
        self._ready = True
        self.status_label.setText("试听波形与时频谱（仅用于试听）")
        self._apply_plots(audio.plots)
        self._update_buttons()

    def _update_buttons(self):
        available = self._ready and not self._closed.is_set() and not self._operation_pending
        self.play_button.setEnabled(available)
        self.stop_button.setEnabled(available)
        self.channel_list.setEnabled(self._ready and not self._closed.is_set())

    @pyqtSlot(str, int, object, object)
    def _worker_completed(self, kind, generation, value, error):
        if self._closed.is_set():
            return
        if kind == "audio":
            if error is None:
                self._apply_audio(value)
            else:
                self.status_label.setText(f"试听音频不可用：{error}")
        elif kind == "plots":
            if generation != self._plot_generation:
                return
            if error is None:
                self._apply_plots(value)
            else:
                self.status_label.setText(f"试听时频谱不可用：{error}")
        else:
            self._operation_pending = False
            self._update_buttons()
            if kind == "play" and (error is not None or type(value) is not PlaybackResult
                    or value.status is not PlaybackStatus.STARTED or value.code is not PlaybackCode.STARTED):
                self.status_label.setText("回放启动失败")
            elif error is not None:
                self.status_label.setText("停止回放失败")

    def _play(self) -> None:
        if not self._ready or self._closed.is_set() or self._operation_pending:
            return
        selected = self._selected_channels()
        if not selected:
            QMessageBox.warning(self, "回放", "请至少选择一个通道")
            return
        start, end = self._range()
        physical_channels = tuple(physical for physical, _stored in selected)
        request = RunPlaybackRequest(
            self._record.run_id,
            physical_channels[0],
            float(start),
            float(end),
            display_channel_indices=physical_channels,
            gain_db_override=float(self.gain_spin.value()),
        )
        playback, owner, closed = self._playback, self._owner, self._closed

        def work():
            try:
                if closed.is_set():
                    return None
                if callable(getattr(playback, "stop_if_owner", None)):
                    return playback.play(request, owner=owner)
                return playback.play(request)  # Compatibility with legacy simple fakes.
            finally:
                if closed.is_set():
                    _stop_owned(playback, owner, wait_for_gate=True)

        self._run_operation("play", work)

    def _stop(self) -> None:
        if self._closed.is_set() or self._operation_pending:
            return
        playback, owner = self._playback, self._owner
        self._run_operation("stop", lambda: _stop_owned(playback, owner))

    def _run_operation(self, kind, work):
        self._operation_pending = True
        self._update_buttons()
        if self._synchronous:
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
            self._worker_completed(kind, 0, value, error)
        else:
            _start_worker(kind, 0, work, self._worker_completed)

    def shutdown(self):
        """Invalidate queued results and stop only this panel's playback, without waiting."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._update_buttons()
        playback, owner = self._playback, self._owner
        _start_worker("close", 0, lambda: _stop_owned(playback, owner, wait_for_gate=True))

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)


def _stop_owned(playback, owner, *, wait_for_gate=False):
    conditional = getattr(playback, "stop_if_owner", None)
    if callable(conditional):
        while True:
            result = conditional(owner, 1.0)
            if not wait_for_gate or getattr(result, "code", None) is not PlaybackLifecycleCode.BUSY:
                return result
            # Cleanup runs in a worker. Retry the guard after the other operation
            # releases the gate; a successful takeover will then be a no-op.
            time.sleep(0.02)
    stop = getattr(playback, "stop", None)
    if callable(stop):
        return stop(1.0)


class RunAudioReplayControls(QWidget):
    """Metadata-only, single-channel full-record replay for historical viewers.

    Hosts call shutdown before replacing or disposing of these controls.
    """

    def __init__(self, *, record, playback, parent=None, unavailable_message=None):
        super().__init__(parent)
        self._record, self._playback = record, playback
        self._owner = object()
        self._closed = threading.Event()
        self._operation_pending = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("通道", self))
        self.channel_combo = QComboBox(self)
        self.channel_combo.setMinimumWidth(100)
        if record is not None:
            for channel in record.channel_order:
                self.channel_combo.addItem(f"CH{channel + 1}", channel)
            self.channel_combo.setCurrentIndex(max(0, self.channel_combo.findData(0)))
        row.addWidget(self.channel_combo)
        row.addWidget(QLabel("增益(dB)", self))
        self.gain_spin = QDoubleSpinBox(self)
        self.gain_spin.setRange(-60.0, 35.0)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setValue(20.0)
        row.addWidget(self.gain_spin)
        self.play_button = QPushButton("重放", self)
        self.play_button.clicked.connect(self._play)
        row.addWidget(self.play_button)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)
        if record is None:
            self.status_label.setText(unavailable_message or "未保存录音")
        elif playback is None:
            self.status_label.setText("录音重放不可用：播放服务未就绪")
        self._update_buttons()

    def _update_buttons(self):
        available = (self._record is not None and self._playback is not None
                     and self.channel_combo.currentData() is not None and not self._closed.is_set())
        self.play_button.setEnabled(available and not self._operation_pending)
        self.channel_combo.setEnabled(available)
        self.gain_spin.setEnabled(available)

    def _play(self):
        if not self.play_button.isEnabled():
            return
        channel = self.channel_combo.currentData()
        request = RunPlaybackRequest(
            self._record.run_id, channel, 0.0,
            float(self._record.frame_count) / float(self._record.sample_rate_hz),
            display_channel_indices=(channel,),
            gain_db_override=float(self.gain_spin.value()),
        )
        playback, owner, closed = self._playback, self._owner, self._closed

        def work():
            try:
                if closed.is_set():
                    return None
                if callable(getattr(playback, "stop_if_owner", None)):
                    return playback.play(request, owner=owner)
                return playback.play(request)  # Compatibility with legacy simple fakes.
            finally:
                if closed.is_set():
                    _stop_owned(playback, owner, wait_for_gate=True)

        self._operation_pending = True
        self.status_label.setText("正在启动重放…")
        self._update_buttons()
        _start_worker("play", 0, work, self._worker_completed)

    @pyqtSlot(str, int, object, object)
    def _worker_completed(self, kind, generation, value, error):
        if self._closed.is_set():
            return
        self._operation_pending = False
        self._update_buttons()
        if (error is not None or type(value) is not PlaybackResult
                or value.status is not PlaybackStatus.STARTED or value.code is not PlaybackCode.STARTED):
            self.status_label.setText("回放启动失败")
        else:
            self.status_label.clear()

    def shutdown(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._update_buttons()
        playback, owner = self._playback, self._owner
        if playback is not None:
            _start_worker("close", 0, lambda: _stop_owned(playback, owner, wait_for_gate=True))

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


class RunAudioReplayDialog(QDialog):
    """Legacy synchronous dialog API with waveform and range audition."""

    def __init__(self, *, record, playback, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音频回放")
        self.resize(900, 620)
        self.panel = RunAudioReplayPanel(record=record, playback=playback, parent=self, _synchronous=True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel)
        self.panel.close_button.clicked.disconnect()
        self.panel.close_button.clicked.connect(self.close)

    def __getattr__(self, name):
        panel = self.__dict__.get("panel")
        if panel is not None:
            return getattr(panel, name)
        raise AttributeError(name)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)

    def done(self, result):
        self.panel.shutdown()
        super().done(result)
