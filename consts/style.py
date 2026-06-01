import os

from consts.running_consts import base_dir


_COMBO_CHEVRON_PATH = os.path.join(base_dir, "consts", "combo_chevron.svg").replace("\\", "/")

APP_QSS = """
/* Base */
QMainWindow { background: #f3f5f8; }
QWidget {
  font-family: "Segoe UI", "Microsoft YaHei UI";
  font-size: 12px;
  color: #111827;
}
QMessageBox { background: white; }

/* Popups / Dialog-like windows */
QWidget#LoginWindow,
QWidget#ChangePasswordWindow,
QWidget#AddUserWindow,
QWidget#DeleteUserWindow {
  background: white;
}
QWidget#LoginRoot,
QWidget#ChangePasswordRoot,
QWidget#AddUserRoot,
QWidget#DeleteUserRoot {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}

/* Top bar / menu */
QWidget#TopBar { background: transparent; }
QToolButton#TopTab {
  border: 1px solid transparent;
  background: transparent;
  padding: 6px 10px;
  border-radius: 8px;
  color: #334155;
}
QToolButton#TopTab:checked {
  background: #2563eb;
  color: white;
}
QToolButton#TopTab:hover:!checked {
  background: rgba(37, 99, 235, 0.10);
}
QMenu {
  background: white;
  color: #111827;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 6px;
}
QMenu::item {
  background: transparent;
  padding: 6px 14px;
  border-radius: 6px;
}
QMenu::item:selected {
  background: rgba(37, 99, 235, 0.10);
  color: #1d4ed8;
}

/* Card / group containers */
QFrame#Card {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}
QLabel#CardTitle {
  font-weight: 700;
  color: #0f172a;
  font-size: 13px;
}
QLabel#CardTitleSuffix {
  color: #0f172a;
  font-size: 13px;
}
QLabel#CardTitleMetricLabel {
  color: #0f172a;
  font-size: 15px;
}
QLabel#CardTitleMetricValue {
  background: #f8fafc;
  border: 1px solid #cbd5e1;
  border-radius: 5px;
  color: #0f172a;
  font-size: 15px;
  font-weight: 700;
  padding: 2px 10px;
}
QGroupBox {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  margin-top: 10px;
  padding-top: 6px;
}
QGroupBox::title {
  subcontrol-origin: margin;
  left: 12px;
  padding: 0 6px;
  color: #334155;
  font-weight: 700;
}

/* Labels / badges / empty state */
QLabel#Muted { color: #64748b; }
QLabel#CurrentDevice {
  color: #1d4ed8;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 8px;
}
QLabel#ResultBadge {
  padding: 6px 14px;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  background: #f8fafc;
  color: #334155;
  font-weight: 700;
  font-size: 26px;
  min-height: 36px;
}
QLabel#ResultBadge[resultLevel="pass"] {
  border: 1px solid rgba(22, 163, 74, 0.35);
  background: rgba(22, 163, 74, 0.10);
  color: #16a34a;
}
QLabel#ResultBadge[resultLevel="fail"] {
  border: 1px solid rgba(239, 68, 68, 0.35);
  background: rgba(239, 68, 68, 0.10);
  color: #ef4444;
}
QWidget#ResultMetricBadge {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
}
QLabel#ResultMetricLabel {
  color: #64748b;
  font-weight: 700;
}
QLabel#ResultMetricValue {
  color: #0f172a;
  font-size: 20px;
  font-weight: 700;
}
QWidget#EmptyState { background: transparent; }
QLabel#EmptyIcon {
  min-width: 56px;
  min-height: 56px;
  border-radius: 12px;
  background: #f1f5f9;
  color: #94a3b8;
  font-size: 26px;
  font-weight: 700;
}
QLabel#EmptyText { color: #94a3b8; }

/* Inputs */
QComboBox {
  background: #ffffff;
  color: #111827;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 6px 28px 6px 10px;
}
QComboBox::drop-down {
  border: 0px;
  width: 28px;
}
QComboBox::down-arrow {
  image: url("__COMBO_CHEVRON_PATH__");
  width: 8px;
  height: 5px;
  margin-right: 10px;
}
QComboBox QAbstractItemView {
  background: #ffffff;
  color: #111827;
  selection-background-color: rgba(37, 99, 235, 0.18);
  selection-color: #111827;
  outline: 0px;
}
QLineEdit {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
}
QLineEdit#PlcField {
  padding: 4px 8px;
}
QLineEdit:focus {
  border: 1px solid #93c5fd;
  background: white;
}
QTextEdit {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  color: #111827;
  padding: 8px;
}

/* Buttons */
QPushButton {
  border: 1px solid #d1d5db;
  background: white;
  border-radius: 8px;
  padding: 8px 10px;
}
QPushButton:disabled {
  background: #e5e7eb;
  border: 1px solid #d1d5db;
  color: #94a3b8;
}
QPushButton:hover { background: #f8fafc; }
QPushButton:pressed { background: #eef2ff; }
QPushButton#Primary {
  background: #2563eb;
  border: 1px solid #2563eb;
  color: white;
}
QPushButton#Primary:disabled {
  background: #93c5fd;
  border: 1px solid #93c5fd;
  color: #e5e7eb;
}
QPushButton#Primary:hover { background: #1d4ed8; }
QPushButton#Danger {
  background: white;
  border: 1px solid #ef4444;
  color: #ef4444;
}
QPushButton#Success {
  background: #16a34a;
  border: 1px solid #16a34a;
  color: white;
}
QPushButton#Fail {
  background: #ef4444;
  border: 1px solid #ef4444;
  color: white;
}
QPushButton#TablePlayButton {
  border-radius: 4px;
  padding: 0px;
}
QPushButton#TablePlayButton:hover {
  background: #f8fafc;
}
QPushButton#TablePlayButton:pressed {
  background: #e2e8f0;
  padding-top: 1px;
  padding-left: 1px;
}

/* Tables */
QTableWidget {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  gridline-color: #e5e7eb;
}
QTableWidget:disabled,
QTableView:disabled {
  background: #f8fafc;
  color: #94a3b8;
  gridline-color: #e5e7eb;
  selection-background-color: #e5e7eb;
  selection-color: #94a3b8;
}
QHeaderView::section {
  background: #f1f5f9;
  padding: 6px 8px;
  border: 0px;
  color: #334155;
  font-weight: 700;
}
QHeaderView::section:disabled {
  background: #f1f5f9;
  color: #94a3b8;
}
QTableView::item { padding: 6px 8px; }
""".replace("__COMBO_CHEVRON_PATH__", _COMBO_CHEVRON_PATH)
