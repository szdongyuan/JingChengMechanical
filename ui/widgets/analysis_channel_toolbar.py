from __future__ import annotations

from collections.abc import Iterable

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget


def _channel_label(channel_index: int) -> str:
    return f"CH{channel_index + 1}"


class AnalysisChannelToolbar(QWidget):
    channel_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.context_label = QLabel("", self)
        layout.addWidget(self.context_label)
        layout.addStretch(1)

        self.channel_label = QLabel("分析通道", self)
        layout.addWidget(self.channel_label)

        self.channel_combo = QComboBox(self)
        self.channel_combo.currentIndexChanged.connect(self._on_current_index_changed)
        layout.addWidget(self.channel_combo)

    def set_available_channels(self, channels: Iterable[int]) -> None:
        normalized_channels: list[int] = []
        for channel in channels:
            try:
                channel_index = int(channel)
            except (TypeError, ValueError):
                continue
            if channel_index not in normalized_channels:
                normalized_channels.append(channel_index)

        blocker = QSignalBlocker(self.channel_combo)
        current_channel = self.current_channel_index()
        self.channel_combo.clear()
        for channel_index in normalized_channels:
            self.channel_combo.addItem(_channel_label(channel_index), channel_index)

        if current_channel in normalized_channels:
            self.channel_combo.setCurrentIndex(
                self.channel_combo.findData(current_channel)
            )
        del blocker

        selector_visible = len(normalized_channels) > 1
        self.channel_label.setVisible(selector_visible)
        self.channel_combo.setVisible(selector_visible)
        self.channel_combo.setEnabled(selector_visible)

    def set_current_channel(self, channel_index: int | None) -> None:
        blocker = QSignalBlocker(self.channel_combo)
        combo_index = self.channel_combo.findData(channel_index)
        if combo_index >= 0:
            self.channel_combo.setCurrentIndex(combo_index)
        del blocker

    def current_channel_index(self) -> int | None:
        channel_index = self.channel_combo.currentData()
        return channel_index if isinstance(channel_index, int) else None

    def set_context_label_text(self, text: str | None) -> None:
        label_text = (text or "").strip()
        self.context_label.setText(label_text)
        self.context_label.setVisible(bool(label_text))

    def _on_current_index_changed(self, combo_index: int) -> None:
        channel_index = self.channel_combo.itemData(combo_index)
        if isinstance(channel_index, int):
            self.channel_changed.emit(channel_index)
