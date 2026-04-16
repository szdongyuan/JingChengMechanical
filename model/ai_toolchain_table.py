"""Global AI toolchain registry table.

A toolchain is an executable capability package, not a single model file.
The primary key is a human-readable string identifier (e.g. "motor-dual-segment-ai").
"""
from __future__ import annotations

from peewee import SQL, BooleanField, IntegerField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class AiToolchainTable(BaseModel):
    toolchain_id = TextField(primary_key=True)
    display_name = TextField()
    version = TextField(default="")
    entrypoint = TextField()
    protocol_version = IntegerField(default=1)
    execution_mode = TextField(
        default="resident_process",
        constraints=[SQL("DEFAULT 'resident_process'")],
    )
    default_timeout_ms = IntegerField(default=30000)
    enabled = BooleanField(default=True)
    description = TextField(default="")
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)

    class Meta:
        table_name = "ai_toolchain_table"
