"""Disk-backed capture artifacts with quota-bound ownership transitions."""

from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Callable

import numpy as np

from .artifact_cleanup import ArtifactCleanupMixin, ArtifactCleanupReport
from .artifact_io import ArtifactFileIO
from .artifact_registry import ArtifactRegistry, DEFAULT_TOMBSTONE_LIMIT
from .artifact_writer import ArtifactEntry, CaptureArtifactWriter
from .artifact_session import SessionOwnership
from .contracts import (
    ArtifactState,
    CaptureErrorCode,
    CaptureLayerError,
    FrozenCaptureArtifact,
)

GIB = 1024**3
_SAFE_RUN_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class CaptureArtifactStore(ArtifactCleanupMixin):
    """Owns one random staging session and all artifact ownership CAS operations."""

    def __init__(
        self,
        staging_root: str | os.PathLike[str],
        *,
        quota_bytes: int,
        free_space_bytes: Callable[[Path], int] | None = None,
        minimum_free_bytes: int = GIB,
        remove_file: Callable[[Path], None] | None = None,
        tombstone_limit: int = DEFAULT_TOMBSTONE_LIMIT,
    ) -> None:
        if isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int):
            raise TypeError("quota_bytes must be an integer")
        if quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        if (
            isinstance(minimum_free_bytes, bool)
            or not isinstance(minimum_free_bytes, int)
            or minimum_free_bytes < 0
        ):
            raise ValueError("minimum_free_bytes must be a non-negative integer")

        self._root = Path(staging_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_ownership = SessionOwnership.create(self._root)
        self._session = self._session_ownership.directory
        self._quota_bytes = quota_bytes
        self._minimum_free_bytes = minimum_free_bytes
        self._file_io = ArtifactFileIO(
            self._root,
            free_space_bytes=free_space_bytes,
            remove_file=remove_file,
        )
        self._lock = threading.RLock()
        self._allocation_lock = threading.Lock()
        self._registry = ArtifactRegistry(tombstone_limit)
        self._entries = self._registry.live
        self._reserved_bytes = 0
        self._closing = False

    @property
    def session_directory(self) -> Path:
        return self._session

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    @property
    def live_artifact_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def tombstone_count(self) -> int:
        with self._lock:
            return self._registry.tombstone_count

    @property
    def idempotency_record_count(self) -> int:
        with self._lock:
            return self._registry.outcome_count

    def state(self, artifact_id: str) -> ArtifactState | None:
        with self._lock:
            return self._registry.state(artifact_id)

    def publication_pending(self, artifact_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(artifact_id)
            return bool(entry and entry.publication_pending)

    def allocate_writer(
        self,
        *,
        run_id: str,
        target_frames: int,
        channels: int,
        sample_rate: float,
        sample_start: int | None = None,
        pool_epoch: str,
        stream_generation: int,
        trigger_monotonic: float,
        capture_start_monotonic: float | None = None,
        time_quality: str | None = None,
    ) -> CaptureArtifactWriter:
        self._validate_allocation(
            run_id=run_id,
            target_frames=target_frames,
            channels=channels,
            sample_rate=sample_rate,
            sample_start=sample_start,
            pool_epoch=pool_epoch,
            stream_generation=stream_generation,
            trigger_monotonic=trigger_monotonic,
            capture_start_monotonic=capture_start_monotonic,
            time_quality=time_quality,
        )
        artifact_bytes = target_frames * channels * np.dtype(np.float32).itemsize
        artifact_id = uuid.uuid4().hex
        safe_run = _SAFE_RUN_COMPONENT.sub("_", run_id)[:80] or "capture"
        stem = f"{safe_run}-{artifact_id}"
        entry = ArtifactEntry(
            artifact_id=artifact_id,
            run_id=run_id,
            partial_path=self._session / f"{stem}.partial",
            ready_path=self._session / f"{stem}.ready",
            artifact_bytes=artifact_bytes,
            target_frames=target_frames,
            channels=channels,
            sample_rate=float(sample_rate),
            sample_start=sample_start,
            next_sample=sample_start,
            pool_epoch=pool_epoch,
            stream_generation=stream_generation,
            trigger_monotonic=float(trigger_monotonic),
            capture_start_monotonic=(
                None
                if capture_start_monotonic is None
                else float(capture_start_monotonic)
            ),
            time_quality=time_quality,
        )

        with self._allocation_lock, entry.io_lock:
            try:
                disk_free = self._file_io.current_free_bytes()
            except (OSError, TypeError, ValueError, OverflowError) as error:
                raise self._io_error(
                    "failed to check artifact free space", self._root, error
                ) from error
            disk_required = artifact_bytes + self._minimum_free_bytes
            with self._lock:
                if self._closing:
                    raise CaptureLayerError(
                        CaptureErrorCode.POOL_SHUTTING_DOWN,
                        "artifact store is closing",
                    )
                available_quota = self._quota_bytes - self._reserved_bytes
                if artifact_bytes > available_quota or disk_free < disk_required:
                    raise self._capacity_error(
                        artifact_bytes, available_quota, disk_free, disk_required
                    )
                entry.io_operation = "ALLOCATING"
                self._registry.add(entry)
                self._reserved_bytes += artifact_bytes
            try:
                mapping = self._file_io.create_mapping(entry)
            except (OSError, ValueError) as error:
                self._rollback_allocation_with_io(entry, None)
                raise self._io_error(
                    "failed to allocate artifact", entry.partial_path, error
                ) from error
            try:
                post_free = self._file_io.current_free_bytes()
            except (OSError, TypeError, ValueError, OverflowError) as error:
                self._rollback_allocation_with_io(entry, mapping)
                raise self._io_error(
                    "failed to verify artifact free space", self._root, error
                ) from error
            if post_free < self._minimum_free_bytes:
                self._rollback_allocation_with_io(entry, mapping)
                raise self._capacity_error(
                    artifact_bytes,
                    available_quota,
                    post_free,
                    self._minimum_free_bytes,
                )
            with self._lock:
                closing = self._closing
                if not closing:
                    entry.mapping = mapping
                    entry.io_operation = None
            if closing:
                self._rollback_allocation_with_io(entry, mapping)
                raise CaptureLayerError(
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "artifact store is closing",
                )
        return CaptureArtifactWriter(self, entry)

    def discard_partial(self, artifact_id: str) -> bool:
        with self._lock:
            entry = self._registry.get(artifact_id)
            if entry is None:
                return self._registry.state(artifact_id) is ArtifactState.DELETED
        return self._delete_entry(
            entry,
            allowed_states=(ArtifactState.PARTIAL,),
            final_state=ArtifactState.DELETED,
            close_mapping=True,
            idempotent_states=(ArtifactState.DELETED, ArtifactState.DELETE_PENDING),
        )

    def claim_ready(
        self, artifact_id: str, publication_token: str, consumer_id: str
    ) -> str | None:
        if not consumer_id:
            raise ValueError("consumer_id must be non-empty")
        with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None or publication_token != entry.publication_token:
                return None
            if entry.state in (ArtifactState.CLAIMED, ArtifactState.TRANSFERRED):
                if entry.consumer_id == consumer_id:
                    return entry.claim_token
                return None
            if (
                entry.state is not ArtifactState.READY_UNCLAIMED
                or not entry.publication_pending
            ):
                return None
            entry.state = ArtifactState.CLAIMED
            entry.publication_pending = False
            entry.consumer_id = consumer_id
            entry.claim_token = uuid.uuid4().hex
            self._registry.bind_claim(entry.claim_token, entry)
            return entry.claim_token

    def decline_publication(
        self, artifact_id: str, publication_token: str, reason: str
    ) -> bool:
        del reason
        outcome_token = f"{artifact_id}:{publication_token}"
        with self._lock:
            entry = self._registry.get(artifact_id)
            if entry is None:
                return self._registry.has_outcome("decline", outcome_token)
        with entry.io_lock:
            with self._lock:
                if publication_token != entry.publication_token:
                    return False
                if entry.decline_recorded:
                    return True
                if (
                    entry.state is not ArtifactState.READY_UNCLAIMED
                    or not entry.publication_pending
                ):
                    return False
                entry.publication_pending = False
                entry.decline_recorded = True
                self._registry.remember_outcome("decline", outcome_token)
                path, mapping = self._prepare_delete_locked(
                    entry, ArtifactState.DELETED, close_mapping=False
                )
            self._perform_delete_with_io(entry, path, mapping)
            return True

    def accept_transfer(self, claim_token: str) -> bool:
        with self._lock:
            entry = self._entry_for_claim_locked(claim_token)
            if entry is None:
                return False
            if entry.state is ArtifactState.TRANSFERRED:
                return True
            if entry.state is not ArtifactState.CLAIMED:
                return False
            entry.state = ArtifactState.TRANSFERRED
            return True

    def abandon_claim(self, claim_token: str, disposition: str) -> bool:
        del disposition
        with self._lock:
            entry = self._entry_for_claim_locked(claim_token)
            if entry is None:
                return self._registry.has_outcome("abandon", claim_token)
        with entry.io_lock:
            with self._lock:
                if (
                    entry.claim_token != claim_token
                    or entry.state is not ArtifactState.CLAIMED
                ):
                    return self._registry.has_outcome("abandon", claim_token)
                entry.abandoned_claim_token = claim_token
                self._registry.unbind_claim(claim_token)
                self._registry.remember_outcome("abandon", claim_token)
                entry.claim_token = None
                entry.consumer_id = None
                if not self._closing:
                    entry.state = ArtifactState.READY_UNCLAIMED
                    entry.publication_pending = True
                    return True
                path, mapping = self._prepare_delete_locked(
                    entry, ArtifactState.DELETED, close_mapping=False
                )
            self._perform_delete_with_io(entry, path, mapping)
            return True

    def release_transferred(
        self,
        claim_token: str,
        disposition: str,
        *,
        moved_path: str | os.PathLike[str] | None = None,
    ) -> bool:
        del disposition
        with self._lock:
            entry = self._entry_for_claim_locked(claim_token)
            if entry is None:
                return self._registry.has_outcome("release", claim_token)
        with entry.io_lock:
            with self._lock:
                if entry.claim_token != claim_token:
                    return False
                if entry.state in (
                    ArtifactState.RELEASED,
                    ArtifactState.DELETE_PENDING,
                ):
                    return True
                if entry.state is not ArtifactState.TRANSFERRED:
                    return False
            if moved_path is not None:
                destination = Path(moved_path)
                if not self._file_io.credible_moved_destination(entry, destination):
                    return False
                with self._lock:
                    if entry.state is not ArtifactState.TRANSFERRED:
                        return False
                    self._registry.remember_outcome("release", claim_token)
                    self._release_quota_locked(entry)
                    entry.state = ArtifactState.RELEASED
                    self._registry.retire(entry)
                return True
            with self._lock:
                self._registry.remember_outcome("release", claim_token)
                path, mapping = self._prepare_delete_locked(
                    entry,
                    ArtifactState.RELEASED,
                    close_mapping=False,
                    require_physical_unlink=True,
                )
            self._perform_delete_with_io(entry, path, mapping)
            return True

    def _validate_allocation(self, **values: object) -> None:
        for name in ("run_id", "pool_epoch"):
            if not isinstance(values[name], str) or not values[name]:
                raise ValueError(f"{name} must be a non-empty string")
        for name, minimum in (
            ("target_frames", 1),
            ("channels", 1),
            ("stream_generation", 1),
        ):
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in (
            "sample_rate",
            "trigger_monotonic",
        ):
            value = values[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not np.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        binding_names = (
            "sample_start",
            "capture_start_monotonic",
            "time_quality",
        )
        binding_present = [
            values[name] is not None for name in binding_names
        ]
        if any(binding_present) and not all(binding_present):
            raise ValueError(
                "capture start binding metadata must be all present or all absent"
            )
        if all(binding_present):
            sample_start = values["sample_start"]
            if (
                isinstance(sample_start, bool)
                or not isinstance(sample_start, int)
                or sample_start < 0
            ):
                raise ValueError("sample_start must be an integer >= 0")
            capture_start = values["capture_start_monotonic"]
            if (
                isinstance(capture_start, bool)
                or not isinstance(capture_start, (int, float))
                or not np.isfinite(capture_start)
                or capture_start <= 0
            ):
                raise ValueError(
                    "capture_start_monotonic must be positive and finite"
                )
            if (
                not isinstance(values["time_quality"], str)
                or not values["time_quality"]
            ):
                raise ValueError("time_quality must be a non-empty string")

    def _frozen_locked(self, entry: ArtifactEntry) -> FrozenCaptureArtifact:
        assert entry.publication_token is not None
        assert entry.sample_start is not None
        assert entry.capture_start_monotonic is not None
        assert entry.time_quality is not None
        return FrozenCaptureArtifact(
            run_id=entry.run_id,
            artifact_id=entry.artifact_id,
            path=str(entry.ready_path),
            format_version=1,
            dtype="float32",
            sample_rate=entry.sample_rate,
            channels=entry.channels,
            captured_frames=entry.target_frames,
            target_frames=entry.target_frames,
            sample_start=entry.sample_start,
            sample_end=entry.sample_start + entry.target_frames,
            pool_epoch=entry.pool_epoch,
            stream_generation=entry.stream_generation,
            ownership=ArtifactState.READY_UNCLAIMED,
            trigger_monotonic=entry.trigger_monotonic,
            capture_start_monotonic=entry.capture_start_monotonic,
            time_quality=entry.time_quality,
            publication_token=entry.publication_token,
        )

    def _entry_for_claim_locked(self, claim_token: str) -> ArtifactEntry | None:
        if not claim_token:
            return None
        return self._registry.claim(claim_token)

    def _prepare_delete_locked(
        self,
        entry: ArtifactEntry,
        final_state: ArtifactState,
        *,
        close_mapping: bool,
        require_physical_unlink: bool = False,
    ) -> tuple[Path, np.memmap | None]:
        path = entry.delete_path or entry.path
        mapping = entry.mapping if close_mapping else None
        if close_mapping:
            entry.mapping = None
        entry.delete_final_state = final_state
        entry.delete_path = path
        entry.require_physical_unlink = require_physical_unlink
        entry.state = ArtifactState.DELETE_PENDING
        entry.io_operation = "DELETING"
        return path, mapping

    def _perform_delete_with_io(
        self,
        entry: ArtifactEntry,
        path: Path,
        mapping: np.memmap | None,
    ) -> None:
        if mapping is not None:
            self._file_io.close_mapping(mapping, flush=False)
        if entry.require_physical_unlink:
            removed = self._file_io.remove_existing(path)
        else:
            removed = self._file_io.remove(path)
        self._settle_delete_locked(entry, removed)

    def _settle_delete_locked(self, entry: ArtifactEntry, removed: bool) -> None:
        with self._lock:
            entry.io_operation = None
            if removed:
                self._release_quota_locked(entry)
                entry.state = entry.delete_final_state
                self._registry.retire(entry)
            else:
                entry.state = ArtifactState.DELETE_PENDING

    def _delete_entry(
        self,
        entry: ArtifactEntry,
        *,
        allowed_states: tuple[ArtifactState, ...],
        final_state: ArtifactState,
        close_mapping: bool,
        idempotent_states: tuple[ArtifactState, ...] = (),
    ) -> bool:
        with entry.io_lock:
            with self._lock:
                if entry.state in idempotent_states:
                    return True
                if entry.state not in allowed_states or entry.io_operation is not None:
                    return False
                path, mapping = self._prepare_delete_locked(
                    entry, final_state, close_mapping=close_mapping
                )
            self._perform_delete_with_io(entry, path, mapping)
            return True

    def _rollback_allocation_with_io(
        self, entry: ArtifactEntry, mapping: np.memmap | None
    ) -> None:
        with self._lock:
            path, _ = self._prepare_delete_locked(
                entry, ArtifactState.DELETED, close_mapping=False
            )
        if mapping is not None:
            self._file_io.close_mapping(mapping, flush=False)
        removed = self._file_io.remove(path)
        self._settle_delete_locked(entry, removed)

    def _release_quota_locked(self, entry: ArtifactEntry) -> None:
        if entry.quota_released:
            return
        self._reserved_bytes -= entry.artifact_bytes
        entry.quota_released = True

    @staticmethod
    def _capacity_error(
        artifact_bytes: int,
        available_quota: int,
        disk_free: int,
        disk_required: int,
    ) -> CaptureLayerError:
        return CaptureLayerError(
            CaptureErrorCode.CAPTURE_STORAGE_CAPACITY,
            "insufficient artifact staging capacity",
            {
                "artifact_bytes": artifact_bytes,
                "available_quota_bytes": available_quota,
                "free_space_bytes": disk_free,
                "required_free_space_bytes": disk_required,
            },
        )

    @staticmethod
    def _io_error(message: str, path: Path, error: Exception) -> CaptureLayerError:
        return CaptureLayerError(
            CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED,
            message,
            {"path": str(path), "error": type(error).__name__},
        )


__all__ = [
    "ArtifactCleanupReport",
    "CaptureArtifactStore",
    "CaptureArtifactWriter",
]
