from __future__ import annotations

from types import SimpleNamespace

from app.main_window import MainWindow
from app.services_container import FactoryControllerState


class _ConfigServiceStub:
    def __init__(self, configs: dict[int, dict]):
        self.configs = configs

    def load_config(self, config_id: int) -> dict:
        return dict(self.configs[config_id])


def test_selected_configuration_plcs_are_resolved_in_selection_order():
    probe = SimpleNamespace(
        _factory_state=FactoryControllerState(
            selected_config_ids=(101, 102, 103, 104),
            run_phase="armed",
        ),
        services=SimpleNamespace(
            config_service=_ConfigServiceStub(
                {
                    101: {"config_name": "Alpha", "plc_id": 7},
                    102: {"config_name": "Beta", "plc_id": 7},
                    103: {"config_name": "Gamma", "plc_id": 9},
                    104: {"config_name": "Manual", "plc_id": None},
                }
            )
        ),
    )

    MainWindow._reload_selected_plc_bindings(probe)

    assert probe._selected_plc_bindings == (7, 9)
    assert probe._selected_config_count == 4
    assert probe._selected_unbound_plc_count == 1
    assert probe._selected_config_load_error_count == 0
    assert probe._selected_plc_tooltip.splitlines() == [
        "Alpha：PLC 7",
        "Beta：PLC 7",
        "Gamma：PLC 9",
        "Manual：未绑定 PLC",
    ]


def test_selected_plc_status_uses_bound_ids_instead_of_fixed_plc_one_and_two():
    text = MainWindow._selected_plc_status_html(
        (7, 9),
        {7: "已连接", 9: "重连中", 1: "已连接", 2: "已连接"},
        selected_config_count=3,
        unbound_config_count=1,
        load_error_count=0,
    )

    assert "PLC 7" in text
    assert "PLC 9" in text
    assert "PLC 1：" not in text
    assert "PLC 2：" not in text
    assert "已连接" in text
    assert "重连中" in text
    assert "1 个配置未绑定 PLC" in text


def test_selected_plc_status_explains_when_no_configuration_is_selected():
    text = MainWindow._selected_plc_status_html(
        (),
        {},
        selected_config_count=0,
        unbound_config_count=0,
        load_error_count=0,
    )

    assert "未选择配置" in text
