from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from consts.style import APP_QSS
from model.users_table import UsersTable


ROLE_MAP = {
    "工程师": "Engineer",
}


class DeleteUserWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        self.load_users()

    def init_ui(self):
        self.setWindowTitle("删除用户")
        self.setFixedSize(420, 280)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setObjectName("DeleteUserWindow")
        self.setStyleSheet(APP_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        body = QWidget()
        body.setObjectName("DeleteUserRoot")
        outer.addWidget(body)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("删除用户")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(title)

        role_row = QHBoxLayout()
        role_label = QLabel("权限")
        role_label.setFixedWidth(44)
        role_row.addWidget(role_label)
        self.role_combo = QComboBox()
        self.role_combo.addItems(["工程师"])
        self.role_combo.setMinimumHeight(34)
        self.role_combo.currentIndexChanged.connect(self.load_users)
        role_row.addWidget(self.role_combo, 1)
        root.addLayout(role_row)

        user_row = QHBoxLayout()
        user_label = QLabel("账号")
        user_label.setFixedWidth(44)
        user_row.addWidget(user_label)
        self.user_combo = QComboBox()
        self.user_combo.setMinimumHeight(34)
        user_row.addWidget(self.user_combo, 1)
        root.addLayout(user_row)

        self.delete_button = QPushButton("删除")
        self.delete_button.setMinimumHeight(34)
        self.delete_button.clicked.connect(self.delete_user)
        root.addWidget(self.delete_button)

    def load_users(self):
        self.user_combo.clear()
        role_text = self.role_combo.currentText()
        db_role = ROLE_MAP.get(role_text, "Engineer")
        users = (
            UsersTable.select()
            .where(UsersTable.access_level == db_role)
            .order_by(UsersTable.user_name)
        )

        for user in users:
            self.user_combo.addItem(user.user_name, user.user_name)

        has_user = self.user_combo.count() > 0
        if not has_user:
            self.user_combo.addItem("暂无可删除用户", None)

        self.user_combo.setEnabled(has_user)
        self.delete_button.setEnabled(has_user)

    def delete_user(self):
        username = self.user_combo.currentData()
        role_text = self.role_combo.currentText()
        db_role = ROLE_MAP.get(role_text, "Engineer")
        if not username:
            QMessageBox.warning(self, "提示", "没有可删除的用户。")
            return

        reply = QMessageBox.question(
            self,
            "提示",
            f"确认删除{role_text}用户：{username}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = (
            UsersTable.delete()
            .where(
                UsersTable.user_name == username,
                UsersTable.access_level == db_role,
            )
            .execute()
        )
        if deleted == 0:
            QMessageBox.warning(self, "提示", "删除失败。              ")
            return

        QMessageBox.information(self, "提示", "删除用户成功。              ")
        self.load_users()
