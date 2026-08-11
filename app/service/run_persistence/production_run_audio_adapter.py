"""Deterministic PCM16 publication and loading for persisted run audio."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import stat
from uuid import uuid4
import wave

import numpy

from app.service.analysis.scheduler import (
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisStage,
    AudioLoadError,
    AudioStorageKind,
    CancellationToken,
    PersistedAudioReference,
)
from app.service.run_persistence.audio_service import (
    AudioPersistenceResult,
    AudioPersistenceService,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    VerifiedAudioFact,
    canonicalize_absolute_path,
    validate_utc_timestamp,
    validate_uuid4,
)


class ProductionAudioInputError(ValueError):
    """A production audio request failed before any I/O was allowed."""


def normalize_absolute_path(value: str) -> str:
    """Resolve once and return the canonical Windows absolute-path spelling."""

    try:
        return canonicalize_absolute_path(value)
    except ContractValidationError as exc:
        raise ProductionAudioInputError(str(exc)) from None


def _require_uuid4(value: object, name: str) -> None:
    try:
        validate_uuid4(value)  # type: ignore[arg-type]
    except (ContractValidationError, TypeError, ValueError) as exc:
        raise ProductionAudioInputError(f"{name}: {exc}") from None


def _require_utc(value: object, name: str) -> None:
    try:
        validate_utc_timestamp(value)  # type: ignore[arg-type]
    except (ContractValidationError, TypeError, ValueError) as exc:
        raise ProductionAudioInputError(f"{name}: {exc}") from None


def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ProductionAudioInputError(f"{name} must be a positive integer")


def _validate_captured_samples(
    samples: object, frame_count: int, channel_count: int
) -> None:
    if type(samples) is not numpy.ndarray:
        raise ProductionAudioInputError("samples must be an exact numpy.ndarray")
    if samples.dtype != numpy.dtype(numpy.float32):
        raise ProductionAudioInputError("samples dtype must be float32")
    if samples.ndim != 2:
        raise ProductionAudioInputError("samples must have exactly two dimensions")
    if samples.shape != (frame_count, channel_count):
        raise ProductionAudioInputError(
            "samples shape must equal (frame_count, channel_count)"
        )
    if not samples.flags.c_contiguous:
        raise ProductionAudioInputError("samples must be C-contiguous")
    if not bool(numpy.isfinite(samples).all()):
        raise ProductionAudioInputError("samples must all be finite")


@dataclass(frozen=True, slots=True)
class CapturedAudioSaveRequest:
    save_job_id: str
    run_id: str
    source_identity: str
    content_revision: int
    samples: numpy.ndarray
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    final_path: str
    now: str

    def __post_init__(self) -> None:
        _require_uuid4(self.save_job_id, "save_job_id")
        _require_uuid4(self.run_id, "run_id")
        if type(self.source_identity) is not str or not self.source_identity.strip():
            raise ProductionAudioInputError(
                "source_identity must be nonempty text"
            )
        _require_positive_int(self.content_revision, "content_revision")
        _require_positive_int(self.sample_rate_hz, "sample_rate_hz")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.frame_count, "frame_count")
        _require_utc(self.now, "now")
        _validate_captured_samples(
            self.samples, self.frame_count, self.channel_count
        )
        object.__setattr__(
            self, "final_path", normalize_absolute_path(self.final_path)
        )


@dataclass(frozen=True, slots=True)
class ImportedAudioSaveRequest:
    save_job_id: str
    run_id: str
    source_identity: str
    content_revision: int
    source_path: str
    final_path: str
    expected_sample_rate_hz: int
    expected_channel_count: int
    expected_frame_count: int
    now: str

    def __post_init__(self) -> None:
        _require_uuid4(self.save_job_id, "save_job_id")
        _require_uuid4(self.run_id, "run_id")
        if type(self.source_identity) is not str or not self.source_identity.strip():
            raise ProductionAudioInputError(
                "source_identity must be nonempty text"
            )
        _require_positive_int(self.content_revision, "content_revision")
        _require_positive_int(
            self.expected_sample_rate_hz, "expected_sample_rate_hz"
        )
        _require_positive_int(
            self.expected_channel_count, "expected_channel_count"
        )
        _require_positive_int(self.expected_frame_count, "expected_frame_count")
        _require_utc(self.now, "now")
        object.__setattr__(
            self, "source_path", normalize_absolute_path(self.source_path)
        )
        object.__setattr__(
            self, "final_path", normalize_absolute_path(self.final_path)
        )


class ProductionAudioStatus(str, Enum):
    PERSISTED = "PERSISTED"
    FAILED = "FAILED"


def _validate_optional_string(value: object, name: str) -> None:
    if value is not None and type(value) is not str:
        raise TypeError(f"{name} must be an exact str or None")


def _reference_matches_fact(
    reference: PersistedAudioReference, fact: VerifiedAudioFact
) -> bool:
    return (
        reference.reference_id == fact.audio_id
        and reference.audio_data_id == fact.audio_id
        and reference.absolute_path == normalize_absolute_path(fact.final_path)
        and reference.sample_rate_hz == fact.sample_rate_hz
        and reference.channel_count == fact.channel_count
        and reference.frame_count == fact.frame_count
        and reference.dtype == "<i2"
        and reference.content_revision == f"{fact.audio_id}:{fact.byte_size}"
    )


@dataclass(frozen=True, slots=True)
class ProductionAudioResult:
    status: ProductionAudioStatus
    run_id: str
    reference: PersistedAudioReference | None
    code: str | None
    reason: str | None
    persistence_result: AudioPersistenceResult | None
    staging_cleanup_code: str | None
    staging_cleanup_path: str | None

    def __post_init__(self) -> None:
        if type(self.status) is not ProductionAudioStatus:
            raise TypeError("status must be an exact ProductionAudioStatus")
        _require_uuid4(self.run_id, "run_id")
        _validate_optional_string(self.code, "code")
        _validate_optional_string(self.reason, "reason")
        _validate_optional_string(
            self.staging_cleanup_code, "staging_cleanup_code"
        )
        _validate_optional_string(
            self.staging_cleanup_path, "staging_cleanup_path"
        )
        if (self.staging_cleanup_code is None) != (
            self.staging_cleanup_path is None
        ):
            raise ValueError("staging cleanup code and path must occur together")
        if self.staging_cleanup_code == "" or self.staging_cleanup_path == "":
            raise ValueError("staging cleanup diagnostics must not be empty")
        if (
            self.persistence_result is not None
            and type(self.persistence_result) is not AudioPersistenceResult
        ):
            raise TypeError(
                "persistence_result must be an exact AudioPersistenceResult or None"
            )

        if self.status is ProductionAudioStatus.PERSISTED:
            if type(self.reference) is not PersistedAudioReference:
                raise TypeError("PERSISTED requires an exact reference")
            if self.code is not None or self.reason is not None:
                raise ValueError("PERSISTED forbids code and reason")
            result = self.persistence_result
            if (
                result is None
                or result.initial_attempt_permitted is not True
                or result.restart_required is not False
                or result.code is not None
                or result.reason is not None
                or type(result.fact) is not VerifiedAudioFact
                or not _reference_matches_fact(self.reference, result.fact)
            ):
                raise ValueError(
                    "PERSISTED requires the exact successful persistence result"
                )
            if result.fact.run_id != self.run_id:
                raise ValueError("PERSISTED run identity does not match its fact")
            return

        if self.reference is not None:
            raise ValueError("FAILED forbids a reference")
        if type(self.code) is not str or not self.code:
            raise ValueError("FAILED requires a non-empty code")
        if self.persistence_result is not None:
            result = self.persistence_result
            if (
                result.initial_attempt_permitted is not False
                or type(result.code) is not str
                or not result.code
                or self.code != result.code
                or self.reason != result.reason
            ):
                raise ValueError(
                    "FAILED must preserve the exact failed persistence result"
                )


def _quantize_pcm16(samples: numpy.ndarray) -> numpy.ndarray:
    clipped = numpy.clip(samples.astype(numpy.float64), -1.0, 1.0)
    return numpy.where(
        clipped >= 0.0,
        numpy.floor(clipped * 32767.0 + 0.5),
        numpy.ceil(clipped * 32768.0 - 0.5),
    ).astype(numpy.dtype("<i2"))


def _build_reference(
    result: AudioPersistenceResult,
    storage_kind: AudioStorageKind,
    normalized_final_path: str,
) -> PersistedAudioReference:
    fact = result.fact
    if type(fact) is not VerifiedAudioFact:
        raise ValueError("successful persistence result requires an immutable fact")
    if normalize_absolute_path(fact.final_path) != normalized_final_path:
        raise ValueError("persisted fact final path does not match the request")
    return PersistedAudioReference(
        reference_id=fact.audio_id,
        audio_data_id=fact.audio_id,
        storage_kind=storage_kind,
        absolute_path=normalized_final_path,
        sample_rate_hz=fact.sample_rate_hz,
        channel_count=fact.channel_count,
        frame_count=fact.frame_count,
        dtype="<i2",
        content_revision=f"{fact.audio_id}:{fact.byte_size}",
    )


def _cleanup_staging(path: Path) -> tuple[str | None, str | None]:
    try:
        path.unlink()
    except OSError:
        return "AUDIO_CAPTURE_STAGING_CLEANUP_FAILED", str(path)
    return None, None


class _OwnedLoadedAudio:
    __slots__ = (
        "_samples",
        "_sample_rate_hz",
        "_channel_count",
        "_frame_count",
    )

    def __init__(
        self,
        samples: numpy.ndarray,
        sample_rate_hz: int,
        channel_count: int,
        frame_count: int,
    ) -> None:
        self._samples: numpy.ndarray | None = samples
        self._sample_rate_hz = sample_rate_hz
        self._channel_count = channel_count
        self._frame_count = frame_count

    @property
    def samples(self) -> object:
        if self._samples is None:
            raise RuntimeError("loaded audio is closed")
        return self._samples

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def channel_count(self) -> int:
        return self._channel_count

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def close(self) -> None:
        if self._samples is not None:
            self._samples = None


def _raise_load_error(
    category: AnalysisErrorCategory,
    summary: str,
    *,
    stage: AnalysisStage = AnalysisStage.AUDIO_LOAD,
) -> None:
    raise AudioLoadError(
        AnalysisError(
            category=category,
            stage=stage,
            summary=summary,
        )
    )


def _check_cancel(cancel_token: CancellationToken) -> None:
    if cancel_token.is_cancelled:
        _raise_load_error(
            AnalysisErrorCategory.ANALYSIS_CANCELED,
            "analysis audio load canceled",
            stage=AnalysisStage.CANCEL,
        )


def _parse_revision(reference: PersistedAudioReference) -> int:
    audio_id, separator, byte_size_text = reference.content_revision.partition(":")
    if (
        separator != ":"
        or ":" in byte_size_text
        or reference.audio_data_id is None
        or reference.reference_id != reference.audio_data_id
        or audio_id != reference.audio_data_id
        or not byte_size_text.isascii()
        or not byte_size_text.isdecimal()
    ):
        _raise_load_error(
            AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
            "audio content revision is invalid",
        )
    try:
        byte_size = int(byte_size_text)
    except (ValueError, OverflowError):
        _raise_load_error(
            AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
            "audio content revision size is invalid",
        )
    if byte_size <= 0 or str(byte_size) != byte_size_text:
        _raise_load_error(
            AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
            "audio content revision size is invalid",
        )
    return byte_size


class ProductionRunAudioAdapter:
    """Single-threaded production boundary for persisted PCM16 run audio."""

    def __init__(
        self,
        persistence_service: AudioPersistenceService,
        *,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self._persistence_service = persistence_service
        self._utc_now = utc_now or (
            lambda: datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        )

    @staticmethod
    def _bounded_reason(value: str | None) -> str:
        encoded = (value or "audio persistence failed").encode("utf-8")
        if len(encoded) <= 512:
            return encoded.decode("utf-8")
        return encoded[:512].decode("utf-8", errors="ignore")

    @classmethod
    def _coordinator_outcome(cls, request: object, result: ProductionAudioResult):
        from app.service.multi_run_coordinator_mvp.contracts import (
            AudioSaveOutcome,
            AudioSaveStatus,
            PersistedAudioFormat,
            PersistedAudioReference as CoordinatorPersistedAudioReference,
            TerminalErrorCategory,
        )

        if result.status is ProductionAudioStatus.PERSISTED:
            fact = result.persistence_result.fact
            persisted = CoordinatorPersistedAudioReference(
                audio_id=fact.audio_id,
                save_job_id=fact.save_job_id,
                run_id=fact.run_id,
                source_kind=request.source_kind,
                source_identity=fact.source_identity,
                final_path=fact.final_path,
                sample_rate_hz=fact.sample_rate_hz,
                channel_count=fact.channel_count,
                frame_count=fact.frame_count,
                byte_size=fact.byte_size,
                format=PersistedAudioFormat.PCM16_WAV,
                content_revision=fact.content_revision,
            )
            return AudioSaveOutcome(
                save_job_id=request.save_job_id,
                run_id=request.run_id,
                source_kind=request.source_kind,
                source_identity=request.source_identity,
                status=AudioSaveStatus.SAVED,
                persisted_audio=persisted,
                error_category=None,
                reason="",
            )
        status = (
            AudioSaveStatus.PATH_EXISTS
            if result.code == "AUDIO_PATH_COLLISION"
            else AudioSaveStatus.FAILED
        )
        category = (
            TerminalErrorCategory.AUDIO_PATH_EXISTS
            if status is AudioSaveStatus.PATH_EXISTS
            else TerminalErrorCategory.AUDIO_SAVE_FAILED
        )
        reason = result.reason
        if result.code == "AUDIO_DB_STATE_UNKNOWN":
            reason = (
                result.code
                if not reason
                else f"{result.code}: {reason}"
            )
        return AudioSaveOutcome(
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            status=status,
            persisted_audio=None,
            error_category=category,
            reason=cls._bounded_reason(reason),
        )

    @classmethod
    def _cancelled_outcome(cls, request: object):
        from app.service.multi_run_coordinator_mvp.contracts import (
            AudioSaveOutcome,
            AudioSaveStatus,
            TerminalErrorCategory,
        )

        return AudioSaveOutcome(
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            status=AudioSaveStatus.CANCELLED,
            persisted_audio=None,
            error_category=TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
            reason="audio save cancelled",
        )

    def save_captured(self, request: object, cancel_token: object):
        from app.service.multi_run_coordinator_mvp.contracts import (
            CapturedAudioSaveRequest as CoordinatorCapturedAudioSaveRequest,
            CapturedSourceFormat,
            SaveCancelToken,
        )

        if (
            type(request) is not CoordinatorCapturedAudioSaveRequest
            or type(cancel_token) is not SaveCancelToken
        ):
            raise TypeError("captured save requires exact Coordinator contracts")
        if cancel_token.is_cancel_requested():
            return self._cancelled_outcome(request)
        audio = request.audio
        if audio.format is CapturedSourceFormat.FLOAT32_INTERLEAVED:
            samples = numpy.frombuffer(
                audio.interleaved_bytes, dtype=numpy.dtype("<f4")
            ).reshape((audio.frame_count, audio.channel_count)).copy(order="C")
        else:
            pcm = numpy.frombuffer(
                audio.interleaved_bytes, dtype=numpy.dtype("<i2")
            ).reshape((audio.frame_count, audio.channel_count))
            samples = numpy.where(
                pcm >= 0,
                pcm.astype(numpy.float64) / 32767.0,
                pcm.astype(numpy.float64) / 32768.0,
            ).astype(numpy.float32)
        result = self.persist_captured(
            CapturedAudioSaveRequest(
                save_job_id=request.save_job_id,
                run_id=request.run_id,
                source_identity=request.source_identity,
                content_revision=1,
                samples=samples,
                sample_rate_hz=audio.sample_rate_hz,
                channel_count=audio.channel_count,
                frame_count=audio.frame_count,
                final_path=request.final_path_request.final_path,
                now=self._utc_now(),
            )
        )
        return self._coordinator_outcome(request, result)

    def save_imported(self, request: object, cancel_token: object):
        from app.service.multi_run_coordinator_mvp.contracts import (
            ImportedAudioSaveRequest as CoordinatorImportedAudioSaveRequest,
            SaveCancelToken,
        )

        if (
            type(request) is not CoordinatorImportedAudioSaveRequest
            or type(cancel_token) is not SaveCancelToken
        ):
            raise TypeError("imported save requires exact Coordinator contracts")
        if cancel_token.is_cancel_requested():
            return self._cancelled_outcome(request)
        result = self.persist_imported(
            ImportedAudioSaveRequest(
                save_job_id=request.save_job_id,
                run_id=request.run_id,
                source_identity=request.source_identity,
                content_revision=1,
                source_path=request.source.absolute_path,
                final_path=request.final_path_request.final_path,
                expected_sample_rate_hz=request.metadata.expected_sample_rate_hz,
                expected_channel_count=request.metadata.expected_channel_count,
                expected_frame_count=request.metadata.expected_frame_count,
                now=self._utc_now(),
            )
        )
        return self._coordinator_outcome(request, result)

    @staticmethod
    def _validate_captured_request(request: object) -> CapturedAudioSaveRequest:
        if type(request) is not CapturedAudioSaveRequest:
            raise ProductionAudioInputError(
                "request must be an exact CapturedAudioSaveRequest"
            )
        _validate_captured_samples(
            request.samples, request.frame_count, request.channel_count
        )
        return request

    @staticmethod
    def _validate_imported_request(request: object) -> ImportedAudioSaveRequest:
        if type(request) is not ImportedAudioSaveRequest:
            raise ProductionAudioInputError(
                "request must be an exact ImportedAudioSaveRequest"
            )
        return request

    @staticmethod
    def _from_persistence(
        request: CapturedAudioSaveRequest | ImportedAudioSaveRequest,
        result: object,
        storage_kind: AudioStorageKind,
        *,
        staging_cleanup_code: str | None = None,
        staging_cleanup_path: str | None = None,
    ) -> ProductionAudioResult:
        if type(result) is not AudioPersistenceResult:
            raise TypeError(
                "persistence service must return an exact AudioPersistenceResult"
            )
        if result.initial_attempt_permitted is True:
            reference = _build_reference(result, storage_kind, request.final_path)
            return ProductionAudioResult(
                status=ProductionAudioStatus.PERSISTED,
                run_id=request.run_id,
                reference=reference,
                code=None,
                reason=None,
                persistence_result=result,
                staging_cleanup_code=staging_cleanup_code,
                staging_cleanup_path=staging_cleanup_path,
            )
        return ProductionAudioResult(
            status=ProductionAudioStatus.FAILED,
            run_id=request.run_id,
            reference=None,
            code=result.code,
            reason=result.reason,
            persistence_result=result,
            staging_cleanup_code=staging_cleanup_code,
            staging_cleanup_path=staging_cleanup_path,
        )

    def persist_captured(
        self, request: CapturedAudioSaveRequest
    ) -> ProductionAudioResult:
        request = self._validate_captured_request(request)
        final_path = Path(request.final_path)
        staging_path = final_path.with_name(
            f".{final_path.name}.{uuid4()}.captured.wav"
        )
        descriptor: int | None = None
        staging_owned = False
        try:
            descriptor = os.open(
                staging_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            staging_owned = True
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                with wave.open(output, "wb") as writer:
                    writer.setframerate(request.sample_rate_hz)
                    writer.setnchannels(request.channel_count)
                    writer.setsampwidth(2)
                    writer.writeframes(
                        _quantize_pcm16(request.samples).tobytes(order="C")
                    )
                output.flush()
                os.fsync(output.fileno())
        except Exception as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            cleanup_code = None
            cleanup_path = None
            if staging_owned:
                cleanup_code, cleanup_path = _cleanup_staging(staging_path)
            return ProductionAudioResult(
                status=ProductionAudioStatus.FAILED,
                run_id=request.run_id,
                reference=None,
                code="AUDIO_CAPTURE_STAGING_WRITE_FAILED",
                reason=str(exc),
                persistence_result=None,
                staging_cleanup_code=cleanup_code,
                staging_cleanup_path=cleanup_path,
            )

        try:
            result = self._persistence_service.persist(
                run_id=request.run_id,
                save_job_id=request.save_job_id,
                source_kind="CAPTURED_AUDIO",
                source_identity=request.source_identity,
                content_revision=request.content_revision,
                source_path=str(staging_path),
                final_path=request.final_path,
                sample_rate_hz=request.sample_rate_hz,
                channel_count=request.channel_count,
                sample_width_bytes=2,
                expected_frame_count=request.frame_count,
                source_frame_count=request.frame_count,
                now=request.now,
            )
        except Exception:
            _cleanup_staging(staging_path)
            raise
        cleanup_code, cleanup_path = _cleanup_staging(staging_path)
        return self._from_persistence(
            request,
            result,
            AudioStorageKind.CAPTURED_FINAL_WAV,
            staging_cleanup_code=cleanup_code,
            staging_cleanup_path=cleanup_path,
        )

    def persist_imported(
        self, request: ImportedAudioSaveRequest
    ) -> ProductionAudioResult:
        request = self._validate_imported_request(request)
        result = self._persistence_service.persist(
            run_id=request.run_id,
            save_job_id=request.save_job_id,
            source_kind="IMPORTED_SOURCE",
            source_identity=request.source_identity,
            content_revision=request.content_revision,
            source_path=request.source_path,
            final_path=request.final_path,
            sample_rate_hz=request.expected_sample_rate_hz,
            channel_count=request.expected_channel_count,
            sample_width_bytes=2,
            expected_frame_count=request.expected_frame_count,
            source_frame_count=request.expected_frame_count,
            now=request.now,
        )
        return self._from_persistence(
            request, result, AudioStorageKind.IMPORTED_FINAL_AUDIO
        )

    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: CancellationToken,
    ) -> _OwnedLoadedAudio:
        if type(reference) is not PersistedAudioReference:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                "audio reference is invalid",
            )
        if reference.dtype != "<i2":
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                "audio dtype is not little-endian PCM16",
            )
        expected_byte_size = _parse_revision(reference)
        try:
            normalized_path = normalize_absolute_path(reference.absolute_path)
        except (OSError, ValueError):
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                "audio path is invalid",
            )
        if normalized_path != reference.absolute_path:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                "audio path normalization changed",
            )

        _check_cancel(cancel_token)
        path = Path(normalized_path)
        try:
            file_stat = path.stat()
        except (OSError, ValueError):
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                "audio reference is unreadable",
            )
        if not stat.S_ISREG(file_stat.st_mode):
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                "audio reference is not a regular file",
            )
        if file_stat.st_size != expected_byte_size:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                "audio reference byte size changed",
            )

        try:
            reader = wave.open(str(path), "rb")
        except OSError:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                "audio reference could not be opened",
            )
        except (EOFError, wave.Error):
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                "audio reference is not a valid WAV",
            )

        try:
            with reader:
                try:
                    compression = reader.getcomptype()
                    width = reader.getsampwidth()
                    rate = reader.getframerate()
                    channels = reader.getnchannels()
                    frames = reader.getnframes()
                except (EOFError, OSError, wave.Error):
                    _raise_load_error(
                        AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                        "audio metadata could not be read",
                    )
                if compression != "NONE" or width != 2:
                    _raise_load_error(
                        AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                        "audio WAV is not uncompressed PCM16",
                    )
                if (
                    rate != reference.sample_rate_hz
                    or channels != reference.channel_count
                    or frames != reference.frame_count
                ):
                    _raise_load_error(
                        AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                        "audio metadata changed",
                    )
                try:
                    payload = reader.readframes(frames)
                except (EOFError, OSError, wave.Error):
                    _raise_load_error(
                        AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                        "audio frames could not be read",
                    )
        except AudioLoadError:
            raise
        except Exception:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                "audio loading failed",
            )

        expected_payload_size = frames * channels * 2
        if len(payload) != expected_payload_size:
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                "audio PCM payload is truncated",
            )
        _check_cancel(cancel_token)
        try:
            samples = (
                numpy.frombuffer(payload, dtype=numpy.dtype("<i2"))
                .reshape((frames, channels))
                .copy(order="C")
            )
        except (TypeError, ValueError):
            _raise_load_error(
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                "audio PCM payload could not be decoded",
            )
        return _OwnedLoadedAudio(samples, rate, channels, frames)
