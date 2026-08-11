from __future__ import annotations

from dataclasses import replace
import threading

import numpy
import pytest

from app.service.run_persistence import (
    AudioRecord,
    RunRecord,
    freeze_analysis_envelope,
)
from app.service.run_scoped_playback.contracts import (
    BackendStartReport,
    BackendStartStatus,
)


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
AUDIO_ID = "123e4567-e89b-42d3-a456-426614174010"
OTHER_RUN_ID = "123e4567-e89b-42d3-a456-426614174001"
UTC = "2026-07-27T01:02:03.000000Z"
FINAL_PATH = r"C:\durable\run-audio.wav"
_DEFAULT = object()


def make_config_snapshot(**config_overrides: object) -> str:
    config = {
        "selected_channels": [7, 3],
        "output_device_name": "Durable Speakers",
        "output_device_hostapi": "WASAPI",
        "monitor_gain_db": -6.0,
    }
    config.update(config_overrides)
    return freeze_analysis_envelope(
        {
            "schema": "analysis-input/v1",
            "config": config,
            "workorder": {},
            "channel_mapping": {},
            "analysis": {
                "window_start_seconds": 0.0,
                "window_end_seconds": 1.0,
                "algorithm_revision": "test-v1",
                "execution_timeout_seconds": 1.0,
                "queue_timeout_seconds": None,
                "toolchain": None,
            },
        }
    )


def make_run(**overrides: object) -> RunRecord:
    ingress_kind = overrides.get("ingress_kind", "CAPTURED")
    values = {
        "run_id": RUN_ID,
        "ingress_kind": "CAPTURED",
        "trigger_source": "MANUAL",
        "config_id": 91,
        "hardware_id": 5,
        "imported_source_identity": (
            "sha256:run-scoped-playback-import"
            if ingress_kind == "IMPORTED"
            else None
        ),
        "imported_source_path": (
            r"C:\managed\run-scoped-playback-import.wav"
            if ingress_kind == "IMPORTED"
            else None
        ),
        "imported_expected_frame_count": (
            8 if ingress_kind == "IMPORTED" else None
        ),
        "causal_revision": 0,
        "config_snapshot_json": make_config_snapshot(),
        "hardware_snapshot_json": '{"kind":"NONE"}',
        "calibration_snapshot_json": '{"kind":"NONE"}',
        "state": "SUCCEEDED",
        "judgment": "PASS",
        "judgment_source": "AUTOMATIC",
        "judged_at": UTC,
        "terminal_code": "DONE",
        "terminal_summary": None,
        "accepted_at": UTC,
        "terminal_at": UTC,
        "updated_at": UTC,
    }
    values.update(overrides)
    return RunRecord(**values)


def make_audio(**overrides: object) -> AudioRecord:
    source_kind = overrides.get("source_kind", "CAPTURED_AUDIO")
    values = {
        "audio_id": AUDIO_ID,
        "save_job_id": "33333333-3333-4333-8333-333333333333",
        "run_id": RUN_ID,
        "source_kind": "CAPTURED_AUDIO",
        "source_identity": (
            "sha256:run-scoped-playback-import"
            if source_kind == "IMPORTED_SOURCE"
            else (
                "managed-input:run-scoped-playback"
                if source_kind == "CAPTURED_AUDIO"
                else "invalid:run-scoped-playback"
            )
        ),
        "content_revision": 1,
        "final_path": FINAL_PATH,
        "container_format": "WAV_PCM",
        "sample_rate_hz": 4,
        "channel_count": 2,
        "frame_count": 8,
        "duration_ms": 2000,
        "byte_size": 76,
        "closed_verified_at": UTC,
        "created_at": UTC,
    }
    values.update(overrides)
    return AudioRecord(**values)


class FakeRunRepository:
    def __init__(
        self,
        record: object = _DEFAULT,
        *,
        calls: list[tuple[str, object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.record = make_run() if record is _DEFAULT else record
        self.calls = [] if calls is None else calls
        self.error = error

    def get(self, run_id: str) -> object:
        self.calls.append(("run.get", run_id))
        if self.error is not None:
            raise self.error
        return self.record


class FakeAudioRepository:
    def __init__(
        self,
        record: object = _DEFAULT,
        *,
        calls: list[tuple[str, object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.record = make_audio() if record is _DEFAULT else record
        self.calls = [] if calls is None else calls
        self.error = error

    def get_for_run(self, run_id: str) -> object:
        self.calls.append(("audio.get_for_run", run_id))
        if self.error is not None:
            raise self.error
        return self.record


class FakeLoadedAudio:
    def __init__(
        self,
        samples: object = None,
        *,
        sample_rate_hz: object = 4,
        channel_count: object = 2,
        frame_count: object = 8,
        close_error: Exception | None = None,
    ) -> None:
        if samples is None:
            samples = numpy.array(
                [
                    [-32768, 32767],
                    [-16384, 16384],
                    [0, -1],
                    [16384, -16384],
                    [32767, 0],
                    [1000, -1000],
                    [2000, -2000],
                    [3000, -3000],
                ],
                dtype="<i2",
                order="C",
            )
        self.samples = samples
        self.sample_rate_hz = sample_rate_hz
        self.channel_count = channel_count
        self.frame_count = frame_count
        self.close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeAudioLoader:
    def __init__(
        self,
        loaded: object = _DEFAULT,
        *,
        calls: list[tuple[str, object]] | None = None,
        error: Exception | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.loaded = FakeLoadedAudio() if loaded is _DEFAULT else loaded
        self.calls = [] if calls is None else calls
        self.error = error
        self.entered = entered
        self.release = release
        self.tokens: list[object] = []
        self.references: list[object] = []

    def open(self, reference: object, cancel_token: object) -> object:
        self.calls.append(("loader.open", reference))
        self.references.append(reference)
        self.tokens.append(cancel_token)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(2.0)
        if self.error is not None:
            raise self.error
        return self.loaded


class FakeBackend:
    def __init__(
        self,
        report: object = None,
        *,
        calls: list[tuple[str, object]] | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.report = (
            BackendStartReport(BackendStartStatus.STARTED, True, False)
            if report is None
            else report
        )
        self.calls = [] if calls is None else calls
        self.start_error = start_error
        self.stop_error = stop_error
        self.start_arguments: list[tuple[object, ...]] = []
        self.stop_calls = 0
        self.join_calls: list[float] = []

    def start(
        self,
        segment: numpy.ndarray,
        sample_rate_hz: int,
        output_device_name: str,
        output_host_api: str,
        gain_db: float,
        startup_timeout_seconds: float,
    ) -> object:
        arguments = (
            segment,
            sample_rate_hz,
            output_device_name,
            output_host_api,
            gain_db,
            startup_timeout_seconds,
        )
        self.calls.append(("backend.start", arguments))
        self.start_arguments.append(arguments)
        if self.start_error is not None:
            raise self.start_error
        return self.report

    def request_stop(self) -> None:
        self.calls.append(("backend.request_stop", None))
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error

    def join(self, timeout_seconds: float) -> object:
        self.calls.append(("backend.join", timeout_seconds))
        self.join_calls.append(timeout_seconds)
        raise AssertionError("Task 2 first-start behavior must not join")


class SequenceClock:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> float:
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def service_parts():
    calls: list[tuple[str, object]] = []
    run_repository = FakeRunRepository(calls=calls)
    audio_repository = FakeAudioRepository(calls=calls)
    loaded = FakeLoadedAudio()
    audio_loader = FakeAudioLoader(loaded, calls=calls)
    backend = FakeBackend(calls=calls)
    return {
        "calls": calls,
        "run_repository": run_repository,
        "audio_repository": audio_repository,
        "loaded": loaded,
        "audio_loader": audio_loader,
        "backend": backend,
    }


def with_run(record: RunRecord, **changes: object) -> RunRecord:
    return replace(record, **changes)


def with_audio(record: AudioRecord, **changes: object) -> AudioRecord:
    return replace(record, **changes)
