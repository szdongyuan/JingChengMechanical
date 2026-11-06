from __future__ import annotations

from dataclasses import replace
from concurrent.futures import CancelledError
import multiprocessing
from multiprocessing import shared_memory
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from app.service.capture_process.protocol import (
    CaptureLimits,
    CompletedAudioDescriptor,
    TransportCapacityError,
)
from app.service.capture_process.transport import (
    CompletedAudioStore,
    copy_completed_audio,
)


INCARNATION = "1" * 32
OTHER_INCARNATION = "2" * 32


def _limits(**overrides):
    values = dict(max_capture_frames=32, channel_counts=(1, 2))
    values.update(overrides)
    return CaptureLimits(**values)


def _reserve(store, capture_id="capture-1", frames=8, channels=2):
    return store.reserve(
        capture_id, frames=frames, channels=channels, sample_rate_hz=48_000
    )


def _pcm(frames=8, channels=2):
    return np.arange(frames * channels, dtype=np.float32).reshape(frames, channels)


def _assert_mapping_removed(name):
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)


def test_completed_audio_admission_counts_active_and_unacknowledged_windows():
    limits = _limits()
    with CompletedAudioStore(INCARNATION, limits) as store:
        reservations = [_reserve(store, str(i)) for i in range(6)]
        descriptors = [store.publish(item, _pcm()) for item in reservations]
        assert store.pending_count == 6
        assert store.reserved_bytes == 6 * _pcm().nbytes
        with pytest.raises(TransportCapacityError):
            _reserve(store, "seventh")
        assert store.receipt(descriptors[0]) is True
        assert store.receipt(descriptors[0]) is False
        replacement = _reserve(store, "seventh")
        assert store.pending_count == 6
        assert store.cancel(replacement) is True
        assert store.cancel(replacement) is False


def test_completed_audio_byte_budget_rejects_before_allocating():
    with CompletedAudioStore(
        INCARNATION, _limits(max_retained_bytes=96)
    ) as store:
        _reserve(store)  # 64 bytes, held before any mapping exists.
        with pytest.raises(TransportCapacityError):
            _reserve(store, "too-large")
        assert store.pending_count == 1
        assert store.reserved_bytes == 64
        smaller = _reserve(store, "fits", frames=4)
        assert store.reserved_bytes == 96
        store.cancel(smaller)
        assert store.reserved_bytes == 64


def test_delayed_receipt_retains_exact_pcm_and_parent_copy_is_immutable():
    source = _pcm()
    expected = source.copy()
    limits = _limits()
    with CompletedAudioStore(INCARNATION, limits) as store:
        descriptor = store.publish(_reserve(store), source)
        source[:] = -99
        # Other capture activity cannot overwrite the unacknowledged mapping.
        for i in range(12):
            other = _reserve(store, f"next-{i}")
            store.receipt(store.publish(other, _pcm() + i))
        received = copy_completed_audio(descriptor, incarnation=INCARNATION, limits=limits)
        assert received.dtype == np.float32
        np.testing.assert_array_equal(received, expected)
        with pytest.raises(ValueError):
            received[0, 0] = 99
        with pytest.raises(ValueError):
            received.flags.writeable = True
        assert store.pending_count == 1
        store.receipt(descriptor)
        _assert_mapping_removed(descriptor.shm_name)
        np.testing.assert_array_equal(received, expected)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_captures": 7},
        {"max_captures": True},
        {"max_capture_frames": 2**31},
        {"max_capture_frames": 2**28, "channel_counts": (2,)},
        {"channel_counts": (65,)},
        {"channel_counts": (1,) * 7},
        {"max_retained_bytes": 6 * 256 * 1024 * 1024 + 1},
        {"max_retained_bytes": 1537},  # Greater than this configuration's budget.
    ],
)
def test_capture_limits_reject_unbounded_configuration(kwargs):
    with pytest.raises(ValueError):
        _limits(**kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 999),
        ("incarnation", OTHER_INCARNATION),
        ("frames", 0),
        ("frames", 33),
        ("frames", True),
        ("channels", 65),
        ("sample_rate_hz", 384_001),
        ("nbytes", 2**40),
        ("capture_id", "x" * 257),
        ("shm_name", "../../unrelated"),
    ],
)
def test_bad_descriptors_are_rejected_before_attachment(monkeypatch, field, value):
    limits = _limits()
    with CompletedAudioStore(INCARNATION, limits) as store:
        descriptor = store.publish(_reserve(store), _pcm())
        wire = descriptor.to_wire()
        wire[field] = value
        attached = []

        def unexpected_attach(*args, **kwargs):
            attached.append((args, kwargs))
            raise AssertionError("invalid metadata reached shared memory")

        monkeypatch.setattr(shared_memory, "SharedMemory", unexpected_attach)
        with pytest.raises(ValueError):
            invalid = CompletedAudioDescriptor.from_wire(wire)
            copy_completed_audio(invalid, incarnation=INCARNATION, limits=limits)
        assert attached == []


def test_receipts_cannot_release_another_owner_or_reused_capture_id():
    limits = _limits()
    with CompletedAudioStore(INCARNATION, limits) as first:
        with CompletedAudioStore(INCARNATION, limits) as second:
            old = first.publish(_reserve(first), _pcm())
            other = second.publish(_reserve(second), _pcm() + 1)
            with pytest.raises(ValueError):
                first.receipt(other)
            with pytest.raises(ValueError):
                first.receipt(replace(old, incarnation=OTHER_INCARNATION))
            assert first.receipt(old)
            new = first.publish(_reserve(first), _pcm() + 2)
            assert first.receipt(old) is False
            np.testing.assert_array_equal(
                copy_completed_audio(new, incarnation=INCARNATION, limits=limits), _pcm() + 2
            )
            assert first.pending_count == second.pending_count == 1


def test_mismatched_pcm_fails_without_consuming_reservation():
    with CompletedAudioStore(INCARNATION, _limits()) as store:
        reservation = _reserve(store)
        with pytest.raises(ValueError):
            store.publish(reservation, _pcm().astype(np.float64))
        assert store.pending_count == 1
        assert store.publish(reservation, _pcm()).nbytes == 64


def test_partial_publication_failure_cleans_mapping_and_releases_credit(monkeypatch):
    import app.service.capture_process.transport as transport

    names = []
    real_shared_memory = shared_memory.SharedMemory

    def create(*args, **kwargs):
        mapping = real_shared_memory(*args, **kwargs)
        if kwargs.get("create"):
            names.append(mapping.name)
        return mapping

    def fail_copy(*args, **kwargs):
        raise OSError("injected copy failure after allocation")

    monkeypatch.setattr(shared_memory, "SharedMemory", create)
    monkeypatch.setattr(transport, "_write_pcm", fail_copy)
    with CompletedAudioStore(INCARNATION, _limits()) as store:
        with pytest.raises(OSError, match="injected copy failure"):
            store.publish(_reserve(store), _pcm())
        assert store.pending_count == store.reserved_bytes == 0
    assert len(names) == 1
    _assert_mapping_removed(names[0])


def test_close_waits_for_local_publication_lease_before_unlinking(monkeypatch):
    import app.service.capture_process.transport as transport

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original_write = transport._write_pcm
    errors = []

    def delayed_write(*args):
        entered.set()
        assert release.wait(5)
        original_write(*args)

    monkeypatch.setattr(transport, "_write_pcm", delayed_write)
    store = CompletedAudioStore(INCARNATION, _limits())
    reservation = _reserve(store)

    def publish():
        try:
            store.publish(reservation, _pcm())
        except BaseException as exc:
            errors.append(exc)

    producer = threading.Thread(target=publish)
    closer = threading.Thread(target=lambda: (store.close(), closed.set()))
    producer.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert not closed.wait(0.1)
    finally:
        release.set()
        producer.join(5)
        if closer.ident is not None:
            closer.join(5)
        store.close()
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    assert closed.is_set()
    assert store.pending_count == 0


def test_slow_publication_does_not_block_next_capture_admission(monkeypatch):
    import app.service.capture_process.transport as transport

    entered, release, accepted = threading.Event(), threading.Event(), threading.Event()
    original_write = transport._write_pcm
    errors = []

    def delayed_write(*args):
        entered.set()
        assert release.wait(5)
        original_write(*args)

    monkeypatch.setattr(transport, "_write_pcm", delayed_write)
    with CompletedAudioStore(INCARNATION, _limits()) as store:
        reservation = _reserve(store)

        def publish():
            try:
                store.publish(reservation, _pcm())
            except BaseException as exc:
                errors.append(exc)

        def reserve_next():
            try:
                _reserve(store, "next-capture")
                accepted.set()
            except BaseException as exc:
                errors.append(exc)

        producer = threading.Thread(target=publish)
        starter = threading.Thread(target=reserve_next)
        producer.start()
        try:
            assert entered.wait(5)
            starter.start()
            assert accepted.wait(0.5), "bulk publication held the admission lock"
        finally:
            release.set()
            producer.join(5)
            if starter.ident is not None:
                starter.join(5)
        assert not errors
        assert store.pending_count == 2


@pytest.mark.parametrize("operation", ["cancel", "close"])
def test_cleanup_during_publication_is_bounded_and_defers_unlink_until_lease_ends(
    monkeypatch, operation
):
    import app.service.capture_process.transport as transport

    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    original_write = transport._write_pcm
    names, errors = [], []
    outcome = []

    def delayed_write(mapping, pcm):
        names.append(mapping.name)
        entered.set()
        assert release.wait(5)
        original_write(mapping, pcm)

    monkeypatch.setattr(transport, "_write_pcm", delayed_write)
    store = CompletedAudioStore(INCARNATION, _limits())
    reservation = _reserve(store)

    def publish():
        try:
            store.publish(reservation, _pcm())
        except BaseException as exc:
            errors.append(exc)

    def cleanup():
        try:
            if operation == "cancel":
                outcome.append(store.cancel(reservation))
            else:
                outcome.append(store.close(timeout_s=0.01))
        except BaseException as exc:
            errors.append(exc)
        finally:
            cleaned.set()

    producer = threading.Thread(target=publish)
    closer = threading.Thread(target=cleanup)
    producer.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert cleaned.wait(0.5), "cleanup waited for a bulk publication lease"
        assert outcome == [operation == "cancel"]
        assert store.pending_count == 1  # The mapping still consumes its credit.
        mapping = shared_memory.SharedMemory(name=names[0])
        mapping.close()
    finally:
        release.set()
        producer.join(5)
        if closer.ident is not None:
            closer.join(5)
        store.close()
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    assert store.pending_count == store.reserved_bytes == 0
    assert store.close() is True
    assert store.cancel(reservation) is False
    _assert_mapping_removed(names[0])


def _child_publish(connection):
    with CompletedAudioStore(INCARNATION, _limits()) as store:
        descriptor = store.publish(_reserve(store), _pcm())
        connection.send(descriptor.to_wire())
        assert connection.poll(10)
        assert connection.recv() == "receipt"
        connection.send(store.receipt(descriptor))
    connection.close()


def test_real_spawn_retains_mapping_until_parent_copy_and_receipt():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_child_publish, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        descriptor = CompletedAudioDescriptor.from_wire(parent.recv())
        received = copy_completed_audio(
            descriptor, incarnation=INCARNATION, limits=_limits()
        )
        np.testing.assert_array_equal(received, _pcm())
        parent.send("receipt")
        assert parent.poll(5)
        assert parent.recv() is True
        process.join(5)
        assert process.exitcode == 0
        _assert_mapping_removed(descriptor.shm_name)
        np.testing.assert_array_equal(received, _pcm())
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()


def test_protocol_and_package_import_without_numerical_or_ui_runtime():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import app.service.capture_process.protocol; "
            "import app.service.capture_process.transport; "
            "assert not any(name.startswith(('numpy', 'PyQt', 'scipy', 'model.')) "
            "for name in sys.modules)"
        )],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _preview_streams():
    from app.service.capture_process.protocol import PreviewStreamConfig

    return (
        PreviewStreamConfig("left", max_frames=8, channels=2, sample_rate_hz=48_000),
        PreviewStreamConfig("right", max_frames=8, channels=1, sample_rate_hz=44_100),
    )


def _offer(ring, sequence, hardware_id="left", generation_id="generation-a"):
    channels = 2 if hardware_id == "left" else 1
    return ring.offer(
        hardware_id, _pcm(channels=channels) + sequence,
        sequence=sequence, first_sample=sequence * 8,
        monotonic_ns=1_000_000_000 + sequence * 166_667,
        generation_id=generation_id,
    )


def test_preview_unread_ring_retains_only_latest_four_blocks_with_exact_metadata():
    from app.service.capture_process.transport import PreviewRing

    context = multiprocessing.get_context("spawn")
    with PreviewRing.create(context, INCARNATION, _preview_streams()) as ring:
        size = ring.allocated_bytes
        for sequence in range(100):
            assert _offer(ring, sequence, generation_id="new-generation")
        assert ring.allocated_bytes == size
        blocks = ring.read("left")
        assert len(blocks) == 4
        assert [block.sequence for block in blocks] == [96, 97, 98, 99]
        for block in blocks:
            assert block.hardware_id == "left"
            assert block.incarnation == INCARNATION
            assert block.generation_id == "new-generation"
            assert block.first_sample == block.sequence * 8
            assert block.monotonic_ns == 1_000_000_000 + block.sequence * 166_667
            assert block.sample_rate_hz == 48_000
            np.testing.assert_array_equal(block.pcm, _pcm() + block.sequence)
            with pytest.raises(ValueError):
                block.pcm.flags.writeable = True
        assert ring.read("left") == ()
        assert ring.read("right") == ()


def test_preview_congestion_does_not_change_completed_pcm_retention():
    from app.service.capture_process.transport import PreviewRing

    limits = _limits()
    with CompletedAudioStore(INCARNATION, limits) as store:
        descriptor = store.publish(_reserve(store), _pcm())
        with PreviewRing.create(
            multiprocessing.get_context("spawn"), INCARNATION, _preview_streams()
        ) as ring:
            for i in range(1000):
                assert _offer(ring, i)
            assert store.pending_count == 1
            np.testing.assert_array_equal(
                copy_completed_audio(descriptor, incarnation=INCARNATION, limits=limits),
                _pcm(),
            )
            assert store.receipt(descriptor)


def _child_offer_preview(bootstrap, connection):
    from app.service.capture_process.transport import PreviewRing

    with PreviewRing.attach(bootstrap, incarnation=INCARNATION) as ring:
        started = time.monotonic()
        accepted = sum(_offer(ring, i) for i in range(1000))
        assert _offer(ring, 50, hardware_id="right", generation_id="right-recovered")
        connection.send((accepted, time.monotonic() - started))
    connection.close()


@pytest.mark.parametrize("hold_parent_lock", [False, True])
def test_spawn_preview_producer_and_cleanup_do_not_wait_for_stalled_parent(hold_parent_lock):
    from app.service.capture_process.transport import PreviewRing

    context = multiprocessing.get_context("spawn")
    ring = PreviewRing.create(context, INCARNATION, _preview_streams())
    bootstrap = ring.bootstrap
    parent, child = context.Pipe()
    process = context.Process(target=_child_offer_preview, args=(bootstrap, child))
    names = [handle.shm_name for handle in bootstrap.streams]
    if hold_parent_lock:
        bootstrap.streams[0].lock.acquire()
    try:
        process.start()
        child.close()
        assert parent.poll(10), "producer waited for parent reader lock"
        accepted, duration = parent.recv()
        assert accepted == (0 if hold_parent_lock else 1000)
        assert duration < 2.0
        process.join(5)
        assert process.exitcode == 0
        right, = ring.read("right")
        assert right.generation_id == "right-recovered"
        assert right.sample_rate_hz == 44_100
        assert right.sequence == 50
        np.testing.assert_array_equal(right.pcm, _pcm(channels=1) + 50)
        if not hold_parent_lock:
            assert [block.sequence for block in ring.read("left")] == [996, 997, 998, 999]
        # Cleanup must not acquire an abandoned interprocess reader lock either.
        started = time.monotonic()
        ring.close()
        assert time.monotonic() - started < 1.0
        for name in names:
            _assert_mapping_removed(name)
    finally:
        if hold_parent_lock:
            bootstrap.streams[0].lock.release()
        parent.close()
        child.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
        ring.close()


@pytest.mark.parametrize(
    "field,value",
    [("sequence", -1), ("sequence", True), ("first_sample", 2**63),
     ("monotonic_ns", -1), ("generation_id", "x" * 257)],
)
def test_preview_rejects_invalid_metadata_without_publishing(field, value):
    from app.service.capture_process.transport import PreviewRing

    with PreviewRing.create(
        multiprocessing.get_context("spawn"), INCARNATION, _preview_streams()
    ) as ring:
        metadata = dict(sequence=0, first_sample=0, monotonic_ns=1, generation_id="generation")
        metadata[field] = value
        with pytest.raises(ValueError):
            ring.offer("left", _pcm(), **metadata)
        assert ring.read("left") == ()


def test_preview_rejects_stale_bootstrap_and_more_than_four_slots():
    from app.service.capture_process.transport import PreviewRing

    context = multiprocessing.get_context("spawn")
    with PreviewRing.create(context, INCARNATION, _preview_streams()) as ring:
        with pytest.raises(ValueError):
            PreviewRing.attach(ring.bootstrap, incarnation=OTHER_INCARNATION)
    with pytest.raises(ValueError):
        PreviewRing.create(context, INCARNATION, _preview_streams(), slots_per_stream=5)


def test_preview_partial_allocation_cleans_only_owned_mappings(monkeypatch):
    from app.service.capture_process.transport import PreviewRing

    real_shared_memory = shared_memory.SharedMemory
    names = []

    def fail_second(*args, **kwargs):
        if kwargs.get("create") and names:
            raise OSError("injected second mapping allocation failure")
        mapping = real_shared_memory(*args, **kwargs)
        if kwargs.get("create"):
            names.append(mapping.name)
        return mapping

    monkeypatch.setattr(shared_memory, "SharedMemory", fail_second)
    with pytest.raises(OSError, match="injected second"):
        PreviewRing.create(multiprocessing.get_context("spawn"), INCARNATION, _preview_streams())
    assert len(names) == 1
    _assert_mapping_removed(names[0])


@pytest.mark.parametrize("operation", ["offer", "read"])
@pytest.mark.parametrize("timeout_s", [None, 0.01])
def test_preview_close_bounds_local_copy_leases_and_disposes_when_they_end(
    monkeypatch, operation, timeout_s
):
    import app.service.capture_process.transport as transport

    ring = transport.PreviewRing.create(
        multiprocessing.get_context("spawn"), INCARNATION, _preview_streams()
    )
    names = [handle.shm_name for handle in ring.bootstrap.streams]
    entered, release, close_finished = (
        threading.Event(), threading.Event(), threading.Event()
    )
    operation_results, close_results, errors = [], [], []
    original_copyto, original_bytes = np.copyto, bytes

    if operation == "read":
        assert _offer(ring, 10)

    def delayed_copyto(target, source, **kwargs):
        if target.shape == (8, 2):
            entered.set()
            assert release.wait(5)
        return original_copyto(target, source, **kwargs)

    def delayed_bytes(value):
        if isinstance(value, memoryview) and len(value) == _pcm().nbytes:
            entered.set()
            assert release.wait(5)
        return original_bytes(value)

    if operation == "offer":
        monkeypatch.setattr(np, "copyto", delayed_copyto)
    else:
        # Pause the real shared-memory-to-owned-bytes copy, with its view alive.
        monkeypatch.setattr(transport, "bytes", delayed_bytes, raising=False)

    def use_ring():
        try:
            result = _offer(ring, 10) if operation == "offer" else ring.read("left")
            operation_results.append(result)
        except BaseException as exc:
            errors.append(exc)

    def close_ring():
        try:
            result = ring.close() if timeout_s is None else ring.close(timeout_s=timeout_s)
            close_results.append(result)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_finished.set()

    copier = threading.Thread(target=use_ring)
    closer = threading.Thread(target=close_ring)
    copier.start()
    try:
        try:
            assert entered.wait(5)
            closer.start()
            wait_budget = 1.5 if timeout_s is None else 0.5
            assert close_finished.wait(wait_budget), "preview close waited on its local copy lease"
            assert close_results == [False]
            assert ring.close(timeout_s=0.0) is False
            # Shutdown stops admission on every stream while the old lease survives.
            assert _offer(ring, 11) is False
            assert _offer(ring, 11, hardware_id="right") is False
            assert ring.read("left") == ()
            assert ring.read("right") == ()
            mapping = shared_memory.SharedMemory(name=names[0])
            mapping.close()
            _assert_mapping_removed(names[1])
        finally:
            release.set()
            copier.join(5)
            if closer.ident is not None:
                closer.join(5)
        assert not copier.is_alive()
        assert not closer.is_alive()
        assert not errors
        if operation == "offer":
            assert operation_results == [True]
        else:
            block, = operation_results[0]
            np.testing.assert_array_equal(block.pcm, _pcm() + 10)
        # Final lease release itself cleans up, without another close being needed.
        for name in names:
            _assert_mapping_removed(name)
        assert ring.close(timeout_s=0.0) is True
        assert ring.close() is True
    finally:
        ring.close()
