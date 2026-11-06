from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import wave

import numpy
import pytest

from app.service.analysis.scheduler import AudioStorageKind
from app.service.run_persistence import (
    AudioPersistenceService,
    AudioPublication,
    AudioRepository,
    CapturedAudioSaveRequest,
    ImportedAudioSaveRequest,
    IngressKind,
    Judgment,
    ProductionAudioStatus,
    ProductionRunAudioAdapter,
    RunInsert,
    RunRepository,
    RunState,
    RunTerminal,
    TransactionState,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from app.service.run_scoped_playback.contracts import (
    BackendStartReport,
    BackendStartStatus,
    PlaybackCode,
    RunPlaybackRequest,
)
from app.service.run_scoped_playback.service import RunScopedPlaybackService
from tests.run_persistence.test_audio_repository import ready_runtime


RATE = 4
NOW = "2026-07-27T12:34:56.123456Z"
CAPTURED_SOURCE_IDENTITY = "managed-input:run-scoped-playback"
IMPORTED_SOURCE_IDENTITY = "sha256:run-scoped-playback-import"
CONFIG = {
    "schema": "analysis-input/v1",
    "config": {
        "selected_channels": [7, 3],
        "output_device_name": "Durable Speakers",
        "output_device_hostapi": "WASAPI",
        "monitor_gain_db": -6.0,
    },
    "workorder": {},
    "channel_mapping": {
        "schema": "audio-channel-map/v1",
        "channel_order": [7, 3],
    },
    "analysis": {
        "window_start_seconds": 0.0,
        "window_end_seconds": 1.0,
        "algorithm_revision": "integration-v1",
        "execution_timeout_seconds": 1.0,
        "queue_timeout_seconds": None,
        "toolchain": None,
    },
}


@pytest.fixture
def prepared_database_path(tmp_path: Path) -> Path:
    return prepare_run_persistence_database(
        tmp_path / "prepared-run-persistence.sqlite3"
    )


class _CountingLoadedAudio:
    def __init__(self, loaded: object) -> None:
        self._loaded = loaded
        self.close_calls = 0

    @property
    def samples(self) -> object:
        return self._loaded.samples

    @property
    def sample_rate_hz(self) -> object:
        return self._loaded.sample_rate_hz

    @property
    def channel_count(self) -> object:
        return self._loaded.channel_count

    @property
    def frame_count(self) -> object:
        return self._loaded.frame_count

    def close(self) -> None:
        self.close_calls += 1
        self._loaded.close()


class _RecordingProductionLoader:
    def __init__(self, adapter: ProductionRunAudioAdapter) -> None:
        self._adapter = adapter
        self.references: list[object] = []
        self.loaded: list[_CountingLoadedAudio] = []

    def open(self, reference: object, cancel_token: object) -> object:
        self.references.append(reference)
        loaded = _CountingLoadedAudio(
            self._adapter.open(reference, cancel_token)
        )
        self.loaded.append(loaded)
        return loaded


class _NoHardwareBackend:
    def __init__(self) -> None:
        self.starts: list[tuple[object, ...]] = []
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
    ) -> BackendStartReport:
        self.starts.append(
            (
                segment,
                sample_rate_hz,
                output_device_name,
                output_host_api,
                gain_db,
                startup_timeout_seconds,
            )
        )
        return BackendStartReport(BackendStartStatus.STARTED, True, False)

    def request_stop(self) -> None:
        self.stop_calls += 1

    def join(self, timeout_seconds: float) -> object:
        self.join_calls.append(timeout_seconds)
        raise AssertionError("integration start must not join")


def _insert_run(
    repository: RunRepository,
    ingress: IngressKind,
    *,
    imported_source_path: Path | None = None,
) -> str:
    run_id = str(uuid4())
    trigger = (
        TriggerSource.IMPORT
        if ingress is IngressKind.IMPORTED
        else TriggerSource.MANUAL
    )
    if ingress is IngressKind.IMPORTED:
        assert imported_source_path is not None
    result = repository.insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=ingress,
            trigger_source=trigger,
            config_id=19,
            hardware_id=5 if ingress is IngressKind.CAPTURED else None,
            snapshots=freeze_run_inputs(CONFIG),
            accepted_at=NOW,
            imported_source_identity=(
                IMPORTED_SOURCE_IDENTITY
                if ingress is IngressKind.IMPORTED
                else None
            ),
            imported_source_path=(
                str(imported_source_path)
                if ingress is IngressKind.IMPORTED
                else None
            ),
            imported_expected_frame_count=(
                4 if ingress is IngressKind.IMPORTED else None
            ),
        )
    )
    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    return run_id


def _terminalize(repository: RunRepository, run_id: str) -> None:
    result = repository.finish_active(
        run_id,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="DONE",
            summary=None,
            terminal_at=NOW,
            judgment=Judgment.PASS,
        ),
    )
    assert result.state is TransactionState.COMMITTED
    assert result.value is True


def _write_imported_wav(path: Path) -> None:
    pcm = numpy.asarray(
        [
            [0, -32768],
            [8192, 16384],
            [-8192, -16384],
            [32767, 0],
        ],
        dtype="<i2",
    )
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(RATE)
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.writeframes(pcm.tobytes())


@pytest.mark.parametrize(
    ("ingress", "expected_storage_kind"),
    [
        (IngressKind.CAPTURED, AudioStorageKind.CAPTURED_FINAL_WAV),
        (IngressKind.IMPORTED, AudioStorageKind.IMPORTED_FINAL_AUDIO),
    ],
)
def test_real_persistence_and_adapter_start_exact_durable_segment(
    prepared_database_path: Path,
    tmp_path: Path,
    ingress: IngressKind,
    expected_storage_kind: AudioStorageKind,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_repository = RunRepository(runtime)
    audio_repository = AudioRepository(runtime)
    persistence = AudioPersistenceService(
        AudioPublication(), audio_repository, run_repository
    )
    adapter = ProductionRunAudioAdapter(persistence)
    final_path = tmp_path / f"{ingress.value.lower()}-final.wav"
    source_path = (
        tmp_path / "import-source.wav"
        if ingress is IngressKind.IMPORTED
        else None
    )
    if source_path is not None:
        _write_imported_wav(source_path)
    run_id = _insert_run(
        run_repository,
        ingress,
        imported_source_path=source_path,
    )

    if ingress is IngressKind.CAPTURED:
        samples = numpy.asarray(
            [
                [0.0, -1.0],
                [0.25, 0.5],
                [-0.25, -0.5],
                [1.0, 0.0],
            ],
            dtype=numpy.float32,
        )
        publication = adapter.persist_captured(
            CapturedAudioSaveRequest(
                save_job_id=str(uuid4()),
                run_id=run_id,
                source_identity=CAPTURED_SOURCE_IDENTITY,
                content_revision=1,
                samples=samples,
                sample_rate_hz=RATE,
                channel_count=2,
                frame_count=4,
                final_path=str(final_path),
                now=NOW,
            )
        )
    else:
        assert source_path is not None
        publication = adapter.persist_imported(
            ImportedAudioSaveRequest(
                save_job_id=str(uuid4()),
                run_id=run_id,
                source_identity=IMPORTED_SOURCE_IDENTITY,
                content_revision=1,
                source_path=str(source_path),
                final_path=str(final_path),
                expected_sample_rate_hz=RATE,
                expected_channel_count=2,
                expected_frame_count=4,
                now=NOW,
            )
        )
    assert publication.status is ProductionAudioStatus.PERSISTED
    _terminalize(run_repository, run_id)

    persisted_run = run_repository.get(run_id)
    persisted_audio = audio_repository.get_for_run(run_id)
    assert persisted_run is not None and persisted_run.state == "SUCCEEDED"
    assert persisted_audio is not None

    loader = _RecordingProductionLoader(adapter)
    backend = _NoHardwareBackend()
    service = RunScopedPlaybackService(
        run_repository=run_repository,
        audio_repository=audio_repository,
        audio_loader=loader,
        backend=backend,
        monotonic=lambda: 10.0,
    )
    result = service.play(
        RunPlaybackRequest(run_id, 3, 0.25, 0.75)
    )

    assert result.code is PlaybackCode.STARTED
    assert result.run_id == run_id
    assert len(loader.references) == len(loader.loaded) == 1
    reference = loader.references[0]
    assert reference.storage_kind is expected_storage_kind
    assert reference.reference_id == persisted_audio.audio_id
    assert reference.audio_data_id == persisted_audio.audio_id
    assert reference.content_revision == (
        f"{persisted_audio.audio_id}:{persisted_audio.byte_size}"
    )
    assert loader.loaded[0].close_calls == 1
    assert len(backend.starts) == 1
    segment, rate, device, host_api, gain, startup_timeout = backend.starts[0]
    numpy.testing.assert_array_equal(
        segment, numpy.asarray([0.5, -0.5], dtype=numpy.float32)
    )
    assert segment.flags.owndata and segment.flags.c_contiguous
    assert (rate, device, host_api, gain, startup_timeout) == (
        RATE,
        "Durable Speakers",
        "WASAPI",
        -6.0,
        1.0,
    )


def test_real_active_and_terminal_missing_audio_are_unavailable(
    prepared_database_path: Path,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_repository = RunRepository(runtime)
    audio_repository = AudioRepository(runtime)
    adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(), audio_repository, run_repository
        )
    )
    loader = _RecordingProductionLoader(adapter)
    backend = _NoHardwareBackend()
    service = RunScopedPlaybackService(
        run_repository=run_repository,
        audio_repository=audio_repository,
        audio_loader=loader,
        backend=backend,
        monotonic=lambda: 10.0,
    )
    active_run_id = _insert_run(run_repository, IngressKind.CAPTURED)
    missing_audio_run_id = _insert_run(
        run_repository,
        IngressKind.IMPORTED,
        imported_source_path=prepared_database_path.parent
        / "missing-imported-audio.wav",
    )
    _terminalize(run_repository, missing_audio_run_id)

    active = service.play(
        RunPlaybackRequest(active_run_id, 3, 0.0, 0.5)
    )
    missing = service.play(
        RunPlaybackRequest(missing_audio_run_id, 3, 0.0, 0.5)
    )

    assert active.code is PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE
    assert missing.code is PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE
    assert loader.references == []
    assert backend.starts == []
