from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QStyle, QStyleOptionFrame

from app.services_container import FactoryControllerState
from consts.style import APP_QSS
from model.plc_table import PlcTable
from ui.modules.workorder import WorkOrderModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.workorder_changed = _SignalStub()
        self.log = _SignalStub()
        self.config_changed = _SignalStub()
        self.plc_changed = _SignalStub()
        self.plc_workorder_changed = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_selection_notice = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.current_workorder_payload = {}


class _ConfigServiceStub:
    def __init__(
        self,
        configs: list[dict],
        *,
        runtime_errors_by_id: dict[int, list[str]] | None = None,
    ):
        self._configs = [dict(config) for config in configs]
        self._details = {config["config_id"]: dict(config) for config in configs}
        self._runtime_errors_by_id = {
            config_id: list(errors)
            for config_id, errors in (runtime_errors_by_id or {}).items()
        }

    def list_configs(self):
        return [
            {
                "config_id": config["config_id"],
                "config_name": config["config_name"],
            }
            for config in self._configs
        ]

    def load_config(self, config_id: int):
        return dict(self._details[config_id])

    def validate_factory_runtime(self, config: dict):
        return list(self._runtime_errors_by_id.get(config["config_id"], ()))

    def delete_config(self, config_id: int) -> bool:
        if config_id not in self._details:
            return False
        self._details.pop(config_id, None)
        self._configs = [
            config for config in self._configs if config["config_id"] != config_id
        ]
        return True


class _SharedSettingsStub:
    def __init__(
        self,
        *,
        saved_selected_ids: tuple[int, ...] = (),
        save_errors: list[str | None] | None = None,
    ):
        self._saved_selected_ids = tuple(saved_selected_ids)
        self._save_errors = list(save_errors or [])
        self.save_calls: list[tuple[int, ...]] = []

    def load_selected_config_ids(self) -> tuple[int, ...]:
        return self._saved_selected_ids

    def save_selected_config_ids(self, config_ids: tuple[int, ...]) -> str | None:
        self.save_calls.append(tuple(config_ids))
        error = self._save_errors.pop(0) if self._save_errors else None
        if error:
            return error
        self._saved_selected_ids = tuple(config_ids)
        return None


class _FactoryModeStub:
    def __init__(
        self,
        initial_state: FactoryControllerState | None = None,
        *,
        operation_order: list[tuple[str, tuple[int, ...]]] | None = None,
    ):
        self.state = initial_state or FactoryControllerState()
        self.restore_selection_calls: list[tuple[tuple[int, ...], bool]] = []
        self.set_selection_calls: list[tuple[int, ...]] = []
        self.operation_order = operation_order

    def restore_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        *,
        had_missing_configs: bool,
    ):
        selected = tuple(selected_config_ids)
        self.restore_selection_calls.append((selected, had_missing_configs))
        if self.operation_order is not None:
            self.operation_order.append(("restore_selection", selected))
        if had_missing_configs:
            self.state = FactoryControllerState(
                selected_config_ids=selected,
                run_phase="not_ready",
                selection_ready=False,
                selection_ready_reason="配置已失效",
                start_ready=False,
                start_ready_reason="配置已失效",
            )
        else:
            self.state = FactoryControllerState(
                selected_config_ids=selected,
                run_phase="armed" if selected else "not_ready",
            )
        return self.state

    def set_selection(self, selected_config_ids: tuple[int, ...] | list[int]):
        selected = tuple(selected_config_ids)
        self.set_selection_calls.append(selected)
        if self.operation_order is not None:
            self.operation_order.append(("set_selection", selected))
        self.state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if selected else "not_ready",
        )
        return self.state


class _IdleTestManagerStub:
    @staticmethod
    def is_running():
        return False


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_config(
    config_id: int,
    config_name: str,
    *,
    record_duration: float = 5.0,
    analysis_duration: float = 2.0,
    plc_device_key: str | None = None,
    plc_device_label: str | None = None,
    scan_triggers_test: bool = False,
    selected_channels: list[int] | None = None,
):
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": record_duration,
        "analysis_duration": analysis_duration,
        "analysis_window_offset_ms": 0,
        "plc_device_key": plc_device_key,
        "plc_device_label": plc_device_label,
        "scan_triggers_test": scan_triggers_test,
        "selected_channels": list(selected_channels or []),
        "channels": [],
    }


def _make_module(
    *,
    saved_selected_ids: tuple[int, ...] = (),
    shared_settings: _SharedSettingsStub | None = None,
    initial_factory_state: FactoryControllerState | None = None,
    configs: list[dict] | None = None,
    include_reset_coordinator: bool = True,
):
    bus = _BusStub()
    settings = shared_settings or _SharedSettingsStub(saved_selected_ids=saved_selected_ids)
    operation_order: list[tuple[str, tuple[int, ...]]] = []
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(
            configs
            or [
                _make_config(
                    101,
                    "Config Alpha",
                    plc_device_key="plc-a",
                    plc_device_label="PLC A",
                    selected_channels=[0, 1],
                ),
                _make_config(
                    202,
                    "Config Beta",
                    selected_channels=[1],
                ),
                _make_config(
                    303,
                    "Config Gamma",
                    scan_triggers_test=True,
                    selected_channels=[0],
                ),
            ]
        ),
        factory_selection_settings=settings,
        factory_mode=_FactoryModeStub(
            initial_factory_state,
            operation_order=operation_order,
        ),
        plc_services={},
        plc_threads={},
        plc_state={},
        operation_order=operation_order,
    )
    if include_reset_coordinator:
        def request_plc_start_signal_resets_for_configs(config_ids):
            operation_order.append(("reset", tuple(config_ids)))

        services.request_plc_start_signal_resets_for_configs = (
            request_plc_start_signal_resets_for_configs
        )
    return WorkOrderModule(services), services


def _module_with_plcs(count: int):
    module, services = _make_module()
    plc_ids = tuple(
        PlcTable.create(plc_name=f"PLC {index + 1}").id
        for index in range(count)
    )
    services.bus.plc_changed.emit()
    return module, services, plc_ids


def _workorder_rendered_text(module: WorkOrderModule) -> str:
    summary_label = getattr(module, "lbl_factory_summary", None)
    return "\n".join(
        (
            module.lbl_phase_summary.text(),
            summary_label.text() if summary_label is not None else "",
        )
    )


def test_workorder_homepage_has_no_factory_config_selector(qapp):
    selected_state = FactoryControllerState(selected_config_ids=(303, 101))
    module, _ = _make_module(initial_factory_state=selected_state)

    assert hasattr(module, "config_selector") is False
    assert hasattr(module, "factory_config_section") is False
    assert "配置选择" not in [
        label.text() for label in module.findChildren(QLabel)
    ]
    assert hasattr(module, "ed_sn") is False
    assert hasattr(module, "ed_model") is False
    assert hasattr(module, "form_layout") is False
    assert module.layout().indexOf(module.plc_workorder_scroll_area) >= 0
    assert module.plc_workorder_scroll_area.isAncestorOf(module.plc_sn_widget)
    assert hasattr(module, "ed_cfg") is False


def test_workorder_module_does_not_render_factory_summary_block(qapp):
    module, _ = _make_module(saved_selected_ids=(303, 101))

    assert hasattr(module, "lbl_factory_summary") is False
    assert "Factory summary:" not in _workorder_rendered_text(module)


def test_workorder_value_reads_authoritative_factory_state(qapp):
    selected_state = FactoryControllerState(selected_config_ids=(303, 101))
    changed_state = FactoryControllerState(selected_config_ids=(202, 303))
    module, services = _make_module(initial_factory_state=selected_state)

    assert module.value()["selected_config_ids"] == (303, 101)
    services.factory_mode.state = changed_state
    assert module.value()["selected_config_ids"] == (202, 303)
    services.bus.factory_state_changed.emit(changed_state)
    assert module.value()["selected_config_ids"] == (202, 303)


def test_workorder_state_change_republishes_ordered_selection(qapp):
    module, services = _make_module(
        initial_factory_state=FactoryControllerState(selected_config_ids=(101,))
    )
    services.bus.workorder_changed.emissions.clear()

    changed_state = FactoryControllerState(selected_config_ids=(303, 202, 101))
    services.factory_mode.state = changed_state
    services.bus.factory_state_changed.emit(changed_state)

    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert payload["selected_config_ids"] == (303, 202, 101)


def test_workorder_value_drops_manual_primary_and_apply_button(qapp):
    module, _ = _make_module()

    assert hasattr(module, "btn_apply") is False
    assert set(module.value()) == {"sn", "model", "selected_config_ids"}


def test_workorder_projection_keeps_sn_and_model_payload_empty(qapp):
    module, _ = _make_module(
        initial_factory_state=FactoryControllerState(selected_config_ids=(303, 101))
    )

    assert module.value() == {
        "sn": "",
        "model": "",
        "selected_config_ids": (303, 101),
    }


def test_plc_groups_render_full_width_in_one_column(qapp):
    module, services = _make_module()
    first = PlcTable.create(plc_name="PLC A")
    second = PlcTable.create(plc_name="PLC B")
    services.bus.plc_changed.emit()

    first_group = module.plc_sn_layout.itemAtPosition(0, 0).widget()
    separator = module.plc_sn_layout.itemAtPosition(1, 0).widget()
    second_group = module.plc_sn_layout.itemAtPosition(2, 0).widget()

    assert first_group is not None
    assert separator.objectName() == "plc_group_separator"
    assert second_group is not None
    assert module.plc_sn_layout.itemAtPosition(0, 1) is None
    assert module.plc_sn_layout.columnStretch(0) == 1
    assert module.plc_sn_layout.columnStretch(1) == 0
    assert [label.text() for label in first_group.findChildren(QLabel)] == [
        "PLC A SN码",
        "PLC A 型号",
        "PLC A 扭矩",
    ]

    first_layout = first_group.layout()
    assert first_layout.itemAtPosition(0, 0).widget().text() == "PLC A SN码"
    assert first_layout.itemAtPosition(0, 1).widget() is module._plc_sn_edits[first.id]
    assert first_layout.itemAtPosition(1, 0).widget().text() == "PLC A 型号"
    assert first_layout.itemAtPosition(1, 1).widget() is module._plc_model_edits[first.id]


@pytest.mark.parametrize(
    ("plc_count", "separator_count"),
    [(0, 0), (1, 0), (2, 1), (3, 2)],
)
def test_plc_group_separator_count_and_style(qapp, plc_count, separator_count):
    module, services = _make_module()
    for index in range(plc_count):
        PlcTable.create(plc_name=f"PLC {index + 1}")
    services.bus.plc_changed.emit()

    separators = module.plc_sn_widget.findChildren(
        QFrame,
        "plc_group_separator",
    )
    assert len(separators) == separator_count
    for separator in separators:
        assert separator.frameShape() == QFrame.Shape.HLine
        assert separator.frameShadow() == QFrame.Shadow.Plain
        assert separator.lineWidth() == 1
        assert separator.height() == 13
        assert "#e2e8f0" in separator.styleSheet()
        assert separator.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_two_plc_groups_never_show_vertical_scrollbar(qapp):
    module, _services, _plc_ids = _module_with_plcs(2)
    module.resize(420, 260)
    module.show()
    qapp.processEvents()

    scroll = module.plc_workorder_scroll_area
    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.verticalScrollBar().isVisible() is False
    assert scroll.verticalScrollBar().maximum() == 0


def test_tall_viewport_keeps_plc_groups_compact_and_top_aligned(qapp):
    module, _services, (first_id, second_id) = _module_with_plcs(2)
    module.resize(420, 400)
    module.show()
    qapp.processEvents()

    first_group = module.plc_sn_layout.itemAtPosition(0, 0).widget()
    separator = module.plc_sn_layout.itemAtPosition(1, 0).widget()
    second_group = module.plc_sn_layout.itemAtPosition(2, 0).widget()
    first_edit_gap = (
        module._plc_model_edits[first_id].geometry().top()
        - module._plc_sn_edits[first_id].geometry().bottom()
        - 1
    )
    second_edit_gap = (
        module._plc_model_edits[second_id].geometry().top()
        - module._plc_sn_edits[second_id].geometry().bottom()
        - 1
    )
    unused_height = (
        module.plc_sn_widget.height() - second_group.geometry().bottom() - 1
    )

    assert first_group.height() <= first_group.sizeHint().height() + 2
    assert second_group.height() <= second_group.sizeHint().height() + 2
    assert first_edit_gap <= first_group.layout().verticalSpacing() + 2
    assert second_edit_gap <= second_group.layout().verticalSpacing() + 2
    assert abs(first_edit_gap - second_edit_gap) <= 1
    assert separator.height() == 13
    assert unused_height >= 100


def test_plc_fields_use_compact_style_without_clipping_text(qapp):
    module, _services, _plc_ids = _module_with_plcs(2)
    module.setStyleSheet(APP_QSS)
    module.show()
    qapp.processEvents()

    edits = tuple(module._plc_sn_edits.values()) + tuple(
        module._plc_model_edits.values()
    )
    assert len(edits) == 4
    for edit in edits:
        assert edit.objectName() == "PlcField"
        assert 28 <= edit.height() <= 30
        option = QStyleOptionFrame()
        edit.initStyleOption(option)
        content = edit.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents,
            option,
            edit,
        )
        assert content.height() >= edit.fontMetrics().height() + 2


def test_three_plc_groups_scroll_only_inside_plc_viewport(qapp):
    module, _services, _plc_ids = _module_with_plcs(3)
    scroll = module.plc_workorder_scroll_area
    scroll.setFixedHeight(110)
    module.show()
    qapp.processEvents()

    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.verticalScrollBar().maximum() > 0
    assert module.layout().indexOf(module.lbl_phase_summary) > module.layout().indexOf(scroll)
    assert scroll.isAncestorOf(module.plc_sn_widget)
    assert scroll.isAncestorOf(module.lbl_phase_summary) is False


@pytest.mark.parametrize("plc_count", [0, 1, 2])
def test_zero_to_two_plcs_force_vertical_scrollbar_off(qapp, plc_count):
    module, _services, _plc_ids = _module_with_plcs(plc_count)
    assert (
        module.plc_workorder_scroll_area.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    if plc_count == 0:
        empty = module.plc_sn_layout.itemAtPosition(0, 0).widget()
        assert empty.text() == "未配置 PLC"
        assert module.plc_sn_layout.itemAtPosition(0, 1) is None


def test_scroll_policy_updates_when_plc_count_drops_to_two(qapp):
    module, services, plc_ids = _module_with_plcs(3)
    assert (
        module.plc_workorder_scroll_area.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )

    PlcTable.delete().where(PlcTable.id == plc_ids[-1]).execute()
    services.bus.plc_changed.emit()
    qapp.processEvents()

    scroll = module.plc_workorder_scroll_area
    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.verticalScrollBar().isVisible() is False
    assert scroll.verticalScrollBar().maximum() == 0


def test_cached_values_and_post_rebuild_sn_highlight_remain_correct(qapp):
    module, services, (_first_id, second_id) = _module_with_plcs(2)
    services.bus.plc_workorder_changed.emit(
        {"plc_id": second_id, "sn": "SN-B", "model": "MODEL-B"}
    )
    PlcTable.create(plc_name="PLC 3")
    services.bus.plc_changed.emit()

    assert module._plc_sn_edits[second_id].text() == "SN-B"
    assert module._plc_model_edits[second_id].text() == "MODEL-B"

    services.bus.plc_workorder_changed.emit(
        {"plc_id": second_id, "sn": "SN-B-2", "model": "MODEL-B"}
    )
    assert "#2563eb" in module._plc_sn_edits[second_id].styleSheet()


@pytest.mark.parametrize("reason", ["manual_judgment_ok", "manual_judgment_ng"])
def test_manual_judgment_reset_clears_sn_and_publishes_blank_payload(qapp, reason):
    module, services = _make_module(
        initial_factory_state=FactoryControllerState(selected_config_ids=(101,))
    )
    services.bus.workorder_changed.emissions.clear()

    services.bus.factory_display_reset.emit(reason)

    assert module.value()["selected_config_ids"] == (101,)
    assert services.bus.current_workorder_payload == {
        "sn": "",
        "model": "",
        "selected_config_ids": (101,),
    }
    assert services.bus.workorder_changed.emissions[-1][0][0]["sn"] == ""


def test_plc_workorder_display_follows_plc_add_delete_and_updates_by_plc_id(qapp):
    module, services = _make_module(saved_selected_ids=(101,))

    first = PlcTable.create(plc_name="PLC A")
    second = PlcTable.create(plc_name="PLC B")
    services.bus.plc_changed.emit()

    assert tuple(module._plc_sn_edits) == (first.id, second.id)
    assert tuple(module._plc_model_edits) == (first.id, second.id)

    services.bus.plc_workorder_changed.emit(
        {
            "plc_id": second.id,
            "sn": "SN-PLC-B",
            "model": "MODEL-B",
        }
    )

    assert module._plc_sn_edits[first.id].text() == ""
    assert module._plc_model_edits[first.id].text() == ""
    assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
    assert module._plc_model_edits[second.id].text() == "MODEL-B"
    assert "#2563eb" in module._plc_sn_edits[second.id].styleSheet()

    services.bus.plc_workorder_changed.emit(
        {
            "plc_id": first.id,
            "sn": "SN-PLC-A",
            "model": "MODEL-A",
        }
    )
    assert module._plc_model_edits[first.id].text() == "MODEL-A"
    assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
    assert module._plc_model_edits[second.id].text() == "MODEL-B"
    assert "#2563eb" in module._plc_sn_edits[first.id].styleSheet()
    assert module._plc_sn_edits[second.id].styleSheet() == ""

    third = PlcTable.create(plc_name="PLC C")
    services.bus.plc_changed.emit()

    assert tuple(module._plc_sn_edits) == (first.id, second.id, third.id)
    assert tuple(module._plc_model_edits) == (first.id, second.id, third.id)
    assert module.plc_sn_layout.itemAtPosition(0, 0).widget() is not None
    assert module.plc_sn_layout.itemAtPosition(1, 0).widget() is not None
    assert module.plc_sn_layout.itemAtPosition(2, 0).widget() is not None
    assert module.plc_sn_layout.itemAtPosition(3, 0).widget() is not None
    assert module.plc_sn_layout.itemAtPosition(4, 0).widget() is not None
    assert all(
        module.plc_sn_layout.itemAtPosition(row, 1) is None
        for row in range(5)
    )
    first_card = module.plc_sn_layout.itemAtPosition(0, 0).widget()
    first_labels = [label.text() for label in first_card.findChildren(QLabel)]
    assert first_labels == ["PLC A SN码", "PLC A 型号", "PLC A 扭矩"]
    assert module._plc_torque_edits[first.id].text() == "未启用"
    assert first_card.isAncestorOf(module._plc_model_edits[first.id])
    assert module._plc_model_edits[first.id].isReadOnly() is True
    assert module._plc_model_edits[first.id].placeholderText() == "未读取"

    third.delete_instance()
    services.bus.plc_changed.emit()

    assert tuple(module._plc_sn_edits) == (first.id, second.id)
    assert tuple(module._plc_model_edits) == (first.id, second.id)

    services.bus.plc_workorder_changed.emit(
        {"plc_id": first.id, "sn": "SN-PLC-A-2"}
    )
    assert module._plc_model_edits[first.id].text() == ""

    first.delete_instance()
    second.delete_instance()
    services.bus.plc_changed.emit()

    assert tuple(module._plc_sn_edits) == (first.id, second.id)
    assert tuple(module._plc_model_edits) == (first.id, second.id)
    assert module._plc_sn_edits[first.id].text() == "SN-PLC-A-2"
    assert module._plc_model_edits[first.id].text() == ""
    assert module._plc_sn_edits[second.id].text() == "SN-PLC-B"
    assert module._plc_model_edits[second.id].text() == "MODEL-B"
    assert hasattr(module, "ed_model") is False


@pytest.mark.parametrize(
    ("run_phase", "expected_text"),
    [
        ("not_ready", "当前阶段：未就绪"),
        ("armed", "当前阶段：待启动"),
        ("running", "当前阶段：录音中"),
        ("analyzing", "当前阶段：分析中"),
        ("reviewing", "当前阶段：等待复核"),
        ("reanalyzing", "当前阶段：重新分析中"),
        ("stopped", "当前阶段：已停止"),
    ],
)
def test_workorder_phase_summary_renders_all_known_chinese_labels(
    qapp, run_phase, expected_text
):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase=run_phase,
        )
    )

    assert module.lbl_phase_summary.text() == expected_text


def test_workorder_module_only_renders_phase_summary_without_trigger_summary_row(qapp):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            trigger_source="manual",
        )
    )

    assert hasattr(module, "lbl_trigger_summary") is False
    assert "最近触发方式" not in _workorder_rendered_text(module)
    assert module.lbl_phase_summary.text() == "当前阶段：待启动"


def test_workorder_summary_preserves_unknown_non_empty_values(qapp):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="future_phase",
            trigger_source="custom_trigger",
        )
    )

    assert module.lbl_phase_summary.text() == "当前阶段：future_phase"
    assert "最近触发方式" not in _workorder_rendered_text(module)


@pytest.mark.parametrize("name", [None, "", "  ", "  工位一  "])
def test_plc_name_uses_trimmed_configuration_or_id_fallback(qapp, name):
    row = PlcTable.create(plc_name=name)
    module, services = _make_module()
    expected = (name or "").strip() or f"PLC{row.id}"
    group = module._plc_sn_edits[row.id].parentWidget()
    assert [label.text() for label in group.findChildren(QLabel)] == [
        f"{expected} {field}" for field in ("SN码", "型号", "扭矩")
    ]


def test_same_id_rename_retains_fields_and_fits_chinese_labels(qapp):
    from app.service.torque.types import TorqueStatus

    row = PlcTable.create(plc_name="old")
    module, services = _make_module()
    services.bus.plc_workorder_changed.emit({"plc_id": row.id, "sn": "SN1", "model": "M1"})
    module._on_plc_torque_changed(TorqueStatus(row.id, 100, "plc", 3.0, "plc", "model", "M1", 1))
    edits = (module._plc_sn_edits[row.id], module._plc_model_edits[row.id], module._plc_torque_edits[row.id])
    row.plc_name = "  装配检测工位一  "
    row.save()
    services.bus.plc_changed.emit()
    assert tuple(edit.text() for edit in edits) == ("SN1", "M1", "3（PLC）")
    assert edits == (module._plc_sn_edits[row.id], module._plc_model_edits[row.id], module._plc_torque_edits[row.id])
    module.setStyleSheet(APP_QSS)
    module.resize(420, 260)
    module.show()
    qapp.processEvents()
    labels = edits[0].parentWidget().findChildren(QLabel)
    assert [label.text() for label in labels] == [f"装配检测工位一 {field}" for field in ("SN码", "型号", "扭矩")]
    for label in labels:
        assert label.width() >= label.sizeHint().width()
        assert label.contentsRect().width() >= label.fontMetrics().horizontalAdvance(label.text())
    services.bus.plc_workorder_changed.emit({"plc_id": row.id, "sn": "SN2", "model": "M2"})
    assert module._plc_sn_edits[row.id] is edits[0]
    assert edits[0].text() == "SN2"
    row.plc_name = " "
    row.save()
    services.bus.plc_changed.emit()
    assert labels[0].text() == f"PLC{row.id} SN码"
    assert edits[1].text() == "M2"
    module.close()


def test_runtime_only_plc_uses_id_fallback(qapp):
    module, services = _make_module()
    services.plc_services[42] = object()
    services.bus.plc_changed.emit()
    group = module._plc_sn_edits[42].parentWidget()
    assert [label.text() for label in group.findChildren(QLabel)] == [f"PLC42 {field}" for field in ("SN码", "型号", "扭矩")]


@pytest.mark.parametrize("valid", [True, False])
def test_settings_save_refreshes_workorder_only_after_success(qapp, monkeypatch, valid):
    from PyQt6.QtWidgets import QMessageBox
    from ui.modules.plc_settings import PlcSettingsModule

    monkeypatch.setattr(PlcSettingsModule, "start_process_param_service", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    row = PlcTable.create(plc_name="原工位", plc_open_state=0)
    module, services = _make_module()
    settings = PlcSettingsModule(services)
    services.bus.plc_workorder_changed.emit({"plc_id": row.id, "sn": "SN1", "model": "M1"})
    edit = module._plc_sn_edits[row.id]
    settings.ed_name.setText("  工位一  ")
    if not valid:
        settings.ed_rack.setText("invalid")
    services.bus.plc_changed.emissions.clear()
    settings.save_current()
    assert len(services.bus.plc_changed.emissions) == int(valid)
    expected = "工位一" if valid else "原工位"
    assert PlcTable.get_by_id(row.id).plc_name == expected
    assert edit.parentWidget().layout().itemAtPosition(0, 0).widget().text() == f"{expected} SN码"
    assert edit is module._plc_sn_edits[row.id]
    assert edit.text() == "SN1"
    assert module._plc_model_edits[row.id].text() == "M1"
    settings.close()
    module.close()
