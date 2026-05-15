from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
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


class ChangePasswordWindow(QWidget):
    def __init__(self, current_user_name: str, parent=None):
        super().__init__(parent)
        self.current_user_name = current_user_name
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("修改密码")
        self.setFixedSize(420, 300)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setObjectName("ChangePasswordWindow")
        self.setStyleSheet(APP_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        body = QWidget()
        body.setObjectName("ChangePasswordRoot")
        outer.addWidget(body)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("修改密码")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        root.addWidget(title)

        user_row = QHBoxLayout()
        user_label = QLabel("账号")
        user_label.setFixedWidth(44)
        user_row.addWidget(user_label)
        self.user_value = QLabel(self.current_user_name or "")
        user_row.addWidget(self.user_value, 1)
        root.addLayout(user_row)

        old_row = QHBoxLayout()
        old_label = QLabel("旧密码")
        old_label.setFixedWidth(44)
        old_row.addWidget(old_label)
        self.old_password_input = QLineEdit()
        self.old_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.old_password_input.setPlaceholderText("请输入旧密码")
        self.old_password_input.setMinimumHeight(34)
        self.old_password_input.returnPressed.connect(self.save_password)
        old_row.addWidget(self.old_password_input, 1)
        root.addLayout(old_row)

        new_row = QHBoxLayout()
        new_label = QLabel("新密码")
        new_label.setFixedWidth(44)
        new_row.addWidget(new_label)
        self.new_password_input = QLineEdit()
        self.new_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_password_input.setPlaceholderText("请输入新密码")
        self.new_password_input.setMinimumHeight(34)
        self.new_password_input.returnPressed.connect(self.save_password)
        new_row.addWidget(self.new_password_input, 1)
        root.addLayout(new_row)

        confirm_row = QHBoxLayout()
        confirm_label = QLabel("确认")
        confirm_label.setFixedWidth(44)
        confirm_row.addWidget(confirm_label)
        self.confirm_password_input = QLineEdit()
        self.confirm_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password_input.setPlaceholderText("请再次输入新密码")
        self.confirm_password_input.setMinimumHeight(34)
        self.confirm_password_input.returnPressed.connect(self.save_password)
        confirm_row.addWidget(self.confirm_password_input, 1)
        root.addLayout(confirm_row)

        self.save_button = QPushButton("保存")
        self.save_button.setMinimumHeight(34)
        self.save_button.clicked.connect(self.save_password)
        root.addWidget(self.save_button)

    def save_password(self):
        if not self.current_user_name:
            QMessageBox.warning(self, "提示", "当前没有登录用户。")
            return

        old_password = self.old_password_input.text().strip()
        new_password = self.new_password_input.text().strip()
        confirm_password = self.confirm_password_input.text().strip()

        if not old_password or not new_password or not confirm_password:
            QMessageBox.warning(self, "提示", "请填写完整。")
            return

        if new_password != confirm_password:
            QMessageBox.warning(self, "提示", "两次输入的新密码不一致。")
            return

        user = (
            UsersTable.select()
            .where(UsersTable.user_name == self.current_user_name)
            .first()
        )
        if not user:
            QMessageBox.warning(self, "提示", "当前用户不存在。")
            return

        old_password_sha = encrypt_password(self.current_user_name, old_password)
        if user.password != old_password_sha:
            QMessageBox.warning(self, "提示", "旧密码不正确。")
            return

        new_password_sha = encrypt_password(self.current_user_name, new_password)
        updated = (
            UsersTable.update(
                password=new_password_sha,
                user_updated_time=now_str(),
            )
            .where(UsersTable.user_name == self.current_user_name)
            .execute()
        )
        if updated == 0:
            QMessageBox.warning(self, "提示", "修改失败。")
            return

        QMessageBox.information(self, "提示", "密码修改成功。")
        self.close()
