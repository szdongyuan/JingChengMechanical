from __future__ import annotations

import dataclasses
import math
import sys

import numpy as np
import pytest

from app.service.analysis.resident_audio.contracts import (
    ArtifactState,
    BlockTimeQuality,
    CaptureArtifactOwnership,
    CaptureErrorCode,
    CaptureLayerError,
    CaptureOwnerState,
    CaptureRequest,
    CapturedAudioBlock,
    FrozenCaptureArtifact,
    LeaseState,
    RawCallbackBlock,
    ResolvedInputDevice,
    StreamReadyToken,
    StreamState,
    checked_capture_request,
    checked_capture_size,
)
from app.service.analysis.resident_audio.timeline import (
    MappingStatus,
    StreamTimeline,
    TimelineMapping,
    TimelineState,
)


EXPECTED_ERROR_CODES = {
    "TOO_MANY_REQUIRED_HARDWARE",
    "CAPTURE_CAPACITY_FULL",
    "INVALID_CAPTURE_REQUEST",
    "CAPTURE_STORAGE_CAPACITY",
    "HARDWARE_RESOLUTION_CONFLICT",
    "STREAM_OPEN_FAILED",
    "STREAM_NOT_READY",
    "STREAM_GENERATION_CHANGED",
    "FIRST_VALID_BLOCK_TIMEOUT",
    "TIMING_UNAVAILABLE",
    "TRIGGER_AUDIO_EXPIRED",
    "INPUT_OVERFLOW_OR_GAP",
    "INPUT_INGRESS_BACKLOG",
    "INPUT_DATA_TIMEOUT",
    "CAPTURE_PERSISTENCE_BACKLOG",
    "CAPTURE_ARTIFACT_IO_FAILED",
    "CAPTURE_CANCELED",
    "POOL_SHUTTING_DOWN",
    "SHUTDOWN_INCOMPLETE",
}


def _token(**overrides: object) -> StreamReadyToken:
    values = {
        "hardware_id": "hw-1",
        "pool_epoch": "pool-a",
        "stream_generation": 3,
        "sample_rate": 10.0,
        "channels": 2,
        "earliest_mappable_monotonic": 9.0,
        "latest_mappable_monotonic": 10.0,
        "earliest_sample": 0,
        "latest_sample": 10,
    }
    values.update(overrides)
    return StreamReadyToken(**values)


def _raw(
    *,
    sequence: int,
    callback_monotonic: float,
    adc_time: float,
    current_time: float,
    frames: int = 10,
    sample_rate: float = 10.0,
) -> RawCallbackBlock:
    del sample_rate
    return RawCallbackBlock(
        pool_epoch="pool-a",
        hardware_id="hw-1",
        stream_generation=3,
        callback_sequence=sequence,
        callback_monotonic=callback_monotonic,
        input_buffer_adc_time=adc_time,
        current_time=current_time,
        status="",
        frames=frames,
        data=np.zeros((frames, 2), dtype=np.float32),
    )


def test_contracts_are_frozen_and_metadata_is_immutable():
    resolved = ResolvedInputDevice(
        hardware_id="hw-1",
        device_index=4,
        name="Input",
        host_api="WASAPI",
        sample_rate=48_000.0,
        channels=2,
        dtype="float32",
        resolved_at_monotonic=1.25,
        resolution_version="scan-7",
    )
    request = CaptureRequest(
        run_id="run-1",
        trigger_monotonic=4.0,
        record_duration=1.0,
        stream_token=_token(),
        artifact_metadata={"configuration_id": 8},
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.device_index = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.artifact_metadata["configuration_id"] = 9  # type: ignore[index]


def test_resolved_input_device_requires_resolution_provenance():
    with pytest.raises(ValueError, match="resolution provenance"):
        ResolvedInputDevice(
            hardware_id="hw-1",
            device_index=4,
            name="Input",
            host_api="WASAPI",
            sample_rate=48_000.0,
            channels=2,
        )


@pytest.mark.parametrize(
    "resolution_version",
    ["", "   ", True, False, -1, 1.5, object()],
)
def test_resolved_input_device_rejects_unusable_resolution_versions(
    resolution_version: object,
):
    with pytest.raises((TypeError, ValueError), match="resolution_version"):
        ResolvedInputDevice(
            hardware_id="hw-1",
            device_index=4,
            name="Input",
            host_api="WASAPI",
            sample_rate=48_000.0,
            channels=2,
            resolution_version=resolution_version,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("resolution_version", ["scan-7", 0, 7, np.int64(8)])
def test_resolved_input_device_accepts_usable_resolution_versions(
    resolution_version: str | int,
):
    resolved = ResolvedInputDevice(
        hardware_id="hw-1",
        device_index=4,
        name="Input",
        host_api="WASAPI",
        sample_rate=48_000.0,
        channels=2,
        resolution_version=resolution_version,
    )

    assert resolved.resolution_version == resolution_version
    if not isinstance(resolution_version, str):
        assert type(resolved.resolution_version) is int


def test_nested_lightweight_metadata_is_recursively_frozen():
    source = {
        "configuration": {
            "labels": ["left", "right"],
            "thresholds": (1.0, 2.0),
        }
    }
    request = CaptureRequest(
        run_id="run-1",
        trigger_monotonic=4.0,
        record_duration=1.0,
        stream_token=_token(),
        artifact_metadata=source,
    )
    error = CaptureLayerError(
        CaptureErrorCode.INVALID_CAPTURE_REQUEST,
        "invalid",
        source,
    )
    source["configuration"]["labels"].append("mutated")

    assert request.artifact_metadata["configuration"]["labels"] == (
        "left",
        "right",
    )
    assert error.context["configuration"]["labels"] == ("left", "right")
    with pytest.raises(TypeError):
        request.artifact_metadata["configuration"]["new"] = "value"
    with pytest.raises(TypeError):
        error.context["configuration"]["new"] = "value"


def test_metadata_and_error_context_reject_objects_that_can_retain_audio():
    raw = _raw(
        sequence=0,
        callback_monotonic=10.0,
        adc_time=20.0,
        current_time=20.0,
    )

    with pytest.raises(TypeError, match="lightweight"):
        CaptureRequest(
            run_id="run-1",
            trigger_monotonic=4.0,
            record_duration=1.0,
            stream_token=_token(),
            artifact_metadata={"nested": [{"raw_block": raw}]},
        )
    with pytest.raises(TypeError, match="lightweight"):
        CaptureLayerError(
            CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
            "unsafe",
            {"nested": [{"raw_block": raw}]},
        )
    with pytest.raises(TypeError, match="mapping"):
        CaptureLayerError(
            CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
            "unsafe",
            [],  # type: ignore[arg-type]
        )


def test_all_stable_errors_and_explicit_states_are_present():
    assert {code.value for code in CaptureErrorCode} == EXPECTED_ERROR_CODES
    assert {state.value for state in LeaseState} == {
        "RESERVED",
        "COMMITTING",
        "COMMITTED",
        "RELEASED",
        "FAILED",
        "CANCELED",
    }
    assert {state.value for state in StreamState} == {
        "CLOSED",
        "OPENING",
        "READY",
        "TIMING_RECOVERY",
        "FAILED",
        "RETRY_WAIT",
        "CLOSING",
    }
    assert {state.value for state in CaptureOwnerState} == {
        "PREPARED",
        "WAITING_FOR_MAPPING",
        "CATCHING_UP",
        "LIVE",
        "FREEZING",
        "COMPLETED",
        "FAILED",
        "CANCELED",
    }
    assert {state.value for state in ArtifactState} == {
        "PARTIAL",
        "READY_UNCLAIMED",
        "CLAIMED",
        "TRANSFERRED",
        "RELEASED",
        "DELETED",
        "DELETE_PENDING",
    }


def test_capture_layer_error_has_stable_code_message_and_safe_context():
    error = CaptureLayerError(
        CaptureErrorCode.INVALID_CAPTURE_REQUEST,
        "duration is invalid",
        {"run_id": "r1"},
    )

    assert error.code is CaptureErrorCode.INVALID_CAPTURE_REQUEST
    assert str(error) == "duration is invalid"
    assert error.context == {"run_id": "r1"}
    with pytest.raises(TypeError):
        error.context["frames"] = 1  # type: ignore[index]
    with pytest.raises(TypeError, match="audio arrays"):
        CaptureLayerError(
            CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
            "unsafe",
            {"data": np.zeros((1, 1), dtype=np.float32)},
        )


def test_checked_capture_size_uses_ceil_and_float32_bytes():
    assert checked_capture_size(0.1001, 10.0, 2).target_frames == 2
    sizing = checked_capture_size(3600.0, 48_000.0, 2)
    assert sizing.target_frames == 172_800_000
    assert sizing.artifact_bytes == 1_382_400_000


@pytest.mark.parametrize("value", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_checked_capture_size_rejects_invalid_duration(value: float):
    with pytest.raises(CaptureLayerError) as caught:
        checked_capture_size(value, 10.0, 2)
    assert caught.value.code is CaptureErrorCode.INVALID_CAPTURE_REQUEST


def test_checked_capture_size_rejects_duration_over_product_limit():
    with pytest.raises(CaptureLayerError):
        checked_capture_size(3600.00001, 10.0, 2)


@pytest.mark.parametrize("value", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_checked_capture_size_rejects_invalid_sample_rate(value: float):
    with pytest.raises(CaptureLayerError):
        checked_capture_size(1.0, value, 2)


@pytest.mark.parametrize("channels", [True, False, 0, -1, 1.5])
def test_checked_capture_size_rejects_invalid_channels(channels: object):
    with pytest.raises(CaptureLayerError):
        checked_capture_size(1.0, 10.0, channels)  # type: ignore[arg-type]


def test_checked_capture_size_rejects_zero_frames_platform_overflow_and_8_gib():
    with pytest.raises(CaptureLayerError):
        checked_capture_size(1e-300, 1e-300, 1)
    with pytest.raises(CaptureLayerError):
        checked_capture_size(3600.0, float(sys.maxsize), 1)

    exactly_8_gib = checked_capture_size(1.0, 2_147_483_648.0, 1)
    assert exactly_8_gib.artifact_bytes == 8 * 1024**3
    with pytest.raises(CaptureLayerError):
        checked_capture_size(1.0, 2_147_483_649.0, 1)


def test_checked_capture_request_rejects_invalid_or_future_trigger():
    request = CaptureRequest(
        run_id="run-1",
        trigger_monotonic=10.11,
        record_duration=0.5,
        stream_token=_token(),
    )
    with pytest.raises(CaptureLayerError):
        checked_capture_request(request, commit_monotonic=10.0)

    for trigger in (0.0, -1.0, math.nan, math.inf):
        invalid = dataclasses.replace(request, trigger_monotonic=trigger)
        with pytest.raises(CaptureLayerError):
            checked_capture_request(invalid, commit_monotonic=10.0)

    allowed = dataclasses.replace(request, trigger_monotonic=10.1)
    assert checked_capture_request(allowed, commit_monotonic=10.0).target_frames == 5


@pytest.mark.parametrize(
    "array",
    [
        np.zeros((2, 2), dtype=np.float64),
        np.zeros((4, 2), dtype=np.float32)[::2],
        np.zeros((2,), dtype=np.float32),
    ],
)
def test_raw_callback_block_requires_contiguous_float32_matrix(array: np.ndarray):
    with pytest.raises((TypeError, ValueError)):
        RawCallbackBlock(
            pool_epoch="pool-a",
            hardware_id="hw-1",
            stream_generation=3,
            callback_sequence=0,
            callback_monotonic=1.0,
            input_buffer_adc_time=1.0,
            current_time=1.0,
            status="",
            frames=2,
            data=array,
        )


def test_raw_callback_block_normalizes_numeric_timestamp_fields():
    raw = RawCallbackBlock(
        pool_epoch="pool-a",
        hardware_id="hw-1",
        stream_generation=3,
        callback_sequence=0,
        callback_monotonic=np.float32(10.0),
        input_buffer_adc_time=np.int64(20),
        current_time=np.float64(20.5),
        status="",
        frames=2,
        data=np.zeros((2, 2), dtype=np.float32),
    )
    invalid_timing = dataclasses.replace(
        raw,
        input_buffer_adc_time=math.nan,
        current_time=math.inf,
    )

    assert type(raw.callback_monotonic) is float
    assert type(raw.input_buffer_adc_time) is float
    assert type(raw.current_time) is float
    assert math.isnan(invalid_timing.input_buffer_adc_time)
    assert math.isinf(invalid_timing.current_time)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("callback_monotonic", "10"),
        ("callback_monotonic", True),
        ("callback_monotonic", None),
        ("input_buffer_adc_time", "20"),
        ("input_buffer_adc_time", object()),
        ("current_time", "20.5"),
        ("current_time", False),
    ],
)
def test_raw_callback_block_rejects_malformed_timestamp_types(
    field_name: str,
    value: object,
):
    values = {
        "pool_epoch": "pool-a",
        "hardware_id": "hw-1",
        "stream_generation": 3,
        "callback_sequence": 0,
        "callback_monotonic": 10.0,
        "input_buffer_adc_time": 20.0,
        "current_time": 20.5,
        "status": "",
        "frames": 2,
        "data": np.zeros((2, 2), dtype=np.float32),
    }
    values[field_name] = value

    with pytest.raises((TypeError, ValueError), match=field_name):
        RawCallbackBlock(**values)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_raw_callback_block_requires_finite_callback_monotonic(value: float):
    with pytest.raises(ValueError, match="callback_monotonic"):
        RawCallbackBlock(
            pool_epoch="pool-a",
            hardware_id="hw-1",
            stream_generation=3,
            callback_sequence=0,
            callback_monotonic=value,
            input_buffer_adc_time=20.0,
            current_time=20.5,
            status="",
            frames=2,
            data=np.zeros((2, 2), dtype=np.float32),
        )


def test_raw_and_captured_blocks_preserve_identity_and_half_open_range():
    raw = _raw(
        sequence=4,
        callback_monotonic=10.0,
        adc_time=20.0,
        current_time=20.1,
        frames=4,
    )
    data = np.arange(8, dtype=np.float32).reshape(4, 2)
    captured = CapturedAudioBlock(
        pool_epoch=raw.pool_epoch,
        hardware_id=raw.hardware_id,
        stream_generation=raw.stream_generation,
        sample_start=7,
        sample_end=11,
        data=data,
        monotonic_start=9.9,
        monotonic_end=10.3,
        time_quality=BlockTimeQuality.CONFIRMED,
        status_summary="",
    )

    assert (captured.pool_epoch, captured.hardware_id, captured.stream_generation) == (
        "pool-a",
        "hw-1",
        3,
    )
    assert captured.sample_end - captured.sample_start == captured.data.shape[0]
    assert not captured.data.flags.writeable
    with pytest.raises(ValueError):
        captured.data[0, 0] = 99.0

    with pytest.raises(ValueError, match="sample range"):
        dataclasses.replace(captured, sample_end=12)


def test_captured_block_time_quality_is_separate_from_portaudio_status():
    data = np.zeros((4, 2), dtype=np.float32)
    confirmed = CapturedAudioBlock(
        pool_epoch="pool-a",
        hardware_id="hw-1",
        stream_generation=3,
        sample_start=0,
        sample_end=4,
        data=data.copy(),
        monotonic_start=9.9,
        monotonic_end=10.3,
        time_quality=BlockTimeQuality.CONFIRMED,
        status_summary="portaudio status text",
    )
    recovery = dataclasses.replace(
        confirmed,
        data=data.copy(),
        monotonic_start=None,
        monotonic_end=None,
        time_quality=BlockTimeQuality.RECOVERY_UNCONFIRMED,
    )

    assert confirmed.time_quality is BlockTimeQuality.CONFIRMED
    assert recovery.time_quality is BlockTimeQuality.RECOVERY_UNCONFIRMED
    assert recovery.status_summary == "portaudio status text"


@pytest.mark.parametrize(
    ("quality", "start", "end"),
    [
        (BlockTimeQuality.CONFIRMED, None, None),
        (BlockTimeQuality.CONFIRMED, 1.0, None),
        (BlockTimeQuality.RECOVERY_UNCONFIRMED, 1.0, 1.4),
        (BlockTimeQuality.RECOVERY_UNCONFIRMED, None, 1.4),
    ],
)
def test_captured_block_rejects_time_quality_bound_mismatches(
    quality: BlockTimeQuality,
    start: float | None,
    end: float | None,
):
    with pytest.raises(ValueError, match="time quality"):
        CapturedAudioBlock(
            pool_epoch="pool-a",
            hardware_id="hw-1",
            stream_generation=3,
            sample_start=0,
            sample_end=4,
            data=np.zeros((4, 2), dtype=np.float32),
            monotonic_start=start,
            monotonic_end=end,
            time_quality=quality,
            status_summary="",
        )


def test_frozen_artifact_is_lightweight_and_immutable():
    artifact = FrozenCaptureArtifact(
        run_id="run-1",
        artifact_id="artifact-1",
        path="staging/capture-1.raw",
        format_version=1,
        dtype="float32",
        sample_rate=48_000.0,
        channels=2,
        captured_frames=48_000,
        target_frames=48_000,
        sample_start=7,
        sample_end=48_007,
        pool_epoch="pool-a",
        stream_generation=3,
        ownership=CaptureArtifactOwnership.READY_UNCLAIMED,
        trigger_monotonic=3.0,
        capture_start_monotonic=3.00001,
        time_quality="strict",
        publication_token="publication-1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.path = "other"  # type: ignore[misc]
    assert not any(
        isinstance(getattr(artifact, item.name), np.ndarray)
        for item in dataclasses.fields(artifact)
    )


def test_timeline_uses_portaudio_offset_and_ignores_injected_wall_clock():
    wall_clock = [1000.0]
    timeline = StreamTimeline(
        sample_rate=10.0,
        input_latency=0.0,
        monotonic_clock=lambda: 999.0,
        wall_clock=lambda: wall_clock[0],
    )
    update = timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=100.0,
            adc_time=49.0,
            current_time=50.0,
        )
    )
    wall_clock[0] = -50_000.0

    assert update.block_start_monotonic == pytest.approx(99.0)
    assert timeline.map_trigger(99.5).sample_index == 5


@pytest.mark.parametrize(
    ("adc", "current"),
    [
        (0.0, 1.0),
        (1.0, 0.0),
        (math.nan, 1.0),
        (1.0, math.inf),
        (2.0, 1.0),
        (1.0, 1.50001),
    ],
)
def test_first_invalid_timestamp_never_becomes_ready(adc: float, current: float):
    timeline = StreamTimeline(sample_rate=10.0, input_latency=0.0)
    update = timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=5.0,
            adc_time=adc,
            current_time=current,
            frames=1,
        )
    )
    assert update.state is TimelineState.AWAITING_FIRST_ANCHOR
    assert not timeline.ready


def test_adc_age_uses_latency_and_four_block_duration_and_future_tolerance():
    timeline = StreamTimeline(sample_rate=100.0, input_latency=0.8)
    accepted = timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=18.81,
            current_time=19.61,
            frames=10,
        )
    )
    assert accepted.state is TimelineState.READY

    too_future = StreamTimeline(sample_rate=100.0, input_latency=0.0)
    update = too_future.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.011,
            current_time=20.0,
            frames=10,
        )
    )
    assert update.state is TimelineState.AWAITING_FIRST_ANCHOR


def test_mapping_ceil_boundaries_and_after_confirmed_range():
    timeline = StreamTimeline(sample_rate=10.0, input_latency=0.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )

    exact = timeline.map_trigger(10.0)
    between = timeline.map_trigger(10.00001)
    assert exact.sample_index == 0
    assert exact.mapped_sample_monotonic == pytest.approx(10.0)
    assert between.sample_index == 1
    assert between.mapped_sample_monotonic == pytest.approx(10.1)
    assert timeline.map_trigger(10.5).mapped_sample_monotonic == pytest.approx(10.5)
    assert timeline.map_trigger(11.0).mapped_sample_monotonic == pytest.approx(11.0)
    after = timeline.map_trigger(11.00001)
    expired = timeline.map_trigger(9.999)
    assert after.status is MappingStatus.AFTER_CONFIRMED_RANGE
    assert after.mapped_sample_monotonic is None
    assert expired.status is MappingStatus.EXPIRED
    assert expired.mapped_sample_monotonic is None


@pytest.mark.parametrize("mapped_time", [None, float("nan"), float("inf")])
def test_mapped_timeline_result_requires_finite_sample_time(mapped_time) -> None:
    with pytest.raises(ValueError, match="finite sample time"):
        TimelineMapping(MappingStatus.MAPPED, 1, mapped_time)


def test_non_mapped_timeline_result_cannot_carry_sample_coordinates() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        TimelineMapping(MappingStatus.EXPIRED, 1, 10.1)


def test_multiple_anchors_interpolate_and_exact_boundary_uses_next_sample():
    timeline = StreamTimeline(sample_rate=10.0, input_latency=0.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    timeline.ingest(
        _raw(
            sequence=1,
            callback_monotonic=11.1,
            adc_time=21.0,
            current_time=21.0,
        )
    )

    exact_anchor = timeline.map_trigger(11.1)
    interpolated = timeline.map_trigger(10.56)
    extrapolated = timeline.map_trigger(12.1)
    assert exact_anchor.sample_index == 10
    assert exact_anchor.mapped_sample_monotonic == pytest.approx(11.1)
    assert interpolated.sample_index == 6
    assert interpolated.mapped_sample_monotonic == pytest.approx(10.66)
    assert extrapolated.sample_index == 20
    assert extrapolated.mapped_sample_monotonic == pytest.approx(12.1)


def test_one_invalid_block_recovers_with_next_continuous_anchor():
    timeline = StreamTimeline(sample_rate=10.0, input_latency=0.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    recovery = timeline.ingest(
        _raw(
            sequence=1,
            callback_monotonic=11.0,
            adc_time=0.0,
            current_time=0.0,
        )
    )

    assert recovery.state is TimelineState.TIMING_RECOVERY
    assert timeline.map_trigger(11.5).status is MappingStatus.AFTER_CONFIRMED_RANGE

    restored = timeline.ingest(
        _raw(
            sequence=2,
            callback_monotonic=12.0,
            adc_time=22.0,
            current_time=22.0,
        )
    )
    assert restored.state is TimelineState.READY
    recovered = timeline.map_trigger(11.5)
    allowed_extrapolation = timeline.map_trigger(12.5)
    assert recovered.sample_index == 15
    assert recovered.mapped_sample_monotonic == pytest.approx(11.5)
    assert allowed_extrapolation.sample_index == 25
    assert allowed_extrapolation.mapped_sample_monotonic == pytest.approx(12.5)


def test_second_invalid_backwards_discontinuous_or_gap_fails_generation():
    second_invalid = StreamTimeline(sample_rate=10.0)
    second_invalid.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    second_invalid.ingest(
        _raw(
            sequence=1,
            callback_monotonic=11.0,
            adc_time=0.0,
            current_time=0.0,
        )
    )
    with pytest.raises(CaptureLayerError) as invalid:
        second_invalid.ingest(
            _raw(
                sequence=2,
                callback_monotonic=12.0,
                adc_time=0.0,
                current_time=0.0,
            )
        )
    assert invalid.value.code is CaptureErrorCode.TIMING_UNAVAILABLE

    backwards = StreamTimeline(sample_rate=10.0)
    backwards.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    with pytest.raises(CaptureLayerError):
        backwards.ingest(
            _raw(
                sequence=1,
                callback_monotonic=9.5,
                adc_time=21.0,
                current_time=21.0,
            )
        )

    discontinuous = StreamTimeline(sample_rate=10.0)
    discontinuous.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    with pytest.raises(CaptureLayerError):
        discontinuous.ingest(
            _raw(
                sequence=1,
                callback_monotonic=13.1,
                adc_time=21.0,
                current_time=21.0,
            )
        )

    gap = StreamTimeline(sample_rate=10.0)
    with pytest.raises(CaptureLayerError) as gap_error:
        gap.ingest(
            _raw(
                sequence=0,
                callback_monotonic=10.0,
                adc_time=20.0,
                current_time=20.0,
            ),
            input_overflow_or_gap=True,
        )
    assert gap_error.value.code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP


def test_mapping_is_unavailable_after_consecutive_invalid_timing_failure():
    timeline = StreamTimeline(sample_rate=10.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    timeline.ingest(
        _raw(
            sequence=1,
            callback_monotonic=11.0,
            adc_time=0.0,
            current_time=0.0,
        )
    )
    with pytest.raises(CaptureLayerError):
        timeline.ingest(
            _raw(
                sequence=2,
                callback_monotonic=12.0,
                adc_time=0.0,
                current_time=0.0,
            )
        )

    with pytest.raises(CaptureLayerError) as mapping_error:
        timeline.map_trigger(10.5)
    assert mapping_error.value.code is CaptureErrorCode.TIMING_UNAVAILABLE


def test_mapping_is_unavailable_after_backwards_anchor_failure():
    timeline = StreamTimeline(sample_rate=10.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    with pytest.raises(CaptureLayerError):
        timeline.ingest(
            _raw(
                sequence=1,
                callback_monotonic=9.5,
                adc_time=21.0,
                current_time=21.0,
            )
        )

    with pytest.raises(CaptureLayerError) as mapping_error:
        timeline.map_trigger(10.5)
    assert mapping_error.value.code is CaptureErrorCode.TIMING_UNAVAILABLE


def test_mapping_is_unavailable_after_callback_sequence_gap():
    timeline = StreamTimeline(sample_rate=10.0)
    timeline.ingest(
        _raw(
            sequence=0,
            callback_monotonic=10.0,
            adc_time=20.0,
            current_time=20.0,
        )
    )
    with pytest.raises(CaptureLayerError) as ingest_error:
        timeline.ingest(
            _raw(
                sequence=2,
                callback_monotonic=11.0,
                adc_time=21.0,
                current_time=21.0,
            )
        )
    assert ingest_error.value.code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP

    with pytest.raises(CaptureLayerError) as mapping_error:
        timeline.map_trigger(10.5)
    assert mapping_error.value.code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP


def test_timeline_rejects_cursor_overflow():
    timeline = StreamTimeline(sample_rate=10.0, initial_cursor=sys.maxsize - 2)
    with pytest.raises(CaptureLayerError) as caught:
        timeline.ingest(
            _raw(
                sequence=0,
                callback_monotonic=10.0,
                adc_time=20.0,
                current_time=20.0,
                frames=3,
            )
        )
    assert caught.value.code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP


def test_trim_retains_boundary_anchor_for_interpolation():
    timeline = StreamTimeline(sample_rate=10.0)
    for sequence, block_start in enumerate((10.0, 11.0, 12.0)):
        timeline.ingest(
            _raw(
                sequence=sequence,
                callback_monotonic=block_start,
                adc_time=20.0 + sequence,
                current_time=20.0 + sequence,
            )
        )

    timeline.trim_before(15)

    assert timeline.earliest_sample == 15
    assert timeline.map_trigger(11.5).sample_index == 15
    assert timeline.map_trigger(11.499).status is MappingStatus.EXPIRED
    assert timeline.anchor_count >= 2
