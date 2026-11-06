"""Injected ports for run-scoped playback."""

from __future__ import annotations

from typing import Protocol

import numpy

from app.service.analysis.scheduler import (
    CancellationToken,
    LoadedAudio,
    PersistedAudioReference,
)
from app.service.run_persistence import AudioRecord, RunRecord

from .contracts import BackendJoinReport, BackendStartReport


class RunRepositoryReader(Protocol):
    def get(self, run_id: str) -> RunRecord | None: ...


class AudioRepositoryReader(Protocol):
    def get_for_run(self, run_id: str) -> AudioRecord | None: ...


class PlaybackAudioLoader(Protocol):
    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: CancellationToken,
    ) -> LoadedAudio: ...


class PlaybackBackend(Protocol):
    def start(
        self,
        segment: numpy.ndarray,
        sample_rate_hz: int,
        output_device_name: str,
        output_host_api: str,
        gain_db: float,
        startup_timeout_seconds: float,
    ) -> BackendStartReport: ...

    def request_stop(self) -> None: ...

    def join(self, timeout_seconds: float) -> BackendJoinReport: ...
