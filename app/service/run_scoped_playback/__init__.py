"""Independent run-scoped playback service."""

from app.service.run_scoped_playback.backend import SoundDevicePlaybackBackend
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
)
from app.service.run_scoped_playback.ports import (
    AudioRepositoryReader,
    PlaybackAudioLoader,
    PlaybackBackend,
    RunRepositoryReader,
)
from app.service.run_scoped_playback.service import RunScopedPlaybackService


__all__ = [
    "RunScopedPlaybackService",
    "SoundDevicePlaybackBackend",
    "RunPlaybackRequest",
    "PlaybackStatus",
    "PlaybackCode",
    "PlaybackResult",
    "PlaybackLifecycleStatus",
    "PlaybackLifecycleCode",
    "PlaybackLifecycleReport",
    "BackendStartStatus",
    "BackendStartReport",
    "BackendJoinStatus",
    "BackendJoinReport",
    "RunRepositoryReader",
    "AudioRepositoryReader",
    "PlaybackAudioLoader",
    "PlaybackBackend",
    "PlaybackContractError",
    "DEFAULT_REPLACEMENT_TIMEOUT_SECONDS",
    "DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS",
]


_SUBMODULE_NAMES = ("backend", "contracts", "ports", "service")


for _submodule_name in _SUBMODULE_NAMES:
    globals().pop(_submodule_name, None)
del _submodule_name
