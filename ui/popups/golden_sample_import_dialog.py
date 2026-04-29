from __future__ import annotations

import os
from dataclasses import asdict

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from scipy.io import wavfile

from app.service.golden_sample_service import GoldenSampleService


class GoldenSampleImportDialog(QDialog):
    def __init__(
        self,
        *,
        source_path: str,
        analysis_duration: float | None,
        config_sample_rate,
        golden_sample_service: GoldenSampleService,
        parent=None,
    ):
        super().__init__(parent)
        self._source_path = str(source_path)
        self._analysis_duration = analysis_duration
        self._config_sample_rate = config_sample_rate
        self._golden_sample_service = golden_sample_service
        self._staged_metadata: dict | None = None
        self._wav_sample_rate: float | None = None
        self._wav_data = None
        self._required_num_samples: int | None = None
        self._total_duration_ms = 0
        self._region_item: pg.LinearRegionItem | None = None
        self._syncing_start = False
        self._validation_error: str | None = None

        self.setWindowTitle("导入黄金样本")
        self.resize(920, 640)
        self.setModal(True)

        self._init_ui()
        self._load_source_wav()
        self._refresh_validation_state()

    def staged_metadata(self) -> dict | None:
        if self._staged_metadata is None:
            return None
        return dict(self._staged_metadata)

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        summary = QWidget(self)
        summary_form = QFormLayout(summary)
        summary_form.setContentsMargins(0, 0, 0, 0)
        summary_form.setHorizontalSpacing(12)
        summary_form.setVerticalSpacing(8)

        self._sample_rate_value = QLabel("-")
        self._sample_rate_value.setObjectName("file_sample_rate_value")
        summary_form.addRow("采样率", self._sample_rate_value)

        self._channel_count_value = QLabel("-")
        self._channel_count_value.setObjectName("file_channel_count_value")
        summary_form.addRow("通道数", self._channel_count_value)

        self._total_duration_value = QLabel("-")
        self._total_duration_value.setObjectName("file_total_duration_value")
        summary_form.addRow("总时长", self._total_duration_value)

        self._required_duration_value = QLabel("-")
        self._required_duration_value.setObjectName("required_golden_duration_value")
        summary_form.addRow("所需时长", self._required_duration_value)
        root.addWidget(summary)

        controls = QWidget(self)
        controls_form = QFormLayout(controls)
        controls_form.setContentsMargins(0, 0, 0, 0)
        controls_form.setHorizontalSpacing(12)
        controls_form.setVerticalSpacing(8)

        self._source_channel_selector = QComboBox(self)
        self._source_channel_selector.setObjectName("source_channel_selector")
        self._source_channel_selector.currentIndexChanged.connect(
            self._refresh_waveform_preview
        )
        controls_form.addRow("源通道", self._source_channel_selector)

        self._start_ms_spin = QSpinBox(self)
        self._start_ms_spin.setObjectName("start_ms_spin")
        self._start_ms_spin.setRange(0, 0)
        self._start_ms_spin.setSuffix(" ms")
        self._start_ms_spin.valueChanged.connect(self._on_start_ms_changed)
        controls_form.addRow("起点", self._start_ms_spin)

        self._end_ms_display = QLineEdit(self)
        self._end_ms_display.setObjectName("end_ms_display")
        self._end_ms_display.setReadOnly(True)
        controls_form.addRow("终点", self._end_ms_display)
        root.addWidget(controls)

        self._plot_widget = pg.PlotWidget(self)
        self._plot_widget.setMenuEnabled(False)
        self._plot_widget.setMouseEnabled(x=False, y=False)
        self._plot_widget.hideButtons()
        self._plot_widget.setBackground("w")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.18)
        self._plot_widget.setLabel("bottom", "时间", units="s")
        self._plot_widget.setLabel("left", "幅值")
        self._waveform_curve = self._plot_widget.plot(pen=pg.mkPen(color="#2563eb", width=1.5))
        root.addWidget(self._plot_widget, 1)

        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(10)

        self._validation_message = QLabel("")
        self._validation_message.setObjectName("validation_message")
        self._validation_message.setWordWrap(True)
        footer_layout.addWidget(self._validation_message, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        self._buttons.setObjectName("dialog_buttons")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self._buttons.accepted.connect(self._on_confirm)
        self._buttons.rejected.connect(self.reject)
        footer_layout.addWidget(self._buttons)
        root.addWidget(footer)

    def _load_source_wav(self) -> None:
        try:
            wav_sample_rate, wav_data = wavfile.read(self._source_path)
        except Exception:
            self._validation_error = "无法读取 WAV 文件"
            self._refresh_metadata_labels(channel_count=None, total_duration_ms=None)
            return

        self._wav_sample_rate = float(wav_sample_rate)
        self._wav_data = wav_data
        channel_count = self._channel_count()
        total_duration_ms = round(self._sample_count() * 1000 / self._wav_sample_rate)
        self._total_duration_ms = total_duration_ms

        self._source_channel_selector.clear()
        for channel_index in range(channel_count):
            self._source_channel_selector.addItem(str(channel_index + 1), channel_index)

        self._refresh_metadata_labels(
            channel_count=channel_count,
            total_duration_ms=total_duration_ms,
        )
        self._refresh_waveform_preview()

    def _refresh_metadata_labels(
        self,
        *,
        channel_count: int | None,
        total_duration_ms: int | None,
    ) -> None:
        if self._wav_sample_rate is None:
            self._sample_rate_value.setText("-")
        else:
            self._sample_rate_value.setText(f"{self._wav_sample_rate:g} Hz")

        self._channel_count_value.setText(
            "-" if channel_count is None else str(channel_count)
        )
        self._total_duration_value.setText(
            "-" if total_duration_ms is None else f"{total_duration_ms} ms"
        )

        if self._is_positive_number(self._analysis_duration) and self._is_positive_number(
            self._config_sample_rate
        ):
            required_ms = round(
                round(float(self._config_sample_rate) * float(self._analysis_duration))
                * 1000
                / float(self._config_sample_rate)
            )
            self._required_duration_value.setText(f"{required_ms} ms")
        else:
            self._required_duration_value.setText("-")

    def _refresh_validation_state(self) -> None:
        self._required_num_samples = None
        self._validation_error = None

        if not self._is_positive_number(self._analysis_duration):
            self._validation_error = "分析时长必须大于 0"
        elif not self._is_positive_number(self._config_sample_rate):
            self._validation_error = "配置采样率无效"
        elif self._wav_data is None or self._wav_sample_rate is None:
            if self._validation_error is None:
                self._validation_error = "无法读取 WAV 文件"
        else:
            self._required_num_samples = round(
                float(self._config_sample_rate) * float(self._analysis_duration)
            )
            if float(self._wav_sample_rate) != float(self._config_sample_rate):
                self._validation_error = "WAV 采样率与当前配置采样率不一致"
            elif self._sample_count() < self._required_num_samples:
                self._validation_error = "黄金样本文件短于当前分析时长"

        if self._validation_error is not None:
            self._set_region(None, movable=False)
            self._start_ms_spin.setEnabled(False)
            self._start_ms_spin.setRange(0, 0)
            self._start_ms_spin.setValue(0)
        else:
            self._configure_region()

        self._update_end_time()
        self._validation_message.setText(self._validation_error or "就绪")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._validation_error is None
        )

    def _configure_region(self) -> None:
        assert self._required_num_samples is not None
        assert self._wav_sample_rate is not None

        max_start_sample = self._sample_count() - self._required_num_samples
        if max_start_sample < 0:
            max_start_sample = 0
        max_start_ms = round(max_start_sample * 1000 / self._wav_sample_rate)
        self._start_ms_spin.blockSignals(True)
        self._start_ms_spin.setRange(0, max_start_ms)
        self._start_ms_spin.setValue(min(self._start_ms_spin.value(), max_start_ms))
        self._start_ms_spin.setEnabled(max_start_ms > 0)
        self._start_ms_spin.blockSignals(False)
        start_sec = self._start_ms_spin.value() / 1000.0
        end_sec = start_sec + float(self._analysis_duration)
        self._set_region((start_sec, end_sec), movable=max_start_ms > 0)

    def _set_region(
        self,
        values: tuple[float, float] | None,
        *,
        movable: bool,
    ) -> None:
        if self._region_item is None:
            self._region_item = pg.LinearRegionItem(
                values=(0.0, 0.0),
                movable=movable,
                brush=(37, 99, 235, 40),
                pen=pg.mkPen(color="#1d4ed8", width=1),
            )
            self._region_item.sigRegionChangeFinished.connect(
                self._on_region_change_finished
            )
            self._plot_widget.addItem(self._region_item)
        self._region_item.setMovable(movable)
        if values is None:
            self._region_item.hide()
            self._region_item.setRegion((0.0, 0.0))
            return
        self._region_item.show()
        self._region_item.setRegion(values)

    def _refresh_waveform_preview(self) -> None:
        channel = self._selected_channel_data()
        if channel is None or self._wav_sample_rate is None:
            self._waveform_curve.setData([], [])
            return

        times_sec = np.arange(channel.shape[0], dtype=float) / float(self._wav_sample_rate)
        x_values, y_values = self._downsample(times_sec, channel.astype(float))
        self._waveform_curve.setData(x_values, y_values)
        if x_values.size > 1:
            self._plot_widget.setXRange(float(x_values[0]), float(x_values[-1]), padding=0.0)

    def _downsample(
        self,
        x_values: np.ndarray,
        y_values: np.ndarray,
        *,
        max_points: int = 5000,
    ) -> tuple[np.ndarray, np.ndarray]:
        if x_values.size <= max_points:
            return x_values, y_values
        stride = max(1, x_values.size // max_points)
        return x_values[::stride], y_values[::stride]

    def _on_start_ms_changed(self, _value: int) -> None:
        if self._syncing_start or self._validation_error is not None:
            return
        self._syncing_start = True
        try:
            start_sample = self._normalized_start_sample(self._start_ms_spin.value())
            normalized_start_ms = self._normalized_start_ms(start_sample)
            if normalized_start_ms != self._start_ms_spin.value():
                self._start_ms_spin.setValue(normalized_start_ms)
            start_sec = start_sample / float(self._wav_sample_rate)
            end_sec = start_sec + float(self._analysis_duration)
            self._set_region((start_sec, end_sec), movable=self._start_ms_spin.isEnabled())
            self._update_end_time()
        finally:
            self._syncing_start = False

    def _on_region_change_finished(self) -> None:
        if (
            self._region_item is None
            or self._validation_error is not None
            or self._wav_sample_rate is None
            or not self._is_positive_number(self._analysis_duration)
        ):
            return
        start_sec, end_sec = self._region_item.getRegion()
        width_sec = float(self._analysis_duration)
        start_sec = max(0.0, min(float(start_sec), max(0.0, float(end_sec) - width_sec)))
        max_start_sec = max(0.0, (self._sample_count() - self._required_num_samples) / float(self._wav_sample_rate))
        start_sec = min(start_sec, max_start_sec)
        start_sample = self._normalized_start_sample(round(start_sec * 1000))
        start_ms = self._normalized_start_ms(start_sample)
        self._syncing_start = True
        try:
            self._start_ms_spin.setValue(start_ms)
        finally:
            self._syncing_start = False
        normalized_start_sec = start_sample / float(self._wav_sample_rate)
        self._set_region(
            (normalized_start_sec, normalized_start_sec + width_sec),
            movable=self._start_ms_spin.isEnabled(),
        )
        self._update_end_time()

    def _update_end_time(self) -> None:
        required_duration_ms = self._required_duration_ms()
        self._end_ms_display.setText(f"{self._start_ms_spin.value() + required_duration_ms} ms")

    def _required_duration_ms(self) -> int:
        if self._required_num_samples is None or not self._is_positive_number(
            self._config_sample_rate
        ):
            return 0
        return round(self._required_num_samples * 1000 / float(self._config_sample_rate))

    def _on_confirm(self) -> None:
        if self._validation_error is not None or self._required_num_samples is None:
            return
        start_sample = self._normalized_start_sample(self._start_ms_spin.value())
        try:
            staged = self._golden_sample_service.create_staged_sample(
                source_path=self._source_path,
                source_channel=self._selected_channel_index(),
                start_sample=start_sample,
                num_samples=self._required_num_samples,
                sample_rate=float(self._config_sample_rate),
                label=os.path.basename(self._source_path),
            )
        except Exception as exc:
            self._staged_metadata = None
            error_text = f"导入失败：{exc}"
            self._validation_message.setText(error_text)
            QMessageBox.warning(self, "导入失败", error_text)
            return
        self._staged_metadata = asdict(staged)
        self.accept()

    def _selected_channel_index(self) -> int:
        data = self._source_channel_selector.currentData()
        return int(data or 0)

    def _selected_channel_data(self):
        if self._wav_data is None:
            return None
        if getattr(self._wav_data, "ndim", 1) == 1:
            return np.asarray(self._wav_data)
        channel_index = self._selected_channel_index()
        return np.asarray(self._wav_data[:, channel_index])

    def _channel_count(self) -> int:
        if self._wav_data is None:
            return 0
        if getattr(self._wav_data, "ndim", 1) == 1:
            return 1
        return int(self._wav_data.shape[1])

    def _sample_count(self) -> int:
        if self._wav_data is None:
            return 0
        return int(self._wav_data.shape[0])

    def _normalized_start_sample(self, start_ms: int) -> int:
        if self._wav_sample_rate is None or self._required_num_samples is None:
            return 0
        start_sample = round(int(start_ms) * float(self._wav_sample_rate) / 1000.0)
        max_start_sample = max(0, self._sample_count() - self._required_num_samples)
        return max(0, min(start_sample, max_start_sample))

    def _normalized_start_ms(self, start_sample: int) -> int:
        if self._wav_sample_rate is None:
            return 0
        return round(int(start_sample) * 1000 / float(self._wav_sample_rate))

    @staticmethod
    def _is_positive_number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


__all__ = ["GoldenSampleImportDialog"]
