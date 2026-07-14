"""Tests for the pure per-session audio capture window."""
from __future__ import annotations

import math
import threading

import numpy as np
import pytest

from app.service.analysis.session_capture import (
    SESSION_WARMUP_SECONDS,
    CapturedAudioBlock,
    RecorderAttachment,
    ResolvedCaptureBounds,
    SessionCaptureWindow,
    frames_for_duration,
)


@pytest.mark.parametrize(
    ("sample_rate", "duration", "expected"),
    [
        (44100, 0.05, 2205),
        (48000, 0.05, 2400),
        (44101, 0.05, 2206),
        (10, 0.21, 3),
    ],
)
def test_frames_for_duration_never_undershoots(
    sample_rate: float,
    duration: float,
    expected: int,
):
    assert frames_for_duration(sample_rate, duration) == expected


@pytest.mark.parametrize("sample_rate", [0, -1, math.inf, -math.inf, math.nan])
def test_frames_for_duration_rejects_invalid_sample_rate(sample_rate: float):
    with pytest.raises(ValueError, match="sample_rate must be positive and finite"):
        frames_for_duration(sample_rate, 1.0)


@pytest.mark.parametrize("duration", [-0.1, math.inf, -math.inf, math.nan])
def test_frames_for_duration_rejects_invalid_duration(duration: float):
    with pytest.raises(ValueError, match="duration_sec must be non-negative and finite"):
        frames_for_duration(100.0, duration)


def test_frames_for_duration_accepts_zero_duration():
    assert frames_for_duration(100.0, 0.0) == 0


def test_captured_audio_block_preserves_the_supplied_array_without_copying():
    source = np.arange(12, dtype=np.float64).reshape(6, 2)
    non_contiguous_view = source[::2]

    block = CapturedAudioBlock(
        data=non_contiguous_view,
        sample_start=7,
        sample_end=10,
        monotonic_start=1.0,
        monotonic_end=1.03,
    )

    assert block.data is non_contiguous_view


@pytest.mark.parametrize(
    ("data", "sample_start", "sample_end", "message"),
    [
        (np.array(1.0, dtype=np.float32), 0, 1, "at least one-dimensional"),
        (np.zeros((1, 1), dtype=np.float32), -1, 0, "non-negative"),
        (np.zeros((1, 1), dtype=np.float32), 1, 0, "non-negative"),
        (np.zeros((2, 1), dtype=np.float32), 3, 4, "data frame count"),
    ],
)
def test_captured_audio_block_rejects_invalid_spans(
    data: np.ndarray,
    sample_start: int,
    sample_end: int,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        CapturedAudioBlock(
            data=data,
            sample_start=sample_start,
            sample_end=sample_end,
            monotonic_start=None,
            monotonic_end=None,
        )


def test_resolved_capture_bounds_exposes_exact_retained_range():
    bounds = ResolvedCaptureBounds(
        attach_sample=100,
        attach_monotonic=10.0,
        warmup_samples=5,
        target_samples=20,
    )

    assert bounds.retained_start == 105
    assert bounds.retained_end == 125


def test_recorder_attachment_publishes_resolution_once_and_releases_idempotently():
    attachment = RecorderAttachment(
        request_monotonic=9.5,
        warmup_samples=5,
        target_samples=20,
    )
    bounds = ResolvedCaptureBounds(
        attach_sample=100,
        attach_monotonic=10.0,
        warmup_samples=5,
        target_samples=20,
    )

    assert attachment.request_monotonic == 9.5
    assert attachment.warmup_samples == 5
    assert attachment.target_samples == 20
    assert attachment.resolution is None
    assert attachment.current_epoch() is None
    assert attachment.released is False

    attachment._resolve_once(bounds)
    attachment._resolve_once(bounds)

    assert attachment.resolution is bounds
    assert attachment.current_epoch() == pytest.approx(
        bounds.attach_monotonic + SESSION_WARMUP_SECONDS
    )

    attachment._mark_released()
    attachment._mark_released()
    assert attachment.released is True


def test_recorder_attachment_rejects_a_different_second_resolution():
    attachment = RecorderAttachment(
        request_monotonic=9.5,
        warmup_samples=5,
        target_samples=20,
    )
    first = ResolvedCaptureBounds(100, 10.0, 5, 20)
    different = ResolvedCaptureBounds(101, 10.01, 5, 20)

    attachment._resolve_once(first)

    with pytest.raises(ValueError, match="already resolved"):
        attachment._resolve_once(different)


def test_recorder_attachment_cannot_resolve_after_release():
    attachment = RecorderAttachment(
        request_monotonic=9.5,
        warmup_samples=5,
        target_samples=20,
    )
    attachment._mark_released()

    with pytest.raises(RuntimeError, match="released"):
        attachment._resolve_once(ResolvedCaptureBounds(100, 10.0, 5, 20))


def test_recorder_attachment_allows_idempotent_resolution_repeat_after_release():
    attachment = RecorderAttachment(
        request_monotonic=9.5,
        warmup_samples=5,
        target_samples=20,
    )
    bounds = ResolvedCaptureBounds(100, 10.0, 5, 20)
    attachment._resolve_once(bounds)
    attachment._mark_released()

    attachment._resolve_once(bounds)

    assert attachment.resolution is bounds
    assert attachment.released is True


def test_recorder_attachment_serializes_competing_resolutions():
    attachment = RecorderAttachment(
        request_monotonic=9.5,
        warmup_samples=5,
        target_samples=20,
    )
    bounds = [
        ResolvedCaptureBounds(100, 10.0, 5, 20),
        ResolvedCaptureBounds(101, 10.01, 5, 20),
    ]
    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, object]] = []

    def resolve(candidate: ResolvedCaptureBounds):
        barrier.wait()
        try:
            attachment._resolve_once(candidate)
        except ValueError as exc:
            outcomes.append(("rejected", exc))
        else:
            outcomes.append(("published", candidate))

    threads = [threading.Thread(target=resolve, args=(candidate,)) for candidate in bounds]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert [kind for kind, _ in outcomes].count("published") == 1
    assert [kind for kind, _ in outcomes].count("rejected") == 1
    assert attachment.resolution in bounds


def _attachment(
    *,
    attach_sample: int = 100,
    attach_monotonic: float = 10.0,
    warmup_samples: int = 5,
    target_samples: int = 10,
    resolved: bool = True,
) -> RecorderAttachment:
    attachment = RecorderAttachment(
        request_monotonic=attach_monotonic - 0.1,
        warmup_samples=warmup_samples,
        target_samples=target_samples,
    )
    if resolved:
        attachment._resolve_once(
            ResolvedCaptureBounds(
                attach_sample=attach_sample,
                attach_monotonic=attach_monotonic,
                warmup_samples=warmup_samples,
                target_samples=target_samples,
            )
        )
    return attachment


def _window(
    *,
    attach_sample: int = 100,
    attach_monotonic: float = 10.0,
) -> SessionCaptureWindow:
    return SessionCaptureWindow(
        attachment=_attachment(
            attach_sample=attach_sample,
            attach_monotonic=attach_monotonic,
        ),
        sample_rate=100,
        record_duration=0.1,
    )


def _tagged_block(sample_start: int, sample_end: int) -> CapturedAudioBlock:
    return CapturedAudioBlock(
        data=np.arange(sample_start, sample_end, dtype=np.float32).reshape(-1, 1),
        sample_start=sample_start,
        sample_end=sample_end,
        monotonic_start=sample_start / 100,
        monotonic_end=sample_end / 100,
    )


@pytest.mark.parametrize(("sample_start", "sample_end"), [(100, 104), (100, 105)])
def test_window_drops_blocks_at_or_before_retained_start(
    sample_start: int,
    sample_end: int,
):
    window = _window()

    assert window.accept(_tagged_block(sample_start, sample_end)) is None
    assert window.retained_frames == 0
    assert window.absolute_retained_end == 105


def test_window_slices_a_block_crossing_retained_start_and_rebases_to_zero():
    window = _window()
    source = np.arange(8, dtype=np.float32).reshape(-1, 1)
    block = CapturedAudioBlock(
        data=source,
        sample_start=102,
        sample_end=110,
        monotonic_start=1.02,
        monotonic_end=1.10,
    )

    retained = window.accept(block)

    assert retained is not None
    np.testing.assert_array_equal(retained.data[:, 0], [3, 4, 5, 6, 7])
    assert np.shares_memory(retained.data, source)
    assert (retained.sample_start, retained.sample_end) == (0, 5)
    assert window.retained_frames == 5
    assert window.absolute_retained_end == 110


def test_window_retains_a_block_beginning_exactly_at_retained_start():
    window = _window()
    block = _tagged_block(105, 109)

    retained = window.accept(block)

    assert retained is not None
    np.testing.assert_array_equal(retained.data, block.data)
    assert (retained.sample_start, retained.sample_end) == (0, 4)


def test_window_clips_exactly_at_target_end_and_then_ignores_later_blocks():
    window = _window()

    retained = window.accept(_tagged_block(105, 120))

    assert retained is not None
    np.testing.assert_array_equal(
        retained.data[:, 0], np.arange(105, 115, dtype=np.float32)
    )
    assert (retained.sample_start, retained.sample_end) == (0, 10)
    assert window.target_samples == 10
    assert window.retained_frames == 10
    assert window.absolute_retained_end == 115
    assert window.complete is True
    assert window.accept(_tagged_block(115, 118)) is None


def test_window_deduplicates_repeated_and_overlapping_deliveries():
    window = _window()
    first = window.accept(_tagged_block(105, 110))

    assert first is not None
    assert window.accept(_tagged_block(105, 110)) is None

    overlap = window.accept(_tagged_block(108, 113))

    assert overlap is not None
    np.testing.assert_array_equal(overlap.data[:, 0], [110, 111, 112])
    assert (overlap.sample_start, overlap.sample_end) == (5, 8)
    assert window.retained_frames == 8


def test_adjacent_differently_sized_blocks_produce_a_continuous_timeline():
    window = _window()

    retained = [
        window.accept(_tagged_block(105, 107)),
        window.accept(_tagged_block(107, 112)),
        window.accept(_tagged_block(112, 119)),
    ]

    assert all(block is not None for block in retained)
    assert [
        (block.sample_start, block.sample_end)  # type: ignore[union-attr]
        for block in retained
    ] == [(0, 2), (2, 7), (7, 10)]
    np.testing.assert_array_equal(
        np.concatenate([block.data for block in retained if block is not None])[:, 0],
        np.arange(105, 115, dtype=np.float32),
    )
    assert window.complete is True


def test_window_rejects_a_gap_in_the_retained_sample_timeline():
    window = _window()
    window.accept(_tagged_block(105, 110))

    with pytest.raises(ValueError, match="gap.*110.*112"):
        window.accept(_tagged_block(112, 115))

    assert window.retained_frames == 5
    assert window.absolute_retained_end == 110


def test_freeze_is_idempotent_and_prevents_later_acceptance():
    window = _window()
    window.accept(_tagged_block(105, 109))

    assert window.freeze() == 4
    assert window.freeze() == 4
    assert window.accept(_tagged_block(109, 115)) is None
    assert window.retained_frames == 4
    assert window.complete is False


def test_two_windows_trim_independently_from_their_own_attachment():
    early = _window(attach_sample=100)
    late = _window(attach_sample=200)
    block = _tagged_block(100, 210)

    early_retained = early.accept(block)
    late_retained = late.accept(block)

    assert early_retained is not None
    assert late_retained is not None
    np.testing.assert_array_equal(
        early_retained.data[:, 0], np.arange(105, 115, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        late_retained.data[:, 0], np.arange(205, 210, dtype=np.float32)
    )
    assert (early_retained.sample_start, early_retained.sample_end) == (0, 10)
    assert (late_retained.sample_start, late_retained.sample_end) == (0, 5)


def test_unresolved_window_emits_nothing_until_exact_bounds_are_published():
    attachment = _attachment(resolved=False)
    window = SessionCaptureWindow(
        attachment=attachment,
        sample_rate=100,
        record_duration=0.1,
    )

    assert window.current_epoch is None
    assert window.absolute_retained_end is None
    assert window.accept(_tagged_block(100, 110)) is None

    attachment._resolve_once(ResolvedCaptureBounds(100, 10.0, 5, 10))
    retained = window.accept(_tagged_block(100, 110))

    assert retained is not None
    np.testing.assert_array_equal(retained.data[:, 0], [105, 106, 107, 108, 109])
    assert window.current_epoch == pytest.approx(10.05)


@pytest.mark.parametrize(
    ("warmup_samples", "target_samples", "message"),
    [(4, 10, "warmup_samples"), (5, 11, "target_samples")],
)
def test_window_rejects_attachment_metadata_from_a_different_request(
    warmup_samples: int,
    target_samples: int,
    message: str,
):
    attachment = _attachment(
        warmup_samples=warmup_samples,
        target_samples=target_samples,
    )

    with pytest.raises(ValueError, match=message):
        SessionCaptureWindow(
            attachment=attachment,
            sample_rate=100,
            record_duration=0.1,
        )


def test_freeze_wins_deterministically_over_an_accept_waiting_for_the_lock():
    window = _window()
    ready = threading.Event()
    result: list[object] = []

    def accept_block():
        ready.set()
        result.append(window.accept(_tagged_block(105, 110)))

    with window._lock:
        worker = threading.Thread(target=accept_block)
        worker.start()
        assert ready.wait(timeout=2)
        frozen_count = window.freeze()

    worker.join(timeout=2)

    assert not worker.is_alive()
    assert frozen_count == 0
    assert result == [None]
    assert window.retained_frames == 0
    assert window.freeze() == 0


def test_accept_wins_deterministically_over_freeze_waiting_for_the_lock():
    window = _window()
    ready = threading.Event()
    frozen_counts: list[int] = []

    def freeze_window():
        ready.set()
        frozen_counts.append(window.freeze())

    with window._lock:
        worker = threading.Thread(target=freeze_window)
        worker.start()
        assert ready.wait(timeout=2)
        retained = window.accept(_tagged_block(105, 110))

    worker.join(timeout=2)

    assert not worker.is_alive()
    assert retained is not None
    assert (retained.sample_start, retained.sample_end) == (0, 5)
    assert frozen_counts == [5]
    assert window.retained_frames == 5
    assert window.accept(_tagged_block(110, 115)) is None
