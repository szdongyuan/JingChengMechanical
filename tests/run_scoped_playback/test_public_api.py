from __future__ import annotations

import app.service.run_scoped_playback as playback
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


EXPECTED_EXPORTS = [
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


def test_public_package_has_exact_exports_and_attributes() -> None:
    expected_attributes = {
        "RunScopedPlaybackService": RunScopedPlaybackService,
        "SoundDevicePlaybackBackend": SoundDevicePlaybackBackend,
        "RunPlaybackRequest": RunPlaybackRequest,
        "PlaybackStatus": PlaybackStatus,
        "PlaybackCode": PlaybackCode,
        "PlaybackResult": PlaybackResult,
        "PlaybackLifecycleStatus": PlaybackLifecycleStatus,
        "PlaybackLifecycleCode": PlaybackLifecycleCode,
        "PlaybackLifecycleReport": PlaybackLifecycleReport,
        "BackendStartStatus": BackendStartStatus,
        "BackendStartReport": BackendStartReport,
        "BackendJoinStatus": BackendJoinStatus,
        "BackendJoinReport": BackendJoinReport,
        "RunRepositoryReader": RunRepositoryReader,
        "AudioRepositoryReader": AudioRepositoryReader,
        "PlaybackAudioLoader": PlaybackAudioLoader,
        "PlaybackBackend": PlaybackBackend,
        "PlaybackContractError": PlaybackContractError,
        "DEFAULT_REPLACEMENT_TIMEOUT_SECONDS": (
            DEFAULT_REPLACEMENT_TIMEOUT_SECONDS
        ),
        "DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS": (
            DEFAULT_BACKEND_STARTUP_TIMEOUT_SECONDS
        ),
    }

    assert playback.__all__ == EXPECTED_EXPORTS
    assert {
        name for name in vars(playback) if not name.startswith("_")
    } == set(EXPECTED_EXPORTS)
    for name, expected in expected_attributes.items():
        assert getattr(playback, name) is expected
    for name in ("backend", "contracts", "ports", "service"):
        assert not hasattr(playback, name)
        assert getattr(playback, name, None) is None
