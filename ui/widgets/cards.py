from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    def __init__(self, title: str, body: QWidget):
        super().__init__()
        self.setObjectName("Card")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        lay.addWidget(self.title_label)
        lay.addWidget(body, 1)

    def set_title(self, title: str) -> None:
        self.title_label.setText(str(title))

    def title(self) -> str:
        return self.title_label.text()

