import os
import sys
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.services_container import ServiceContainer
from base.utils import asset_path
from ui.modules.login import LoginWindow


def main():
    app = QApplication(sys.argv)

    icon_path = asset_path("logo.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    services = ServiceContainer()

    # 主窗口在登录成功后才创建；用变量持有引用，避免被 Python 回收导致界面消失。
    main_window = None

    def on_login_success(username: str, role: str):
        # 内层函数要修改外层变量时必须声明 nonlocal，否则赋值会变成局部变量。
        nonlocal main_window
        main_window = MainWindow(services)
        main_window.current_user_name = username
        main_window.current_user_role = role
        main_window.set_role()
        main_window.showMaximized()

    def on_login_cancel():
        # 未登录就关闭登录框（点关闭按钮）：结束应用事件循环，进程退出。
        app.quit()

    # 登录框：校验通过后回调 on_login_success；关闭且未成功登录时回调 on_login_cancel。
    login_window = LoginWindow(
        on_login_success=on_login_success,
        on_login_cancel=on_login_cancel,
        window_title="登录",
    )
    login_window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()