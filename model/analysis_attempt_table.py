"""Analysis attempts tied to one completed audio fact and its run."""

from __future__ import annotations

from peewee import ModelIndex, SQL, TextField

from model.base_mode import BaseModel


class AnalysisAttemptTable(BaseModel):
    attempt_id = TextField(primary_key=True)
    run_id = TextField()
    audio_id = TextField()
    attempt_kind = TextField()
    state = TextField()
    judgment = TextField(null=True)
    result_payload_json = TextField(null=True)
    error_code = TextField(null=True)
    error_summary = TextField(null=True)
    queued_at = TextField()
    started_at = TextField(null=True)
    terminal_at = TextField(null=True)
    updated_at = TextField()

    class Meta:
        table_name = "analysis_attempt"
        constraints = [
            SQL(
                "FOREIGN KEY (run_id) REFERENCES run(run_id) "
                "ON DELETE RESTRICT"
            ),
            SQL(
                "FOREIGN KEY (audio_id, run_id) "
                "REFERENCES audio(audio_id, run_id) "
                "ON DELETE RESTRICT"
            ),
            SQL("CHECK (attempt_kind IN ('INITIAL', 'REANALYSIS'))"),
            SQL(
                "CHECK (state IN "
                "('QUEUED', 'RUNNING', 'SUCCEEDED', 'NO_RESULT', "
                "'FAILED', 'CANCELED', 'INTERRUPTED'))"
            ),
            SQL("CHECK (judgment IS NULL OR judgment IN ('PASS', 'FAIL'))"),
            SQL(
                "CHECK (result_payload_json IS NULL "
                "OR json_valid(result_payload_json))"
            ),
            SQL(
                "CHECK ("
                "(state = 'QUEUED' "
                "AND started_at IS NULL "
                "AND terminal_at IS NULL "
                "AND judgment IS NULL "
                "AND result_payload_json IS NULL "
                "AND error_code IS NULL) "
                "OR "
                "(state = 'RUNNING' "
                "AND started_at IS NOT NULL "
                "AND terminal_at IS NULL "
                "AND judgment IS NULL "
                "AND result_payload_json IS NULL "
                "AND error_code IS NULL) "
                "OR "
                "(state = 'SUCCEEDED' "
                "AND started_at IS NOT NULL "
                "AND terminal_at IS NOT NULL "
                "AND judgment IS NOT NULL "
                "AND result_payload_json IS NOT NULL "
                "AND error_code IS NULL) "
                "OR "
                "(state = 'NO_RESULT' "
                "AND started_at IS NOT NULL "
                "AND terminal_at IS NOT NULL "
                "AND judgment IS NULL "
                "AND error_code IS NOT NULL) "
                "OR "
                "(state IN ('FAILED', 'CANCELED', 'INTERRUPTED') "
                "AND terminal_at IS NOT NULL "
                "AND judgment IS NULL "
                "AND error_code IS NOT NULL)"
                ")"
            ),
        ]


AnalysisAttemptTable.add_index(
    ModelIndex(
        AnalysisAttemptTable,
        (AnalysisAttemptTable.run_id,),
        unique=True,
        name="uq_attempt_initial_per_run",
        where=SQL("attempt_kind = 'INITIAL'"),
    )
)
