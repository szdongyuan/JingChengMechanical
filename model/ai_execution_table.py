"""AI execution history table.

One row per AI execution attempt, keyed by request_id.
Fields are intentionally denormalized (config_id as IntegerField, toolchain_id
as TextField) so execution records survive deletion of the originating config
or toolchain. These are historical audit records.
"""
from __future__ import annotations

from peewee import BooleanField, FloatField, IntegerField, TextField

from model.base_mode import BaseModel


class AiExecutionTable(BaseModel):
    request_id = TextField(primary_key=True)

    test_run_id = TextField(index=True)
    config_id = IntegerField()
    toolchain_id = TextField()

    module_status = TextField()
    started_time = TextField(null=True)
    finished_time = TextField(null=True)
    duration_ms = IntegerField(null=True)
    exit_code = IntegerField(null=True)
    error_summary = TextField(null=True)
    stdout_excerpt = TextField(null=True)
    stderr_excerpt = TextField(null=True)

    toolchain_name = TextField(null=True)
    toolchain_version = TextField(null=True)
    protocol_version = IntegerField(default=1)

    window_overridden = BooleanField(default=False)
    default_window_offset_ms = IntegerField(default=0)
    default_window_duration_ms = IntegerField(default=0)
    final_window_offset_ms = IntegerField(default=0)
    final_window_duration_ms = IntegerField(default=0)

    is_current_effective = BooleanField(default=False)

    ai_label = TextField(null=True)
    score = FloatField(null=True)
    summary = TextField(null=True)
    confidence = FloatField(null=True)
    extra_json = TextField(null=True)
    artifacts_json = TextField(null=True)
    raw_response_json = TextField(null=True)

    request_json_path = TextField(null=True)
    response_json_path = TextField(null=True)
    artifacts_dir = TextField(null=True)

    class Meta:
        table_name = "ai_execution_table"
