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
        self.quit_called = False
        self.exit_code = None
        self.__class__.instances.append(self)

    def setWindowIcon(self, icon):
        self.window_icon = icon

    def exec(self):
        self.exec_called = True
        return 37 if self.exit_code is None else self.exit_code

    def quit(self):
        self.quit_called = True

    def exit(self, code=0):
        self.exit_code = code


class FakeWindow:
    def __init__(self):
        self.show_maximized_called = False

    def showMaximized(self):
        self.show_maximized_called = True


@pytest.fixture(autouse=True)
def fake_qt_startup(monkeypatch):
    FakeApplication.instances.clear()
    monkeypatch.setattr(startup_mod, "QApplication", FakeApplication)
    monkeypatch.setattr(startup_mod, "QIcon", MagicMock(side_effect=lambda path: path))
    monkeypatch.setattr(startup_mod, "asset_path", lambda name: f"asset:{name}")
    monkeypatch.setattr(startup_mod.os.path, "exists", lambda path: False)


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


def test_run_app_reports_container_startup_failure_shuts_down_once_and_skips_login(
    monkeypatch,
):
    services = MagicMock()
    services.startup_failure = "final composition prepare: output directory denied"
    monkeypatch.setattr(startup_mod, "ServiceContainer", lambda: services)
    critical = MagicMock()
    monkeypatch.setattr(
        startup_mod,
        "QMessageBox",
        MagicMock(critical=critical),
        raising=False,
    )
    login_window = MagicMock()
    main_window = MagicMock()
    monkeypatch.setattr(startup_mod, "LoginWindow", login_window)
    monkeypatch.setattr(startup_mod, "MainWindow", main_window)

    result = startup_mod.run_app(["jingcheng"])

    assert result == 1
    critical.assert_called_once()
    assert services.startup_failure in critical.call_args.args[2]
    services.shutdown.assert_called_once_with()
    login_window.assert_not_called()
    main_window.assert_not_called()
    assert FakeApplication.instances[0].exec_called is False


def test_login_with_unavailable_production_still_opens_main_window(
    monkeypatch,
):
    services = MagicMock()
    services.startup_failure = None
    services.production_unavailable_reason = (
        "生产功能不可用，请修复设备或配置后重启软件。"
    )
    monkeypatch.setattr(startup_mod, "ServiceContainer", lambda: services)
    critical = MagicMock()
    monkeypatch.setattr(
        startup_mod,
        "QMessageBox",
        MagicMock(critical=critical),
        raising=False,
    )

    class SuccessfulLogin:
        def __init__(self, *, on_login_success, **_kwargs):
            self._on_login_success = on_login_success

        def show(self):
            self._on_login_success("operator", "操作员")

    class DegradedWindow(FakeWindow):
        def __init__(self, received_services):
            super().__init__()
            assert received_services is services
            self.current_user_name = None
            self.current_user_role = None
            self.set_role = MagicMock()

    main_window = MagicMock(side_effect=DegradedWindow)
    monkeypatch.setattr(startup_mod, "LoginWindow", SuccessfulLogin)
    monkeypatch.setattr(startup_mod, "MainWindow", main_window)

    result = startup_mod.run_app(["jingcheng"])

    assert result == 37
    main_window.assert_called_once_with(services)
    critical.assert_not_called()
    services.shutdown.assert_not_called()
    assert FakeApplication.instances[0].exec_called is True


def test_login_cancel_runs_cached_application_shutdown_before_quit(monkeypatch):
    order = []
    services = MagicMock()
    services.shutdown.side_effect = lambda: order.append("shutdown")
    monkeypatch.setattr(startup_mod, "ServiceContainer", lambda: services)

    class CancelingLogin:
        def __init__(self, *, on_login_cancel, **_kwargs):
            self._on_login_cancel = on_login_cancel

        def show(self):
            self._on_login_cancel()
            order.append("cancel-returned")

    monkeypatch.setattr(startup_mod, "LoginWindow", CancelingLogin)

    assert startup_mod.run_app(["jingcheng"]) == 37

    app = FakeApplication.instances[0]
    assert order == ["shutdown", "cancel-returned"]
    services.shutdown.assert_called_once_with()
    assert app.quit_called is True
