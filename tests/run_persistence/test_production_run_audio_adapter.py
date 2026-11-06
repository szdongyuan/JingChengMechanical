from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import inspect
import ntpath
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import wave

import numpy
import pytest

from app.service.analysis.scheduler import (
    AnalysisErrorCategory,
    AnalysisStage,
    AudioLoadError,
    AudioStorageKind,
    PersistedAudioReference,
)
from app.service.run_persistence import (
    AudioPersistenceResult,
    AudioPersistenceService,
    AudioPublication,
    AudioRepository,
    CapturedAudioSaveRequest,
    ImportedAudioSaveRequest,
    ProductionAudioInputError,
    ProductionAudioResult,
    ProductionAudioStatus,
    ProductionRunAudioAdapter,
    RunRepository,
    normalize_absolute_path,
)
from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)
from tests.run_persistence.test_audio_repository import (
    insert_run,
    ready_runtime,
)
from tests.run_persistence.test_audio_service import insert_imported_run


RUN_ID = "12345678-1234-4234-9234-1234567890ab"
NOW = "2026-07-27T12:34:56.123456Z"


class _NeverPersist:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def persist(self, *args: object, **kwargs: object) -> AudioPersistenceResult:
        self.calls.append((args, kwargs))
        raise AssertionError("persistence must not be called")


class _InspectingPersistence:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.frames: bytes | None = None
        self.metadata: tuple[int, int, int, int] | None = None

    def persist(self, *args: object, **kwargs: object) -> AudioPersistenceResult:
        assert not args
        source = Path(str(kwargs["source_path"]))
        with source.open("rb"):
            pass
        with wave.open(str(source), "rb") as reader:
            self.metadata = (
                reader.getframerate(),
                reader.getnchannels(),
                reader.getnframes(),
                reader.getsampwidth(),
            )
            self.frames = reader.readframes(reader.getnframes())
        self.calls.append(kwargs)
        return AudioPersistenceResult(
            initial_attempt_permitted=False,
            code="EXPECTED_TEST_FAILURE",
            reason="stop after inspecting staging",
        )


class _RaisingPersistence:
    def persist(self, *args: object, **kwargs: object) -> AudioPersistenceResult:
        raise RuntimeError("database boundary failed")


class _Token:
    def __init__(self, states: list[bool] | None = None, reason: str | None = None):
        self._states = list(states or [False])
        self._last = self._states[-1]
        self.reason = reason
        self.checks = 0

    @property
    def is_cancelled(self) -> bool:
        self.checks += 1
        if self._states:
            self._last = self._states.pop(0)
        return self._last

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._last


class _ControlledWaveReader:
    def __init__(self, payload: bytes | None) -> None:
        self._payload = payload
        self.close_calls = 0

    def __enter__(self) -> _ControlledWaveReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close_calls += 1

    def getcomptype(self) -> str:
        return "NONE"

    def getsampwidth(self) -> int:
        return 2

    def getframerate(self) -> int:
        return 8_000

    def getnchannels(self) -> int:
        return 1

    def getnframes(self) -> int:
        return 2

    def readframes(self, frame_count: int) -> bytes:
        assert frame_count == 2
        if self._payload is None:
            raise OSError("private frame-read failure detail")
        return self._payload


def _samples(values: object, shape: tuple[int, int] | None = None) -> numpy.ndarray:
    result = numpy.asarray(values, dtype=numpy.float32)
    return result.reshape(shape or (result.size, 1))


def _captured_request(tmp_path: Path, **changes: object) -> CapturedAudioSaveRequest:
    values: dict[str, object] = {
        "save_job_id": str(uuid4()),
        "run_id": RUN_ID,
        "source_identity": "capture:source-1",
        "content_revision": 1,
        "samples": _samples([0.0, 0.25]),
        "sample_rate_hz": 8_000,
        "channel_count": 1,
        "frame_count": 2,
        "final_path": str(tmp_path / "final.wav"),
        "now": NOW,
    }
    values.update(changes)
    return CapturedAudioSaveRequest(**values)


def _write_wav(
    path: Path,
    frames: bytes = b"\x00\x00\x01\x00",
    *,
    rate: int = 8_000,
    channels: int = 1,
    width: int = 2,
) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(rate)
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.writeframes(frames)


def test_imported_request_requires_expected_frame_count_before_io(
    tmp_path: Path,
) -> None:
    persistence = _NeverPersist()
    parameters = inspect.signature(ImportedAudioSaveRequest).parameters

    assert parameters["expected_frame_count"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        ImportedAudioSaveRequest(
            run_id=RUN_ID,
            source_path=str(tmp_path / "source.wav"),
            final_path=str(tmp_path / "final.wav"),
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            now=NOW,
        )
    assert persistence.calls == []


def test_coordinator_captured_save_contract_round_trips_exact_identity(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    from app.service.multi_run_coordinator_mvp.contracts import (
        AudioMetadataConstraints,
        AudioSaveSourceKind,
        AudioSaveStatus,
        CapturedAudioSaveRequest as CoordinatorCapturedAudioSaveRequest,
        CapturedAudioValue,
        CapturedSourceFormat,
        FinalPathRequest,
        PersistedAudioFormat,
        SaveCancelToken,
    )

    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    save_job_id = str(uuid4())
    source_identity = "capture:coordinator-source"
    request = CoordinatorCapturedAudioSaveRequest(
        save_job_id=save_job_id,
        run_id=run_id,
        source_kind=AudioSaveSourceKind.CAPTURED_AUDIO,
        source_identity=source_identity,
        audio=CapturedAudioValue(
            source_identity=source_identity,
            sample_rate_hz=8_000,
            channel_count=1,
            frame_count=2,
            format=CapturedSourceFormat.INT16_INTERLEAVED,
            interleaved_bytes=b"\x00\x80\xff\x7f",
            channel_indices=(0,),
        ),
        final_path_request=FinalPathRequest(
            final_path=str((tmp_path / "coordinator.wav").resolve())
        ),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )
    adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            RunRepository(runtime),
        ),
        utc_now=lambda: now,
    )

    outcome = adapter.save_captured(request, SaveCancelToken())

    assert outcome.status is AudioSaveStatus.SAVED
    assert outcome.save_job_id == save_job_id
    assert outcome.source_identity == source_identity
    assert outcome.persisted_audio is not None
    assert outcome.persisted_audio.save_job_id == save_job_id
    assert outcome.persisted_audio.source_identity == source_identity
    assert outcome.persisted_audio.content_revision == 1
    assert AudioRepository(runtime).get_for_run(run_id).save_job_id == save_job_id


def test_database_unknown_token_survives_coordinator_terminal_path_without_retry(
    prepared_database_path,
    tmp_path,
    now,
    monkeypatch,
) -> None:
    from app.service.multi_run_coordinator_mvp.contracts import (
        AudioSaveStatus,
        FinalPathRequest,
        SaveCancelToken,
    )
    from tests.multi_run_coordinator_mvp._support import (
        Harness,
        captured_request,
        completed_capture,
        frozen_captured,
    )

    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    harness = Harness()
    final_path = (tmp_path / "unknown-final.wav").resolve()
    harness.captured_freezer.result_factory = lambda request: replace(
        frozen_captured(request),
        final_path_request=FinalPathRequest(str(final_path)),
    )
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request(run_id))
    request, sink = harness.save_dispatcher.calls[0]

    repository = AudioRepository(runtime)
    insert_calls = 0

    def unknown_insert(fact):
        nonlocal insert_calls
        insert_calls += 1
        runtime.quarantine_unknown()
        return TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="ambiguous commit detail " + ("x" * 1_024),
            restart_required=True,
        )

    monkeypatch.setattr(repository, "insert_verified", unknown_insert)
    persistence_results = []
    service = AudioPersistenceService(
        AudioPublication(),
        repository,
        RunRepository(runtime),
    )

    class RecordingPersistence:
        def persist(self, *args, **kwargs):
            result = service.persist(*args, **kwargs)
            persistence_results.append(result)
            return result

    adapter = ProductionRunAudioAdapter(
        RecordingPersistence(),
        utc_now=lambda: now,
    )

    outcome = adapter.save_captured(request, SaveCancelToken())
    sink(outcome)

    assert outcome.status is AudioSaveStatus.FAILED
    assert "AUDIO_DB_STATE_UNKNOWN" in outcome.reason
    assert len(outcome.reason.encode("utf-8")) <= 512
    assert len(persistence_results) == 1
    persistence_result = persistence_results[0]
    assert persistence_result.code == "AUDIO_DB_STATE_UNKNOWN"
    assert persistence_result.restart_required is True
    assert persistence_result.database_result.restart_required is True
    assert runtime.closed is True
    assert insert_calls == 1
    assert final_path.exists()
    with sqlite3.connect(prepared_database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM audio").fetchone()[0] == 0
    assert len(harness.persistence.terminal_calls) == 1
    assert (
        "AUDIO_DB_STATE_UNKNOWN"
        in harness.persistence.terminal_calls[0].reason
    )


def _reference(
    path: Path,
    *,
    audio_id: str | None = None,
    rate: int = 8_000,
    channels: int = 1,
    frames: int = 2,
    dtype: str = "<i2",
    revision_size: int | None = None,
) -> PersistedAudioReference:
    identity = audio_id or str(uuid4())
    size = path.stat().st_size if revision_size is None else revision_size
    return PersistedAudioReference(
        reference_id=identity,
        audio_data_id=identity,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=normalize_absolute_path(str(path)),
        sample_rate_hz=rate,
        channel_count=channels,
        frame_count=frames,
        dtype=dtype,
        content_revision=f"{identity}:{size}",
    )


def _real_adapter(
    prepared_database_path: Path,
    now: str,
    *,
    imported_source_path: Path | None = None,
):
    runtime = ready_runtime(prepared_database_path)
    run_id = (
        insert_run(runtime, now)
        if imported_source_path is None
        else insert_imported_run(
            runtime,
            now,
            imported_source_path,
            source_identity="imported:source-1",
            expected_frame_count=2,
        )
    )
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )
    return runtime, run_id, ProductionRunAudioAdapter(service)


def _counts(runtime) -> tuple[int, int]:
    with runtime.operation() as database:
        return (
            database.execute_sql("SELECT COUNT(*) FROM audio").fetchone()[0],
            database.execute_sql(
                "SELECT COUNT(*) FROM analysis_attempt"
            ).fetchone()[0],
        )


def test_requests_are_frozen_slotted_and_normalize_relative_paths_once(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    captured = CapturedAudioSaveRequest(
        save_job_id=str(uuid4()),
        run_id=RUN_ID,
        source_identity="capture:source-1",
        content_revision=1,
        samples=_samples([0.0]),
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=1,
        final_path="managed/../final.wav",
        now=NOW,
    )
    imported = ImportedAudioSaveRequest(
        save_job_id=str(uuid4()),
        run_id=RUN_ID,
        source_identity="imported:source-1",
        content_revision=1,
        source_path="source.wav",
        final_path="managed/final.wav",
        expected_sample_rate_hz=8_000,
        expected_channel_count=1,
        expected_frame_count=2,
        now=NOW,
    )

    assert captured.final_path == ntpath.normpath(str((tmp_path / "final.wav").resolve()))
    assert imported.source_path == ntpath.normpath(
        str((tmp_path / "source.wav").resolve())
    )
    assert imported.final_path == ntpath.normpath(
        str((tmp_path / "managed/final.wav").resolve())
    )
    assert "$AUDIO_ROOT" in normalize_absolute_path("$AUDIO_ROOT/file.wav")
    with pytest.raises(FrozenInstanceError):
        captured.frame_count = 2
    assert not hasattr(captured, "__dict__")


@pytest.mark.parametrize("value", [None, "", 1, b"path"])
def test_normalize_absolute_path_rejects_non_exact_nonempty_strings(value):
    with pytest.raises(ProductionAudioInputError):
        normalize_absolute_path(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "not-a-uuid"},
        {"sample_rate_hz": True},
        {"sample_rate_hz": 0},
        {"channel_count": 0},
        {"frame_count": 0},
        {"now": "not-utc"},
        {"samples": [[0.0]]},
        {"samples": numpy.asarray([[0]], dtype=numpy.int16)},
        {"samples": numpy.asarray([0.0], dtype=numpy.float32)},
        {"samples": _samples([0.0, 1.0]), "frame_count": 1},
        {"samples": numpy.zeros((2, 2), dtype=numpy.float32)[:, ::2], "frame_count": 2},
        {"samples": _samples([numpy.nan])},
        {"samples": _samples([numpy.inf])},
    ],
)
def test_invalid_captured_input_has_no_filesystem_or_persistence_side_effect(
    tmp_path, monkeypatch, changes
):
    create_calls = 0
    real_open = os.open

    def counting_open(*args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)
    persistence = _NeverPersist()
    adapter = ProductionRunAudioAdapter(persistence)

    with pytest.raises(ProductionAudioInputError):
        request = _captured_request(tmp_path, **changes)
        adapter.persist_captured(request)

    assert create_calls == 0
    assert persistence.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "not-a-uuid"},
        {"source_path": ""},
        {"final_path": ""},
        {"expected_sample_rate_hz": True},
        {"expected_sample_rate_hz": 0},
        {"expected_channel_count": 0},
        {"now": "not-utc"},
    ],
)
def test_invalid_imported_input_has_no_filesystem_or_persistence_side_effect(
    tmp_path, changes
):
    persistence = _NeverPersist()
    adapter = ProductionRunAudioAdapter(persistence)
    values = {
        "save_job_id": str(uuid4()),
        "run_id": RUN_ID,
        "source_identity": "imported:source-1",
        "content_revision": 1,
        "source_path": str(tmp_path / "source.wav"),
        "final_path": str(tmp_path / "final.wav"),
        "expected_sample_rate_hz": 8_000,
        "expected_channel_count": 1,
        "expected_frame_count": 2,
        "now": NOW,
    }
    values.update(changes)

    with pytest.raises(ProductionAudioInputError):
        request = ImportedAudioSaveRequest(**values)
        adapter.persist_imported(request)

    assert persistence.calls == []


def test_captured_quantization_is_exact_little_endian_and_interleaved(tmp_path):
    half_positive = numpy.nextafter(
        numpy.float32(0.5 / 32767.0), numpy.float32(numpy.inf)
    )
    half_negative = numpy.nextafter(
        numpy.float32(-0.5 / 32768.0), numpy.float32(-numpy.inf)
    )
    samples = _samples(
        [
            -2.0,
            2.0,
            -1.0,
            1.0,
            -0.5,
            0.5,
            half_negative,
            half_positive,
            0.0,
            -0.0,
        ],
        (5, 2),
    )
    persistence = _InspectingPersistence()
    result = ProductionRunAudioAdapter(persistence).persist_captured(
        _captured_request(
            tmp_path,
            samples=samples,
            channel_count=2,
            frame_count=5,
        )
    )

    expected = numpy.asarray(
        [-32768, 32767, -32768, 32767, -16384, 16384, -1, 1, 0, 0],
        dtype=numpy.dtype("<i2"),
    )
    assert persistence.frames == expected.tobytes(order="C")
    assert persistence.metadata == (8_000, 2, 5, 2)
    assert persistence.calls[0]["sample_width_bytes"] == 2
    assert result.status is ProductionAudioStatus.FAILED
    assert result.code == "EXPECTED_TEST_FAILURE"
    assert result.persistence_result is not None
    assert not any(".captured." in path.name for path in tmp_path.iterdir())


def test_captured_staging_missing_directory_is_typed_failure_without_publication(
    tmp_path,
):
    persistence = _NeverPersist()
    request = _captured_request(tmp_path, final_path=str(tmp_path / "missing/final.wav"))

    result = ProductionRunAudioAdapter(persistence).persist_captured(request)

    assert result.status is ProductionAudioStatus.FAILED
    assert result.code == "AUDIO_CAPTURE_STAGING_WRITE_FAILED"
    assert result.persistence_result is None
    assert persistence.calls == []
    assert not (tmp_path / "missing").exists()


def test_captured_staging_is_cleaned_once_when_persistence_raises(tmp_path):
    adapter = ProductionRunAudioAdapter(_RaisingPersistence())

    with pytest.raises(RuntimeError, match="database boundary failed"):
        adapter.persist_captured(_captured_request(tmp_path))

    assert not any(".captured." in path.name for path in tmp_path.iterdir())


def test_captured_publication_is_pcm16_no_replace_and_commits_one_audio_row(
    prepared_database_path, tmp_path, now
):
    runtime, run_id, adapter = _real_adapter(prepared_database_path, now)
    final = tmp_path / "captured.wav"

    result = adapter.persist_captured(
        CapturedAudioSaveRequest(
            save_job_id=str(uuid4()),
            run_id=run_id,
            source_identity="capture:source-1",
            content_revision=1,
            samples=_samples([-1.0, -0.5, 0.0, 0.5, 1.0]),
            sample_rate_hz=8_000,
            channel_count=1,
            frame_count=5,
            final_path=str(final),
            now=now,
        )
    )

    assert result.status is ProductionAudioStatus.PERSISTED
    assert result.code is result.reason is None
    assert result.persistence_result is not None
    assert result.persistence_result.initial_attempt_permitted is True
    assert result.reference is not None
    fact = result.persistence_result.fact
    assert fact is not None
    assert result.reference == PersistedAudioReference(
        reference_id=fact.audio_id,
        audio_data_id=fact.audio_id,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=normalize_absolute_path(str(final)),
        sample_rate_hz=fact.sample_rate_hz,
        channel_count=fact.channel_count,
        frame_count=fact.frame_count,
        dtype="<i2",
        content_revision=f"{fact.audio_id}:{fact.byte_size}",
    )
    assert _counts(runtime) == (1, 0)
    assert not any(".captured." in path.name for path in tmp_path.iterdir())


def test_imported_publication_uses_width_two_and_builds_imported_reference(
    prepared_database_path, tmp_path, now
):
    source = tmp_path / "source.wav"
    final = tmp_path / "imported.wav"
    _write_wav(source)
    runtime, run_id, adapter = _real_adapter(
        prepared_database_path,
        now,
        imported_source_path=source,
    )

    result = adapter.persist_imported(
        ImportedAudioSaveRequest(
            save_job_id=str(uuid4()),
            run_id=run_id,
            source_identity="imported:source-1",
            content_revision=1,
            source_path=str(source),
            final_path=str(final),
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            now=now,
        )
    )

    assert result.status is ProductionAudioStatus.PERSISTED
    assert result.reference is not None
    assert result.reference.storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO
    assert result.reference.dtype == "<f8"
    assert result.reference.absolute_path == normalize_absolute_path(str(final))
    assert _counts(runtime) == (1, 0)


def test_imported_collision_preserves_service_result_and_existing_bytes(
    prepared_database_path, tmp_path, now
):
    source = tmp_path / "source.wav"
    final = tmp_path / "winner.wav"
    _write_wav(source)
    final.write_bytes(b"winner")
    runtime, run_id, adapter = _real_adapter(
        prepared_database_path,
        now,
        imported_source_path=source,
    )

    result = adapter.persist_imported(
        ImportedAudioSaveRequest(
            save_job_id=str(uuid4()),
            run_id=run_id,
            source_identity="imported:source-1",
            content_revision=1,
            source_path=str(source),
            final_path=str(final),
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            now=now,
        )
    )

    assert result.status is ProductionAudioStatus.FAILED
    assert result.code == "AUDIO_PATH_COLLISION"
    assert result.persistence_result is not None
    assert result.persistence_result.code == result.code
    assert final.read_bytes() == b"winner"
    assert _counts(runtime) == (0, 0)


def test_production_result_rejects_illegal_coherence(
    prepared_database_path, tmp_path, now
):
    _, run_id, adapter = _real_adapter(prepared_database_path, now)
    persisted = adapter.persist_captured(
        CapturedAudioSaveRequest(
            save_job_id=str(uuid4()),
            run_id=run_id,
            source_identity="capture:source-1",
            content_revision=1,
            samples=_samples([0.0]),
            sample_rate_hz=8_000,
            channel_count=1,
            frame_count=1,
            final_path=str(tmp_path / "final.wav"),
            now=now,
        )
    )
    assert persisted.reference is not None
    assert persisted.persistence_result is not None

    illegal = [
        {"status": "PERSISTED"},
        {"status": ProductionAudioStatus.PERSISTED, "reference": None},
        {"status": ProductionAudioStatus.PERSISTED, "code": "bad"},
        {
            "status": ProductionAudioStatus.PERSISTED,
            "persistence_result": AudioPersistenceResult(
                initial_attempt_permitted=False, code="FAILED"
            ),
        },
        {"status": ProductionAudioStatus.FAILED, "reference": persisted.reference},
        {"status": ProductionAudioStatus.FAILED, "code": None},
        {
            "status": ProductionAudioStatus.FAILED,
            "code": "FAILED",
            "persistence_result": persisted.persistence_result,
        },
    ]
    for changes in illegal:
        values = {
            "status": ProductionAudioStatus.PERSISTED,
            "run_id": run_id,
            "reference": persisted.reference,
            "code": None,
            "reason": None,
            "persistence_result": persisted.persistence_result,
            "staging_cleanup_code": None,
            "staging_cleanup_path": None,
        }
        values.update(changes)
        with pytest.raises((TypeError, ValueError, ProductionAudioInputError)):
            ProductionAudioResult(**values)


def test_loader_reads_owned_little_endian_array_and_close_is_idempotent(tmp_path):
    path = tmp_path / "audio.wav"
    expected = numpy.asarray(
        [[-32768, 32767], [-1, 1]], dtype=numpy.dtype("<i2")
    )
    _write_wav(path, expected.tobytes(), channels=2)
    token = _Token([False, False])

    loaded = ProductionRunAudioAdapter(_NeverPersist()).open(
        _reference(path, channels=2, frames=2), token
    )

    actual = loaded.samples
    assert isinstance(actual, numpy.ndarray)
    assert actual.dtype == numpy.dtype("<i2")
    assert actual.shape == (2, 2)
    assert actual.flags.c_contiguous and actual.flags.owndata
    numpy.testing.assert_array_equal(actual, expected)
    assert (loaded.sample_rate_hz, loaded.channel_count, loaded.frame_count) == (
        8_000,
        2,
        2,
    )
    assert token.checks == 2
    loaded.close()
    loaded.close()
    with pytest.raises(RuntimeError):
        _ = loaded.samples


def test_loader_maps_frame_read_exception_to_bounded_load_failure_and_closes_reader(
    tmp_path, monkeypatch
):
    path = tmp_path / "audio.wav"
    _write_wav(path)
    reader = _ControlledWaveReader(None)
    monkeypatch.setattr(wave, "open", lambda *args, **kwargs: reader)

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(_reference(path), _Token())

    assert (
        raised.value.error.category
        is AnalysisErrorCategory.AUDIO_LOAD_FAILED
    )
    assert raised.value.error.stage is AnalysisStage.AUDIO_LOAD
    assert raised.value.error.summary == "audio frames could not be read"
    assert len(raised.value.error.summary.encode("utf-8")) <= 1_024
    assert reader.close_calls == 1


def test_loader_maps_truncated_pcm_to_bounded_load_failure_and_closes_reader(
    tmp_path, monkeypatch
):
    path = tmp_path / "audio.wav"
    _write_wav(path)
    reader = _ControlledWaveReader(b"\x00\x00")
    monkeypatch.setattr(wave, "open", lambda *args, **kwargs: reader)

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(_reference(path), _Token())

    assert (
        raised.value.error.category
        is AnalysisErrorCategory.AUDIO_LOAD_FAILED
    )
    assert raised.value.error.stage is AnalysisStage.AUDIO_LOAD
    assert raised.value.error.summary == "audio PCM payload is truncated"
    assert len(raised.value.error.summary.encode("utf-8")) <= 1_024
    assert reader.close_calls == 1


def test_loader_maps_oversized_decimal_revision_to_reference_changed(tmp_path):
    path = tmp_path / "audio.wav"
    _write_wav(path)
    reference = _reference(path)
    reference = replace(
        reference,
        content_revision=f"{reference.audio_data_id}:{'9' * 5_000}",
    )

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(reference, _Token())

    assert (
        raised.value.error.category
        is AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED
    )
    assert raised.value.error.stage is AnalysisStage.AUDIO_LOAD
    assert raised.value.error.summary == "audio content revision size is invalid"


@pytest.mark.parametrize(
    ("case", "category"),
    [
        ("missing", AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE),
        ("size", AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED),
        ("rate", AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED),
        ("channels", AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED),
        ("frames", AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED),
        ("dtype", AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH),
        ("not-wav", AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH),
        ("width", AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH),
        ("revision", AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED),
    ],
)
def test_loader_maps_exact_reference_failures(tmp_path, case, category):
    path = tmp_path / "audio.wav"
    _write_wav(path)
    reference = _reference(path)
    if case == "missing":
        path.unlink()
    elif case == "size":
        reference = _reference(path, revision_size=path.stat().st_size + 1)
    elif case == "rate":
        reference = _reference(path, rate=16_000)
    elif case == "channels":
        reference = _reference(path, channels=2)
    elif case == "frames":
        reference = _reference(path, frames=1)
    elif case == "dtype":
        reference = _reference(path, dtype="int16")
    elif case == "not-wav":
        path.write_bytes(b"x" * path.stat().st_size)
    elif case == "width":
        _write_wav(path, b"\x00\x01", width=1)
        reference = _reference(path, frames=2)
    elif case == "revision":
        reference = replace(reference, content_revision=f"{uuid4()}:{path.stat().st_size}")

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(reference, _Token())

    assert raised.value.error.category is category
    expected_stage = (
        AnalysisStage.CANCEL
        if category is AnalysisErrorCategory.ANALYSIS_CANCELED
        else AnalysisStage.AUDIO_LOAD
    )
    assert raised.value.error.stage is expected_stage


def test_loader_maps_os_invalid_exact_path_to_unreadable():
    audio_id = str(uuid4())
    reference = PersistedAudioReference(
        reference_id=audio_id,
        audio_data_id=audio_id,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path="C:\\bad\x00name.wav",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=2,
        dtype="<i2",
        content_revision=f"{audio_id}:48",
    )

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(reference, _Token())

    assert (
        raised.value.error.category
        is AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE
    )
    assert raised.value.error.stage is AnalysisStage.AUDIO_LOAD
    assert raised.value.error.summary == "audio reference is unreadable"


@pytest.mark.parametrize("states", [[True], [False, True]])
def test_loader_checks_cancel_before_open_and_after_read(tmp_path, states):
    path = tmp_path / "audio.wav"
    _write_wav(path)
    token = _Token(states, reason="operator stop")

    with pytest.raises(AudioLoadError) as raised:
        ProductionRunAudioAdapter(_NeverPersist()).open(_reference(path), token)

    assert raised.value.error.category is AnalysisErrorCategory.ANALYSIS_CANCELED
    assert raised.value.error.stage is AnalysisStage.CANCEL
    assert token.checks == len(states)
