"""Atomic compressed storage for large successful analysis result payloads."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import threading
from uuid import uuid4
import zlib

from app.service.run_persistence.contracts import validate_uuid4


_REFERENCE_SCHEMA = "analysis-result-artifact/v1"
_CODEC = "zlib"
_MAX_RESULT_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSED_BYTES = _MAX_RESULT_BYTES + 1024 * 1024


class AnalysisResultArtifactError(RuntimeError):
    """A compressed result artifact could not be published or verified."""


@dataclass(frozen=True, slots=True)
class PublishedAnalysisResult:
    reference_json: str
    path: Path
    created: bool
    lease_id: str


@dataclass(slots=True)
class _PublicationState:
    inflight: int
    retained: bool


@dataclass(frozen=True, slots=True)
class _ArtifactReference:
    filename: str
    judgment: str
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _validate_payload_json(payload_json: str) -> bytes:
    if type(payload_json) is not str or not payload_json:
        raise AnalysisResultArtifactError(
            "analysis result payload must be a nonempty exact string"
        )
    try:
        raw = payload_json.encode("utf-8")
        json.loads(payload_json, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AnalysisResultArtifactError(
            "analysis result payload is not valid UTF-8 JSON"
        ) from None
    if len(raw) > _MAX_RESULT_BYTES:
        raise AnalysisResultArtifactError(
            "analysis result payload exceeds the artifact size limit"
        )
    return raw


def _prevalidated_payload_bytes(payload_json: str) -> bytes:
    if type(payload_json) is not str or not payload_json:
        raise AnalysisResultArtifactError(
            "analysis result payload must be a nonempty exact string"
        )
    try:
        raw = payload_json.encode("utf-8")
    except UnicodeError:
        raise AnalysisResultArtifactError(
            "analysis result payload is not valid UTF-8"
        ) from None
    if len(raw) > _MAX_RESULT_BYTES:
        raise AnalysisResultArtifactError(
            "analysis result payload exceeds the artifact size limit"
        )
    return raw


def _decompress_exact(compressed: bytes, expected_bytes: int) -> bytes:
    if type(compressed) is not bytes or not compressed:
        raise AnalysisResultArtifactError(
            "analysis result artifact is empty or has an invalid type"
        )
    if not 0 < expected_bytes <= _MAX_RESULT_BYTES:
        raise AnalysisResultArtifactError(
            "analysis result artifact declares an invalid payload size"
        )
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, expected_bytes + 1)
        if len(raw) > expected_bytes or decompressor.unconsumed_tail:
            raise AnalysisResultArtifactError(
                "analysis result artifact expands beyond its declared size"
            )
        raw += decompressor.flush()
    except zlib.error as exc:
        raise AnalysisResultArtifactError(
            "analysis result artifact decompression failed"
        ) from exc
    if (
        len(raw) != expected_bytes
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise AnalysisResultArtifactError(
            "analysis result artifact size or stream boundary is invalid"
        )
    return raw


class AnalysisResultArtifactStore:
    """Publish zlib payloads before their compact database references commit."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._lock = threading.RLock()
        self._publication_states: dict[Path, _PublicationState] = {}
        self._publication_leases: dict[str, Path] = {}

    @property
    def root(self) -> Path:
        return self._root

    def ensure_ready(self) -> None:
        with self._lock:
            self._ensure_ready_locked()

    def _ensure_ready_locked(self) -> None:
        probe_id = uuid4().hex
        temporary_path = self._root / f".{probe_id}.probe.part"
        final_path = self._root / f".{probe_id}.probe"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if not self._root.is_dir():
                raise NotADirectoryError(str(self._root))
            with temporary_path.open("xb") as probe:
                probe.write(b"analysis-result-artifact-probe")
                probe.flush()
                os.fsync(probe.fileno())
            os.replace(temporary_path, final_path)
            if final_path.read_bytes() != b"analysis-result-artifact-probe":
                raise OSError("analysis result artifact probe changed")
        except Exception as exc:
            raise AnalysisResultArtifactError(
                f"analysis result artifact directory is unavailable: {exc}"
            ) from exc
        finally:
            for path in (temporary_path, final_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def is_reference(payload_json: str | None) -> bool:
        if type(payload_json) is not str:
            return False
        try:
            value = json.loads(
                payload_json,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, RecursionError):
            return False
        return (
            type(value) is dict
            and value.get("schema") == _REFERENCE_SCHEMA
        )

    def publish(
        self,
        attempt_id: str,
        judgment: str,
        payload_json: str,
        *,
        precompressed_zlib: bytes | None = None,
        prevalidated_json: bool = False,
    ) -> PublishedAnalysisResult:
        if type(prevalidated_json) is not bool:
            raise TypeError("prevalidated_json must be an exact bool")
        with self._lock:
            return self._publish_locked(
                attempt_id,
                judgment,
                payload_json,
                precompressed_zlib=precompressed_zlib,
                prevalidated_json=prevalidated_json,
            )

    def _publish_locked(
        self,
        attempt_id: str,
        judgment: str,
        payload_json: str,
        *,
        precompressed_zlib: bytes | None,
        prevalidated_json: bool,
    ) -> PublishedAnalysisResult:
        validate_uuid4(attempt_id)
        if judgment not in {"PASS", "FAIL"}:
            raise AnalysisResultArtifactError(
                "analysis result artifact judgment is invalid"
            )
        raw = (
            _prevalidated_payload_bytes(payload_json)
            if prevalidated_json
            else _validate_payload_json(payload_json)
        )
        digest = hashlib.sha256(raw).hexdigest()
        if precompressed_zlib is None:
            compressed = zlib.compress(raw, level=3)
        else:
            if type(precompressed_zlib) is not bytes:
                raise AnalysisResultArtifactError(
                    "precompressed analysis result must be exact bytes"
                )
            compressed = precompressed_zlib
        filename = f"{attempt_id}.{digest}.json.zlib"
        reference = _ArtifactReference(
            filename=filename,
            judgment=judgment,
            sha256=digest,
            uncompressed_bytes=len(raw),
            compressed_bytes=len(compressed),
        )
        reference_json = self._encode_reference(reference)
        final_path = self._path_for(reference)
        temporary_path = self._root / (
            f".{attempt_id}.{uuid4().hex}.part"
        )
        created = False
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if not self._root.is_dir():
                raise NotADirectoryError(str(self._root))
            created = not final_path.exists()
            with temporary_path.open("xb") as artifact:
                artifact.write(compressed)
                artifact.flush()
                os.fsync(artifact.fileno())
            self._verify_path(final_path=temporary_path, reference=reference)
            if final_path.exists():
                try:
                    self._verify_path(
                        final_path=final_path,
                        reference=reference,
                    )
                except AnalysisResultArtifactError:
                    os.replace(temporary_path, final_path)
                else:
                    temporary_path.unlink(missing_ok=True)
            else:
                os.replace(temporary_path, final_path)
            self._fsync_root_best_effort()
        except Exception as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, AnalysisResultArtifactError):
                raise
            raise AnalysisResultArtifactError(
                f"analysis result artifact publication failed: {exc}"
            ) from exc
        state = self._publication_states.get(final_path)
        if state is None:
            state = _PublicationState(
                inflight=0,
                retained=not created,
            )
            self._publication_states[final_path] = state
        state.inflight += 1
        lease_id = uuid4().hex
        self._publication_leases[lease_id] = final_path
        return PublishedAnalysisResult(
            reference_json=reference_json,
            path=final_path,
            created=created,
            lease_id=lease_id,
        )

    def settle_publication(
        self,
        published: PublishedAnalysisResult,
        *,
        retain: bool,
    ) -> None:
        if type(published) is not PublishedAnalysisResult:
            raise TypeError("published must be an exact PublishedAnalysisResult")
        if type(retain) is not bool:
            raise TypeError("retain must be an exact bool")
        with self._lock:
            path = self._publication_leases.pop(
                published.lease_id,
                None,
            )
            if path is None or path != published.path:
                raise AnalysisResultArtifactError(
                    "analysis result publication lease is invalid"
                )
            state = self._publication_states.get(path)
            if state is None or state.inflight <= 0:
                raise AnalysisResultArtifactError(
                    "analysis result publication state is invalid"
                )
            state.inflight -= 1
            state.retained = state.retained or retain
            if state.inflight != 0:
                return
            try:
                if not state.retained:
                    path.unlink(missing_ok=True)
            finally:
                self._publication_states.pop(path, None)

    def _fsync_root_best_effort(self) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(self._root, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def load_reference(
        self,
        reference_json: str,
        *,
        expected_judgment: str | None = None,
    ) -> str:
        with self._lock:
            return self._load_reference_locked(
                reference_json,
                expected_judgment=expected_judgment,
            )

    def _load_reference_locked(
        self,
        reference_json: str,
        *,
        expected_judgment: str | None,
    ) -> str:
        reference = self._decode_reference(reference_json)
        if (
            expected_judgment is not None
            and reference.judgment != expected_judgment
        ):
            raise AnalysisResultArtifactError(
                "analysis result artifact judgment does not match the database"
            )
        raw = self._verify_path(
            final_path=self._path_for(reference),
            reference=reference,
        )
        try:
            payload_json = raw.decode("utf-8")
            json.loads(payload_json, parse_constant=_reject_json_constant)
        except (UnicodeError, TypeError, ValueError, RecursionError):
            raise AnalysisResultArtifactError(
                "analysis result artifact contains invalid JSON"
            ) from None
        return payload_json

    def path_for_reference(self, reference_json: str) -> Path:
        return self._path_for(self._decode_reference(reference_json))

    def delete_reference(self, reference_json: str) -> Path | None:
        with self._lock:
            if not self.is_reference(reference_json):
                return None
            path = self.path_for_reference(reference_json)
            path.unlink(missing_ok=True)
            return path

    def _verify_path(
        self,
        *,
        final_path: Path,
        reference: _ArtifactReference,
    ) -> bytes:
        try:
            if final_path.stat().st_size != reference.compressed_bytes:
                raise AnalysisResultArtifactError(
                    "analysis result artifact compressed size does not match"
                )
            compressed = final_path.read_bytes()
        except OSError as exc:
            raise AnalysisResultArtifactError(
                f"analysis result artifact is unavailable: {final_path}"
            ) from exc
        if len(compressed) != reference.compressed_bytes:
            raise AnalysisResultArtifactError(
                "analysis result artifact compressed size does not match"
            )
        raw = _decompress_exact(compressed, reference.uncompressed_bytes)
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise AnalysisResultArtifactError(
                "analysis result artifact checksum does not match"
            )
        return raw

    def _path_for(self, reference: _ArtifactReference) -> Path:
        if (
            not reference.filename
            or Path(reference.filename).name != reference.filename
            or "/" in reference.filename
            or "\\" in reference.filename
        ):
            raise AnalysisResultArtifactError(
                "analysis result artifact filename is unsafe"
            )
        path = (self._root / reference.filename).resolve()
        if path.parent != self._root:
            raise AnalysisResultArtifactError(
                "analysis result artifact escapes its storage root"
            )
        return path

    @staticmethod
    def _encode_reference(reference: _ArtifactReference) -> str:
        return json.dumps(
            {
                "artifact_file": reference.filename,
                "codec": _CODEC,
                "compressed_bytes": reference.compressed_bytes,
                "judgment": reference.judgment,
                "schema": _REFERENCE_SCHEMA,
                "sha256": reference.sha256,
                "uncompressed_bytes": reference.uncompressed_bytes,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_reference(reference_json: str) -> _ArtifactReference:
        try:
            value = json.loads(
                reference_json,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, RecursionError):
            raise AnalysisResultArtifactError(
                "analysis result artifact reference is invalid JSON"
            ) from None
        expected_keys = {
            "artifact_file",
            "codec",
            "compressed_bytes",
            "judgment",
            "schema",
            "sha256",
            "uncompressed_bytes",
        }
        if type(value) is not dict or set(value) != expected_keys:
            raise AnalysisResultArtifactError(
                "analysis result artifact reference has invalid fields"
            )
        if value["schema"] != _REFERENCE_SCHEMA or value["codec"] != _CODEC:
            raise AnalysisResultArtifactError(
                "analysis result artifact reference version is unsupported"
            )
        filename = value["artifact_file"]
        judgment = value["judgment"]
        digest = value["sha256"]
        raw_size = value["uncompressed_bytes"]
        compressed_size = value["compressed_bytes"]
        if type(filename) is not str or not filename:
            raise AnalysisResultArtifactError(
                "analysis result artifact filename is invalid"
            )
        if judgment not in {"PASS", "FAIL"}:
            raise AnalysisResultArtifactError(
                "analysis result artifact judgment is invalid"
            )
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise AnalysisResultArtifactError(
                "analysis result artifact checksum is invalid"
            )
        if (
            type(raw_size) is not int
            or isinstance(raw_size, bool)
            or not 0 < raw_size <= _MAX_RESULT_BYTES
            or type(compressed_size) is not int
            or isinstance(compressed_size, bool)
            or not 0 < compressed_size <= _MAX_COMPRESSED_BYTES
        ):
            raise AnalysisResultArtifactError(
                "analysis result artifact sizes are invalid"
            )
        return _ArtifactReference(
            filename=filename,
            judgment=judgment,
            sha256=digest,
            uncompressed_bytes=raw_size,
            compressed_bytes=compressed_size,
        )


__all__ = [
    "AnalysisResultArtifactError",
    "AnalysisResultArtifactStore",
    "PublishedAnalysisResult",
]
