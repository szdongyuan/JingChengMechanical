from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLineEdit

from app.service.torque.types import TorqueConfig, TorqueConfigApplied, TorqueStatus
from app.service.torque.validation import validate_torque
from app.services_container import EventBus, FactoryControllerState
from model.plc_table import PlcTable
from ui.modules.workorder import WorkOrderModule


@pytest.fixture
def make_module(qapp):
    modules = []

    def make(count=1, enabled=True):
        ids = tuple(PlcTable.create(plc_name=f"PLC {i + 1}").id for i in range(count))
        configs = {plc_id: TorqueConfig(plc_id, enabled=enabled) for plc_id in ids}
        services = SimpleNamespace(
            bus=EventBus(), factory_mode=SimpleNamespace(state=FactoryControllerState()),
            plc_services={}, plc_threads={}, plc_state={},
            torque_store=SimpleNamespace(get_config=lambda plc_id: configs.get(plc_id, TorqueConfig(plc_id))),
        )
        module = WorkOrderModule(services)
        modules.append(module)
        return module, services, ids, configs

    yield make
    for module in modules:
        module.close()
        module.deleteLater()
    qapp.processEvents()


def emit(services, plc_id, trigger_ns, state, value=None, source=None, **kwargs):
    status = TorqueStatus(plc_id, trigger_ns, state, value, source,
                          kwargs.pop("match_type", "model"),
                          kwargs.pop("match_key", "M100"), 1, **kwargs)
    services.bus.plc_torque_changed.emit(status)
    return status


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("enabled, expected", [(False, "未启用"), (True, "待读取")])
def test_readonly_torque_row_below_each_model(make_module, count, enabled, expected, qapp):
    module, _, ids, _ = make_module(count, enabled)
    module.resize(420, 400)
    module.show()
    qapp.processEvents()
    assert tuple(module._plc_torque_edits) == ids
    for plc_id in ids:
        edit = module._plc_torque_edits[plc_id]
        assert isinstance(edit, QLineEdit) and edit.isReadOnly()
        assert edit.text() == expected
        group = edit.parentWidget()
        assert group.layout().itemAtPosition(2, 0).widget().text() == f"PLC {ids.index(plc_id) + 1} 扭矩"
        assert group.layout().itemAtPosition(2, 1).widget() is edit
        assert edit.geometry().top() > module._plc_model_edits[plc_id].geometry().bottom()
        assert "与 PLC 单位一致" in edit.toolTip()
    scroll = module.plc_workorder_scroll_area
    if count <= 2:
        assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert scroll.verticalScrollBar().maximum() == 0
    else:
        scroll.setFixedHeight(110)
        qapp.processEvents()
        assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert scroll.verticalScrollBar().maximum() > 0


@pytest.mark.parametrize("state,value,source,expected", [
    ("reading", None, None, "读取中"),
    ("plc", 3.0, "plc", "3（PLC）"),
    ("table_pending", 2.5, "table", "2.5（软件表，回传中）"),
    ("table_sent", 2.5, "table", "2.5（软件表，已回传）"),
    ("write_error", 2.5, "table", "2.5（软件表，回传失败）"),
    ("read_error", None, None, "读取失败"),
    ("invalid_value", None, None, "PLC 扭矩无效"),
    ("config_error", None, None, "扭矩配置错误"),
    ("unmatched", None, None, "未匹配（型号：M100）"),
])
def test_new_cycle_replaces_old_success_with_exact_status(make_module, state, value, source, expected):
    module, services, (plc_id,), _ = make_module()
    emit(services, plc_id, 100, "table_sent", 99.5, "table")
    emit(services, plc_id, 200, state, value, source, detail="本次详细信息")
    edit = module._plc_torque_edits[plc_id]
    assert edit.text() == expected
    assert "99.5" not in edit.toolTip()
    assert "本次详细信息" in edit.toolTip()


@pytest.mark.parametrize("match_type,key,expected", [
    ("model", "", "未匹配（型号为空）"),
    ("sn", "", "未匹配（SN为空）"),
    ("sn", "000123", "未匹配（SN：000123）"),
])
def test_unmatched_uses_cycle_key_not_live_workorder(make_module, match_type, key, expected):
    module, services, (plc_id,), _ = make_module()
    services.bus.plc_workorder_changed.emit({"plc_id": plc_id, "sn": "new", "model": "new"})
    emit(services, plc_id, 100, "unmatched", match_type=match_type, match_key=key)
    assert module._plc_torque_edits[plc_id].text() == expected


def test_late_outcomes_cannot_replace_newer_cycle_and_plcs_are_isolated(make_module):
    module, services, (first, second), _ = make_module(2)
    emit(services, first, 200, "reading")
    emit(services, first, 100, "table_sent", 2.5, "table")
    emit(services, second, 100, "plc", 3.0, "plc")
    assert module._plc_torque_edits[first].text() == "读取中"
    assert module._plc_torque_edits[second].text() == "3（PLC）"
    emit(services, first, 200, "table_pending", 2.5, "table")
    assert module._plc_torque_edits[first].text() == "2.5（软件表，回传中）"
    emit(services, first, 200, "table_sent", 2.5, "table")
    emit(services, first, 300, "config_error")
    emit(services, first, 200, "reading")
    emit(services, first, 200, "table_sent", 2.5, "table")
    assert module._plc_torque_edits[first].text() == "扭矩配置错误"
    # Repeated nonzero PLC values remain valid on successive cycles.
    emit(services, second, 200, "reading")
    emit(services, second, 200, "plc", 3.0, "plc")
    assert module._plc_torque_edits[second].text() == "3（PLC）"


@pytest.mark.parametrize("value", [0.0, -2.5, 2.6, 1e-40, 3.4028235e38])
def test_numeric_display_is_readable_and_preserves_real(make_module, value):
    module, services, (plc_id,), _ = make_module()
    value = validate_torque(value)
    emit(services, plc_id, 100, "table_sent", value, "table")
    numeric = module._plc_torque_edits[plc_id].text().split("（")[0]
    assert validate_torque(numeric) == value
    if value == validate_torque(2.6):
        assert numeric == "2.6"


def test_config_changes_refresh_idle_but_preserve_fixed_cycle(make_module):
    module, services, (first, second), configs = make_module(2, enabled=False)
    configs[first] = replace(configs[first], enabled=True, revision=1)
    services.bus.plc_torque_config_saved.emit(configs[first])
    assert module._plc_torque_edits[first].text() == "待读取"
    assert module._plc_torque_edits[second].text() == "未启用"
    configs[first] = replace(configs[first], enabled=False, revision=2)
    services.bus.plc_torque_config_applied.emit(TorqueConfigApplied(first, "generation", 2, True))
    assert module._plc_torque_edits[first].text() == "未启用"
    # The actual event is authoritative even if the saved config is now disabled.
    emit(services, first, 100, "table_pending", 2.5, "table")
    configs[first] = replace(configs[first], enabled=True, match_type="sn", revision=3)
    services.bus.plc_torque_config_saved.emit(configs[first])
    services.bus.plc_torque_config_applied.emit(TorqueConfigApplied(first, "new", 3, False, "地址冲突"))
    assert module._plc_torque_edits[first].text() == "2.5（软件表，回传中）"
    emit(services, first, 100, "table_sent", 2.5, "table")
    configs[first] = replace(configs[first], enabled=False, revision=4)
    services.bus.plc_torque_config_saved.emit(configs[first])
    assert module._plc_torque_edits[first].text() == "2.5（软件表，已回传）"


def test_refresh_and_reconnect_preserve_cycle_and_sn_model(make_module):
    module, services, (plc_id,), _ = make_module()
    services.bus.plc_workorder_changed.emit({"plc_id": plc_id, "sn": "000123", "model": "M100"})
    emit(services, plc_id, 100, "table_sent", 2.5, "table")
    PlcTable.create(plc_name="new")
    services.bus.plc_changed.emit()
    services.plc_state[plc_id] = "disconnected"
    services.bus.plc_changed.emit()
    services.plc_state[plc_id] = "connected"
    services.bus.plc_changed.emit()
    assert module._plc_sn_edits[plc_id].text() == "000123"
    assert module._plc_model_edits[plc_id].text() == "M100"
    assert module._plc_torque_edits[plc_id].text() == "2.5（软件表，已回传）"
    emit(services, plc_id, 200, "reading")
    assert module._plc_torque_edits[plc_id].text() == "读取中"
    assert module._plc_sn_edits[plc_id].text() == "000123"


def test_next_workorder_clears_success_when_disabled_and_rejects_old_cycle(make_module):
    module, services, (plc_id,), configs = make_module()
    emit(services, plc_id, 100, "table_sent", 2.5, "table")
    configs[plc_id] = replace(configs[plc_id], enabled=False, revision=1)
    services.bus.plc_torque_config_saved.emit(configs[plc_id])
    assert module._plc_torque_edits[plc_id].text() == "2.5（软件表，已回传）"
    services.bus.plc_workorder_changed.emit({"plc_id": plc_id, "sn": "next", "model": "M100"})
    assert module._plc_torque_edits[plc_id].text() == "未启用"
    emit(services, plc_id, 100, "table_sent", 2.5, "table")
    emit(services, plc_id, 99, "reading")
    assert module._plc_torque_edits[plc_id].text() == "未启用"
    configs[plc_id] = replace(configs[plc_id], enabled=True, revision=2)
    services.bus.plc_torque_config_saved.emit(configs[plc_id])
    assert module._plc_torque_edits[plc_id].text() == "待读取"
    emit(services, plc_id, 200, "reading")
    emit(services, plc_id, 200, "plc", 3.0, "plc")
    assert module._plc_torque_edits[plc_id].text() == "3（PLC）"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_deleted_and_unknown_plcs_ignore_events_and_drop_cached_torque(make_module, count):
    module, services, ids, configs = make_module(count)
    removed = ids[-1]
    services.bus.plc_workorder_changed.emit({"plc_id": removed, "sn": "cached"})
    emit(services, removed, 100, "table_sent", 2.5, "table")
    PlcTable.delete().where(PlcTable.id == removed).execute()
    services.bus.plc_changed.emit()
    emit(services, removed, 200, "plc", 77.0, "plc")
    emit(services, 9999, 999, "plc", 77.0, "plc")
    services.bus.plc_torque_config_saved.emit(TorqueConfig(9999, True))
    assert 9999 not in module._plc_torque_edits
    if removed in module._plc_torque_edits:  # Legacy last-SN fallback is preserved.
        assert module._plc_torque_edits[removed].text() == "未启用"
    PlcTable.create(id=removed, plc_name="replacement")
    configs[removed] = TorqueConfig(removed, enabled=True)
    services.bus.plc_changed.emit()
    assert module._plc_torque_edits[removed].text() == "待读取"
    emit(services, removed, 300, "plc", 3.0, "plc")
    assert module._plc_torque_edits[removed].text() == "3（PLC）"
