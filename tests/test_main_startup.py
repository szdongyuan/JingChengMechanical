from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import main as startup_mod
from app.service.db_connection_service import DatabaseStartupError


class FakeApplication:
    instances: list["FakeApplication"] = []

    def __init__(self, argv):
        self.argv = argv
        self.exec_called = False
        self.window_icon = None
        self.__class__.instances.append(self)

    def setWindowIcon(self, icon):
        self.window_icon = icon

    def exec(self):
        self.exec_called = True
        return 37


class FakeWindow:
    def __init__(self):
        self.show_maximized_called = False
        self.set_role_called = False

    def showMaximized(self):
        self.show_maximized_called = True

    def set_role(self):
        self.set_role_called = True


class FakeLoginWindow:
    instances: list["FakeLoginWindow"] = []

    def __init__(self, *, on_login_success, on_login_cancel, window_title):
        self.on_login_success = on_login_success
        self.on_login_cancel = on_login_cancel
        self.window_title = window_title
        self.show_called = False
        self.__class__.instances.append(self)

    def show(self):
        self.show_called = True
        self.on_login_success("tester", "管理员")


@pytest.fixture(autouse=True)
def fake_qt_startup(monkeypatch, tmp_path):
    FakeApplication.instances.clear()
    FakeLoginWindow.instances.clear()
    monkeypatch.setattr(startup_mod, "QApplication", FakeApplication)
    monkeypatch.setattr(startup_mod, "QIcon", MagicMock(side_effect=lambda path: path))
    monkeypatch.setattr(startup_mod, "LoginWindow", FakeLoginWindow)
    monkeypatch.setattr(startup_mod, "asset_path", lambda name: str(tmp_path / name))


@pytest.mark.parametrize(
    "startup_error",
    [
        DatabaseStartupError(
            "Database schema is incomplete: missing required runtime tables"
        ),
        DatabaseStartupError("Database file does not exist: data/jingchen.db"),
    ],
)
def test_run_app_shows_database_dialog_and_stops_before_main_window(
    monkeypatch,
    startup_error,
):
    critical = MagicMock()
    monkeypatch.setattr(
        startup_mod,
        "QMessageBox",
        MagicMock(critical=critical),
        raising=False,
    )
    monkeypatch.setattr(
        startup_mod,
        "ServiceContainer",
        MagicMock(side_effect=startup_error),
    )
    main_window = MagicMock()
    monkeypatch.setattr(startup_mod, "MainWindow", main_window)

    result = startup_mod.run_app(["jingcheng"])

    assert result == 1
    assert len(FakeApplication.instances) == 1
    assert FakeApplication.instances[0].exec_called is False
    critical.assert_called_once()
    _, title, message = critical.call_args.args
    assert title == "数据库启动失败"
    assert "数据库文件缺失或不完整" in message
    assert "软件无法启动" in message
    assert str(startup_error) in message
    assert "jingchen.db" in message
    assert "重新启动" in message
    main_window.assert_not_called()


def test_run_app_propagates_unrelated_startup_errors_without_database_dialog(
    monkeypatch,
):
    startup_error = RuntimeError("other startup bug")
    critical = MagicMock()
    monkeypatch.setattr(
        startup_mod,
        "QMessageBox",
        MagicMock(critical=critical),
        raising=False,
    )
    monkeypatch.setattr(
        startup_mod,
        "ServiceContainer",
        MagicMock(side_effect=startup_error),
    )
    main_window = MagicMock()
    monkeypatch.setattr(startup_mod, "MainWindow", main_window)

    with pytest.raises(RuntimeError, match="other startup bug"):
        startup_mod.run_app(["jingcheng"])

    assert len(FakeApplication.instances) == 1
    assert FakeApplication.instances[0].exec_called is False
    critical.assert_not_called()
    main_window.assert_not_called()


def test_run_app_success_constructs_main_window_and_returns_event_loop_code(
    monkeypatch,
):
    services = object()
    window = FakeWindow()
    critical = MagicMock()
    service_container = MagicMock(return_value=services)
    main_window = MagicMock(return_value=window)
    monkeypatch.setattr(
        startup_mod,
        "QMessageBox",
        MagicMock(critical=critical),
        raising=False,
    )
    monkeypatch.setattr(startup_mod, "ServiceContainer", service_container)
    monkeypatch.setattr(startup_mod, "MainWindow", main_window)

    result = startup_mod.run_app(["jingcheng", "--factory"])

    assert result == 37
    assert FakeApplication.instances[0].argv == ["jingcheng", "--factory"]
    assert len(FakeLoginWindow.instances) == 1
    assert FakeLoginWindow.instances[0].window_title == "登录"
    assert FakeLoginWindow.instances[0].show_called is True
    service_container.assert_called_once_with()
    main_window.assert_called_once_with(services)
    assert window.current_user_name == "tester"
    assert window.current_user_role == "管理员"
    assert window.set_role_called is True
    assert window.show_maximized_called is True
    assert FakeApplication.instances[0].exec_called is True
    critical.assert_not_called()
