from __future__ import annotations

import math
import threading

import numpy
import pytest

from app.service.analysis.scheduler import AudioStorageKind, PersistedAudioReference
from app.service.run_scoped_playback.contracts import (
    DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_REPLACEMENT_TIMEOUT_SECONDS,
    BackendStartReport,
    BackendStartStatus,
    PlaybackCode,
    PlaybackContractError,
    PlaybackStatus,
    RunPlaybackRequest,
)
from app.service.run_scoped_playback.service import RunScopedPlaybackService

from .conftest import (
    AUDIO_ID,
    FINAL_PATH,
    OTHER_RUN_ID,
    RUN_ID,
    FakeAudioLoader,
    FakeAudioRepository,
    FakeBackend,
    FakeLoadedAudio,
    FakeRunRepository,
    SequenceClock,
    make_audio,
    make_config_snapshot,
    make_run,
)


def request(**overrides: object) -> RunPlaybackRequest:
    values = {
        "run_id": RUN_ID,
        "display_channel_index": 7,
        "window_start_seconds": 0.0,
        "window_end_seconds": 2.0,
    }
    values.update(overrides)
    return RunPlaybackRequest(**values)


def make_service(
    *,
    run_repository: object | None = None,
    audio_repository: object | None = None,
    audio_loader: object | None = None,
    backend: object | None = None,
    monotonic: object | None = None,
    replacement_timeout_seconds: float = DEFAULT_REPLACEMENT_TIMEOUT_SECONDS,
    backend_startup_timeout_seconds: float = (
        DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS
    ),
) -> RunScopedPlaybackService:
    return RunScopedPlaybackService(
        run_repository=FakeRunRepository()
        if run_repository is None
        else run_repository,
        audio_repository=FakeAudioRepository()
        if audio_repository is None
        else audio_repository,
        audio_loader=FakeAudioLoader()
        if audio_loader is None
        else audio_loader,
        backend=FakeBackend() if backend is None else backend,
        monotonic=SequenceClock(10.0, 10.0)
        if monotonic is None
        else monotonic,
        replacement_timeout_seconds=replacement_timeout_seconds,
        backend_startup_timeout_seconds=backend_startup_timeout_seconds,
    )


def assert_code(result, code: PlaybackCode) -> None:
    assert result.code is code
    assert result.replaced_run_id is None
    assert len(result.summary.encode("utf-8")) <= 256


def test_constructor_accepts_exact_defaults_and_freezes_inputs():
    clock = SequenceClock(10.0, 10.25)
    service = make_service(monotonic=clock)

    assert service._monotonic is clock
    assert service._replacement_timeout_seconds == 1.0
    assert service._backend_startup_timeout_seconds == 1.0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("run_repository", object(), TypeError),
        ("audio_repository", object(), TypeError),
        ("audio_loader", object(), TypeError),
        ("backend", object(), TypeError),
        ("monotonic", object(), TypeError),
        ("replacement_timeout_seconds", 1, PlaybackContractError),
        ("replacement_timeout_seconds", True, PlaybackContractError),
        ("replacement_timeout_seconds", 0.0, PlaybackContractError),
        ("replacement_timeout_seconds", -1.0, PlaybackContractError),
        ("replacement_timeout_seconds", math.inf, PlaybackContractError),
        ("backend_startup_timeout_seconds", 1, PlaybackContractError),
        ("backend_startup_timeout_seconds", True, PlaybackContractError),
        ("backend_startup_timeout_seconds", 0.0, PlaybackContractError),
        ("backend_startup_timeout_seconds", math.nan, PlaybackContractError),
    ],
)
def test_constructor_rejects_invalid_boundaries(field, value, error):
    kwargs = {field: value}
    with pytest.raises(error):
        make_service(**kwargs)


@pytest.mark.parametrize("value", [None, object(), {"run_id": RUN_ID}])
def test_play_requires_exact_request_before_gate_or_dependencies(value):
    calls: list[tuple[str, object]] = []
    service = make_service(
        run_repository=FakeRunRepository(calls=calls),
        audio_repository=FakeAudioRepository(calls=calls),
        audio_loader=FakeAudioLoader(calls=calls),
        backend=FakeBackend(calls=calls),
        monotonic=lambda: (_ for _ in ()).throw(AssertionError("clock called")),
    )

    with pytest.raises(TypeError):
        service.play(value)
    assert calls == []


def test_second_play_is_fail_fast_busy_with_zero_work():
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, object]] = []
    loaded = FakeLoadedAudio()
    service = make_service(
        run_repository=FakeRunRepository(calls=calls),
        audio_repository=FakeAudioRepository(calls=calls),
        audio_loader=FakeAudioLoader(
            loaded, calls=calls, entered=entered, release=release
        ),
        backend=FakeBackend(calls=calls),
        monotonic=SequenceClock(10.0, 10.0),
    )
    result_holder: list[object] = []
    owner = threading.Thread(target=lambda: result_holder.append(service.play(request())))
    owner.start()
    assert entered.wait(2.0)
    calls_before = list(calls)

    busy = service.play(request())

    assert_code(busy, PlaybackCode.BUSY)
    assert busy.status is PlaybackStatus.UNAVAILABLE
    assert calls == calls_before
    assert loaded.close_calls == 0
    release.set()
    owner.join(2.0)
    assert not owner.is_alive()
    assert result_holder[0].code is PlaybackCode.STARTED
    assert loaded.close_calls == 1


def test_repository_loader_backend_order_counts_reference_token_and_full_mono():
    calls: list[tuple[str, object]] = []
    run_repository = FakeRunRepository(
        make_run(config_snapshot_json=make_config_snapshot(monitor_gain_db=35.0)),
        calls=calls,
    )
    audio_repository = FakeAudioRepository(calls=calls)
    loaded = FakeLoadedAudio()
    loader = FakeAudioLoader(loaded, calls=calls)
    backend = FakeBackend(calls=calls)
    clock = SequenceClock(100.0, 100.25)
    service = make_service(
        run_repository=run_repository,
        audio_repository=audio_repository,
        audio_loader=loader,
        backend=backend,
        monotonic=clock,
    )

    result = service.play(request())

    assert_code(result, PlaybackCode.STARTED)
    assert [name for name, _ in calls] == [
        "run.get",
        "audio.get_for_run",
        "loader.open",
        "backend.start",
    ]
    assert len(loader.references) == 1
    reference = loader.references[0]
    assert type(reference) is PersistedAudioReference
    assert reference == PersistedAudioReference(
        reference_id=AUDIO_ID,
        audio_data_id=AUDIO_ID,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=FINAL_PATH,
        sample_rate_hz=4,
        channel_count=2,
        frame_count=8,
        dtype="<i2",
        content_revision=f"{AUDIO_ID}:76",
    )
    token = loader.tokens[0]
    assert token.is_cancelled is False
    assert token.reason is None
    assert token.wait() is False
    assert token.wait(0.0) is False
    assert not hasattr(token, "cancel")
    segment, rate, device, host_api, gain, timeout = backend.start_arguments[0]
    assert type(segment) is numpy.ndarray
    assert segment.dtype == numpy.dtype("float32")
    assert segment.shape == (8,)
    assert segment.flags.c_contiguous and segment.flags.owndata
    numpy.testing.assert_array_equal(
        segment,
        loaded.samples[:, 0].astype(numpy.float32) / numpy.float32(32768.0),
    )
    assert (rate, device, host_api, gain, timeout) == (
        4,
        "Durable Speakers",
        "WASAPI",
        35.0,
        0.75,
    )
    assert loaded.close_calls == 1
    assert service._current_run_id == RUN_ID
    assert service._owned_run_id == RUN_ID
    assert service._generation_owned is True
    assert service._stop_requested is False


def test_imported_audio_maps_storage_and_interior_window_channel_position():
    run_repository = FakeRunRepository(make_run(ingress_kind="IMPORTED"))
    audio_repository = FakeAudioRepository()
    loaded = FakeLoadedAudio()
    loader = FakeAudioLoader(loaded)
    backend = FakeBackend()
    service = make_service(
        run_repository=run_repository,
        audio_repository=audio_repository,
        audio_loader=loader,
        backend=backend,
    )

    result = service.play(
        request(
            display_channel_index=3,
            window_start_seconds=0.25,
            window_end_seconds=1.75,
        )
    )

    assert result.code is PlaybackCode.STARTED
    assert loader.references[0].storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO
    segment = backend.start_arguments[0][0]
    numpy.testing.assert_array_equal(
        segment,
        loaded.samples[1:7, 1].astype(numpy.float32) / numpy.float32(32768.0),
    )


def test_multichannel_replay_mixes_selected_channels_and_overrides_gain():
    loaded = FakeLoadedAudio()
    backend = FakeBackend()
    service = make_service(
        audio_loader=FakeAudioLoader(loaded),
        backend=backend,
    )

    result = service.play(
        request(
            display_channel_index=7,
            display_channel_indices=(7, 3),
            gain_db_override=12.0,
        )
    )

    assert result.code is PlaybackCode.STARTED
    segment, _rate, _device, _api, gain, _timeout = backend.start_arguments[0]
    expected = numpy.mean(
        loaded.samples[:, (0, 1)].astype(numpy.float32),
        axis=1,
    ) / numpy.float32(32768.0)
    numpy.testing.assert_allclose(segment, expected)
    assert gain == 12.0


@pytest.mark.parametrize(
    ("run_record", "expected"),
    [
        (None, PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE),
        (make_run(state="ACTIVE"), PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE),
        (make_run(state="UNKNOWN"), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(ingress_kind="LIVE"), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(run_id=OTHER_RUN_ID), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(config_id=True), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(config_id="91"), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(accepted_at="bad"), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(terminal_at="bad"), PlaybackCode.PERSISTED_FACT_INVALID),
        (make_run(updated_at="bad"), PlaybackCode.PERSISTED_FACT_INVALID),
        (object(), PlaybackCode.PERSISTED_FACT_INVALID),
    ],
)
def test_run_fact_failure_stops_before_audio_lookup(run_record, expected):
    calls: list[tuple[str, object]] = []
    service = make_service(
        run_repository=FakeRunRepository(run_record, calls=calls),
        audio_repository=FakeAudioRepository(calls=calls),
        audio_loader=FakeAudioLoader(calls=calls),
        backend=FakeBackend(calls=calls),
    )

    result = service.play(request())

    assert_code(result, expected)
    assert [name for name, _ in calls] == ["run.get"]


@pytest.mark.parametrize(
    "terminal_state",
    ["SUCCEEDED", "NO_RESULT", "FAILED", "CANCELED", "INTERRUPTED"],
)
def test_all_and_only_closed_terminal_run_states_are_accepted(terminal_state):
    service = make_service(
        run_repository=FakeRunRepository(make_run(state=terminal_state))
    )
    assert service.play(request()).code is PlaybackCode.STARTED


def test_run_repository_exception_is_fixed_fact_failure():
    service = make_service(
        run_repository=FakeRunRepository(error=RuntimeError(FINAL_PATH))
    )
    result = service.play(request())
    assert_code(result, PlaybackCode.PERSISTED_FACT_INVALID)
    assert FINAL_PATH not in result.summary


@pytest.mark.parametrize(
    "audio_record",
    [
        make_audio(run_id=OTHER_RUN_ID),
        make_audio(audio_id="bad"),
        make_audio(final_path=r"C:\durable\..\secret.wav"),
        make_audio(final_path="relative.wav"),
        make_audio(container_format="FLAC"),
        make_audio(sample_rate_hz=True),
        make_audio(sample_rate_hz=0),
        make_audio(channel_count=0),
        make_audio(frame_count=0),
        make_audio(duration_ms=1999),
        make_audio(byte_size=0),
        make_audio(closed_verified_at="bad"),
        make_audio(created_at="bad"),
        object(),
    ],
)
def test_invalid_audio_fact_stops_before_load(audio_record):
    calls: list[tuple[str, object]] = []
    service = make_service(
        run_repository=FakeRunRepository(calls=calls),
        audio_repository=FakeAudioRepository(audio_record, calls=calls),
        audio_loader=FakeAudioLoader(calls=calls),
        backend=FakeBackend(calls=calls),
    )

    result = service.play(request())

    assert_code(result, PlaybackCode.PERSISTED_FACT_INVALID)
    assert [name for name, _ in calls] == ["run.get", "audio.get_for_run"]


def test_missing_audio_is_unavailable_and_audio_repository_exception_is_invalid():
    for repository, expected in [
        (FakeAudioRepository(None), PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE),
        (
            FakeAudioRepository(error=RuntimeError('{"path":"secret"}')),
            PlaybackCode.PERSISTED_FACT_INVALID,
        ),
    ]:
        result = make_service(audio_repository=repository).play(request())
        assert_code(result, expected)
        assert "secret" not in result.summary


@pytest.mark.parametrize(
    ("config_json", "display_channel", "expected"),
    [
        ("not json", 7, PlaybackCode.CONFIG_SNAPSHOT_INVALID),
        (
            make_config_snapshot(selected_channels=[]),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(selected_channels=[7, 7]),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(selected_channels=[7, True]),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(selected_channels=[7]),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(output_device_name=""),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(output_device_hostapi=""),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(monitor_gain_db=0),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(monitor_gain_db=35.01),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(monitor_gain_db=-60.01),
            7,
            PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        ),
        (
            make_config_snapshot(),
            99,
            PlaybackCode.CHANNEL_UNAVAILABLE,
        ),
    ],
)
def test_config_and_channel_failures_prevent_loading(
    config_json, display_channel, expected
):
    calls: list[tuple[str, object]] = []
    service = make_service(
        run_repository=FakeRunRepository(
            make_run(config_snapshot_json=config_json), calls=calls
        ),
        audio_repository=FakeAudioRepository(calls=calls),
        audio_loader=FakeAudioLoader(calls=calls),
        backend=FakeBackend(calls=calls),
    )

    result = service.play(request(display_channel_index=display_channel))

    assert_code(result, expected)
    assert [name for name, _ in calls] == ["run.get", "audio.get_for_run"]


def test_config_id_is_never_lookup_or_authority():
    run_repository = FakeRunRepository(make_run(config_id=-999))
    result = make_service(run_repository=run_repository).play(request())
    assert result.code is PlaybackCode.STARTED
    assert run_repository.calls == [("run.get", RUN_ID)]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0.0, 2.0000000001),
        (0.01, 0.24),
    ],
)
def test_window_failure_including_subframe_overrun_is_before_backend(start, end):
    loaded = FakeLoadedAudio()
    backend = FakeBackend()
    result = make_service(
        audio_loader=FakeAudioLoader(loaded), backend=backend
    ).play(request(window_start_seconds=start, window_end_seconds=end))

    assert_code(result, PlaybackCode.WINDOW_UNAVAILABLE)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


@pytest.mark.parametrize(
    "loaded",
    [
        FakeLoadedAudio(sample_rate_hz=True),
        FakeLoadedAudio(sample_rate_hz=5),
        FakeLoadedAudio(channel_count=1),
        FakeLoadedAudio(frame_count=7),
        FakeLoadedAudio(samples=[[1, 2]]),
        FakeLoadedAudio(samples=numpy.zeros((8, 2), dtype=numpy.float32)),
        FakeLoadedAudio(samples=numpy.zeros((8,), dtype="<i2")),
        FakeLoadedAudio(samples=numpy.zeros((8, 3), dtype="<i2")),
        FakeLoadedAudio(
            samples=numpy.zeros((8, 4), dtype="<i2")[:, ::2]
        ),
    ],
)
def test_invalid_loaded_metadata_or_samples_closes_once_and_never_starts(loaded):
    backend = FakeBackend()
    result = make_service(
        audio_loader=FakeAudioLoader(loaded), backend=backend
    ).play(request())

    assert_code(result, PlaybackCode.AUDIO_LOAD_FAILED)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


def test_non_owned_loaded_array_is_rejected_and_closed_once():
    base = numpy.zeros((9, 2), dtype="<i2")
    loaded = FakeLoadedAudio(samples=base[1:])
    assert not loaded.samples.flags.owndata
    backend = FakeBackend()

    result = make_service(
        audio_loader=FakeAudioLoader(loaded), backend=backend
    ).play(request())

    assert_code(result, PlaybackCode.AUDIO_LOAD_FAILED)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


def test_loader_and_close_exceptions_are_fixed_audio_load_failures():
    cases = [
        FakeAudioLoader(error=RuntimeError(FINAL_PATH)),
        FakeAudioLoader(FakeLoadedAudio(close_error=RuntimeError('{"raw":1}'))),
    ]
    for loader in cases:
        backend = FakeBackend()
        result = make_service(audio_loader=loader, backend=backend).play(request())
        assert_code(result, PlaybackCode.AUDIO_LOAD_FAILED)
        assert FINAL_PATH not in result.summary
        assert "raw" not in result.summary
        assert backend.start_arguments == []
    assert cases[1].loaded.close_calls == 1


class BaseExceptionMetadataLoadedAudio:
    def __init__(self) -> None:
        self.close_calls = 0

    @property
    def sample_rate_hz(self):
        raise KeyboardInterrupt("do not recover")

    def close(self) -> None:
        self.close_calls += 1


def test_post_open_base_exception_propagates_after_close_once():
    loaded = BaseExceptionMetadataLoadedAudio()
    backend = FakeBackend()
    service = make_service(
        audio_loader=FakeAudioLoader(loaded),
        backend=backend,
    )

    with pytest.raises(KeyboardInterrupt, match="do not recover"):
        service.play(request())

    assert loaded.close_calls == 1
    assert backend.start_arguments == []


class CopyFailArray(numpy.ndarray):
    def __getitem__(self, key):
        raise RuntimeError("copy secret")


def test_segment_copy_failure_closes_once_and_never_starts():
    samples = numpy.zeros((8, 2), dtype="<i2").view(CopyFailArray)
    loaded = FakeLoadedAudio(samples=samples)
    backend = FakeBackend()
    result = make_service(
        audio_loader=FakeAudioLoader(loaded), backend=backend
    ).play(request())

    assert_code(result, PlaybackCode.AUDIO_LOAD_FAILED)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


def test_exact_segment_conversion_failure_closes_once_and_never_starts(
    monkeypatch,
):
    loaded = FakeLoadedAudio()
    backend = FakeBackend()

    def fail_conversion(*args, **kwargs):
        raise RuntimeError("conversion detail")

    monkeypatch.setattr(numpy, "array", fail_conversion)

    result = make_service(
        audio_loader=FakeAudioLoader(loaded), backend=backend
    ).play(request())

    assert_code(result, PlaybackCode.AUDIO_LOAD_FAILED)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


@pytest.mark.parametrize(
    ("clock_values", "expected_timeout"),
    [
        ((10.0, 9.0), 1.0),
        ((10.0, 10.25), 0.75),
        ((10.0, 12.0), 0.0),
    ],
)
def test_startup_deadline_clamps_remaining_without_retry(
    clock_values, expected_timeout
):
    clock = SequenceClock(*clock_values)
    backend = FakeBackend()
    result = make_service(monotonic=clock, backend=backend).play(request())

    assert result.code is PlaybackCode.STARTED
    assert backend.start_arguments[0][-1] == expected_timeout
    assert clock.calls == 2


@pytest.mark.parametrize(
    "clock_values",
    [
        (RuntimeError("clock secret"),),
        (1,),
        (math.nan,),
        (10.0, RuntimeError("second clock secret")),
        (10.0, True),
        (10.0, math.inf),
    ],
)
def test_clock_failure_maps_exactly_after_audio_close_without_backend(clock_values):
    clock = SequenceClock(*clock_values)
    loaded = FakeLoadedAudio()
    backend = FakeBackend()
    result = make_service(
        monotonic=clock,
        audio_loader=FakeAudioLoader(loaded),
        backend=backend,
    ).play(request())

    assert_code(result, PlaybackCode.DEADLINE_CLOCK_FAILED)
    assert loaded.close_calls == 1
    assert backend.start_arguments == []


@pytest.mark.parametrize(
    ("report", "code", "current", "owned", "stop_requested"),
    [
        (
            BackendStartReport(BackendStartStatus.STARTED, True, False),
            PlaybackCode.STARTED,
            RUN_ID,
            RUN_ID,
            False,
        ),
        (
            BackendStartReport(BackendStartStatus.FAILED, False, False),
            PlaybackCode.BACKEND_START_FAILED,
            None,
            None,
            False,
        ),
        (
            BackendStartReport(BackendStartStatus.FAILED, True, True),
            PlaybackCode.BACKEND_START_FAILED,
            None,
            RUN_ID,
            True,
        ),
        (
            BackendStartReport(BackendStartStatus.TIMED_OUT, True, True),
            PlaybackCode.BACKEND_START_TIMED_OUT,
            None,
            RUN_ID,
            True,
        ),
    ],
)
def test_exact_start_reports_create_exact_first_generation_state(
    report, code, current, owned, stop_requested
):
    loaded = FakeLoadedAudio()
    backend = FakeBackend(report)
    service = make_service(audio_loader=FakeAudioLoader(loaded), backend=backend)

    result = service.play(request())

    assert_code(result, code)
    assert loaded.close_calls == 1
    assert service._current_run_id == current
    assert service._owned_run_id == owned
    assert service._generation_owned is (owned is not None)
    assert service._stop_requested is stop_requested
    assert backend.stop_calls == 0


def forged_start_report() -> BackendStartReport:
    forged = object.__new__(BackendStartReport)
    object.__setattr__(forged, "status", BackendStartStatus.STARTED)
    object.__setattr__(forged, "generation_owned", False)
    object.__setattr__(forged, "stop_requested", False)
    return forged


@pytest.mark.parametrize(
    "backend",
    [
        FakeBackend(start_error=RuntimeError("backend secret")),
        FakeBackend(report=object()),
        FakeBackend(report=forged_start_report()),
    ],
)
def test_uncertain_start_failures_assume_ownership_and_signal_once(backend):
    loaded = FakeLoadedAudio()
    service = make_service(audio_loader=FakeAudioLoader(loaded), backend=backend)

    result = service.play(request())

    assert_code(result, PlaybackCode.BACKEND_START_FAILED)
    assert loaded.close_calls == 1
    assert backend.stop_calls == 1
    assert service._current_run_id is None
    assert service._owned_run_id == RUN_ID
    assert service._generation_owned is True
    assert service._stop_requested is True
    assert "secret" not in result.summary


def test_uncertain_start_stop_failure_retains_unsignaled_ownership_without_retry():
    backend = FakeBackend(
        report=object(), stop_error=RuntimeError("stop signaling secret")
    )
    service = make_service(backend=backend)

    result = service.play(request())

    assert_code(result, PlaybackCode.BACKEND_START_FAILED)
    assert backend.stop_calls == 1
    assert service._owned_run_id == RUN_ID
    assert service._generation_owned is True
    assert service._stop_requested is False
