from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class CollapsibleGroupBox(QWidget):
    """
    可折叠的分组容器。
    点击标题行切换内容区域的显示/隐藏。
    """

    def __init__(self, title: str, *, section_id: str, parent=None):
        super().__init__(parent)
        if not section_id:
            raise ValueError("section_id must be non-empty")

        self._expanded = True
        self._section_id = section_id

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._header = QLabel(f"▼ {title}")
        self._header.setStyleSheet(
            "font-weight: 700; color: #334155; font-size: 12px; "
            "padding: 6px 4px;"
        )
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.mousePressEvent = self._toggle
        lay.addWidget(self._header)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 0, 0, 0)
        self._content_layout.setSpacing(6)
        lay.addWidget(self._content)

        self._title = title
        self.set_expanded(True)

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def add_widget(self, widget: QWidget):
        self._content_layout.addWidget(widget)

    def section_id(self) -> str:
        return self._section_id

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._content.setVisible(self._expanded)
        self.updateGeometry()
        arrow = "▼" if self._expanded else "►"
        self._header.setText(f"{arrow} {self._title}")

    def _toggle(self, _event=None):
        self.set_expanded(not self._expanded)
