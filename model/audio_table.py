"""Immutable facts for completed, closed, and verified audio files."""

from __future__ import annotations

from peewee import IntegerField, SQL, TextField

from model.base_mode import BaseModel


class AudioTable(BaseModel):
    audio_id = TextField(primary_key=True)
    save_job_id = TextField(unique=True)
    run_id = TextField(unique=True)
    source_kind = TextField()
    source_identity = TextField()
    content_revision = IntegerField()
    final_path = TextField(collation="NOCASE", unique=True)
    container_format = TextField()
    sample_rate_hz = IntegerField()
    channel_count = IntegerField()
    frame_count = IntegerField()
    duration_ms = IntegerField()
    byte_size = IntegerField()
    closed_verified_at = TextField()
    created_at = TextField()

    class Meta:
        table_name = "audio"
        indexes = ((("audio_id", "run_id"), True),)
        constraints = [
            SQL(
                "FOREIGN KEY (run_id) REFERENCES run(run_id) "
                "ON DELETE RESTRICT"
            ),
            SQL(
                "CHECK (source_kind IN "
                "('CAPTURED_AUDIO', 'IMPORTED_SOURCE'))"
            ),
            SQL("CHECK (length(trim(source_identity)) > 0)"),
            SQL(
                "CHECK (typeof(content_revision) = 'integer' "
                "AND content_revision > 0)"
            ),
            SQL("CHECK (container_format = 'WAV_PCM')"),
            SQL("CHECK (sample_rate_hz > 0)"),
            SQL("CHECK (channel_count > 0)"),
            SQL("CHECK (frame_count > 0)"),
            SQL("CHECK (duration_ms > 0)"),
            SQL("CHECK (byte_size > 0)"),
        ]
