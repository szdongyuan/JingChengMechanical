"""Test-level AI toolchain binding table.

Each test configuration binds to at most one AI toolchain (V1).
The toolchain FK is nullable — a binding row can exist with no toolchain
selected, representing a "not_configured" state. Deleting a toolchain
sets the FK to NULL rather than cascade-deleting bindings.
"""
from __future__ import annotations

from peewee import AutoField, BooleanField, FloatField, ForeignKeyField, IntegerField, TextField

from consts.time_consts import now_str
from model.ai_toolchain_table import AiToolchainTable
from model.base_mode import BaseModel
from model.test_config_table import TestConfigTable


class TestConfigAiBindingTable(BaseModel):
    binding_id = AutoField()
    config = ForeignKeyField(
        TestConfigTable, backref="ai_bindings", on_delete="CASCADE", unique=True
    )
    enabled = BooleanField(default=False)
    toolchain = ForeignKeyField(
        AiToolchainTable, backref="bindings", on_delete="SET NULL", null=True
    )
    timeout_ms = IntegerField(null=True, default=None)
    params_json = TextField(default="{}")
    score_threshold = FloatField(null=True, default=None)
    updated_time = TextField(default=now_str)

    class Meta:
        table_name = "test_config_ai_binding_table"
