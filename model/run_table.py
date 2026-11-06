"""Accepted run facts and their current lifecycle projection."""

from __future__ import annotations

from peewee import IntegerField, SQL, TextField

from model.base_mode import BaseModel


class RunTable(BaseModel):
    run_id = TextField(primary_key=True)
    ingress_kind = TextField()
    trigger_source = TextField()
    config_id = IntegerField()
    hardware_id = IntegerField(null=True)
    imported_source_identity = TextField(null=True)
    imported_source_path = TextField(null=True)
    imported_expected_frame_count = IntegerField(null=True)
    causal_revision = IntegerField()
    config_snapshot_json = TextField()
    hardware_snapshot_json = TextField()
    calibration_snapshot_json = TextField()
    current_trace_json = TextField(null=True, default=None)
    state = TextField()
    judgment = TextField(null=True)
    judgment_source = TextField()
    fault_type = TextField(null=True, default=None)
    manual_judgment_revision = IntegerField(null=True, default=None)
    judged_at = TextField(null=True)
    terminal_code = TextField(null=True)
    terminal_summary = TextField(null=True)
    accepted_at = TextField()
    terminal_at = TextField(null=True)
    updated_at = TextField()

    class Meta:
        table_name = "run"
        constraints = [
            SQL("CHECK (ingress_kind IN ('CAPTURED', 'IMPORTED'))"),
            SQL(
                "CHECK (trigger_source IN "
                "('PLC', 'MANUAL', 'SCANNER', 'IMPORT'))"
            ),
            SQL(
                "CHECK (state IN "
                "('ACTIVE', 'SUCCEEDED', 'NO_RESULT', 'FAILED', "
                "'CANCELED', 'INTERRUPTED'))"
            ),
            SQL("CHECK (judgment IS NULL OR judgment IN ('PASS', 'FAIL'))"),
            SQL(
                "CHECK (judgment_source IN ('NONE', 'AUTOMATIC', 'MANUAL'))"
            ),
            SQL(
                "CHECK (ingress_kind <> 'CAPTURED' OR hardware_id IS NOT NULL)"
            ),
            SQL(
                "CHECK ((ingress_kind = 'IMPORTED' AND trigger_source = 'IMPORT') "
                "OR (ingress_kind <> 'IMPORTED' AND trigger_source <> 'IMPORT'))"
            ),
            SQL(
                "CHECK (json_valid(config_snapshot_json) "
                "AND json_type(config_snapshot_json) = 'object')"
            ),
            SQL(
                "CHECK (json_valid(hardware_snapshot_json) "
                "AND json_type(hardware_snapshot_json) = 'object')"
            ),
            SQL(
                "CHECK (json_valid(calibration_snapshot_json) "
                "AND json_type(calibration_snapshot_json) = 'object')"
            ),
            SQL(
                "CHECK ("
                "(state = 'ACTIVE' "
                "AND terminal_at IS NULL "
                "AND terminal_code IS NULL "
                "AND terminal_summary IS NULL "
                "AND judgment_source = 'NONE') "
                "OR "
                "(state <> 'ACTIVE' "
                "AND terminal_at IS NOT NULL "
                "AND terminal_code IS NOT NULL)"
                ")"
            ),
            SQL(
                "CHECK ("
                "(judgment_source = 'NONE' "
                "AND judgment IS NULL "
                "AND judged_at IS NULL) "
                "OR "
                "(judgment_source <> 'NONE' "
                "AND judgment IS NOT NULL "
                "AND judged_at IS NOT NULL)"
                ")"
            ),
            SQL(
                "CHECK ("
                "(judgment_source = 'MANUAL' "
                "AND manual_judgment_revision IS NOT NULL "
                "AND typeof(manual_judgment_revision) = 'integer' "
                "AND manual_judgment_revision > 0) "
                "OR "
                "(judgment_source <> 'MANUAL' "
                "AND manual_judgment_revision IS NULL "
                "AND fault_type IS NULL)"
                ")"
            ),
            SQL(
                "CHECK (fault_type IS NULL OR "
                "(judgment_source = 'MANUAL' AND judgment = 'FAIL'))"
            ),
            SQL(
                "CHECK (current_trace_json IS NULL OR "
                "(json_valid(current_trace_json) "
                "AND json_type(current_trace_json) = 'object'))"
            ),
            SQL(
                "CHECK ("
                "(ingress_kind = 'CAPTURED' "
                "AND imported_source_identity IS NULL "
                "AND imported_source_path IS NULL "
                "AND imported_expected_frame_count IS NULL) "
                "OR "
                "(ingress_kind = 'IMPORTED' "
                "AND imported_source_identity IS NOT NULL "
                "AND imported_source_path IS NOT NULL "
                "AND length(trim(imported_source_identity)) > 0 "
                "AND length(trim(imported_source_path)) > 0 "
                "AND typeof(imported_expected_frame_count) = 'integer' "
                "AND imported_expected_frame_count > 0)"
                ")"
            ),
            SQL(
                "CHECK (typeof(causal_revision) = 'integer' "
                "AND causal_revision >= 0)"
            ),
        ]
