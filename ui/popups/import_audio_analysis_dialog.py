from __future__ import annotations

import math
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.service import audio_wav
from app.service.analysis import imported_audio
from app.service.analysis.imported_audio import (
    ImportedAudioAnalysisRequest,
    ImportedChannelAssignment,
)

class ImportAudioAnalysisDialog(QDialog):
    def __init__(
        self,
        *,
        configs: list[dict],
        default_config_id: int | None,
        parent=None,
    ):
        super().__init__(parent)
        self._configs = [dict(config) for config in configs]
        self._configs_by_id = {
            int(config["config_id"]): config
            for config in self._configs
            if _is_int_like(config.get("config_id"))
        }
        self._wav: audio_wav.WavMetadata | None = None
        self._wav_error: str | None = None
        self._validation_error: str | None = None
        self._status_text: str = ""
        self._mapping_combos: list[QComboBox] = []
        self._window_edited = False
        self._syncing_window_defaults = False

        self.setWindowTitle("导入音频分析")
        self.resize(720, 520)
        self.setModal(True)

        self._init_ui(default_config_id)
        self._refresh_validation_state()

    def load_wav_path(self, path: str) -> None:
        """Load metadata and rebuild channel mapping controls for a WAV path."""
        wav_path = str(path or "").strip()
        self._path_edit.setText(wav_path)
        self._wav = None
        self._wav_error = None
        self._clear_mapping_rows()

        if not wav_path:
            self._wav_error = "请选择 WAV 文件"
            self._refresh_wav_metadata()
            self._refresh_validation_state()
            return

        try:
            self._wav = audio_wav.probe_wav(wav_path)
        except ValueError as exc:
            self._wav_error = f"WAV 格式不支持或文件读取失败：{exc}"

        self._refresh_wav_metadata()
        self._rebuild_mapping_rows()
        self._refresh_validation_state()

    def current_request(self) -> ImportedAudioAnalysisRequest | None:
        """Return a request when the current dialog state is valid, otherwise `None`."""
        request, error, _status = self._build_request()
        if error is not None:
            return None
        return request

    def validation_error(self) -> str | None:
        """Return the current validation error text for tests and disabled OK state."""
        _request, error, _status = self._build_request()
        return error

    def _init_ui(self, default_config_id: int | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        file_row = QHBoxLayout()
        self._path_edit = QLineEdit(self)
        self._path_edit.setObjectName("wav_path_edit")
        self._path_edit.setReadOnly(True)
        file_row.addWidget(self._path_edit, 1)
        self._browse_button = QPushButton("选择 WAV", self)
        self._browse_button.setObjectName("wav_browse_button")
        self._browse_button.clicked.connect(self._on_browse)
        file_row.addWidget(self._browse_button)
        root.addLayout(file_row)

        metadata = QWidget(self)
        metadata_form = QFormLayout(metadata)
        metadata_form.setContentsMargins(0, 0, 0, 0)
        metadata_form.setHorizontalSpacing(12)
        metadata_form.setVerticalSpacing(8)

        self._wav_path_value = QLabel("-")
        self._wav_path_value.setObjectName("wav_path_value")
        self._wav_path_value.setWordWrap(True)
        metadata_form.addRow("文件路径", self._wav_path_value)

        self._wav_name_value = QLabel("-")
        self._wav_name_value.setObjectName("wav_name_value")
        metadata_form.addRow("文件名", self._wav_name_value)

        self._sample_rate_value = QLabel("-")
        self._sample_rate_value.setObjectName("wav_sample_rate_value")
        metadata_form.addRow("采样率", self._sample_rate_value)

        self._channel_count_value = QLabel("-")
        self._channel_count_value.setObjectName("wav_channel_count_value")
        metadata_form.addRow("通道数", self._channel_count_value)

        self._duration_value = QLabel("-")
        self._duration_value.setObjectName("wav_duration_value")
        metadata_form.addRow("时长", self._duration_value)
        self._format_value = QLabel("-")
        self._format_value.setObjectName("wav_format_value")
        metadata_form.addRow("采样格式", self._format_value)
        root.addWidget(metadata)

        config_controls = QWidget(self)
        config_form = QFormLayout(config_controls)
        config_form.setContentsMargins(0, 0, 0, 0)
        config_form.setHorizontalSpacing(12)
        config_form.setVerticalSpacing(8)

        self._config_selector = QComboBox(self)
        self._config_selector.setObjectName("config_selector")
        self._populate_config_selector(default_config_id)
        self._config_selector.currentIndexChanged.connect(self._on_config_changed)
        config_form.addRow("配置", self._config_selector)

        self._start_sec_spin = QDoubleSpinBox(self)
        self._start_sec_spin.setObjectName("start_sec_spin")
        self._start_sec_spin.setRange(0.0, 86400.0)
        self._start_sec_spin.setDecimals(3)
        self._start_sec_spin.setSingleStep(0.1)
        self._start_sec_spin.setSuffix(" s")
        self._start_sec_spin.valueChanged.connect(self._on_window_edited)
        config_form.addRow("起始秒", self._start_sec_spin)

        self._duration_sec_spin = QDoubleSpinBox(self)
        self._duration_sec_spin.setObjectName("duration_sec_spin")
        self._duration_sec_spin.setRange(0.0, 86400.0)
        self._duration_sec_spin.setDecimals(3)
        self._duration_sec_spin.setSingleStep(0.1)
        self._duration_sec_spin.setSuffix(" s")
        self._duration_sec_spin.valueChanged.connect(self._on_window_edited)
        config_form.addRow("分析时长", self._duration_sec_spin)

        self._mic_calibration_checkbox = QCheckBox("使用当前麦克风校准", self)
        self._mic_calibration_checkbox.setObjectName("mic_calibration_checkbox")
        self._mic_calibration_checkbox.setChecked(True)
        self._mic_calibration_checkbox.toggled.connect(self._refresh_validation_state)
        config_form.addRow("", self._mic_calibration_checkbox)
        root.addWidget(config_controls)

        mapping_area = QScrollArea(self)
        mapping_area.setWidgetResizable(True)
        mapping_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self._mapping_container = QWidget(mapping_area)
        self._mapping_container.setObjectName("channel_mapping_container")
        self._mapping_layout = QFormLayout(self._mapping_container)
        self._mapping_layout.setContentsMargins(0, 0, 0, 0)
        self._mapping_layout.setHorizontalSpacing(12)
        self._mapping_layout.setVerticalSpacing(6)
        mapping_area.setWidget(self._mapping_container)
        root.addWidget(mapping_area, 1)

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

        self._apply_config_defaults(force=True)

    def _populate_config_selector(self, default_config_id: int | None) -> None:
        self._config_selector.blockSignals(True)
        try:
            self._config_selector.clear()
            if default_config_id is None:
                self._config_selector.addItem("请选择配置", None)
            for config in self._configs:
                config_id = int(config["config_id"])
                name = str(config.get("config_name") or f"Config {config_id}")
                self._config_selector.addItem(f"{name} ({config_id})", config_id)
            if default_config_id is not None:
                index = self._config_selector.findData(int(default_config_id))
                if index >= 0:
                    self._config_selector.setCurrentIndex(index)
        finally:
            self._config_selector.blockSignals(False)

    def _pick_wav_path(self) -> str:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择 WAV 文件",
            "",
            "WAV 文件 (*.wav)",
        )
        return path

    def _on_browse(self) -> None:
        path = self._pick_wav_path()
        if path:
            self.load_wav_path(path)

    def _on_confirm(self) -> None:
        self._refresh_validation_state()
        if self.current_request() is None:
            return
        self.accept()

    def _on_config_changed(self, _index: int) -> None:
        self._apply_config_defaults(force=not self._window_edited)
        self._rebuild_mapping_rows()
        self._refresh_validation_state()

    def _on_window_edited(self, _value: float) -> None:
        if not self._syncing_window_defaults:
            self._window_edited = True
        self._refresh_validation_state()

    def _apply_config_defaults(self, *, force: bool) -> None:
        config = self._selected_config()
        if config is None or not force:
            return

        start_sec = 0.0
        duration_sec = 0.0
        offset_ms = config.get("analysis_window_offset_ms", 0)
        if _is_number(offset_ms):
            start_sec = max(0.0, float(offset_ms) / 1000.0)
        duration = config.get("analysis_duration", 0.0)
        if _is_number(duration):
            duration_sec = max(0.0, float(duration))

        self._syncing_window_defaults = True
        try:
            self._start_sec_spin.setValue(start_sec)
            self._duration_sec_spin.setValue(duration_sec)
        finally:
            self._syncing_window_defaults = False

    def _refresh_wav_metadata(self) -> None:
        wav_path = self._path_edit.text().strip()
        self._wav_path_value.setText(wav_path or "-")
        self._wav_name_value.setText(Path(wav_path).name if wav_path else "-")

        if self._wav is None:
            self._sample_rate_value.setText("-")
            self._channel_count_value.setText("-")
            self._duration_value.setText("-")
            self._format_value.setText("-")
            return

        self._format_value.setText(f"{self._wav.container_format} · {self._wav.bits_per_sample} bit")
        self._sample_rate_value.setText(f"{self._wav.sample_rate_hz:g} Hz")
        self._channel_count_value.setText(str(self._wav.channel_count))
        self._duration_value.setText(f"{self._wav.duration_seconds:g} s")

    def _rebuild_mapping_rows(self) -> None:
        self._clear_mapping_rows()
        if self._wav is None:
            return

        config = self._selected_config()
        selected_channels = (
            imported_audio.selected_config_channels(config) if config is not None else ()
        )
        defaults = self._default_assignments(config)

        for wav_channel_index in range(self._wav.channel_count):
            combo = QComboBox(self._mapping_container)
            combo.setObjectName(f"wav_channel_mapping_{wav_channel_index}")
            for channel_index in selected_channels:
                combo.addItem(f"通道{channel_index + 1}", ("config", channel_index))
            combo.addItem("仅图表", ("chart", None))
            combo.addItem("忽略", ("ignore", None))

            default_assignment = defaults.get(wav_channel_index, ("chart", None))
            default_index = _find_combo_data(combo, default_assignment)
            if default_index < 0:
                default_index = _find_combo_data(combo, ("chart", None))
            if default_index >= 0:
                combo.setCurrentIndex(default_index)
            combo.currentIndexChanged.connect(self._refresh_validation_state)

            self._mapping_layout.addRow(f"WAV 通道 {wav_channel_index + 1}", combo)
            self._mapping_combos.append(combo)

    def _default_assignments(
        self,
        config: dict | None,
    ) -> dict[int, tuple[str, int | None]]:
        if self._wav is None or config is None:
            return {}
        assignments = imported_audio.default_channel_assignments(
            config,
            wav_channel_count=self._wav.channel_count,
        )
        return {
            assignment.wav_channel_index: (assignment.mode, assignment.channel_index)
            for assignment in assignments
        }

    def _clear_mapping_rows(self) -> None:
        self._mapping_combos.clear()
        while self._mapping_layout.rowCount():
            self._mapping_layout.removeRow(0)

    def _build_request(
        self,
    ) -> tuple[ImportedAudioAnalysisRequest | None, str | None, str | None]:
        if self._wav is None:
            return None, self._wav_error or "请选择 WAV 文件", None

        config = self._selected_config()
        if config is None:
            return None, "请选择配置", None

        try:
            window, warnings = imported_audio.resolve_analysis_window(
                config,
                wav_duration_sec=self._wav.duration_seconds,
                requested_start_sec=self._start_sec_spin.value(),
                requested_duration_sec=self._duration_sec_spin.value(),
            )
        except ValueError as exc:
            return None, str(exc), None

        try:
            assignments = imported_audio.normalize_channel_assignments(
                self._current_assignments(),
                wav_channel_count=self._wav.channel_count,
                allowed_config_channels=imported_audio.selected_config_channels(config),
            )
        except ValueError as exc:
            return None, str(exc), None

        request = ImportedAudioAnalysisRequest(
            config_id=int(config["config_id"]),
            wav_path=str(self._wav.path),
            assignments=assignments,
            start_sec=window[0],
            duration_sec=window[1] - window[0],
            use_mic_calibration=self._mic_calibration_checkbox.isChecked(),
        )
        status = warnings[0].message if warnings else "就绪"
        return request, None, status

    def _current_assignments(self) -> tuple[ImportedChannelAssignment, ...]:
        assignments: list[ImportedChannelAssignment] = []
        for wav_channel_index, combo in enumerate(self._mapping_combos):
            mode, channel_index = combo.currentData()
            assignments.append(
                ImportedChannelAssignment(
                    wav_channel_index=wav_channel_index,
                    mode=mode,
                    channel_index=channel_index,
                )
            )
        return tuple(assignments)

    def _refresh_validation_state(self) -> None:
        _request, error, status = self._build_request()
        self._validation_error = error
        self._status_text = error or status or "就绪"
        self._validation_message.setText(self._status_text)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            error is None
        )

    def _selected_config_id(self) -> int | None:
        value = self._config_selector.currentData()
        if _is_int_like(value):
            return int(value)
        return None

    def _selected_config(self) -> dict | None:
        config_id = self._selected_config_id()
        if config_id is None:
            return None
        return self._configs_by_id.get(config_id)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_int_like(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _find_combo_data(combo: QComboBox, data) -> int:
    for index in range(combo.count()):
        if combo.itemData(index) == data:
            return index
    return -1
