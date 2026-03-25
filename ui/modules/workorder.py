from __future__ import annotations

from PyQt6.QtCore import QSignalBlocker
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services_container import FactoryControllerState
from ui.popups.analysis_overview import open_windows
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.modules.stft import StftModule


class WorkOrderModule(QWidget):
    module_id = "workorder"
    _LOCKED_SELECTION_PHASES = frozenset({"running", "analyzing", "reanalyzing"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._config_rows: list[dict] = []
        self._config_details: dict[int, dict] = {}
        self._last_workorder_payload: dict | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.ed_sn = QLineEdit()
        self.ed_sn.setPlaceholderText("请输入条码")
        self.ed_model = QLineEdit()
        self.ed_model.setPlaceholderText("请输入产品型号")
        self.ed_cfg = QLineEdit()
        self.ed_cfg.setPlaceholderText("Factory mode controlled")
        self.ed_cfg.setReadOnly(True)
        self.ed_cfg.setEnabled(False)
        self.combo_config_a = QComboBox()
        self.combo_config_b = QComboBox()
        self.combo_primary = QComboBox()
        self.lbl_selection_status = QLabel()
        self.lbl_selection_status.setWordWrap(True)
        self.lbl_phase_summary = QLabel()
        self.lbl_phase_summary.setWordWrap(True)
        self.lbl_trigger_summary = QLabel()
        self.lbl_trigger_summary.setWordWrap(True)
        self.lbl_factory_summary = QLabel()
        self.lbl_factory_summary.setWordWrap(True)

        form = QFormLayout()
        form.addRow("条码 SN", self.ed_sn)
        form.addRow("产品型号 Model", self.ed_model)
        form.addRow("测试配置 Config", self.ed_cfg)
        form.addRow("配置 A Config A", self.combo_config_a)
        form.addRow("配置 B Config B", self.combo_config_b)
        form.addRow("手动主配置 Manual Primary", self.combo_primary)
        lay.addLayout(form)

        lay.addWidget(self.lbl_selection_status)
        lay.addWidget(self.lbl_phase_summary)
        lay.addWidget(self.lbl_trigger_summary)
        lay.addWidget(self.lbl_factory_summary)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setObjectName("Primary")

        # self.btn_analysis = QPushButton("显示所有分析项")

        row.addWidget(self.btn_apply)
        # row.addWidget(self.btn_analysis)
        row.addStretch(1)
        lay.addLayout(row)

        self.btn_apply.clicked.connect(self.apply)
        # self.btn_analysis.clicked.connect(self.open_analysis_overview)
        self.ed_sn.textChanged.connect(lambda _text: self._publish_workorder())
        self.ed_model.textChanged.connect(lambda _text: self._publish_workorder())
        self.combo_config_a.currentIndexChanged.connect(
            lambda _index: self._on_config_selection_changed()
        )
        self.combo_config_b.currentIndexChanged.connect(
            lambda _index: self._on_config_selection_changed()
        )
        self.combo_primary.currentIndexChanged.connect(
            lambda _index: self._on_primary_selection_changed()
        )
        if hasattr(self.services.bus, "config_changed"):
            self.services.bus.config_changed.connect(self._reload_config_options)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)

        self._analysis_windows = []
        self._reload_config_options()
        self._sync_factory_state(self._factory_state, publish_workorder=False)
        self._cache_current_workorder_payload(self.value())

    def value(self) -> dict:
        state = self._factory_state
        selected_config_ids = tuple(state.selected_config_ids)
        manual_primary_config_id = state.manual_primary_config_id
        if manual_primary_config_id not in selected_config_ids:
            manual_primary_config_id = None
        return {
            "sn": self.ed_sn.text().strip(),
            "model": self.ed_model.text().strip(),
            "selected_config_ids": selected_config_ids,
            "manual_primary_config_id": manual_primary_config_id,
        }

    def apply(self):
        self._publish_workorder(force=True, log=True)

    def open_analysis_overview(self):
        items = [
            ("频谱 (Spectrum)", SpectrumModule),
            ("STFT 频谱图 (Spectrogram)", StftModule),
            ("SPL 时间曲线 (A计权)", SplModule),
            ("倍频程 (Octave)", OctaveModule),
        ]
        self._analysis_windows = open_windows(self.services, items, prev=self._analysis_windows)

    def _on_config_selection_changed(self):
        selected_a = self.combo_config_a.currentData()
        selected_b = self.combo_config_b.currentData()
        primary_id = self.combo_primary.currentData()

        if selected_a is not None and selected_a == selected_b:
            selected_b = None

        if primary_id not in {selected_a, selected_b}:
            primary_id = None

        self._apply_selection_controls(
            selected_a=selected_a,
            selected_b=selected_b,
            primary_id=primary_id,
        )
        self._emit_selection_intent()

    def _on_primary_selection_changed(self):
        primary_id = self.combo_primary.currentData()
        if primary_id not in self._selected_pair():
            primary_id = None
        self._apply_selection_controls(
            selected_a=self.combo_config_a.currentData(),
            selected_b=self.combo_config_b.currentData(),
            primary_id=primary_id,
        )
        self._emit_selection_intent()

    def _reload_config_options(self):
        self._config_rows = self._list_config_rows()
        self._config_details = {}
        current_a, current_b, current_primary = self._selection_controls_from_state()
        next_a, next_b, next_primary = self._normalized_selection_controls(
            selected_a=current_a,
            selected_b=current_b,
            primary_id=current_primary,
        )
        self._apply_selection_controls(
            selected_a=next_a,
            selected_b=next_b,
            primary_id=next_primary,
        )
        if (current_a, current_b, current_primary) != (next_a, next_b, next_primary):
            self._emit_selection_intent()

    def _list_config_rows(self) -> list[dict]:
        config_service = getattr(self.services, "config_service", None)
        list_configs = getattr(config_service, "list_configs", None)
        if not callable(list_configs):
            return []
        try:
            return list(list_configs())
        except Exception:
            return []

    def _apply_selection_controls(
        self,
        *,
        selected_a: int | None,
        selected_b: int | None,
        primary_id: int | None,
    ) -> None:
        selected_a, selected_b, primary_id = self._normalized_selection_controls(
            selected_a=selected_a,
            selected_b=selected_b,
            primary_id=primary_id,
        )

        primary_candidates = tuple(
            config_id
            for config_id in (selected_a, selected_b)
            if config_id is not None
        )

        blockers = [
            QSignalBlocker(self.combo_config_a),
            QSignalBlocker(self.combo_config_b),
            QSignalBlocker(self.combo_primary),
        ]
        self._populate_config_combo(
            self.combo_config_a,
            selected_id=selected_a,
            excluded_id=selected_b,
            placeholder="请选择 Config A",
        )
        self._populate_config_combo(
            self.combo_config_b,
            selected_id=selected_b,
            excluded_id=selected_a,
            placeholder="请选择 Config B",
        )
        self._populate_primary_combo(primary_candidates, primary_id)
        del blockers
        self._update_selection_status()
        self._update_cfg_display()
        self._update_summary_text()
        self._sync_selector_lock_state()

    def _normalized_selection_controls(
        self,
        *,
        selected_a: int | None,
        selected_b: int | None,
        primary_id: int | None,
    ) -> tuple[int | None, int | None, int | None]:
        available_ids = {
            config_id
            for config_id in (row.get("config_id") for row in self._config_rows)
            if config_id is not None
        }
        available_ids.update(self._controller_locked_selection_ids())
        if selected_a not in available_ids:
            selected_a = None
        if selected_b not in available_ids:
            selected_b = None
        if selected_a == selected_b:
            selected_b = None

        if primary_id not in {selected_a, selected_b}:
            primary_id = None
        return selected_a, selected_b, primary_id

    def _populate_config_combo(
        self,
        combo: QComboBox,
        *,
        selected_id: int | None,
        excluded_id: int | None,
        placeholder: str,
    ) -> None:
        combo.clear()
        combo.addItem(placeholder, None)
        listed_ids = set()
        for row in self._config_rows:
            config_id = row.get("config_id")
            if config_id is None or config_id == excluded_id:
                continue
            combo.addItem(self._config_name(config_id), config_id)
            listed_ids.add(config_id)

        if (
            selected_id is not None
            and selected_id != excluded_id
            and selected_id not in listed_ids
        ):
            combo.addItem(self._config_name(selected_id), selected_id)

        index = combo.findData(selected_id)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _populate_primary_combo(
        self,
        primary_candidates: tuple[int | None, ...],
        primary_id: int | None,
    ) -> None:
        self.combo_primary.clear()
        self.combo_primary.addItem("请选择主配置", None)
        slots = ("A", "B")
        for slot, config_id in zip(slots, primary_candidates):
            if config_id is None:
                continue
            self.combo_primary.addItem(
                f"Config {slot} - {self._config_name(config_id)}",
                config_id,
            )

        index = self.combo_primary.findData(primary_id)
        self.combo_primary.setCurrentIndex(index if index >= 0 else 0)

    def _emit_selection_intent(self) -> None:
        factory_mode = getattr(self.services, "factory_mode", None)
        set_selection = getattr(factory_mode, "set_selection", None)
        if not callable(set_selection):
            self._update_summary_text()
            return

        next_state = set_selection(
            self._selected_pair(),
            manual_primary_config_id=self._selected_primary_config_id(),
        )
        if isinstance(next_state, FactoryControllerState):
            self._sync_factory_state(next_state)
        else:
            self._update_summary_text()

    def _on_factory_state_changed(self, state):
        if isinstance(state, FactoryControllerState):
            self._sync_factory_state(state)

    def _sync_factory_state(
        self,
        state: FactoryControllerState,
        *,
        publish_workorder: bool = True,
    ) -> None:
        self._factory_state = state
        selected_a, selected_b, primary_id = self._selection_controls_from_state(state)
        normalized_selection = self._normalized_selection_controls(
            selected_a=selected_a,
            selected_b=selected_b,
            primary_id=primary_id,
        )
        self._apply_selection_controls(
            selected_a=normalized_selection[0],
            selected_b=normalized_selection[1],
            primary_id=normalized_selection[2],
        )
        self._update_phase_trigger_summary()
        if (
            state.run_phase not in self._LOCKED_SELECTION_PHASES
            and (selected_a, selected_b, primary_id) != normalized_selection
        ):
            self._emit_selection_intent()
        if publish_workorder:
            self._publish_workorder()

    def _selection_controls_from_state(
        self,
        state: FactoryControllerState | None = None,
    ) -> tuple[int | None, int | None, int | None]:
        state = state or self._factory_state
        selected_config_ids = tuple(state.selected_config_ids)
        selected_a = selected_config_ids[0] if len(selected_config_ids) >= 1 else None
        selected_b = selected_config_ids[1] if len(selected_config_ids) >= 2 else None

        primary_id = state.manual_primary_config_id
        if primary_id not in {selected_a, selected_b}:
            primary_id = None
        return selected_a, selected_b, primary_id

    def _sync_selector_lock_state(self) -> None:
        is_locked = self._factory_state.run_phase in self._LOCKED_SELECTION_PHASES
        self.combo_config_a.setEnabled(not is_locked)
        self.combo_config_b.setEnabled(not is_locked)
        self.combo_primary.setEnabled(not is_locked and len(self._selected_pair()) == 2)

    def _selected_pair(self) -> tuple[int, ...]:
        selected = []
        for config_id in (self.combo_config_a.currentData(), self.combo_config_b.currentData()):
            if config_id is None or config_id in selected:
                continue
            selected.append(config_id)
        return tuple(selected)

    def _selected_primary_config_id(self) -> int | None:
        primary_id = self.combo_primary.currentData()
        return primary_id if primary_id in self._selected_pair() else None

    def _controller_locked_selection_ids(self) -> set[int]:
        if self._factory_state.run_phase not in self._LOCKED_SELECTION_PHASES:
            return set()
        return {
            config_id
            for config_id in self._factory_state.selected_config_ids
            if config_id is not None
        }

    def _update_selection_status(self) -> None:
        if self._factory_state.selection_ready:
            text = "Factory selection: 已就绪"
        else:
            reason = self._factory_state.selection_ready_reason or "工厂模式未就绪"
            text = f"Factory selection: {reason}"
        self.lbl_selection_status.setText(text)

    def _update_phase_trigger_summary(self) -> None:
        run_phase = self._factory_state.run_phase or "not_ready"
        trigger_source = self._factory_state.trigger_source or "-"
        self.lbl_phase_summary.setText(f"Current phase: {run_phase}")
        self.lbl_trigger_summary.setText(f"Current/last trigger: {trigger_source}")

    def _update_cfg_display(self) -> None:
        primary_id = self._factory_state.manual_primary_config_id
        if primary_id is None:
            self.ed_cfg.setText("Factory mode selection controls")
            return
        self.ed_cfg.setText(self._config_name(primary_id))

    def _update_summary_text(self) -> None:
        selected_pair = self._factory_state.selected_config_ids
        primary_id = self._factory_state.manual_primary_config_id
        if not selected_pair:
            self.lbl_factory_summary.setText("Factory summary: select Config A and Config B.")
            return

        lines = []
        for index, config_id in enumerate(selected_pair):
            config = self._config_detail(config_id)
            if not config:
                continue
            slot = "AB"[index] if index < 2 else str(index + 1)
            lines.append(
                (
                    f"Config {slot}: {self._config_name(config_id)} | "
                    f"Scan: {self._scan_summary(config)} | "
                    f"PLC: {self._plc_summary(config)} | "
                    f"Channels: {self._channels_summary(config)} | "
                    f"Record: {self._duration_text(config.get('record_duration'))} | "
                    f"Analysis: {self._duration_text(config.get('analysis_duration'))}"
                )
            )

        if primary_id is None:
            lines.append("Manual primary: pending confirmation")
        else:
            lines.append(f"Manual primary: {self._config_name(primary_id)}")

        self.lbl_factory_summary.setText("\n".join(lines))

    def _config_name(self, config_id: int) -> str:
        for row in self._config_rows:
            if row.get("config_id") == config_id:
                return row.get("config_name") or f"Config {config_id}"
        detail = self._config_detail(config_id)
        return detail.get("config_name") or f"Config {config_id}"

    def _config_detail(self, config_id: int) -> dict:
        if config_id in self._config_details:
            return self._config_details[config_id]

        detail = {}
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if callable(load_config):
            try:
                detail = dict(load_config(config_id))
            except Exception:
                detail = {}
        detail.setdefault("config_id", config_id)
        detail.setdefault("config_name", self._config_name_fallback(config_id))
        self._config_details[config_id] = detail
        return detail

    def _config_name_fallback(self, config_id: int) -> str:
        for row in self._config_rows:
            if row.get("config_id") == config_id:
                return row.get("config_name") or f"Config {config_id}"
        return f"Config {config_id}"

    @staticmethod
    def _plc_summary(config: dict) -> str:
        return config.get("plc_device_label") or config.get("plc_device_key") or "-"

    @staticmethod
    def _scan_summary(config: dict) -> str:
        return "enabled" if bool(config.get("scan_triggers_test")) else "disabled"

    @staticmethod
    def _channels_summary(config: dict) -> str:
        channels = config.get("selected_channels") or []
        if not channels:
            return "-"
        return ", ".join(f"CH{channel_index + 1}" for channel_index in channels)

    @staticmethod
    def _duration_text(value) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.1f}s"
        return "-"

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
