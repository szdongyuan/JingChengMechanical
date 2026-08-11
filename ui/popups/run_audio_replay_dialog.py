from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import (
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
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)


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


class RunAudioReplayDialog(QDialog):
    """Small data-management replay window with range and channel selection."""

    def __init__(self, *, record, playback, parent=None) -> None:
        super().__init__(parent)
        self._record = record
        self._playback = playback
        self._sample_rate, self._samples = self._load_audio(
            record.source_wav_path
        )
        self._duration = self._samples.shape[0] / self._sample_rate
        self._selected_range = (0.0, self._duration)
        self.setWindowTitle("音频回放")
        self.resize(900, 620)
        self._build_ui()
        self._refresh_plots()

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

        controls = QHBoxLayout()
        controls.addWidget(QLabel("通道"))
        self.channel_list = QListWidget(self)
        self.channel_list.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self.channel_list.setMaximumHeight(82)
        channel_order = tuple(self._record.channel_order)
        for stored_column in range(self._samples.shape[1]):
            physical = (
                channel_order[stored_column]
                if stored_column < len(channel_order)
                else stored_column
            )
            item = QListWidgetItem(f"CH{physical + 1}")
            item.setData(Qt.ItemDataRole.UserRole, (physical, stored_column))
            self.channel_list.addItem(item)
        if self.channel_list.count():
            self.channel_list.item(0).setSelected(True)
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

    def _selected_mono(self) -> np.ndarray:
        selected = self._selected_channels()
        columns = [stored for _physical, stored in selected]
        if not columns:
            columns = [0]
        return np.mean(self._samples[:, columns], axis=1, dtype=np.float32)

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
        mono = self._selected_mono()
        step = max(1, math.ceil(mono.size / 6000))
        indexes = np.arange(0, mono.size, step)
        self.wave_curve.setData(indexes / self._sample_rate, mono[indexes])
        self.wave_plot.setXRange(0.0, self._duration, padding=0.0)
        self._refresh_stft()

    def _refresh_stft(self) -> None:
        mono = self._selected_mono()
        start, end = self._range()
        start_frame = max(0, int(start * self._sample_rate))
        end_frame = min(mono.size, int(end * self._sample_rate))
        segment = mono[start_frame:end_frame]
        if segment.size < 2:
            self.stft_image.clear()
            return
        nfft = min(1024, int(segment.size))
        times, frequencies, magnitude_db = dsp.compute_stft(
            segment,
            self._sample_rate,
            nfft,
            0.5,
        )
        if times.size == 0 or frequencies.size == 0:
            self.stft_image.clear()
            return
        self.stft_image.setImage(
            magnitude_db,
            autoLevels=True,
        )
        self.stft_image.setRect(
            QRectF(
                start,
                0.0,
                max(end - start, 1.0 / self._sample_rate),
                float(frequencies[-1]),
            )
        )
        self.stft_plot.setXRange(start, end, padding=0.0)
        self.stft_plot.setYRange(
            0.0,
            float(frequencies[-1]),
            padding=0.0,
        )

    def _play(self) -> None:
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
        try:
            result = self._playback.play(request)
        except Exception:
            QMessageBox.warning(self, "回放", "回放启动失败")
            return
        if (
            type(result) is not PlaybackResult
            or result.status is not PlaybackStatus.STARTED
            or result.code is not PlaybackCode.STARTED
        ):
            QMessageBox.warning(self, "回放", "回放启动失败")

    def _stop(self) -> None:
        stop = getattr(self._playback, "stop", None)
        if callable(stop):
            try:
                stop(1.0)
            except Exception:
                pass

    def closeEvent(self, event) -> None:
        self._stop()
        super().closeEvent(event)
