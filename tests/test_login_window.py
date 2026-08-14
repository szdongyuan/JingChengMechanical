from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_login_window_defaults_to_operator_direct_login(qapp):
    from ui.modules.login import LoginWindow

    login_results = []
    window = LoginWindow(
        on_login_success=lambda username, role: login_results.append(
            (username, role)
        ),
        on_login_cancel=lambda: None,
    )

    try:
        assert window.role_combo.currentText() == "操作员"
        assert window.username_input.isHidden()
        assert window.password_input.isHidden()
        assert window.login_button.text() == "直接登录"

        window.login_click()

        assert login_results == [("操作员", "操作员")]
        assert window.current_login_result is True
    finally:
        window.close()


def test_login_window_switches_credential_fields_by_role(qapp):
    from ui.modules.login import LoginWindow

    window = LoginWindow(on_login_cancel=lambda: None)

    try:
        initial_size = window.size()
        window.role_combo.setCurrentText("工程师")
        assert window.size() == initial_size
        assert not window.username_input.isHidden()
        assert not window.password_input.isHidden()
        assert window.login_button.text() == "登录"

        window.username_input.setText("engineer")
        window.password_input.setText("secret")
        window.role_combo.setCurrentText("操作员")

        assert window.size() == initial_size
        assert window.username_input.isHidden()
        assert window.password_input.isHidden()
        assert window.username_input.text() == ""
        assert window.password_input.text() == ""
        assert window.login_button.text() == "直接登录"
    finally:
        window.close()
