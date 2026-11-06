from __future__ import annotations

import multiprocessing
import os
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from app.service.analysis.resident_audio import (
    ArtifactState,
    CaptureArtifactStore,
    CaptureErrorCode,
    CaptureLayerError,
    CapturedAudioBlock,
)
from app.service.analysis.resident_audio.contracts import BlockTimeQuality
from app.service.analysis.resident_audio.artifact_session import safe_session_path
from app.service.analysis.resident_audio.artifact_settlement import (
    ArtifactDisposition,
    ArtifactQuotaOutcome,
    ArtifactSettlementFacade,
)


GIB = 1024**3


def _hold_session_in_child(staging_root: str, ready_queue) -> None:
    store = CaptureArtifactStore(
        staging_root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    live_file = store.session_directory / "child-live.partial"
    live_file.write_bytes(b"child-live")
    ready_queue.put((str(store.session_directory), str(live_file)))
    threading.Event().wait(60)


def make_store(
    tmp_path: Path,
    *,
    quota_bytes: int = 1_000,
    free_bytes: int = GIB + 1_000,
    remove_file=None,
    tombstone_limit: int = 1_024,
) -> CaptureArtifactStore:
    return CaptureArtifactStore(
        tmp_path / "staging",
        quota_bytes=quota_bytes,
        free_space_bytes=lambda _: free_bytes,
        minimum_free_bytes=GIB,
        remove_file=remove_file,
        tombstone_limit=tombstone_limit,
    )


def allocate(
    store: CaptureArtifactStore,
    *,
    run_id: str = "run-1",
    frames: int = 6,
    channels: int = 2,
):
    return store.allocate_writer(
        run_id=run_id,
        target_frames=frames,
        channels=channels,
        sample_rate=10.0,
        sample_start=100,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
        capture_start_monotonic=10.1,
        time_quality="CONFIRMED",
    )


def allocate_unbound(
    store: CaptureArtifactStore,
    *,
    run_id: str = "unbound-run",
    frames: int = 6,
    channels: int = 2,
):
    return store.allocate_writer(
        run_id=run_id,
        target_frames=frames,
        channels=channels,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
    )


def block(start: int, values: list[list[float]]) -> CapturedAudioBlock:
    data = np.asarray(values, dtype=np.float32)
    return CapturedAudioBlock(
        pool_epoch="epoch-1",
        hardware_id="mic-1",
        stream_generation=3,
        sample_start=start,
        sample_end=start + len(data),
        data=data,
        time_quality=BlockTimeQuality.CONFIRMED,
        monotonic_start=10.0,
        monotonic_end=10.0 + len(data) / 10.0,
    )


def frozen_artifact(store: CaptureArtifactStore, *, run_id: str = "run-1"):
    writer = allocate(store, run_id=run_id)
    writer.write(block(98, [[-2, -2], [-1, -1], [0, 10], [1, 11]]))
    writer.write(block(102, [[2, 12], [3, 13], [4, 14], [5, 15], [6, 16]]))
    return writer.freeze()


def test_reserves_quota_and_free_space_before_creating_partial(tmp_path: Path) -> None:
    quota_store = make_store(tmp_path / "quota", quota_bytes=47)
    with pytest.raises(CaptureLayerError) as quota_error:
        allocate(quota_store)
    assert quota_error.value.code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY
    assert quota_store.reserved_bytes == 0
    assert list(quota_store.session_directory.glob("*.partial")) == []

    disk_store = make_store(tmp_path / "disk", free_bytes=GIB + 47)
    with pytest.raises(CaptureLayerError) as disk_error:
        allocate(disk_store)
    assert disk_error.value.code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY
    assert disk_store.reserved_bytes == 0
    assert list(disk_store.session_directory.glob("*.partial")) == []


def test_unbound_allocation_preallocates_exact_shape_and_reserves_quota(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    writer = allocate_unbound(store)

    assert writer.capture_start_bound is False
    assert writer.written_frames == 0
    assert store.state(writer.artifact_id) is ArtifactState.PARTIAL
    assert store.reserved_bytes == 6 * 2 * np.dtype(np.float32).itemsize
    assert writer.partial_path.stat().st_size == store.reserved_bytes
    mapping = np.memmap(
        writer.partial_path,
        dtype=np.float32,
        mode="r",
        shape=(6, 2),
    )
    assert mapping.shape == (6, 2)


def test_unbound_writer_rejects_write_and_freeze_until_exactly_once_bind(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    writer = allocate_unbound(store)

    with pytest.raises(RuntimeError, match="unbound"):
        writer.write(block(100, [[0, 10], [1, 11]]))
    with pytest.raises(RuntimeError, match="unbound"):
        writer.freeze()

    writer.bind_capture_start(
        sample_start=100,
        capture_start_monotonic=10.1,
        time_quality="CONFIRMED",
    )
    assert writer.capture_start_bound is True
    writer.write(block(98, [[-2, -2], [-1, -1], [0, 10], [1, 11]]))
    writer.write(block(102, [[2, 12], [3, 13], [4, 14], [5, 15]]))
    artifact = writer.freeze()

    assert artifact.sample_start == 100
    assert artifact.sample_end == 106
    assert artifact.capture_start_monotonic == 10.1
    assert artifact.time_quality == "CONFIRMED"
    np.testing.assert_array_equal(
        np.memmap(
            artifact.path,
            dtype=np.float32,
            mode="r",
            shape=(6, 2),
        ),
        np.asarray(
            [[0, 10], [1, 11], [2, 12], [3, 13], [4, 14], [5, 15]],
            dtype=np.float32,
        ),
    )


@pytest.mark.parametrize(
    ("sample_start", "capture_start_monotonic", "time_quality"),
    [(100, 10.1, "CONFIRMED"), (101, 10.2, "RECOVERED")],
)
def test_second_capture_start_bind_is_deterministically_rejected(
    tmp_path: Path,
    sample_start: int,
    capture_start_monotonic: float,
    time_quality: str,
) -> None:
    writer = allocate_unbound(make_store(tmp_path))
    writer.bind_capture_start(
        sample_start=100,
        capture_start_monotonic=10.1,
        time_quality="CONFIRMED",
    )

    with pytest.raises(RuntimeError, match="already bound"):
        writer.bind_capture_start(
            sample_start=sample_start,
            capture_start_monotonic=capture_start_monotonic,
            time_quality=time_quality,
        )


def test_never_bound_partial_is_deleted_and_quota_released_on_cleanup(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    writer = allocate_unbound(store)
    partial_path = writer.partial_path

    report = store.begin_closing()

    assert str(partial_path) in report.deleted_paths
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0
    assert not partial_path.exists()


def test_exact_float32_memmap_shape_clipping_and_channel_order(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)

    assert artifact.ownership is ArtifactState.READY_UNCLAIMED
    assert artifact.captured_frames == artifact.target_frames == 6
    assert artifact.sample_start == 100
    assert artifact.sample_end == 106
    assert Path(artifact.path).suffix == ".ready"
    assert not any(store.session_directory.glob("*.partial"))
    audio = np.memmap(artifact.path, dtype=np.float32, mode="r", shape=(6, 2))
    np.testing.assert_array_equal(
        audio,
        np.asarray(
            [[0, 10], [1, 11], [2, 12], [3, 13], [4, 14], [5, 15]],
            dtype=np.float32,
        ),
    )


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        (
            block(100, [[0, 0], [1, 1]]),
            block(103, [[3, 3]]),
            "gap",
        ),
        (
            block(100, [[0, 0], [1, 1]]),
            block(101, [[1, 1], [2, 2]]),
            "overlap",
        ),
    ],
)
def test_writer_rejects_gaps_and_overlaps(
    tmp_path: Path,
    first: CapturedAudioBlock,
    second: CapturedAudioBlock,
    message: str,
) -> None:
    writer = allocate(make_store(tmp_path))
    writer.write(first)
    with pytest.raises(ValueError, match=message):
        writer.write(second)


def test_writer_rejects_dtype_channel_and_incomplete_freeze(tmp_path: Path) -> None:
    writer = allocate(make_store(tmp_path))
    wrong_channels = np.ones((2, 1), dtype=np.float32)
    with pytest.raises(ValueError, match="channels"):
        writer.write(block(100, wrong_channels.tolist()))

    wrong_dtype = np.ones((2, 2), dtype=np.float64)
    with pytest.raises(ValueError, match="float32"):
        writer.write_array(100, wrong_dtype)
    with pytest.raises(ValueError, match="target_frames"):
        writer.freeze()


def test_create_failure_rolls_back_without_file_or_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path, quota_bytes=48)

    def fail_create(_entry) -> None:
        raise OSError("injected create failure")

    monkeypatch.setattr(store._file_io, "create_mapping", fail_create)
    with pytest.raises(CaptureLayerError) as error:
        allocate(store)

    assert error.value.code is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
    assert store.reserved_bytes == 0
    assert store.live_artifact_count == 0
    assert not list(store.session_directory.glob("*.partial"))
    assert not list(store.session_directory.glob("*.ready"))


def test_mapping_failure_delete_pending_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_delete = True

    def remove(path: Path) -> None:
        if fail_delete:
            raise PermissionError("busy")
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)

    def fail_after_truncate(entry) -> None:
        with entry.partial_path.open("xb") as partial:
            partial.truncate(entry.artifact_bytes)
        raise OSError("injected mapping failure")

    monkeypatch.setattr(store._file_io, "create_mapping", fail_after_truncate)
    with pytest.raises(CaptureLayerError):
        allocate(store)
    assert store.reserved_bytes == 48
    assert store.live_artifact_count == 1
    partial = next(store.session_directory.glob("*.partial"))

    fail_delete = False
    report = store.retry_delete_pending(limit=1)
    assert report.deleted_paths == (str(partial),)
    assert store.reserved_bytes == 0
    assert store.live_artifact_count == 0
    assert store.retry_delete_pending(limit=1).deleted_paths == ()


def test_flush_close_failure_never_publishes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path, quota_bytes=48)
    writer = allocate(store)
    writer.write(block(100, [[0, 10], [1, 11], [2, 12], [3, 13], [4, 14], [5, 15]]))
    close_mapping = store._file_io.close_mapping

    def fail_close(mapping, *, flush: bool):
        close_mapping(mapping, flush=flush)
        return OSError("injected flush failure")

    monkeypatch.setattr(store._file_io, "close_mapping", fail_close)
    with pytest.raises(CaptureLayerError) as error:
        writer.freeze()

    assert error.value.code is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert not store.publication_pending(writer.artifact_id)
    assert store.reserved_bytes == 0
    assert not list(store.session_directory.glob("*.ready"))
    assert not list(store.session_directory.glob("*.partial"))


def test_atomic_rename_failure_delete_pending_retries_without_double_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_delete = True

    def remove(path: Path) -> None:
        if fail_delete:
            raise PermissionError("busy")
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    writer.write(block(100, [[0, 10], [1, 11], [2, 12], [3, 13], [4, 14], [5, 15]]))

    def fail_rename(_entry) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr(store._file_io, "freeze", fail_rename)
    with pytest.raises(CaptureLayerError) as error:
        writer.freeze()

    assert error.value.code is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
    assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING
    assert not store.publication_pending(writer.artifact_id)
    assert store.reserved_bytes == 48
    assert not list(store.session_directory.glob("*.ready"))

    fail_delete = False
    report = store.retry_delete_pending(limit=1)
    assert report.deleted_paths == (str(writer.partial_path),)
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0
    assert store.retry_delete_pending(limit=1).deleted_paths == ()


def test_exact_retry_reaches_target_behind_sixty_four_failed_pending_items(
    tmp_path: Path,
) -> None:
    old_paths: set[Path] = set()
    target_path: Path | None = None
    target_attempts = 0

    def remove(path: Path) -> None:
        nonlocal target_attempts
        if path in old_paths:
            raise PermissionError("permanently busy")
        if path == target_path:
            target_attempts += 1
            if target_attempts == 1:
                raise PermissionError("transiently busy")
        path.unlink()

    artifact_bytes = 4
    store = make_store(
        tmp_path,
        quota_bytes=65 * artifact_bytes,
        remove_file=remove,
    )
    old_ids: list[str] = []
    for index in range(64):
        writer = allocate(
            store,
            run_id=f"old-{index}",
            frames=1,
            channels=1,
        )
        old_paths.add(writer.partial_path)
        old_ids.append(writer.artifact_id)
        writer.discard()
        assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING

    target = allocate(store, run_id="target", frames=1, channels=1)
    target_path = target.partial_path
    target.discard()
    assert store.state(target.artifact_id) is ArtifactState.DELETE_PENDING

    receipt = ArtifactSettlementFacade(store).discard(target.artifact_id)

    assert receipt.publication_resolved is True
    assert receipt.disposition is ArtifactDisposition.DELETED
    assert receipt.quota_outcome is ArtifactQuotaOutcome.RELEASED
    assert receipt.complete is True
    assert store.state(target.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 64 * artifact_bytes
    assert all(
        store.state(artifact_id) is ArtifactState.DELETE_PENDING
        for artifact_id in old_ids
    )
    assert target_attempts == 2


def test_exact_retry_missing_wrong_state_and_invalid_ids_do_not_mutate(
    tmp_path: Path,
) -> None:
    removed: list[Path] = []

    def remove(path: Path) -> None:
        removed.append(path)
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    expected_empty = store.retry_delete_pending(limit=0)
    reserved = store.reserved_bytes

    assert store.retry_delete_pending_artifact("missing-artifact") == expected_empty
    assert store.retry_delete_pending_artifact(writer.artifact_id) == expected_empty
    assert store.state(writer.artifact_id) is ArtifactState.PARTIAL
    assert store.reserved_bytes == reserved
    assert removed == []

    with pytest.raises(TypeError, match="artifact_id"):
        store.retry_delete_pending_artifact(object())
    with pytest.raises(ValueError, match="artifact_id"):
        store.retry_delete_pending_artifact("")
    with pytest.raises(ValueError, match="artifact_id"):
        store.retry_delete_pending_artifact("x" * 257)


def test_exact_retry_during_global_retry_does_not_double_delete_or_release(
    tmp_path: Path,
) -> None:
    retry_started = threading.Event()
    allow_retry = threading.Event()
    attempts = 0

    def remove(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("initially busy")
        retry_started.set()
        assert allow_retry.wait(timeout=5)
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    writer.discard()
    assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING
    assert attempts == 1

    global_reports = []
    worker = threading.Thread(
        target=lambda: global_reports.append(store.retry_delete_pending(limit=1))
    )
    worker.start()
    assert retry_started.wait(timeout=5)

    try:
        expected_empty = store.retry_delete_pending(limit=0)
        exact_report = store.retry_delete_pending_artifact(writer.artifact_id)
        assert exact_report == expected_empty
        assert store.reserved_bytes == 48
    finally:
        allow_retry.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(global_reports) == 1
    assert global_reports[0].deleted_paths == (str(writer.partial_path),)
    assert attempts == 2
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0
    assert store.retry_delete_pending_artifact(writer.artifact_id) == expected_empty


def test_six_concurrent_reservations_never_oversell(tmp_path: Path) -> None:
    store = make_store(tmp_path, quota_bytes=5 * 48)
    barrier = threading.Barrier(6)
    writers = []
    errors = []
    guard = threading.Lock()

    def reserve(index: int) -> None:
        barrier.wait()
        try:
            writer = allocate(store, run_id=f"run-{index}")
            with guard:
                writers.append(writer)
        except CaptureLayerError as error:
            with guard:
                errors.append(error)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(writers) == 5
    assert len(errors) == 1
    assert errors[0].code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY
    assert store.reserved_bytes == 5 * 48


def test_dynamic_free_space_does_not_double_count_existing_reservation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"

    def dynamic_free(_: Path) -> int:
        allocated = sum(
            path.stat().st_size
            for path in root.rglob("*")
            if path.is_file() and path.suffix in (".partial", ".ready")
        )
        return GIB + 100 - allocated

    store = CaptureArtifactStore(
        root,
        quota_bytes=96,
        free_space_bytes=dynamic_free,
        minimum_free_bytes=GIB,
    )
    first = allocate(store, run_id="first")
    second = allocate(store, run_id="second")

    assert first.partial_path.exists()
    assert second.partial_path.exists()
    assert store.reserved_bytes == 96


def test_post_allocation_free_space_failure_rolls_back_file_and_quota(
    tmp_path: Path,
) -> None:
    samples = iter((GIB + 48, GIB - 1))
    store = CaptureArtifactStore(
        tmp_path / "staging",
        quota_bytes=48,
        free_space_bytes=lambda _: next(samples),
        minimum_free_bytes=GIB,
    )

    with pytest.raises(CaptureLayerError) as error:
        allocate(store)

    assert error.value.code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY
    assert store.reserved_bytes == 0
    assert list(store.session_directory.glob("*.partial")) == []


def test_blocked_delete_does_not_stall_unrelated_writer(tmp_path: Path) -> None:
    delete_started = threading.Event()
    allow_delete = threading.Event()

    def remove(path: Path) -> None:
        if "first" in path.name:
            delete_started.set()
            assert allow_delete.wait(timeout=5)
        path.unlink()

    store = make_store(tmp_path, remove_file=remove)
    first = allocate(store, run_id="first")
    second = allocate(store, run_id="second")
    delete_thread = threading.Thread(target=first.discard)
    delete_thread.start()
    assert delete_started.wait(timeout=1)

    writer_done = threading.Event()

    def write_second() -> None:
        second.write(block(100, [[0, 10], [1, 11]]))
        writer_done.set()

    writer_thread = threading.Thread(target=write_second)
    writer_thread.start()
    try:
        assert writer_done.wait(timeout=1)
    finally:
        allow_delete.set()
        delete_thread.join(timeout=2)
        writer_thread.join(timeout=2)
    assert not delete_thread.is_alive()
    assert not writer_thread.is_alive()


def test_publication_claim_transfer_release_and_idempotency(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)
    assert store.publication_pending(artifact.artifact_id)

    assert store.claim_ready(artifact.artifact_id, "wrong", "consumer") is None
    assert store.state(artifact.artifact_id) is ArtifactState.READY_UNCLAIMED
    assert store.publication_pending(artifact.artifact_id)

    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim
    assert (
        store.claim_ready(
            artifact.artifact_id, artifact.publication_token, "consumer"
        )
        == claim
    )
    assert store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "other"
    ) is None
    assert store.state(artifact.artifact_id) is ArtifactState.CLAIMED
    assert not store.publication_pending(artifact.artifact_id)
    assert store.accept_transfer("wrong") is False
    assert store.accept_transfer(claim) is True
    assert store.accept_transfer(claim) is True
    assert store.state(artifact.artifact_id) is ArtifactState.TRANSFERRED
    assert store.reserved_bytes == 48
    assert store.release_transferred("wrong", "ignored") is False
    assert store.release_transferred(claim, "saved") is True
    assert store.release_transferred(claim, "saved") is True
    assert store.state(artifact.artifact_id) is ArtifactState.RELEASED
    assert store.reserved_bytes == 0
    assert not Path(artifact.path).exists()


def test_artifact_publication_and_claim_tokens_are_unique(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = frozen_artifact(store, run_id="first")
    second = frozen_artifact(store, run_id="second")
    first_claim = store.claim_ready(
        first.artifact_id, first.publication_token, "consumer-1"
    )
    second_claim = store.claim_ready(
        second.artifact_id, second.publication_token, "consumer-2"
    )

    assert first.artifact_id != second.artifact_id
    assert first.publication_token != second.publication_token
    assert first_claim and second_claim and first_claim != second_claim


def test_claimed_and_transferred_survive_shutdown_and_hold_quota(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, quota_bytes=48)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    report = store.begin_closing()

    assert claim
    assert report.retained_paths == (artifact.path,)
    assert store.state(artifact.artifact_id) is ArtifactState.CLAIMED
    assert Path(artifact.path).exists()
    assert store.reserved_bytes == 48
    assert store.accept_transfer(claim)
    assert store.state(artifact.artifact_id) is ArtifactState.TRANSFERRED
    assert Path(artifact.path).exists()
    assert store.reserved_bytes == 48
    assert store.release_transferred(claim, "shutdown consumer done")
    assert store.reserved_bytes == 0


def test_decline_is_token_guarded_and_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)

    assert store.decline_publication(artifact.artifact_id, "wrong", "no") is False
    assert Path(artifact.path).exists()
    assert store.publication_pending(artifact.artifact_id)
    assert store.decline_publication(
        artifact.artifact_id, artifact.publication_token, "not accepted"
    )
    assert store.decline_publication(
        artifact.artifact_id, artifact.publication_token, "not accepted"
    )
    assert store.state(artifact.artifact_id) is ArtifactState.DELETED
    assert not store.publication_pending(artifact.artifact_id)
    assert store.reserved_bytes == 0


def test_abandon_claim_while_open_returns_ready_but_closing_deletes(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    first = frozen_artifact(store, run_id="open")
    first_claim = store.claim_ready(
        first.artifact_id, first.publication_token, "consumer"
    )
    assert first_claim
    assert store.abandon_claim(first_claim, "retry elsewhere")
    assert store.abandon_claim(first_claim, "retry elsewhere")
    assert store.state(first.artifact_id) is ArtifactState.READY_UNCLAIMED
    assert Path(first.path).exists()
    assert store.publication_pending(first.artifact_id)
    reclaimed = store.claim_ready(
        first.artifact_id, first.publication_token, "replacement-consumer"
    )
    assert reclaimed and reclaimed != first_claim
    assert store.abandon_claim(reclaimed, "decline instead")
    assert store.decline_publication(
        first.artifact_id, first.publication_token, "no replacement"
    )
    assert not Path(first.path).exists()

    second = frozen_artifact(store, run_id="closing")
    second_claim = store.claim_ready(
        second.artifact_id, second.publication_token, "consumer"
    )
    store.begin_closing()
    assert second_claim and store.abandon_claim(second_claim, "shutdown")
    assert store.state(second.artifact_id) is ArtifactState.DELETED
    assert Path(second.path).exists() is False


def test_terminal_registry_and_token_tombstones_remain_bounded(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, quota_bytes=48, tombstone_limit=16)
    released: list[tuple[str, str]] = []

    for index in range(64):
        artifact = frozen_artifact(store, run_id=f"churn-{index}")
        claim = store.claim_ready(
            artifact.artifact_id, artifact.publication_token, f"consumer-{index}"
        )
        assert claim and store.accept_transfer(claim)
        assert store.release_transferred(claim, "done")
        released.append((artifact.artifact_id, claim))

    assert store.live_artifact_count == 0
    assert store.tombstone_count == 16
    assert store.idempotency_record_count <= 16
    assert store.state(released[-1][0]) is ArtifactState.RELEASED
    assert store.release_transferred(released[-1][1], "repeat")
    assert store.state(released[0][0]) is None
    assert store.release_transferred(released[0][1], "expired") is False
    assert store.release_transferred("unknown-token", "wrong") is False


def test_claim_winning_shutdown_race_preserves_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    store.begin_closing()

    assert claim
    assert store.state(artifact.artifact_id) is ArtifactState.CLAIMED
    assert Path(artifact.path).exists()


def test_shutdown_decline_winning_before_claim_is_non_mutating(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)
    store.begin_closing()
    assert store.decline_publication(
        artifact.artifact_id, artifact.publication_token, "shutdown"
    )

    assert (
        store.claim_ready(
            artifact.artifact_id, artifact.publication_token, "consumer"
        )
        is None
    )
    assert store.state(artifact.artifact_id) is ArtifactState.DELETED
    assert not Path(artifact.path).exists()


def test_publication_pending_ready_survives_shutdown_until_declined(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)
    report = store.begin_closing()
    assert artifact.path in report.retained_paths
    assert Path(artifact.path).exists()

    assert store.decline_publication(
        artifact.artifact_id, artifact.publication_token, "shutdown arbitration"
    )
    assert not Path(artifact.path).exists()


def test_delete_pending_retains_quota_until_bounded_retry(tmp_path: Path) -> None:
    fail = True

    def remove(path: Path) -> None:
        nonlocal fail
        if fail:
            raise OSError("injected delete failure")
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    artifact = frozen_artifact(store)
    assert store.decline_publication(
        artifact.artifact_id, artifact.publication_token, "declined"
    )
    assert store.state(artifact.artifact_id) is ArtifactState.DELETE_PENDING
    assert store.reserved_bytes == 48
    with pytest.raises(CaptureLayerError) as error:
        allocate(store, run_id="blocked")
    assert error.value.code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY

    assert store.retry_delete_pending(limit=0).attempted_paths == ()
    fail = False
    report = store.retry_delete_pending(limit=1)
    assert report.deleted_paths == (artifact.path,)
    assert store.state(artifact.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0
    assert store.retry_delete_pending(limit=1).deleted_paths == ()


def test_partial_delete_pending_retries_the_partial_path(tmp_path: Path) -> None:
    fail = True

    def remove(path: Path) -> None:
        nonlocal fail
        if fail:
            raise OSError("injected delete failure")
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    partial_path = str(writer.partial_path)
    assert writer.discard()
    assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING
    assert store.reserved_bytes == 48

    fail = False
    report = store.retry_delete_pending(limit=1)
    assert report.deleted_paths == (partial_path,)
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0


def test_shutdown_reports_failed_partial_cleanup_path(tmp_path: Path) -> None:
    def remove(path: Path) -> None:
        raise PermissionError("busy")

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    report = store.begin_closing()

    assert report.failed_paths == (str(writer.partial_path),)
    assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING
    assert store.reserved_bytes == 48


def test_moved_out_transferred_file_releases_quota_without_deleting_destination(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim and store.accept_transfer(claim)
    destination = tmp_path / "consumer" / "capture.f32"
    destination.parent.mkdir()
    Path(artifact.path).replace(destination)

    assert store.release_transferred(claim, "moved", moved_path=destination)
    assert store.state(artifact.artifact_id) is ArtifactState.RELEASED
    assert store.reserved_bytes == 0
    assert destination.exists()


@pytest.mark.parametrize("invalid_kind", ["directory", "missing", "wrong-size", "unrelated"])
def test_moved_out_release_rejects_noncredible_destination(
    tmp_path: Path, invalid_kind: str
) -> None:
    store = make_store(tmp_path, quota_bytes=48)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim and store.accept_transfer(claim)
    actual = tmp_path / "consumer" / "actual.f32"
    actual.parent.mkdir()
    Path(artifact.path).replace(actual)

    candidate = tmp_path / "candidate"
    if invalid_kind == "directory":
        candidate.mkdir()
    elif invalid_kind == "wrong-size":
        candidate.write_bytes(b"x")
    elif invalid_kind == "unrelated":
        candidate.write_bytes(b"x" * 48)

    assert not store.release_transferred(
        claim, "invalid move receipt", moved_path=candidate
    )
    assert store.state(artifact.artifact_id) is ArtifactState.TRANSFERRED
    assert store.reserved_bytes == 48
    assert actual.exists()
    assert store.release_transferred(claim, "actual move", moved_path=actual)


def test_moved_out_release_rejects_symlink_or_reparse_destination(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, quota_bytes=48)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim and store.accept_transfer(claim)
    actual = tmp_path / "consumer" / "actual.f32"
    actual.parent.mkdir()
    Path(artifact.path).replace(actual)
    link = tmp_path / "linked.f32"
    try:
        link.symlink_to(actual)
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")

    assert not store.release_transferred(claim, "linked", moved_path=link)
    assert store.state(artifact.artifact_id) is ArtifactState.TRANSFERRED
    assert store.reserved_bytes == 48
    assert store.release_transferred(claim, "actual", moved_path=actual)


def test_missing_transferred_source_retains_quota_until_real_unlink(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, quota_bytes=48)
    artifact = frozen_artifact(store)
    claim = store.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim and store.accept_transfer(claim)
    original = Path(artifact.path)
    relocated_inside_staging = store.session_directory / "relocated.ready"
    original.replace(relocated_inside_staging)

    assert store.release_transferred(claim, "source unexpectedly absent")
    assert store.release_transferred(claim, "repeat") is True
    assert store.state(artifact.artifact_id) is ArtifactState.DELETE_PENDING
    assert store.reserved_bytes == 48
    closing = store.begin_closing()
    assert closing.deleted_paths == ()
    assert closing.failed_paths == (str(original),)
    assert closing.retained_paths == ()
    assert store.reserved_bytes == 48
    assert relocated_inside_staging.exists()

    relocated_inside_staging.replace(original)
    retry = store.retry_delete_pending(limit=1)
    assert retry.deleted_paths == (str(original),)
    assert store.state(artifact.artifact_id) is ArtifactState.RELEASED
    assert store.reserved_bytes == 0
    assert store.retry_delete_pending(limit=1).deleted_paths == ()
    assert store.release_transferred(claim, "terminal repeat") is True


def test_begin_closing_retries_and_reports_preexisting_delete_pending(
    tmp_path: Path,
) -> None:
    fail = True

    def remove(path: Path) -> None:
        if fail:
            raise PermissionError("busy")
        path.unlink()

    store = make_store(tmp_path, quota_bytes=48, remove_file=remove)
    writer = allocate(store)
    partial_path = str(writer.partial_path)
    assert writer.discard()
    assert store.state(writer.artifact_id) is ArtifactState.DELETE_PENDING

    first = store.begin_closing()
    assert first.failed_paths == (partial_path,)
    assert store.reserved_bytes == 48

    fail = False
    second = store.begin_closing()
    assert second.deleted_paths == (partial_path,)
    assert second.failed_paths == ()
    assert store.state(writer.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0
    third = store.begin_closing()
    assert third.deleted_paths == ()
    assert third.failed_paths == ()


def test_random_sessions_and_safe_orphan_cleanup_do_not_touch_final_wavs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    first = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    second = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    assert first.session_directory != second.session_directory

    orphan = root / "session-old"
    orphan.mkdir()
    (orphan / ".resident-audio-session").touch()
    (orphan / ".resident-audio-session.lock").touch()
    (orphan / "old.partial").write_bytes(b"orphan")
    final_wavs = root / "final-wavs"
    final_wavs.mkdir()
    final = final_wavs / "important.wav"
    final.write_bytes(b"final")

    report = first.cleanup_orphan_sessions()
    assert str(orphan / "old.partial") in report.deleted_paths
    assert not orphan.exists()
    assert final.read_bytes() == b"final"
    assert second.session_directory.exists()


def test_orphan_cleanup_reports_exact_failed_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    orphan = root / "session-old"
    orphan.mkdir(parents=True)
    (orphan / ".resident-audio-session").touch()
    (orphan / ".resident-audio-session.lock").touch()
    failed = orphan / "old.ready"
    failed.write_bytes(b"x")

    def remove(path: Path) -> None:
        if path == failed:
            raise PermissionError("busy")
        path.unlink()

    store = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
        remove_file=remove,
    )
    report = store.cleanup_orphan_sessions()
    assert report.failed_paths == (str(failed),)
    assert failed.exists()


def test_live_session_lock_is_retained_even_with_different_pid_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    first = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    second = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    live_file = second.session_directory / "live.partial"
    live_file.write_bytes(b"live")
    (second.session_directory / ".resident-audio-session").write_text(
        "different-pid", encoding="ascii"
    )

    report = first.cleanup_orphan_sessions()
    assert str(second.session_directory) in report.retained_paths
    assert live_file.read_bytes() == b"live"


def test_indeterminate_session_lock_is_retained_and_reported(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indeterminate = store.session_directory.parent / "session-indeterminate"
    indeterminate.mkdir()
    (indeterminate / ".resident-audio-session").touch()
    (indeterminate / ".resident-audio-session.lock").mkdir()
    artifact = indeterminate / "unknown.partial"
    artifact.write_bytes(b"unknown")

    report = store.cleanup_orphan_sessions()
    assert str(indeterminate) in report.retained_paths
    assert artifact.read_bytes() == b"unknown"


def test_orphan_cleanup_succeeds_only_after_session_lock_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    first = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    second = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    artifact = frozen_artifact(second)
    claim = second.claim_ready(
        artifact.artifact_id, artifact.publication_token, "consumer"
    )
    assert claim
    second.begin_closing()

    live_report = first.cleanup_orphan_sessions()
    assert str(second.session_directory) in live_report.retained_paths
    assert Path(artifact.path).exists()

    second.release_session_ownership()
    orphan_report = first.cleanup_orphan_sessions()
    assert artifact.path in orphan_report.deleted_paths
    assert not second.session_directory.exists()


def test_safe_session_path_requires_strict_immediate_descendant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    session = root / "session-valid"
    session.mkdir()
    nested = session / "session-nested"
    nested.mkdir()
    outside = tmp_path / "session-outside"
    outside.mkdir()

    assert safe_session_path(root, session) == session.resolve(strict=True)
    assert safe_session_path(root, nested) is None
    assert safe_session_path(root, outside) is None
    assert safe_session_path(root, root / "session-missing") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_orphan_cleanup_never_traverses_windows_junction(tmp_path: Path) -> None:
    root = tmp_path / "root"
    store = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    outside = tmp_path / "outside-final-wavs"
    outside.mkdir()
    victim = outside / "important.wav"
    victim.write_bytes(b"must-survive")
    junction = root / "session-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr.strip()}")

    report = store.cleanup_orphan_sessions()
    assert str(junction) in report.retained_paths
    assert victim.read_bytes() == b"must-survive"


def test_subprocess_session_is_retained_until_forced_child_exit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    parent_store = CaptureArtifactStore(
        root,
        quota_bytes=100,
        free_space_bytes=lambda _: GIB + 100,
    )
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    process = context.Process(
        target=_hold_session_in_child,
        args=(str(root), ready_queue),
    )
    process.start()
    child_session = ""
    child_file = ""
    try:
        child_session, child_file = ready_queue.get(timeout=10)
        live_report = parent_store.cleanup_orphan_sessions()
        assert child_session in live_report.retained_paths
        assert Path(child_file).read_bytes() == b"child-live"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        ready_queue.close()
        ready_queue.join_thread()

    assert not process.is_alive()
    orphan_report = parent_store.cleanup_orphan_sessions()
    assert child_file in orphan_report.deleted_paths
    assert not Path(child_session).exists()
