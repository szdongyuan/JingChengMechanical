"""Pure injected ports for the parallel analysis scheduler."""

from __future__ import annotations

from typing import Protocol

from ..staged_execution import AnalysisEnrichmentReport

from .models import (
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionResult,
    AnalysisJob,
    AnalysisOutcome,
    AnalysisStartGateFailure,
    AnalysisStage,
    PersistedAudioReference,
    SchedulerDiagnosticEvent,
)


class CancellationToken(Protocol):
    @property
    def is_cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    def wait(self, timeout_seconds: float | None = None) -> bool: ...


class LoadedAudio(Protocol):
    @property
    def samples(self) -> object: ...

    @property
    def sample_rate_hz(self) -> int: ...

    @property
    def channel_count(self) -> int: ...

    @property
    def frame_count(self) -> int: ...

    def close(self) -> None: ...


class AudioLoader(Protocol):
    def open(
        self,
        reference: PersistedAudioReference,
        cancel_token: CancellationToken,
    ) -> LoadedAudio: ...


class AnalysisExecutor(Protocol):
    def execute(
        self,
        job: AnalysisJob,
        audio: LoadedAudio,
        cancel_token: CancellationToken,
    ) -> AnalysisExecutionResult: ...


class AnalysisStartGate(Protocol):
    def before_audio_load(self, job: AnalysisJob) -> None: ...


class OutcomeSink(Protocol):
    def __call__(self, outcome: AnalysisOutcome) -> None: ...


class AnalysisEnrichmentSink(Protocol):
    def __call__(self, report: AnalysisEnrichmentReport) -> None: ...


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


class DiagnosticSink(Protocol):
    def __call__(self, event: SchedulerDiagnosticEvent) -> None: ...


class AudioLoadError(Exception):
    __slots__ = ("error",)

    error: AnalysisError

    def __init__(self, error: AnalysisError, /) -> None:
        if type(error) is not AnalysisError:
            raise TypeError("error must be an exact AnalysisError")
        allowed = (
            error.stage is AnalysisStage.AUDIO_LOAD
            and error.category
            in (
                AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
            )
        ) or (
            error.stage is AnalysisStage.CANCEL
            and error.category is AnalysisErrorCategory.ANALYSIS_CANCELED
        )
        if not allowed:
            raise ValueError("AudioLoadError has an illegal category/stage")
        super().__init__(error.summary)
        self.error = error


class AnalysisExecutionError(Exception):
    __slots__ = ("error",)

    error: AnalysisError

    def __init__(self, error: AnalysisError, /) -> None:
        if type(error) is not AnalysisError:
            raise TypeError("error must be an exact AnalysisError")
        allowed = (
            error.stage is AnalysisStage.EXECUTE
            and error.category
            in (
                AnalysisErrorCategory.ANALYSIS_TIMEOUT,
                AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            )
        ) or (
            error.stage is AnalysisStage.CANCEL
            and error.category is AnalysisErrorCategory.ANALYSIS_CANCELED
        )
        if not allowed:
            raise ValueError("AnalysisExecutionError has an illegal category/stage")
        super().__init__(error.summary)
        self.error = error


class AnalysisStartGateError(Exception):
    __slots__ = ("failure",)

    failure: AnalysisStartGateFailure

    def __init__(self, failure: AnalysisStartGateFailure, /) -> None:
        if type(failure) is not AnalysisStartGateFailure:
            raise TypeError("failure must be an exact AnalysisStartGateFailure")
        super().__init__(failure.summary)
        self.failure = failure
