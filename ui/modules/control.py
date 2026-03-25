from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState


class ControlModule(QWidget):
    module_id = "control"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._workorder = dict(getattr(self.services.bus, "current_workorder_payload", {}) or {})
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        row1 = QHBoxLayout()
        self.btn_start = QPushButton("▷ 自动开始")
        self.btn_start.setObjectName("Primary")
        self.btn_stop = QPushButton("□ 停止测试")
        self.btn_stop.setObjectName("Danger")
        row1.addWidget(self.btn_start)
        row1.addWidget(self.btn_stop)
        lay.addLayout(row1)

        self.lbl_start_reason = QLabel()
        self.lbl_start_reason.setWordWrap(True)
        lay.addWidget(self.lbl_start_reason)

        row2 = QHBoxLayout()
        self.btn_pass = QPushButton("√ 手工合格")
        self.btn_pass.setObjectName("Success")
        self.btn_fail = QPushButton("× 手工不合格")
        self.btn_fail.setObjectName("Fail")
        row2.addWidget(self.btn_pass)
        row2.addWidget(self.btn_fail)
        lay.addLayout(row2)

        self.services.bus.workorder_changed.connect(self._on_workorder)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_pass.clicked.connect(lambda: self.services.bus.log.emit("Manual judgment: PASS"))
        self.btn_fail.clicked.connect(lambda: self.services.bus.log.emit("Manual judgment: FAIL"))
        self._sync_start_state()

    def _on_workorder(self, wo: dict):
        self._workorder = wo or {}
        self._sync_start_state()

    def _on_factory_state_changed(self, state):
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
            self._sync_start_state()

    def _start(self):
        if not self.btn_start.isEnabled():
            return
        factory_mode = getattr(self.services, "factory_mode", None)
        start_manual = getattr(factory_mode, "start_manual", None)
        if callable(start_manual):
            start_manual(self._workorder)

    def _stop(self):
        factory_mode = getattr(self.services, "factory_mode", None)
        stop_active_run = getattr(factory_mode, "stop_active_run", None)
        if callable(stop_active_run):
            stop_active_run()

    def _sync_start_state(self) -> None:
        can_start = bool(self._factory_state.start_ready)
        reason = self._factory_state.start_ready_reason or "工厂模式未就绪"
        self.btn_start.setEnabled(can_start)
        self.lbl_start_reason.setText("" if can_start else f"Start blocked: {reason}")

    def _start_disabled_reason(self) -> str | None:
        return self._factory_state.start_ready_reason

