APP_QSS = """
QMainWindow { background: #f3f5f8; }
QWidget { font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 12px; color: #111827; }

QWidget#TopBar { background: transparent; }
QWidget#LoginWindow {
  background: white;
}
QWidget#LoginRoot {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}
QWidget#ChangePasswordWindow {
  background: white;
}
QWidget#ChangePasswordRoot {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}
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

QTableWidget {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  gridline-color: #e5e7eb;
}
QHeaderView::section {
  background: #f1f5f9;
  padding: 6px 8px;
  border: 0px;
  color: #334155;
  font-weight: 700;
}
QTableView::item { padding: 6px 8px; }

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

QLineEdit {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
}
QLineEdit:focus {
  border: 1px solid #93c5fd;
  background: white;
}

QPushButton {
  border: 1px solid #d1d5db;
  background: white;
  border-radius: 8px;
  padding: 8px 10px;
}
QPushButton:hover { background: #f8fafc; }
QPushButton:pressed { background: #eef2ff; }

QPushButton#Primary {
  background: #2563eb;
  border: 1px solid #2563eb;
  color: white;
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

QTextEdit {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  color: #111827;
  padding: 8px;
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

QMessageBox { background: white; }
"""
combo_qss = (
            "QComboBox{background:#ffffff; color:#111827; border:1px solid #e5e7eb;"
            "border-radius:8px; padding:6px 10px;}"
            "QComboBox::drop-down{border:0px; width:28px;}"
            "QComboBox QAbstractItemView{background:#ffffff; color:#111827;"
            "selection-background-color:rgba(37,99,235,0.18); selection-color:#111827;"
            "outline:0px;}"
        )
