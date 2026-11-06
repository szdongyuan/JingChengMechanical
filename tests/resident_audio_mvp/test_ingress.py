"""Focused contracts for the generation-owned callback ingress."""

from __future__ import annotations

import numpy as np

from app.service.analysis.resident_audio_mvp.ingress import _SpscIngress


def make_ingress(*, capacity_blocks: int = 40) -> _SpscIngress:
    return _SpscIngress(
        capacity_blocks=capacity_blocks,
        frames_per_buffer=4,
        input_channel_count=3,
        capture_channel_indices=(0, 2),
    )


def test_ingress_preallocates_at_least_two_seconds_of_production_blocks() -> None:
    ingress = _SpscIngress.for_stream(
        sample_rate_hz=48_000,
        frames_per_buffer=2_400,
        input_channel_count=4,
        capture_channel_indices=(0, 2),
    )

    assert ingress.capacity_blocks >= 40
    assert ingress._storage.shape == (40, 2_400, 2)
    assert ingress._storage.dtype == np.dtype(np.float32)


def test_ingress_copies_selected_channels_and_publishes_heartbeat_first() -> None:
    ingress = make_ingress()
    samples = np.arange(12, dtype=np.float32).reshape(4, 3)

    accepted = ingress.try_push(
        samples,
        frame_count=4,
        callback_monotonic_ns=900,
        heartbeat_monotonic_ns=1_000,
    )

    assert accepted
    assert ingress.accepted_sequence == 1
    assert ingress.heartbeat_monotonic_ns == 1_000
    block = ingress.peek()
    assert block is not None
    assert block.sequence == 1
    assert block.frame_count == 4
    assert block.callback_monotonic_ns == 900
    np.testing.assert_array_equal(block.samples, samples[:, (0, 2)])
    ingress.release(block.sequence)
    assert ingress.peek() is None


def test_full_ingress_never_overwrites_unconsumed_audio() -> None:
    ingress = make_ingress(capacity_blocks=2)
    first = np.full((4, 3), 1.0, dtype=np.float32)
    second = np.full((4, 3), 2.0, dtype=np.float32)
    rejected = np.full((4, 3), 3.0, dtype=np.float32)
    assert ingress.try_push(first, 4, 10, 11)
    assert ingress.try_push(second, 4, 20, 21)

    assert not ingress.try_push(rejected, 4, 30, 31)
    assert ingress.accepted_sequence == 2
    assert ingress.heartbeat_monotonic_ns == 21
    assert ingress.overflow_count == 1

    block = ingress.peek()
    assert block is not None
    np.testing.assert_array_equal(block.samples, first[:, (0, 2)])
    ingress.release(block.sequence)
    block = ingress.peek()
    assert block is not None
    np.testing.assert_array_equal(block.samples, second[:, (0, 2)])


def test_release_requires_the_current_consumer_sequence() -> None:
    ingress = make_ingress(capacity_blocks=2)
    samples = np.zeros((4, 3), dtype=np.float32)
    assert ingress.try_push(samples, 4, 10, 11)

    try:
        ingress.release(2)
    except RuntimeError as exc:
        assert "sequence" in str(exc)
    else:
        raise AssertionError("out-of-order release unexpectedly succeeded")
