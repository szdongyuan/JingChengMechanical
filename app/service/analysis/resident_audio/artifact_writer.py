"""Sequential float32 memmap writer mechanics for resident capture artifacts."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .contracts import (
    ArtifactState,
    CaptureErrorCode,
    CaptureLayerError,
    CapturedAudioBlock,
    FrozenCaptureArtifact,
)

if TYPE_CHECKING:
    from .artifact_store import CaptureArtifactStore


@dataclass(frozen=True, slots=True)
class FileReceipt:
    device: int
    inode: int
    size: int


@dataclass(slots=True)
class ArtifactEntry:
    artifact_id: str
    run_id: str
    partial_path: Path
    ready_path: Path
    artifact_bytes: int
    target_frames: int
    channels: int
    sample_rate: float
    sample_start: int | None
    pool_epoch: str
    stream_generation: int
    trigger_monotonic: float
    capture_start_monotonic: float | None
    time_quality: str | None
    state: ArtifactState = ArtifactState.PARTIAL
    next_sample: int | None = None
    mapping: np.memmap | None = None
    publication_token: str | None = None
    publication_pending: bool = False
    consumer_id: str | None = None
    claim_token: str | None = None
    abandoned_claim_token: str | None = None
    decline_recorded: bool = False
    quota_released: bool = False
    delete_final_state: ArtifactState = ArtifactState.DELETED
    delete_path: Path | None = None
    require_physical_unlink: bool = False
    io_operation: str | None = None
    transfer_receipt: FileReceipt | None = None
    io_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def path(self) -> Path:
        if self.state is ArtifactState.PARTIAL:
            return self.partial_path
        return self.ready_path

    @property
    def capture_start_bound(self) -> bool:
        values = (
            self.sample_start,
            self.capture_start_monotonic,
            self.time_quality,
            self.next_sample,
        )
        return all(value is not None for value in values)


class CaptureArtifactWriter:
    """Single-owner sequential writer into one preallocated memmap."""

    def __init__(self, store: CaptureArtifactStore, entry: ArtifactEntry) -> None:
        self._store = store
        self._entry = entry

    @property
    def artifact_id(self) -> str:
        return self._entry.artifact_id

    @property
    def partial_path(self) -> Path:
        return self._entry.partial_path

    @property
    def written_frames(self) -> int:
        with self._store._lock:
            if self._entry.sample_start is None or self._entry.next_sample is None:
                return 0
            return self._entry.next_sample - self._entry.sample_start

    @property
    def capture_start_bound(self) -> bool:
        with self._store._lock:
            return self._entry.capture_start_bound

    def bind_capture_start(
        self,
        *,
        sample_start: int,
        capture_start_monotonic: float,
        time_quality: str,
    ) -> None:
        if (
            isinstance(sample_start, bool)
            or not isinstance(sample_start, int)
            or sample_start < 0
        ):
            raise ValueError("sample_start must be a non-negative integer")
        if (
            isinstance(capture_start_monotonic, bool)
            or not isinstance(capture_start_monotonic, (int, float))
            or not np.isfinite(capture_start_monotonic)
            or capture_start_monotonic <= 0
        ):
            raise ValueError(
                "capture_start_monotonic must be positive and finite"
            )
        if not isinstance(time_quality, str) or not time_quality:
            raise ValueError("time_quality must be a non-empty string")
        with self._entry.io_lock:
            with self._store._lock:
                if self._entry.capture_start_bound:
                    raise RuntimeError("capture start is already bound")
                if (
                    self._entry.state is not ArtifactState.PARTIAL
                    or self._entry.io_operation is not None
                    or self._entry.mapping is None
                ):
                    raise RuntimeError("artifact cannot bind capture start")
                self._entry.sample_start = sample_start
                self._entry.next_sample = sample_start
                self._entry.capture_start_monotonic = float(
                    capture_start_monotonic
                )
                self._entry.time_quality = time_quality

    def write(self, audio_block: CapturedAudioBlock) -> int:
        if not isinstance(audio_block, CapturedAudioBlock):
            raise TypeError("audio_block must be a CapturedAudioBlock")
        if audio_block.pool_epoch != self._entry.pool_epoch:
            raise ValueError("audio block pool epoch does not match artifact")
        if audio_block.stream_generation != self._entry.stream_generation:
            raise ValueError("audio block generation does not match artifact")
        return self.write_array(audio_block.sample_start, audio_block.data)

    def write_array(self, sample_start: int, data: np.ndarray) -> int:
        if isinstance(sample_start, bool) or not isinstance(sample_start, int):
            raise TypeError("sample_start must be an integer")
        if not isinstance(data, np.ndarray):
            raise TypeError("data must be a numpy.ndarray")
        if data.dtype != np.dtype(np.float32):
            raise ValueError("artifact writes require float32 data")
        if data.ndim != 2 or data.shape[1] != self._entry.channels:
            raise ValueError("artifact write channels do not match")
        if not data.flags.c_contiguous:
            raise ValueError("artifact writes require contiguous data")

        with self._entry.io_lock:
            with self._store._lock:
                if self._entry.state is not ArtifactState.PARTIAL:
                    raise RuntimeError("artifact is not writable")
                if not self._entry.capture_start_bound:
                    raise RuntimeError("artifact capture start is unbound")
                mapping = self._entry.mapping
                expected = self._entry.next_sample
                capture_start = self._entry.sample_start
            if mapping is None:
                raise RuntimeError("artifact mapping is closed")
            assert expected is not None and capture_start is not None

            source_end = sample_start + data.shape[0]
            capture_end = capture_start + self._entry.target_frames
            clipped_start = max(sample_start, capture_start)
            clipped_end = min(source_end, capture_end)
            if clipped_end <= clipped_start:
                return 0
            if clipped_start > expected:
                raise ValueError("gap in artifact write")
            if clipped_start < expected:
                raise ValueError("overlap in artifact write")

            source_offset = clipped_start - sample_start
            write_frames = clipped_end - clipped_start
            target_offset = clipped_start - capture_start
            mapping[target_offset : target_offset + write_frames] = data[
                source_offset : source_offset + write_frames
            ]
            with self._store._lock:
                if self._entry.state is not ArtifactState.PARTIAL:
                    raise RuntimeError("artifact stopped while writing")
                self._entry.next_sample = clipped_end
            return write_frames

    def freeze(self) -> FrozenCaptureArtifact:
        with self._entry.io_lock:
            with self._store._lock:
                if self._entry.state is ArtifactState.READY_UNCLAIMED:
                    return self._store._frozen_locked(self._entry)
                if (
                    self._entry.state is not ArtifactState.PARTIAL
                    or self._entry.io_operation is not None
                ):
                    raise RuntimeError("artifact cannot be frozen")
                if not self._entry.capture_start_bound:
                    raise RuntimeError("artifact capture start is unbound")
                assert (
                    self._entry.next_sample is not None
                    and self._entry.sample_start is not None
                )
                written = self._entry.next_sample - self._entry.sample_start
                if written != self._entry.target_frames:
                    raise ValueError("artifact does not contain exactly target_frames")
                mapping = self._entry.mapping
                self._entry.io_operation = "FREEZING"
            if mapping is None:
                with self._store._lock:
                    self._entry.io_operation = None
                raise RuntimeError("artifact mapping is closed")
            error = self._store._file_io.close_mapping(mapping, flush=True)
            if error is not None:
                with self._store._lock:
                    self._entry.mapping = None
                    path, detached = self._store._prepare_delete_locked(
                        self._entry,
                        ArtifactState.DELETED,
                        close_mapping=False,
                    )
                self._store._perform_delete_with_io(self._entry, path, detached)
                raise self._store._io_error(
                    "failed to flush artifact", self._entry.partial_path, error
                ) from error

            with self._store._lock:
                self._entry.mapping = None
                closing = self._store._closing
                if closing:
                    path, detached = self._store._prepare_delete_locked(
                        self._entry,
                        ArtifactState.DELETED,
                        close_mapping=False,
                    )
            if closing:
                self._store._perform_delete_with_io(self._entry, path, detached)
                raise CaptureLayerError(
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "artifact store is closing",
                    {"artifact_id": self._entry.artifact_id},
                )

            try:
                receipt = self._store._file_io.freeze(self._entry)
            except OSError as error:
                with self._store._lock:
                    path, detached = self._store._prepare_delete_locked(
                        self._entry,
                        ArtifactState.DELETED,
                        close_mapping=False,
                    )
                self._store._perform_delete_with_io(self._entry, path, detached)
                raise self._store._io_error(
                    "failed to freeze artifact", self._entry.partial_path, error
                ) from error
            with self._store._lock:
                self._entry.state = ArtifactState.READY_UNCLAIMED
                self._entry.publication_token = uuid.uuid4().hex
                self._entry.publication_pending = True
                self._entry.io_operation = None
                self._entry.transfer_receipt = receipt
                return self._store._frozen_locked(self._entry)

    def discard(self) -> bool:
        return self._store.discard_partial(self.artifact_id)


__all__ = ["CaptureArtifactWriter", "FileReceipt"]
