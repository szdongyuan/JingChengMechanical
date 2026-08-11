from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError

import pytest

from app.service.run_scoped_playback.contracts import (
    DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_REPLACEMENT_TIMEOUT_SECONDS,
    BackendJoinReport,
    BackendJoinStatus,
    BackendStartReport,
    BackendStartStatus,
    PlaybackCode,
    PlaybackContractError,
    PlaybackLifecycleCode,
    PlaybackLifecycleReport,
    PlaybackLifecycleStatus,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
    _validate_timeout_seconds,
)
from app.service.run_scoped_playback.ports import (
    AudioRepositoryReader,
    PlaybackAudioLoader,
    PlaybackBackend,
    RunRepositoryReader,
)


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
PREVIOUS_RUN_ID = "123e4567-e89b-42d3-a456-426614174001"


PLAYBACK_MAPPING = {
    PlaybackCode.STARTED: (
        PlaybackStatus.STARTED,
        "Playback started.",
    ),
    PlaybackCode.BUSY: (
        PlaybackStatus.UNAVAILABLE,
        "Playback is busy with another operation.",
    ),
    PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE: (
        PlaybackStatus.UNAVAILABLE,
        "Persisted audio is unavailable for the selected run.",
    ),
    PlaybackCode.PERSISTED_FACT_INVALID: (
        PlaybackStatus.FAILED,
        "Persisted playback facts are invalid.",
    ),
    PlaybackCode.CONFIG_SNAPSHOT_INVALID: (
        PlaybackStatus.FAILED,
        "Frozen playback configuration is invalid.",
    ),
    PlaybackCode.CHANNEL_UNAVAILABLE: (
        PlaybackStatus.UNAVAILABLE,
        "The selected channel is unavailable for persisted playback.",
    ),
    PlaybackCode.WINDOW_UNAVAILABLE: (
        PlaybackStatus.UNAVAILABLE,
        "The selected window is unavailable for persisted playback.",
    ),
    PlaybackCode.AUDIO_LOAD_FAILED: (
        PlaybackStatus.FAILED,
        "Persisted audio could not be loaded safely.",
    ),
    PlaybackCode.DEADLINE_CLOCK_FAILED: (
        PlaybackStatus.FAILED,
        "Playback could not establish a safe deadline.",
    ),
    PlaybackCode.REPLACEMENT_TIMED_OUT: (
        PlaybackStatus.FAILED,
        "The previous playback did not stop before the replacement deadline.",
    ),
    PlaybackCode.REPLACEMENT_NON_CLEAN: (
        PlaybackStatus.FAILED,
        "The previous playback did not stop cleanly.",
    ),
    PlaybackCode.BACKEND_START_FAILED: (
        PlaybackStatus.FAILED,
        "Playback could not start.",
    ),
    PlaybackCode.BACKEND_START_TIMED_OUT: (
        PlaybackStatus.FAILED,
        "Playback did not start before the startup deadline.",
    ),
    PlaybackCode.SERVICE_CLOSED: (
        PlaybackStatus.CLOSED,
        "Playback is shut down.",
    ),
}


LIFECYCLE_MAPPING = {
    PlaybackLifecycleCode.CLEAN: (
        PlaybackLifecycleStatus.CLEAN,
        "Playback lifecycle is clean.",
    ),
    PlaybackLifecycleCode.BUSY: (
        PlaybackLifecycleStatus.NON_CLEAN,
        "Playback is busy with another operation.",
    ),
    PlaybackLifecycleCode.DEADLINE_CLOCK_FAILED: (
        PlaybackLifecycleStatus.NON_CLEAN,
        "Playback could not establish a safe deadline.",
    ),
    PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT: (
        PlaybackLifecycleStatus.TIMED_OUT,
        "Playback did not join before the deadline.",
    ),
    PlaybackLifecycleCode.BACKEND_STOP_FAILED: (
        PlaybackLifecycleStatus.NON_CLEAN,
        "Playback stop signaling failed.",
    ),
    PlaybackLifecycleCode.BACKEND_NON_CLEAN: (
        PlaybackLifecycleStatus.NON_CLEAN,
        "Playback ended non-cleanly.",
    ),
}


def make_request(**overrides: object) -> RunPlaybackRequest:
    values = {
        "run_id": RUN_ID,
        "display_channel_index": 0,
        "window_start_seconds": 0.0,
        "window_end_seconds": 1.0,
    }
    values.update(overrides)
    return RunPlaybackRequest(**values)


def test_request_is_frozen_and_accepts_only_the_exact_valid_contract():
    request = make_request()

    assert request == RunPlaybackRequest(RUN_ID, 0, 0.0, 1.0)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        request.run_id = PREVIOUS_RUN_ID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "123E4567-E89B-42D3-A456-426614174000"),
        ("run_id", "123e4567-e89b-12d3-a456-426614174000"),
        ("run_id", "not-a-uuid"),
        ("run_id", 1),
        ("display_channel_index", True),
        ("display_channel_index", 0.0),
        ("display_channel_index", -1),
        ("display_channel_indices", (1,)),
        ("display_channel_indices", (0, 0)),
        ("display_channel_indices", [0, 1]),
        ("gain_db_override", 36.0),
        ("gain_db_override", -61.0),
        ("gain_db_override", 1),
        ("window_start_seconds", 0),
        ("window_start_seconds", True),
        ("window_start_seconds", -0.1),
        ("window_start_seconds", math.inf),
        ("window_start_seconds", math.nan),
        ("window_end_seconds", 1),
        ("window_end_seconds", True),
        ("window_end_seconds", -math.inf),
        ("window_end_seconds", math.nan),
    ],
)
def test_request_rejects_invalid_exact_fields(field: str, value: object):
    with pytest.raises(PlaybackContractError):
        make_request(**{field: value})


@pytest.mark.parametrize(
    ("start", "end"),
    [(0.0, 0.0), (1.0, 1.0), (2.0, 1.0)],
)
def test_request_requires_strict_window_order(start: float, end: float):
    with pytest.raises(PlaybackContractError):
        make_request(window_start_seconds=start, window_end_seconds=end)


def test_playback_enums_are_exact_and_mapping_is_closed():
    assert [item.value for item in PlaybackStatus] == [
        "STARTED",
        "UNAVAILABLE",
        "FAILED",
        "CLOSED",
    ]
    assert [item.value for item in PlaybackCode] == [
        "STARTED",
        "BUSY",
        "PERSISTED_AUDIO_UNAVAILABLE",
        "PERSISTED_FACT_INVALID",
        "CONFIG_SNAPSHOT_INVALID",
        "CHANNEL_UNAVAILABLE",
        "WINDOW_UNAVAILABLE",
        "AUDIO_LOAD_FAILED",
        "DEADLINE_CLOCK_FAILED",
        "REPLACEMENT_TIMED_OUT",
        "REPLACEMENT_NON_CLEAN",
        "BACKEND_START_FAILED",
        "BACKEND_START_TIMED_OUT",
        "SERVICE_CLOSED",
    ]
    assert set(PLAYBACK_MAPPING) == set(PlaybackCode)

    for code, (status, summary) in PLAYBACK_MAPPING.items():
        result = PlaybackResult(status, RUN_ID, code, summary)
        assert result.status is status
        assert len(result.summary.encode("utf-8")) <= 256


@pytest.mark.parametrize("code", list(PlaybackCode))
def test_playback_result_rejects_every_wrong_status_and_summary(code: PlaybackCode):
    status, summary = PLAYBACK_MAPPING[code]
    wrong_status = next(item for item in PlaybackStatus if item is not status)

    with pytest.raises(PlaybackContractError):
        PlaybackResult(wrong_status, RUN_ID, code, summary)
    with pytest.raises(PlaybackContractError):
        PlaybackResult(status, RUN_ID, code, f"{summary} raw dependency detail")


def test_playback_result_requires_exact_enums_and_canonical_run_id():
    status, summary = PLAYBACK_MAPPING[PlaybackCode.STARTED]

    with pytest.raises(PlaybackContractError):
        PlaybackResult(status.value, RUN_ID, PlaybackCode.STARTED, summary)
    with pytest.raises(PlaybackContractError):
        PlaybackResult(status, RUN_ID, PlaybackCode.STARTED.value, summary)
    with pytest.raises(PlaybackContractError):
        PlaybackResult(status, RUN_ID.upper(), PlaybackCode.STARTED, summary)


def test_replaced_run_id_is_legal_only_for_started_results():
    status, summary = PLAYBACK_MAPPING[PlaybackCode.STARTED]
    retained_completion = PlaybackResult(
        status,
        RUN_ID,
        PlaybackCode.STARTED,
        summary,
        replaced_run_id=PREVIOUS_RUN_ID,
    )
    consumed_by_public_join = PlaybackResult(
        status,
        RUN_ID,
        PlaybackCode.STARTED,
        summary,
        replaced_run_id=None,
    )

    assert retained_completion.replaced_run_id == PREVIOUS_RUN_ID
    assert consumed_by_public_join.replaced_run_id is None
    with pytest.raises(PlaybackContractError):
        PlaybackResult(
            status,
            RUN_ID,
            PlaybackCode.STARTED,
            summary,
            replaced_run_id=PREVIOUS_RUN_ID.upper(),
        )
    for code, (other_status, other_summary) in PLAYBACK_MAPPING.items():
        if code is PlaybackCode.STARTED:
            continue
        with pytest.raises(PlaybackContractError):
            PlaybackResult(
                other_status,
                RUN_ID,
                code,
                other_summary,
                replaced_run_id=PREVIOUS_RUN_ID,
            )


def test_lifecycle_enums_are_exact_and_mapping_is_closed():
    assert [item.value for item in PlaybackLifecycleStatus] == [
        "CLEAN",
        "TIMED_OUT",
        "NON_CLEAN",
    ]
    assert [item.value for item in PlaybackLifecycleCode] == [
        "CLEAN",
        "BUSY",
        "DEADLINE_CLOCK_FAILED",
        "BACKEND_JOIN_TIMED_OUT",
        "BACKEND_STOP_FAILED",
        "BACKEND_NON_CLEAN",
    ]
    assert set(LIFECYCLE_MAPPING) == set(PlaybackLifecycleCode)

    for code, (status, summary) in LIFECYCLE_MAPPING.items():
        report = PlaybackLifecycleReport(status, code, summary)
        assert report.status is status
        assert len(report.summary.encode("utf-8")) <= 256


@pytest.mark.parametrize("code", list(PlaybackLifecycleCode))
def test_lifecycle_report_rejects_wrong_mapping_and_non_exact_enums(
    code: PlaybackLifecycleCode,
):
    status, summary = LIFECYCLE_MAPPING[code]
    wrong_status = next(
        item for item in PlaybackLifecycleStatus if item is not status
    )

    with pytest.raises(PlaybackContractError):
        PlaybackLifecycleReport(wrong_status, code, summary)
    with pytest.raises(PlaybackContractError):
        PlaybackLifecycleReport(status, code, f"{summary} backend detail")
    with pytest.raises(PlaybackContractError):
        PlaybackLifecycleReport(status.value, code, summary)
    with pytest.raises(PlaybackContractError):
        PlaybackLifecycleReport(status, code.value, summary)


@pytest.mark.parametrize("value", [0.0, 0.25, 1.0, float.fromhex("0x1.fffffep+127")])
def test_lifecycle_timeout_helper_accepts_exact_finite_nonnegative_float(
    value: float,
):
    assert _validate_timeout_seconds(value) is value


@pytest.mark.parametrize(
    "value",
    [-0.1, math.inf, -math.inf, math.nan, 0, 1, True, "1.0", None],
)
def test_lifecycle_timeout_helper_rejects_every_other_value(value: object):
    with pytest.raises(PlaybackContractError):
        _validate_timeout_seconds(value)


def test_backend_report_enums_are_exact():
    assert [item.value for item in BackendStartStatus] == [
        "STARTED",
        "FAILED",
        "TIMED_OUT",
    ]
    assert [item.value for item in BackendJoinStatus] == [
        "CLEAN",
        "TIMED_OUT",
        "NON_CLEAN",
    ]


@pytest.mark.parametrize(
    ("status", "generation_owned", "stop_requested", "allowed"),
    [
        (status, generation_owned, stop_requested, allowed)
        for status in BackendStartStatus
        for generation_owned in (False, True)
        for stop_requested in (False, True)
        for allowed in [
            (status, generation_owned, stop_requested)
            in {
                (BackendStartStatus.STARTED, True, False),
                (BackendStartStatus.FAILED, False, False),
                (BackendStartStatus.FAILED, True, True),
                (BackendStartStatus.TIMED_OUT, True, True),
            }
        ]
    ],
)
def test_every_backend_start_report_combination_is_closed(
    status: BackendStartStatus,
    generation_owned: bool,
    stop_requested: bool,
    allowed: bool,
):
    if allowed:
        assert BackendStartReport(
            status, generation_owned, stop_requested
        ).status is status
    else:
        with pytest.raises(PlaybackContractError):
            BackendStartReport(status, generation_owned, stop_requested)


@pytest.mark.parametrize(
    ("status", "generation_owned", "allowed"),
    [
        (status, generation_owned, allowed)
        for status in BackendJoinStatus
        for generation_owned in (False, True)
        for allowed in [
            (status, generation_owned)
            in {
                (BackendJoinStatus.CLEAN, False),
                (BackendJoinStatus.TIMED_OUT, True),
                (BackendJoinStatus.NON_CLEAN, False),
            }
        ]
    ],
)
def test_every_backend_join_report_combination_is_closed(
    status: BackendJoinStatus,
    generation_owned: bool,
    allowed: bool,
):
    if allowed:
        assert BackendJoinReport(status, generation_owned).status is status
    else:
        with pytest.raises(PlaybackContractError):
            BackendJoinReport(status, generation_owned)


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_backend_reports_require_exact_bool_fields(value: object):
    with pytest.raises(PlaybackContractError):
        BackendStartReport(BackendStartStatus.STARTED, value, False)
    with pytest.raises(PlaybackContractError):
        BackendStartReport(BackendStartStatus.STARTED, True, value)
    with pytest.raises(PlaybackContractError):
        BackendJoinReport(BackendJoinStatus.CLEAN, value)


def test_backend_reports_require_exact_enum_fields():
    with pytest.raises(PlaybackContractError):
        BackendStartReport(BackendStartStatus.STARTED.value, True, False)
    with pytest.raises(PlaybackContractError):
        BackendJoinReport(BackendJoinStatus.CLEAN.value, False)


def test_forged_incoherent_backend_reports_are_rejected_on_revalidation():
    forged_start = object.__new__(BackendStartReport)
    object.__setattr__(forged_start, "status", BackendStartStatus.STARTED)
    object.__setattr__(forged_start, "generation_owned", False)
    object.__setattr__(forged_start, "stop_requested", False)

    forged_join = object.__new__(BackendJoinReport)
    object.__setattr__(forged_join, "status", BackendJoinStatus.CLEAN)
    object.__setattr__(forged_join, "generation_owned", True)

    with pytest.raises(PlaybackContractError):
        forged_start.__post_init__()
    with pytest.raises(PlaybackContractError):
        forged_join.__post_init__()


def test_service_timeout_constants_are_exact():
    assert DEFAULT_REPLACEMENT_TIMEOUT_SECONDS == 1.0
    assert type(DEFAULT_REPLACEMENT_TIMEOUT_SECONDS) is float
    assert DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS == 1.0
    assert type(DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS) is float


def test_protocols_have_only_the_approved_methods_and_exact_signatures():
    assert {
        name for name in RunRepositoryReader.__dict__ if not name.startswith("_")
    } == {"get"}
    assert {
        name for name in AudioRepositoryReader.__dict__ if not name.startswith("_")
    } == {"get_for_run"}
    assert {
        name for name in PlaybackAudioLoader.__dict__ if not name.startswith("_")
    } == {"open"}
    assert {
        name for name in PlaybackBackend.__dict__ if not name.startswith("_")
    } == {"start", "request_stop", "join"}

    assert str(inspect.signature(RunRepositoryReader.get)) == (
        "(self, run_id: 'str') -> 'RunRecord | None'"
    )
    assert str(inspect.signature(AudioRepositoryReader.get_for_run)) == (
        "(self, run_id: 'str') -> 'AudioRecord | None'"
    )
    assert str(inspect.signature(PlaybackAudioLoader.open)) == (
        "(self, reference: 'PersistedAudioReference', "
        "cancel_token: 'CancellationToken') -> 'LoadedAudio'"
    )
    assert str(inspect.signature(PlaybackBackend.start)) == (
        "(self, segment: 'numpy.ndarray', sample_rate_hz: 'int', "
        "output_device_name: 'str', output_host_api: 'str', gain_db: 'float', "
        "startup_timeout_seconds: 'float') -> 'BackendStartReport'"
    )
    assert str(inspect.signature(PlaybackBackend.request_stop)) == (
        "(self) -> 'None'"
    )
    assert str(inspect.signature(PlaybackBackend.join)) == (
        "(self, timeout_seconds: 'float') -> 'BackendJoinReport'"
    )
