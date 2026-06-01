from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    def __init__(self, title: str, body: QWidget):
        super().__init__()
        self.setObjectName("Card")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        title_row.addWidget(self.title_label, 1)

        self._title_suffix_text = ""

        self.title_suffix_label = QLabel("")
        self.title_suffix_label.setObjectName("CardTitleSuffix")
        self.title_suffix_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.title_suffix_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.title_suffix_label.setVisible(False)
        title_row.addWidget(self.title_suffix_label, 0)

        self.title_metric_widget = QWidget()
        self.title_metric_widget.setObjectName("CardTitleMetric")
        metric_lay = QHBoxLayout(self.title_metric_widget)
        metric_lay.setContentsMargins(0, 0, 0, 0)
        metric_lay.setSpacing(6)

        self.title_metric_label = QLabel("")
        self.title_metric_label.setObjectName("CardTitleMetricLabel")
        self.title_metric_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        metric_lay.addWidget(self.title_metric_label, 0)

        self.title_metric_value_label = QLabel("")
        self.title_metric_value_label.setObjectName("CardTitleMetricValue")
        self.title_metric_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_metric_value_label.setMinimumSize(68, 26)
        self.title_metric_value_label.setSizePolicy(
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Fixed,
        )
        metric_lay.addWidget(self.title_metric_value_label, 0)

        self.title_metric_widget.setVisible(False)
        title_row.addWidget(self.title_metric_widget, 0)

        lay.addLayout(title_row)
        lay.addWidget(body, 1)

    def set_title(self, title: str) -> None:
        self.title_label.setText(str(title))

    def title(self) -> str:
        return self.title_label.text()

    def set_title_suffix(self, text: str) -> None:
        normalized = str(text)
        self._title_suffix_text = normalized
        self.title_suffix_label.setText(normalized)
        self.title_suffix_label.setVisible(bool(normalized))
        self.title_metric_widget.setVisible(False)

    def set_title_metric(self, label: str, value: str) -> None:
        label_text = str(label)
        value_text = str(value)
        self._title_suffix_text = f"{label_text}{value_text}"
        self.title_suffix_label.setVisible(False)
        self.title_metric_label.setText(label_text)
        self.title_metric_value_label.setText(value_text)
        self.title_metric_widget.setVisible(bool(label_text or value_text))

    def title_suffix(self) -> str:
        return self._title_suffix_text

