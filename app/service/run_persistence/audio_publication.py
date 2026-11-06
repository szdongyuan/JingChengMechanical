"""Close, verify, and atomically publish one completed uncompressed WAV."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shutil
import stat
from typing import BinaryIO, Callable
from uuid import uuid4

from app.service.audio_wav import probe_wav

from app.service.run_persistence.contracts import (
    ContractValidationError,
    VerifiedAudioFact,
    _issue_verified_audio_fact,
)


class AudioPublicationCode(str, Enum):
    AUDIO_WRITE_FAILED = "AUDIO_WRITE_FAILED"
    AUDIO_CLOSE_FAILED = "AUDIO_CLOSE_FAILED"
    AUDIO_VERIFY_FAILED = "AUDIO_VERIFY_FAILED"
    AUDIO_PATH_COLLISION = "AUDIO_PATH_COLLISION"
    AUDIO_FINALIZE_FAILED = "AUDIO_FINALIZE_FAILED"
    AUDIO_FINAL_SIZE_MISMATCH = "AUDIO_FINAL_SIZE_MISMATCH"
    AUDIO_TEMP_CLEANUP_FAILED = "AUDIO_TEMP_CLEANUP_FAILED"


@dataclass(frozen=True)
class PublishedAudioMetadata:
    final_path: str
    container_format: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    duration_ms: int
    byte_size: int


@dataclass(frozen=True)
class AudioPublicationResult:
    ok: bool
    metadata: PublishedAudioMetadata | None = None
    code: AudioPublicationCode | None = None
    reason: str | None = None
    cleanup_code: AudioPublicationCode | None = None
    cleanup_path: str | None = None
    cleanup_reason: str | None = None


def verify_wav_pcm(
    path: str | Path,
    expected_sample_rate_hz: int,
    expected_channel_count: int,
    expected_sample_width_bytes: int,
    expected_frame_count: int | None = None,
) -> PublishedAudioMetadata:
    """Keep the PCM-only verification contract for legacy capture callers."""
    metadata = verify_wav(
        path, expected_sample_rate_hz, expected_channel_count,
        expected_sample_width_bytes, expected_frame_count,
    )
    if metadata.container_format != "WAV_PCM":
        raise ValueError("WAV sample format is not PCM")
    return metadata


def verify_wav(
    path: str | Path,
    expected_sample_rate_hz: int,
    expected_channel_count: int,
    expected_sample_width_bytes: int,
    expected_frame_count: int | None = None,
) -> PublishedAudioMetadata:
    """Verify shared header facts and the complete declared payload, without encoding."""
    wav = probe_wav(path)
    for name, actual, expected in (
        ("sample rate", wav.sample_rate_hz, expected_sample_rate_hz),
        ("channel count", wav.channel_count, expected_channel_count),
        ("sample width", wav.sample_width_bytes, expected_sample_width_bytes),
        ("frame count", wav.frame_count, expected_frame_count),
    ):
        if expected is not None and actual != expected:
            raise ValueError(f"WAV {name} does not match")
    return PublishedAudioMetadata(
        final_path=wav.path,
        container_format=wav.container_format,
        sample_rate_hz=wav.sample_rate_hz,
        channel_count=wav.channel_count,
        frame_count=wav.frame_count,
        duration_ms=(wav.frame_count * 1000 + wav.sample_rate_hz - 1) // wav.sample_rate_hz,
        byte_size=wav.byte_size,
    )


class AudioPublication:
    """Publish a source WAV through a task-owned, same-directory temporary file."""

    def __init__(
        self, before_finalize: Callable[[], object] | None = None
    ) -> None:
        self._before_finalize = before_finalize

    def _write(self, source: Path, output: BinaryIO) -> None:
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        output.flush()
        os.fsync(output.fileno())

    def _close(self, output: BinaryIO) -> None:
        output.close()

    def _verify(
        self,
        path: Path,
        rate: int,
        channels: int,
        width: int,
        frames: int | None,
    ) -> PublishedAudioMetadata:
        return verify_wav(path, rate, channels, width, frames)

    def _finalize(self, temporary: Path, final: Path) -> None:
        # Hard-link creation is atomic, same-filesystem, and fails if final exists.
        # It deliberately has no overwrite or delete-then-rename fallback.
        os.link(temporary, final)

    def _cleanup(self, temporary: Path) -> None:
        temporary.unlink()

    def _final_size(self, final: Path) -> int:
        with final.open("rb") as artifact:
            artifact_stat = os.fstat(artifact.fileno())
            if not stat.S_ISREG(artifact_stat.st_mode):
                raise OSError("audio final is not a regular file")
            return artifact_stat.st_size

    def _failure(
        self,
        code: AudioPublicationCode,
        reason: Exception | str,
        temporary: Path | None,
        *,
        cleanup: bool = True,
    ) -> AudioPublicationResult:
        cleanup_code = None
        cleanup_path = None
        cleanup_reason = None
        if cleanup and temporary is not None:
            try:
                self._cleanup(temporary)
            except Exception as exc:
                cleanup_code = AudioPublicationCode.AUDIO_TEMP_CLEANUP_FAILED
                cleanup_path = str(temporary)
                cleanup_reason = str(exc)
        return AudioPublicationResult(
            ok=False,
            code=code,
            reason=str(reason),
            cleanup_code=cleanup_code,
            cleanup_path=cleanup_path,
            cleanup_reason=cleanup_reason,
        )

    def publish(
        self,
        source_path: str | Path,
        final_path: str | Path,
        expected_sample_rate_hz: int,
        expected_channel_count: int,
        expected_sample_width_bytes: int = 2,
        expected_frame_count: int | None = None,
    ) -> AudioPublicationResult:
        source = Path(source_path).resolve(strict=False)
        final = Path(final_path).resolve(strict=False)
        temporary = final.with_name(f".{final.name}.{uuid4()}.part")
        output: BinaryIO | None = None
        descriptor: int | None = None
        temp_owned = False

        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            temp_owned = True
            try:
                output = os.fdopen(descriptor, "wb")
            except Exception:
                try:
                    os.close(descriptor)
                finally:
                    descriptor = None
                raise
            descriptor = None
            self._write(source, output)
        except Exception as exc:
            if output is not None and not output.closed:
                try:
                    output.close()
                except Exception:
                    pass
            return self._failure(
                AudioPublicationCode.AUDIO_WRITE_FAILED,
                exc,
                temporary if temp_owned else None,
            )

        try:
            self._close(output)
        except Exception as exc:
            if not output.closed:
                try:
                    output.close()
                except Exception:
                    pass
            return self._failure(
                AudioPublicationCode.AUDIO_CLOSE_FAILED, exc, temporary
            )

        try:
            metadata = self._verify(
                temporary,
                expected_sample_rate_hz,
                expected_channel_count,
                expected_sample_width_bytes,
                expected_frame_count,
            )
        except Exception as exc:
            return self._failure(
                AudioPublicationCode.AUDIO_VERIFY_FAILED, exc, temporary
            )

        if self._before_finalize is not None:
            self._before_finalize()
        try:
            self._finalize(temporary, final)
        except FileExistsError as exc:
            return self._failure(
                AudioPublicationCode.AUDIO_PATH_COLLISION, exc, temporary
            )
        except Exception as exc:
            return self._failure(
                AudioPublicationCode.AUDIO_FINALIZE_FAILED, exc, temporary
            )

        try:
            self._cleanup(temporary)
        except Exception as exc:
            return AudioPublicationResult(
                ok=False,
                code=AudioPublicationCode.AUDIO_TEMP_CLEANUP_FAILED,
                reason=str(exc),
                cleanup_code=AudioPublicationCode.AUDIO_TEMP_CLEANUP_FAILED,
                cleanup_path=str(temporary),
                cleanup_reason=str(exc),
            )

        try:
            final_size = self._final_size(final)
        except OSError as exc:
            return self._failure(
                AudioPublicationCode.AUDIO_FINAL_SIZE_MISMATCH,
                f"final artifact could not be verified: {final}: {exc}",
                temporary=None,
                cleanup=False,
            )
        if final_size != metadata.byte_size:
            return self._failure(
                AudioPublicationCode.AUDIO_FINAL_SIZE_MISMATCH,
                f"final size does not match verified size: {final}",
                temporary=None,
                cleanup=False,
            )

        return AudioPublicationResult(
            ok=True,
            metadata=PublishedAudioMetadata(
                final_path=str(final),
                container_format=metadata.container_format,
                sample_rate_hz=metadata.sample_rate_hz,
                channel_count=metadata.channel_count,
                frame_count=metadata.frame_count,
                duration_ms=metadata.duration_ms,
                byte_size=metadata.byte_size,
            ),
        )


def _issue_verified_fact_after_publication(
    publication: AudioPublicationResult,
    *,
    audio_id: str,
    save_job_id: str,
    run_id: str,
    source_kind: str,
    source_identity: str,
    content_revision: int,
    now: str,
) -> VerifiedAudioFact:
    """Issue the exact repository value only from a successful publication."""

    if not publication.ok or publication.metadata is None:
        raise ContractValidationError(
            "VerifiedAudioFact requires successful audio publication"
        )
    metadata = publication.metadata
    return _issue_verified_audio_fact(
        audio_id=audio_id,
        save_job_id=save_job_id,
        run_id=run_id,
        source_kind=source_kind,
        source_identity=source_identity,
        content_revision=content_revision,
        final_path=metadata.final_path,
        container_format=metadata.container_format,
        sample_rate_hz=metadata.sample_rate_hz,
        channel_count=metadata.channel_count,
        frame_count=metadata.frame_count,
        duration_ms=metadata.duration_ms,
        byte_size=metadata.byte_size,
        closed_verified_at=now,
        created_at=now,
    )
