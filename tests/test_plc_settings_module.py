from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from peewee import SqliteDatabase
from PyQt6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if "snap7" not in sys.modules:
    snap7_module = ModuleType("snap7")
    snap7_client_module = ModuleType("snap7.client")
    snap7_util_module = ModuleType("snap7.util")

    class _Client:
        def connect(self, *_args, **_kwargs):
            return None

        def disconnect(self):
            return None

    snap7_client_module.Client = _Client
    snap7_util_module.get_bool = lambda *_args, **_kwargs: False
    snap7_util_module.set_bool = lambda *_args, **_kwargs: None
    snap7_module.client = snap7_client_module
    snap7_module.util = snap7_util_module
    sys.modules["snap7"] = snap7_module
    sys.modules["snap7.client"] = snap7_client_module
    sys.modules["snap7.util"] = snap7_util_module

from model.plc_table import PlcTable
from ui.modules.plc_settings import PlcSettingsModule


class _SignalProbe:
    def __init__(self):
        self.messages = []

    def emit(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def plc_db():
    database = SqliteDatabase(":memory:")
    database.bind([PlcTable])
    database.connect()
    database.create_tables([PlcTable])
    yield database
    database.drop_tables([PlcTable])
    database.close()


def test_plc_settings_module_loads_rows_and_initializes_service_maps(qapp, plc_db):
    PlcTable.create(
        plc_name="PLC A",
        plc_ip="127.0.0.1",
        plc_rack=0,
        plc_slot=1,
        plc_db_number=1,
        plc_byte_index=2,
        plc_bit_index=3,
        plc_poll_interval_ms=250,
        plc_reconnect_interval_ms=4000,
        plc_open_state=0,
    )
    services = SimpleNamespace(
        bus=SimpleNamespace(log=_SignalProbe()),
        _start_from_plc=lambda: None,
    )

    module = PlcSettingsModule(services)
    qapp.processEvents()

    assert module.combo_plc.count() == 1
    assert module.ed_name.text() == "PLC A"
    assert module.ed_ip.text() == "127.0.0.1"
    assert module.ed_db_number.text() == "1"
    assert module.ed_byte_index.text() == "2"
    assert module.ed_bit_index.text() == "3"
    assert module.lb_state.text() == "关闭"
    assert module.lb_connect_state.text() == "未连接"
    assert services.plc_services == {}
    assert services.plc_threads == {}
    assert services.plc_state == {1: "未连接"}
