"""Shared pytest fixtures for all tests."""
from __future__ import annotations

import pytest
from peewee import SqliteDatabase

from model.test_config_table import TestConfigTable
from model.plc_table import PlcTable
from model.plc_torque_config_table import PlcTorqueConfigTable
from model.torque_rule_table import TorqueRuleTable
from model.channel_config_table import ChannelConfigTable
from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.device_table import DeviceTable
from model.audio_data_table import AudioDataTable
from model.historical_metric_cache_table import HistoricalMetricCacheTable
from model.input_hardware_table import InputHardwareTable

ALL_TABLES = [
    TestConfigTable,
    PlcTable,
    PlcTorqueConfigTable,
    TorqueRuleTable,
    ChannelConfigTable,
    AiToolchainTable,
    TestConfigAiBindingTable,
    AiExecutionTable,
    DeviceTable,
    InputHardwareTable,
    AudioDataTable,
    HistoricalMetricCacheTable,
]

_test_db = SqliteDatabase(":memory:", pragmas={"foreign_keys": 1})


@pytest.fixture(scope="session")
def qapp():
    """Keep one application alive for tests that use Qt widgets."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def setup_test_db():
    """Bind all models to an in-memory DB, create tables, yield, then drop."""
    _test_db.bind(ALL_TABLES)
    _test_db.connect()
    _test_db.create_tables(ALL_TABLES)
    yield _test_db
    _test_db.drop_tables(ALL_TABLES)
    _test_db.close()
