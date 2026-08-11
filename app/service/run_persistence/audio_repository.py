"""Immutable repository for completed and verified audio facts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
    VerifiedAudioFact,
    _is_issued_verified_audio_fact,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.transactions import run_short_transaction


@dataclass(frozen=True)
class AudioRecord:
    audio_id: str
    save_job_id: str
    run_id: str
    source_kind: str
    source_identity: str
    content_revision: int
    final_path: str
    container_format: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    duration_ms: int
    byte_size: int
    closed_verified_at: str
    created_at: str


_AUDIO_COLUMNS = (
    "audio_id, save_job_id, run_id, source_kind, source_identity, "
    "content_revision, final_path, container_format, sample_rate_hz, "
    "channel_count, frame_count, duration_ms, byte_size, "
    "closed_verified_at, created_at"
)


class AudioFinalArtifactError(Exception):
    """The published final no longer matches its verified immutable fact."""

    code = "AUDIO_FINAL_SIZE_MISMATCH"

    def __init__(self, final_path: str, reason: str) -> None:
        self.final_path = final_path
        self.reason = reason
        super().__init__(
            f"{self.code}: final artifact preflight failed for "
            f"{final_path}: {reason}"
        )


class AudioRepository:
    def __init__(self, runtime: RunPersistenceDatabase) -> None:
        self._runtime = runtime

    def insert_verified(
        self, fact: VerifiedAudioFact
    ) -> TransactionResult[bool]:
        if not _is_issued_verified_audio_fact(fact):
            raise TypeError(
                "fact must be the exact VerifiedAudioFact issued after publication"
            )

        final_path = Path(fact.final_path)
        try:
            with final_path.open("rb") as artifact:
                artifact_stat = os.fstat(artifact.fileno())
        except OSError as exc:
            raise AudioFinalArtifactError(
                fact.final_path, str(exc)
            ) from None
        if not stat.S_ISREG(artifact_stat.st_mode):
            raise AudioFinalArtifactError(
                fact.final_path, "audio final is not a regular file"
            )
        if artifact_stat.st_size != fact.byte_size:
            raise AudioFinalArtifactError(
                fact.final_path,
                "audio final byte size does not match the verified fact "
                f"(expected {fact.byte_size}, got {artifact_stat.st_size})",
            )

        def insert(database):
            cursor = database.execute_sql(
                """
                INSERT INTO audio (
                    audio_id, save_job_id, run_id, source_kind,
                    source_identity, content_revision,
                    final_path, container_format,
                    sample_rate_hz, channel_count, frame_count, duration_ms,
                    byte_size, closed_verified_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.audio_id,
                    fact.save_job_id,
                    fact.run_id,
                    fact.source_kind,
                    fact.source_identity,
                    fact.content_revision,
                    fact.final_path,
                    fact.container_format,
                    fact.sample_rate_hz,
                    fact.channel_count,
                    fact.frame_count,
                    fact.duration_ms,
                    fact.byte_size,
                    fact.closed_verified_at,
                    fact.created_at,
                ),
            )
            return cursor.rowcount == 1

        with self._runtime.operation() as database:
            result = run_short_transaction(database, insert)
            if result.state is TransactionState.UNKNOWN:
                self._runtime.quarantine_unknown()
                return result
            if result.state is TransactionState.ROLLED_BACK:
                return TransactionResult(
                    state=TransactionState.ROLLED_BACK,
                    reason=f"AUDIO_DB_INSERT_FAILED: {result.reason}",
                )
            return result

    def get_for_run(self, run_id: str) -> AudioRecord | None:
        validate_uuid4(run_id)
        with self._runtime.operation() as database:
            row = database.execute_sql(
                f"SELECT {_AUDIO_COLUMNS} FROM audio WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else AudioRecord(*row)
