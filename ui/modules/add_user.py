from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from consts.style import APP_QSS
from consts.time_consts import now_str
from model.users_table import UsersTable
from ui.modules.login import encrypt_password


ROLE_MAP = {
    "工程师": "Engineer",
}


class AddUserWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("添加用户")
        self.setFixedSize(420, 340)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setObjectName("AddUserWindow")
        self.setStyleSheet(APP_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        body = QWidget()
        body.setObjectName("AddUserRoot")
        outer.addWidget(body)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("添加用户")
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
        role_row.addWidget(self.role_combo, 1)
        root.addLayout(role_row)

        user_row = QHBoxLayout()
        user_label = QLabel("账号")
        user_label.setFixedWidth(44)
        user_row.addWidget(user_label)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("请输入账号")
        self.username_input.setMinimumHeight(34)
        self.username_input.returnPressed.connect(self.save_user)
        user_row.addWidget(self.username_input, 1)
        root.addLayout(user_row)

        pwd_row = QHBoxLayout()
        pwd_label = QLabel("密码")
        pwd_label.setFixedWidth(44)
        pwd_row.addWidget(pwd_label)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("请输入密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMinimumHeight(34)
        self.password_input.returnPressed.connect(self.save_user)
        pwd_row.addWidget(self.password_input, 1)
        root.addLayout(pwd_row)

        confirm_row = QHBoxLayout()
        confirm_label = QLabel("确认")
        confirm_label.setFixedWidth(44)
        confirm_row.addWidget(confirm_label)
        self.confirm_password_input = QLineEdit()
        self.confirm_password_input.setPlaceholderText("请再次输入密码")
        self.confirm_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password_input.setMinimumHeight(34)
        self.confirm_password_input.returnPressed.connect(self.save_user)
        confirm_row.addWidget(self.confirm_password_input, 1)
        root.addLayout(confirm_row)

        self.save_button = QPushButton("保存")
        self.save_button.setMinimumHeight(34)
        self.save_button.clicked.connect(self.save_user)
        root.addWidget(self.save_button)

    def save_user(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        confirm_password = self.confirm_password_input.text().strip()
        role_text = self.role_combo.currentText()

        if not username or not password or not confirm_password:
            QMessageBox.warning(self, "提示", "请填写完整。              ")
            return

        if password != confirm_password:
            QMessageBox.warning(self, "提示", "两次输入的密码不一致。")
            return

        if UsersTable.select().where(UsersTable.user_name == username).exists():
            QMessageBox.warning(self, "提示", "账号已存在。              ")
            return

        db_role = ROLE_MAP.get(role_text)
        if not db_role:
            QMessageBox.warning(self, "提示", "请选择有效权限。              ")
            return

        try:
            UsersTable.create(
                user_name=username,
                password=encrypt_password(username, password),
                access_level=db_role,
                user_created_time=now_str(),
                user_updated_time=now_str(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "提示", f"保存失败：{exc}")
            return

        QMessageBox.information(self, "提示", "添加用户成功。              ")
        self.close()
