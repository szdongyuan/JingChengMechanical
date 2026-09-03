from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.services_container import FactoryControllerState
from model.plc_table import PlcTable


class WorkOrderModule(QWidget):
    module_id = "workorder"
    _PHASE_LABELS = {
        "not_ready": "未就绪",
        "armed": "待启动",
        "running": "录音中",
        "analyzing": "分析中",
        "reviewing": "等待复核",
        "reanalyzing": "重新分析中",
        "stopped": "已停止",
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._last_workorder_payload: dict | None = None
        self._plc_sn_values: dict[int, str] = {}
        self._plc_sn_edits: dict[int, QLineEdit] = {}
        self._plc_model_values: dict[int, str] = {}
        self._plc_model_edits: dict[int, QLineEdit] = {}
        self._rendered_plc_ids: tuple[int, ...] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.plc_sn_widget = QWidget()
        self.plc_sn_layout = QGridLayout(self.plc_sn_widget)
        self.plc_sn_layout.setContentsMargins(0, 0, 2, 0)
        self.plc_sn_layout.setHorizontalSpacing(8)
        self.plc_sn_layout.setVerticalSpacing(6)
        self.plc_sn_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.plc_sn_layout.setColumnStretch(0, 1)
        self.plc_sn_layout.setColumnStretch(1, 0)

        self.plc_workorder_scroll_area = QScrollArea()
        self.plc_workorder_scroll_area.setObjectName("plc_workorder_scroll_area")
        self.plc_workorder_scroll_area.setWidgetResizable(True)
        self.plc_workorder_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.plc_workorder_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.plc_workorder_scroll_area.setWidget(self.plc_sn_widget)

        self.lbl_phase_summary = QLabel()
        self.lbl_phase_summary.setWordWrap(True)

        layout.addWidget(self.plc_workorder_scroll_area, 1)
        layout.addWidget(self.lbl_phase_summary)

        if hasattr(self.services.bus, "plc_changed"):
            self.services.bus.plc_changed.connect(self._refresh_plc_sn_rows)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "plc_workorder_changed"):
            self.services.bus.plc_workorder_changed.connect(self._on_plc_workorder_changed)

        self._refresh_plc_sn_rows()
        self._update_phase_trigger_summary()
        initial_payload = self.value()
        self._last_workorder_payload = dict(initial_payload)
        self._cache_current_workorder_payload(initial_payload)

    def value(self) -> dict:
        state = getattr(self.services.factory_mode, "state", FactoryControllerState())
        return {
            "sn": "",
            "model": "",
            "selected_config_ids": tuple(state.selected_config_ids),
        }

    def apply(self):
        self._publish_workorder(force=True, log=True)

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._sync_factory_state(state)

    def _on_factory_display_reset(self, reason: str) -> None:
        if reason not in {"manual_judgment_ok", "manual_judgment_ng"}:
            return
        self._publish_workorder(force=True)

    def _on_plc_workorder_changed(self, payload: dict) -> None:
        sn = str(payload.get("sn") or "").strip()
        model = str(payload.get("model") or "").strip()
        plc_id = self._payload_plc_id(payload)
        if plc_id is None:
            return
        self._plc_sn_values[plc_id] = sn
        self._plc_model_values[plc_id] = model
        self._refresh_plc_sn_rows()
        for other_edit in self._plc_sn_edits.values():
            other_edit.setStyleSheet("")
        edit = self._plc_sn_edits.get(plc_id)
        if edit is not None and sn:
            edit.setStyleSheet("color: #2563eb; font-weight: 600;")

    def _sync_factory_state(
        self,
        state: FactoryControllerState,
        *,
        publish_workorder: bool = True,
    ) -> None:
        self._factory_state = state
        self._update_phase_trigger_summary()
        if publish_workorder:
            self._publish_workorder()

    def _payload_plc_id(self, payload: dict) -> int | None:
        raw_plc_id = payload.get("plc_id")
        if isinstance(raw_plc_id, bool):
            return None
        if isinstance(raw_plc_id, int) and raw_plc_id > 0:
            return raw_plc_id
        try:
            plc_id = int(str(raw_plc_id).strip())
        except (TypeError, ValueError):
            return None
        return plc_id if plc_id > 0 else None

    def _refresh_plc_sn_rows(self) -> None:
        plc_ids = self._plc_ids_for_display()
        if plc_ids != self._rendered_plc_ids:
            self._rebuild_plc_sn_rows(plc_ids)
        for plc_id, edit in self._plc_sn_edits.items():
            edit.setText(self._plc_sn_values.get(plc_id, ""))
        for plc_id, edit in self._plc_model_edits.items():
            edit.setText(self._plc_model_values.get(plc_id, ""))

    def _plc_ids_for_display(self) -> tuple[int, ...]:
        plc_ids = self._known_plc_ids()
        if plc_ids:
            self._plc_sn_values = {
                plc_id: sn
                for plc_id, sn in self._plc_sn_values.items()
                if plc_id in plc_ids
            }
            self._plc_model_values = {
                plc_id: model
                for plc_id, model in self._plc_model_values.items()
                if plc_id in plc_ids
            }
            return tuple(sorted(plc_ids))
        return tuple(sorted(self._plc_sn_values))

    def _known_plc_ids(self) -> set[int]:
        plc_ids: set[int] = set()
        for attr_name in ("plc_services", "plc_threads", "plc_state"):
            mapping = getattr(self.services, attr_name, None)
            keys = getattr(mapping, "keys", None)
            if not callable(keys):
                continue
            for raw_id in keys():
                if isinstance(raw_id, bool):
                    continue
                if isinstance(raw_id, int) and raw_id > 0:
                    plc_ids.add(raw_id)

        try:
            rows = PlcTable.select(PlcTable.id).order_by(PlcTable.id)
            for row in rows:
                plc_id = getattr(row, "id", None)
                if isinstance(plc_id, int) and plc_id > 0:
                    plc_ids.add(plc_id)
        except Exception:
            pass

        return plc_ids

    def _rebuild_plc_sn_rows(self, plc_ids: tuple[int, ...]) -> None:
        while self.plc_sn_layout.count():
            item = self.plc_sn_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._plc_sn_edits = {}
        self._plc_model_edits = {}
        self._rendered_plc_ids = tuple(plc_ids)
        if not plc_ids:
            empty_label = QLabel("未配置 PLC")
            empty_label.setObjectName("Muted")
            self.plc_sn_layout.addWidget(empty_label, 0, 0, 1, 1)
            self._update_plc_scroll_policy(plc_ids)
            return

        outer_row = 0
        for index, plc_id in enumerate(plc_ids):
            if index:
                separator = QFrame()
                separator.setObjectName("plc_group_separator")
                separator.setFrameShape(QFrame.Shape.HLine)
                separator.setFrameShadow(QFrame.Shadow.Plain)
                separator.setLineWidth(1)
                separator.setFixedHeight(13)
                separator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                separator.setStyleSheet(
                    "QFrame#plc_group_separator { color: #e2e8f0; }"
                )
                self.plc_sn_layout.addWidget(separator, outer_row, 0, 1, 1)
                outer_row += 1

            row_widget = QWidget()
            row_layout = QGridLayout(row_widget)
            row_layout.setContentsMargins(0, 1, 2, 1)
            row_layout.setSpacing(6)

            sn_label = QLabel(f"PLC{plc_id} SN码")
            sn_label.setFixedWidth(72)
            model_label = QLabel(f"PLC{plc_id} 型号")
            model_label.setFixedWidth(72)

            sn_edit = QLineEdit()
            sn_edit.setObjectName("PlcField")
            sn_edit.setReadOnly(True)
            sn_edit.setPlaceholderText("未读取")
            sn_edit.setMinimumWidth(0)
            sn_edit.setMinimumHeight(28)
            sn_edit.setMaximumHeight(30)

            model_edit = QLineEdit()
            model_edit.setObjectName("PlcField")
            model_edit.setReadOnly(True)
            model_edit.setPlaceholderText("未读取")
            model_edit.setMinimumWidth(0)
            model_edit.setMinimumHeight(28)
            model_edit.setMaximumHeight(30)

            row_layout.addWidget(sn_label, 0, 0)
            row_layout.addWidget(sn_edit, 0, 1)
            row_layout.addWidget(model_label, 1, 0)
            row_layout.addWidget(model_edit, 1, 1)
            row_layout.setColumnStretch(1, 1)
            self.plc_sn_layout.addWidget(row_widget, outer_row, 0, 1, 1)
            outer_row += 1
            self._plc_sn_edits[plc_id] = sn_edit
            self._plc_model_edits[plc_id] = model_edit

        self._update_plc_scroll_policy(plc_ids)

    def _update_plc_scroll_policy(self, plc_ids: tuple[int, ...]) -> None:
        self.plc_sn_layout.activate()
        self.plc_sn_widget.adjustSize()
        if len(plc_ids) <= 2:
            policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            self.plc_workorder_scroll_area.setMinimumHeight(
                self.plc_sn_widget.sizeHint().height()
            )
        else:
            policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded
            self.plc_workorder_scroll_area.setMinimumHeight(0)
        self.plc_workorder_scroll_area.setVerticalScrollBarPolicy(policy)

    def _update_phase_trigger_summary(self) -> None:
        display_phase = self._display_phase(self._factory_state.run_phase)
        self.lbl_phase_summary.setText(f"当前阶段：{display_phase}")

    def _display_phase(self, run_phase: str | None) -> str:
        phase = run_phase or "not_ready"
        return self._PHASE_LABELS.get(phase, phase)

    def _publish_workorder(self, *, force: bool = False, log: bool = False) -> None:
        payload = self.value()
        self._cache_current_workorder_payload(payload)
        if not force and payload == self._last_workorder_payload:
            return

        self._last_workorder_payload = dict(payload)
        self.services.bus.workorder_changed.emit(payload)
        if log:
            self.services.bus.log.emit(f"[WorkOrder] applied {payload}")

    def _cache_current_workorder_payload(self, payload: dict) -> None:
        self.services.bus.current_workorder_payload = dict(payload)
