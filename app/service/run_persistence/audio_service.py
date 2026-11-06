"""Publication-to-row composition for one completed run audio file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.service.run_persistence.audio_publication import (
    AudioPublication,
    AudioPublicationResult,
    _issue_verified_fact_after_publication,
)
from app.service.run_persistence.audio_repository import (
    AudioFinalArtifactError,
    AudioRepository,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    TransactionResult,
    TransactionState,
    VerifiedAudioFact,
    new_uuid4,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.run_repository import RunRepository


@dataclass(frozen=True)
class AudioPersistenceResult:
    initial_attempt_permitted: bool
    restart_required: bool = False
    code: str | None = None
    reason: str | None = None
    cleanup_code: str | None = None
    cleanup_path: str | None = None
    fact: VerifiedAudioFact | None = None
    publication_result: AudioPublicationResult | None = None
    database_result: TransactionResult | None = None
    run_result: TransactionResult | None = None


class AudioPersistenceService:
    def __init__(
        self,
        publication: AudioPublication,
        audio_repository: AudioRepository,
        run_repository: RunRepository,
    ) -> None:
        self._publication = publication
        self._audio_repository = audio_repository
        self._run_repository = run_repository

    def persist(
        self,
        run_id: str,
        source_path: str | Path,
        final_path: str | Path,
        sample_rate_hz: int,
        channel_count: int,
        sample_width_bytes: int,
        now: str,
        *,
        save_job_id: str,
        source_kind: str,
        source_identity: str,
        content_revision: int,
        expected_frame_count: int,
        source_frame_count: int,
    ) -> AudioPersistenceResult:
        try:
            validate_uuid4(run_id)
        except ContractValidationError as exc:
            raise ContractValidationError(f"run_id: {exc}") from None
        try:
            validate_utc_timestamp(now)
        except ContractValidationError as exc:
            raise ContractValidationError(f"now: {exc}") from None
        try:
            validate_uuid4(save_job_id)
        except ContractValidationError as exc:
            raise ContractValidationError(f"save_job_id: {exc}") from None
        for name, value in (
            ("sample_rate_hz", sample_rate_hz),
            ("channel_count", channel_count),
            ("sample_width_bytes", sample_width_bytes),
            ("expected_frame_count", expected_frame_count),
            ("source_frame_count", source_frame_count),
        ):
            if type(value) is not int or value <= 0:
                raise ContractValidationError(
                    f"{name} must be a positive integer"
                )
        if source_kind not in {"CAPTURED_AUDIO", "IMPORTED_SOURCE"}:
            raise ContractValidationError(
                "source_kind must be CAPTURED_AUDIO or IMPORTED_SOURCE"
            )
        if type(source_identity) is not str or not source_identity.strip():
            raise ContractValidationError(
                "source_identity must be nonempty text"
            )
        if content_revision != 1 or type(content_revision) is not int:
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code="AUDIO_REQUEST_MISMATCH",
                reason="first immutable audio content revision must be exactly 1",
            )
        if (
            (source_kind == "CAPTURED_AUDIO" and sample_width_bytes != 2)
            or source_frame_count != expected_frame_count
        ):
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code="AUDIO_REQUEST_MISMATCH",
                reason="frozen audio metadata does not match save constraints",
            )
        run = self._run_repository.get(run_id)
        expected_source_kind = (
            "CAPTURED_AUDIO"
            if run is not None and run.ingress_kind == "CAPTURED"
            else "IMPORTED_SOURCE"
        )
        mismatch = (
            run is None
            or run.state != "ACTIVE"
            or source_kind != expected_source_kind
        )
        if not mismatch and source_kind == "IMPORTED_SOURCE":
            mismatch = (
                run.imported_source_identity != source_identity
                or run.imported_expected_frame_count != expected_frame_count
                or str(Path(run.imported_source_path).resolve(strict=False))
                != str(Path(source_path).resolve(strict=False))
            )
        if mismatch:
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code="AUDIO_REQUEST_MISMATCH",
                reason="save request does not match immutable ACTIVE run facts",
            )
        publication = self._publication.publish(
            source_path,
            final_path,
            sample_rate_hz,
            channel_count,
            sample_width_bytes,
            expected_frame_count,
        )
        if not publication.ok:
            assert publication.code is not None
            cleanup_code = (
                publication.cleanup_code.value
                if publication.cleanup_code is not None
                else None
            )
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code=publication.code.value,
                reason=publication.reason,
                cleanup_code=cleanup_code,
                cleanup_path=publication.cleanup_path,
                publication_result=publication,
            )

        assert publication.metadata is not None
        fact = _issue_verified_fact_after_publication(
            publication,
            audio_id=new_uuid4(),
            save_job_id=save_job_id,
            run_id=run_id,
            source_kind=source_kind,
            source_identity=source_identity,
            content_revision=content_revision,
            now=now,
        )
        try:
            database_result = self._audio_repository.insert_verified(fact)
        except AudioFinalArtifactError as exc:
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code=exc.code,
                reason=str(exc),
                fact=fact,
                publication_result=publication,
            )
        if database_result.state is TransactionState.UNKNOWN:
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                restart_required=True,
                code="AUDIO_DB_STATE_UNKNOWN",
                reason=database_result.reason,
                fact=fact,
                publication_result=publication,
                database_result=database_result,
            )
        if database_result.state is TransactionState.ROLLED_BACK:
            return AudioPersistenceResult(
                initial_attempt_permitted=False,
                code="AUDIO_DB_INSERT_FAILED",
                reason=database_result.reason,
                fact=fact,
                publication_result=publication,
                database_result=database_result,
            )
        return AudioPersistenceResult(
            initial_attempt_permitted=True,
            fact=fact,
            publication_result=publication,
            database_result=database_result,
        )
