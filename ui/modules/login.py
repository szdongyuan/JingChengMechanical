from __future__ import annotations

import hashlib

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

from app.service.log_manager import LogManager
from base.utils import get_mac_address
from consts.style import APP_QSS
from model.users_table import UsersTable


ROLE_MAP = {
    "管理员": "Admin",
    "工程师": "Engineer",
    "操作员": "Operator",
}

OPERATOR_ROLE = "操作员"
OPERATOR_USERNAME = "操作员"


class LoginWindow(QWidget):
    def __init__(self, on_login_success=None, on_login_cancel=None,  window_title="登录"):
        super().__init__()
        self.window_title = window_title
        self.on_login_success = on_login_success
        self.on_login_cancel = on_login_cancel
        self.logger = LogManager.set_log_handler("core")
        # 不作为登录状态判断, 登录状态读mainwindow的current_user_name
        self.current_login_result = False
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle(self.window_title)
        self.setFixedSize(420, 260)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setObjectName("LoginWindow")
        self.setStyleSheet(APP_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        body = QWidget()
        body.setObjectName("LoginRoot")
        outer.addWidget(body)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("智能电机检测")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")

        root.addWidget(title)

        role_row = QHBoxLayout()
        role_label = QLabel("权限")
        role_label.setFixedWidth(36)
        role_row.addWidget(role_label)
        self.role_combo = QComboBox()
        self.role_combo.addItems([OPERATOR_ROLE, "工程师", "管理员"])
        self.role_combo.setMinimumHeight(34)
        self.role_combo.currentTextChanged.connect(self._sync_role_ui)
        role_row.addWidget(self.role_combo, 1)
        root.addLayout(role_row)

        user_row = QHBoxLayout()
        self.user_label = QLabel("账号")
        self.user_label.setFixedWidth(36)
        user_row.addWidget(self.user_label)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("请输入账号")
        self.username_input.setMinimumHeight(34)
        user_row.addWidget(self.username_input, 1)
        root.addLayout(user_row)

        pwd_row = QHBoxLayout()
        self.pwd_label = QLabel("密码")
        self.pwd_label.setFixedWidth(36)
        pwd_row.addWidget(self.pwd_label)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("请输入密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMinimumHeight(34)
        pwd_row.addWidget(self.password_input, 1)
        root.addLayout(pwd_row)

        self.login_button = QPushButton("登录")
        self.login_button.setDefault(True)
        # enter
        self.username_input.returnPressed.connect(self.login_click)
        self.password_input.returnPressed.connect(self.login_click)
        self.login_button.setMinimumHeight(34)
        self.login_button.clicked.connect(self.login_click)
        root.addWidget(self.login_button)
        root.addStretch(1)
        self._sync_role_ui(self.role_combo.currentText())

    def _sync_role_ui(self, role: str):
        operator_login = role == OPERATOR_ROLE
        self.user_label.setVisible(not operator_login)
        self.username_input.setVisible(not operator_login)
        self.pwd_label.setVisible(not operator_login)
        self.password_input.setVisible(not operator_login)
        self.login_button.setText("直接登录" if operator_login else "登录")
        if operator_login:
            self.username_input.clear()
            self.password_input.clear()
        else:
            self.username_input.setFocus()

    def login_click(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        role = self.role_combo.currentText()
        if role == OPERATOR_ROLE:
            self._finish_login(OPERATOR_USERNAME, OPERATOR_ROLE)
            return

        if not username or not password:
            QMessageBox.warning(self, "提示", "请输入账号和密码。")
            return

        db_role = ROLE_MAP.get(role, "")
        pwd_sha = encrypt_password(username, password)
        user = (
            UsersTable.select()
            .where(
                UsersTable.user_name == username,
                UsersTable.password == pwd_sha,
                UsersTable.access_level == db_role,
            )
            .first()
        )
        if not user:
            QMessageBox.warning(self, "提示", "账号、密码或权限不正确。")
            return

        self._finish_login(username, role)

    def _finish_login(self, username: str, role: str):
        self.current_login_result = True
        self.logger.info(f"用户 {username} 登录成功，权限 {role}")
        if callable(self.on_login_success):
            self.on_login_success(username, role)
        # 登录成功后关闭登录窗口, 一定执行
        self.close()

    def closeEvent(self, event):
        if not self.current_login_result and callable(self.on_login_cancel):
            self.on_login_cancel()
        super().closeEvent(event)

def encrypt_password(user_name, password):
    mac_pwd = get_mac_address() + user_name + password
    sh = hashlib.sha1()
    sh.update(mac_pwd.encode("utf-8"))
    enc_pwd = sh.hexdigest()
    return enc_pwd
