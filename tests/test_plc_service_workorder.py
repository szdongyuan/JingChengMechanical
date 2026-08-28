from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.service.plc_service import PlcService


class _SignalStub:
    def __init__(self, emitted):
        self._emitted = emitted

    def emit(self, payload):
        self._emitted.append(payload)


class _LoggerStub:
    def info(self, _message):
        return None

    def error(self, _message):
        return None


def _service_with_values(values):
    emitted = []
    service = PlcService.__new__(PlcService)
    service._plc_id = 7
    service.bus = SimpleNamespace(
        plc_workorder_changed=_SignalStub(emitted),
    )
    service.logger = _LoggerStub()
    service.read_plc_values = lambda *_field_names: values
    return service, emitted


def _polling_service(*, start_values, workorder_values, on_start):
    service, _emitted = _service_with_values(workorder_values)
    service.bus.log = _SignalStub([])
    service._on_start_requested = on_start
    service.plc_client = object()
    service._last_value = None
    service._auto_reset_bit = False
    start_values = iter(start_values)
    service._read_start_bit = lambda: next(start_values)
    service._poll_current_value = lambda: None
    service._write_testing_signal = lambda _testing: None
    return service


def test_emit_plc_workorder_includes_product_model_in_return_and_display():
    service, emitted = _service_with_values(
        {
            "sn_code_offset": "SN-7",
            "product_model_offset": "MODEL-X",
            "rotation_speed_offset": 1500,
        }
    )

    workorder = service._emit_plc_workorder()

    assert workorder == {
        "sn": "SN-7",
        "model": "MODEL-X",
        "rotation_speed": 1500,
    }
    assert emitted == [
        {
            "plc_id": 7,
            "sn": "SN-7",
            "model": "MODEL-X",
            "rotation_speed": 1500,
        }
    ]


@pytest.mark.parametrize("model", [None, ""])
def test_emit_plc_workorder_omits_empty_product_model(model):
    service, emitted = _service_with_values(
        {
            "sn_code_offset": "SN-7",
            "product_model_offset": model,
            "rotation_speed_offset": 1500,
        }
    )

    workorder = service._emit_plc_workorder()

    assert "model" not in workorder
    assert "model" not in emitted[0]


def test_rising_edge_start_callback_receives_plc_product_model():
    callbacks = []
    service = _polling_service(
        start_values=[False, True],
        workorder_values={
            "sn_code_offset": "SN-7",
            "product_model_offset": "MODEL-X",
            "rotation_speed_offset": 1500,
        },
        on_start=lambda plc_id, workorder, trigger_ns: callbacks.append(
            (plc_id, workorder, trigger_ns)
        ),
    )

    service.poll_once()
    service.poll_once()

    assert len(callbacks) == 1
    assert callbacks[0][0] == 7
    assert callbacks[0][1] == {
        "sn": "SN-7",
        "model": "MODEL-X",
        "rotation_speed": 1500,
    }
    assert type(callbacks[0][2]) is int
