"""Immutable contracts for run-scoped playback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from app.service.run_persistence import validate_uuid4


DEFAULT_REPLACEMENT_TIMEOUT_SECONDS = 1.0
DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS = 1.0


class PlaybackContractError(ValueError):
    """A run-scoped playback value violates its closed public contract."""


def _require_exact_finite_float(value: object, field_name: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise PlaybackContractError(f"{field_name} must be an exact finite float")
    return value


def _validate_timeout_seconds(timeout_seconds: object) -> float:
    value = _require_exact_finite_float(timeout_seconds, "timeout_seconds")
    if value < 0.0:
        raise PlaybackContractError(
            "timeout_seconds must be greater than or equal to zero"
        )
    return value


def _validate_run_id(run_id: object, field_name: str) -> str:
    try:
        return validate_uuid4(run_id)
    except (TypeError, ValueError):
        raise PlaybackContractError(
            f"{field_name} must be lowercase canonical UUIDv4 text"
        ) from None


@dataclass(frozen=True, slots=True)
class RunPlaybackRequest:
    run_id: str
    display_channel_index: int
    window_start_seconds: float
    window_end_seconds: float
    display_channel_indices: tuple[int, ...] | None = None
    gain_db_override: float | None = None

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id, "run_id")
        if (
            type(self.display_channel_index) is not int
            or self.display_channel_index < 0
        ):
            raise PlaybackContractError(
                "display_channel_index must be an exact nonnegative integer"
            )
        if self.display_channel_indices is not None:
            channels = self.display_channel_indices
            if (
                type(channels) is not tuple
                or not channels
                or any(
                    type(channel) is not int or channel < 0
                    for channel in channels
                )
                or len(set(channels)) != len(channels)
                or channels[0] != self.display_channel_index
            ):
                raise PlaybackContractError(
                    "display_channel_indices must be unique nonnegative "
                    "channels beginning with display_channel_index"
                )
        if self.gain_db_override is not None:
            gain_db = _require_exact_finite_float(
                self.gain_db_override,
                "gain_db_override",
            )
            if not -60.0 <= gain_db <= 35.0:
                raise PlaybackContractError(
                    "gain_db_override must be between -60 and 35 dB"
                )
        window_start = _require_exact_finite_float(
            self.window_start_seconds, "window_start_seconds"
        )
        window_end = _require_exact_finite_float(
            self.window_end_seconds, "window_end_seconds"
        )
        if window_start < 0.0:
            raise PlaybackContractError(
                "window_start_seconds must be greater than or equal to zero"
            )
        if window_end <= window_start:
            raise PlaybackContractError(
                "window_end_seconds must be greater than window_start_seconds"
            )


class PlaybackStatus(str, Enum):
    STARTED = "STARTED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class PlaybackCode(str, Enum):
    STARTED = "STARTED"
    BUSY = "BUSY"
    PERSISTED_AUDIO_UNAVAILABLE = "PERSISTED_AUDIO_UNAVAILABLE"
    PERSISTED_FACT_INVALID = "PERSISTED_FACT_INVALID"
    CONFIG_SNAPSHOT_INVALID = "CONFIG_SNAPSHOT_INVALID"
    CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
    WINDOW_UNAVAILABLE = "WINDOW_UNAVAILABLE"
    AUDIO_LOAD_FAILED = "AUDIO_LOAD_FAILED"
    DEADLINE_CLOCK_FAILED = "DEADLINE_CLOCK_FAILED"
    REPLACEMENT_TIMED_OUT = "REPLACEMENT_TIMED_OUT"
    REPLACEMENT_NON_CLEAN = "REPLACEMENT_NON_CLEAN"
    BACKEND_START_FAILED = "BACKEND_START_FAILED"
    BACKEND_START_TIMED_OUT = "BACKEND_START_TIMED_OUT"
    SERVICE_CLOSED = "SERVICE_CLOSED"


_PLAYBACK_RESULT_MAPPING = MappingProxyType(
    {
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
)


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    status: PlaybackStatus
    run_id: str
    code: PlaybackCode
    summary: str
    replaced_run_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not PlaybackStatus:
            raise PlaybackContractError("status must be an exact PlaybackStatus")
        if type(self.code) is not PlaybackCode:
            raise PlaybackContractError("code must be an exact PlaybackCode")
        _validate_run_id(self.run_id, "run_id")
        expected_status, expected_summary = _PLAYBACK_RESULT_MAPPING[self.code]
        if (
            self.status is not expected_status
            or type(self.summary) is not str
            or self.summary != expected_summary
        ):
            raise PlaybackContractError(
                "playback result must use the closed code/status/summary mapping"
            )
        if len(self.summary.encode("utf-8")) > 256:
            raise PlaybackContractError("summary exceeds 256 UTF-8 bytes")
        if self.replaced_run_id is not None:
            if self.code is not PlaybackCode.STARTED:
                raise PlaybackContractError(
                    "replaced_run_id is legal only for a started result"
                )
            _validate_run_id(self.replaced_run_id, "replaced_run_id")


class PlaybackLifecycleStatus(str, Enum):
    CLEAN = "CLEAN"
    TIMED_OUT = "TIMED_OUT"
    NON_CLEAN = "NON_CLEAN"


class PlaybackLifecycleCode(str, Enum):
    CLEAN = "CLEAN"
    BUSY = "BUSY"
    DEADLINE_CLOCK_FAILED = "DEADLINE_CLOCK_FAILED"
    BACKEND_JOIN_TIMED_OUT = "BACKEND_JOIN_TIMED_OUT"
    BACKEND_STOP_FAILED = "BACKEND_STOP_FAILED"
    BACKEND_NON_CLEAN = "BACKEND_NON_CLEAN"


_PLAYBACK_LIFECYCLE_MAPPING = MappingProxyType(
    {
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
)


@dataclass(frozen=True, slots=True)
class PlaybackLifecycleReport:
    status: PlaybackLifecycleStatus
    code: PlaybackLifecycleCode
    summary: str

    def __post_init__(self) -> None:
        if type(self.status) is not PlaybackLifecycleStatus:
            raise PlaybackContractError(
                "status must be an exact PlaybackLifecycleStatus"
            )
        if type(self.code) is not PlaybackLifecycleCode:
            raise PlaybackContractError(
                "code must be an exact PlaybackLifecycleCode"
            )
        expected_status, expected_summary = _PLAYBACK_LIFECYCLE_MAPPING[self.code]
        if (
            self.status is not expected_status
            or type(self.summary) is not str
            or self.summary != expected_summary
        ):
            raise PlaybackContractError(
                "lifecycle report must use the closed code/status/summary mapping"
            )
        if len(self.summary.encode("utf-8")) > 256:
            raise PlaybackContractError("summary exceeds 256 UTF-8 bytes")


class BackendStartStatus(str, Enum):
    STARTED = "STARTED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


@dataclass(frozen=True, slots=True)
class BackendStartReport:
    status: BackendStartStatus
    generation_owned: bool
    stop_requested: bool

    def __post_init__(self) -> None:
        if type(self.status) is not BackendStartStatus:
            raise PlaybackContractError(
                "status must be an exact BackendStartStatus"
            )
        if (
            type(self.generation_owned) is not bool
            or type(self.stop_requested) is not bool
        ):
            raise PlaybackContractError(
                "backend start ownership fields must be exact bool values"
            )
        if (
            self.status,
            self.generation_owned,
            self.stop_requested,
        ) not in {
            (BackendStartStatus.STARTED, True, False),
            (BackendStartStatus.FAILED, False, False),
            (BackendStartStatus.FAILED, True, True),
            (BackendStartStatus.TIMED_OUT, True, True),
        }:
            raise PlaybackContractError("backend start report is incoherent")


class BackendJoinStatus(str, Enum):
    CLEAN = "CLEAN"
    TIMED_OUT = "TIMED_OUT"
    NON_CLEAN = "NON_CLEAN"


@dataclass(frozen=True, slots=True)
class BackendJoinReport:
    status: BackendJoinStatus
    generation_owned: bool

    def __post_init__(self) -> None:
        if type(self.status) is not BackendJoinStatus:
            raise PlaybackContractError(
                "status must be an exact BackendJoinStatus"
            )
        if type(self.generation_owned) is not bool:
            raise PlaybackContractError(
                "backend join ownership field must be an exact bool value"
            )
        if (self.status, self.generation_owned) not in {
            (BackendJoinStatus.CLEAN, False),
            (BackendJoinStatus.TIMED_OUT, True),
            (BackendJoinStatus.NON_CLEAN, False),
        }:
            raise PlaybackContractError("backend join report is incoherent")
