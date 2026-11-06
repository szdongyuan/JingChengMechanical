from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from uuid import uuid4
import wave

import pytest

from app.service.multi_run_coordinator_mvp import (
    AudioMetadataConstraints,
    AudioSaveSourceKind,
    AudioSaveStatus,
    BoundedAudioSaveDispatcher,
    CapturedAudioSaveRequest,
    CapturedAudioValue,
    CapturedSourceFormat,
    FinalPathRequest,
    ImportedAudioSaveRequest,
    ImportedSourceFormat,
    ImportedSourceReference,
    PersistedAudioFormat,
    SaveCancelToken,
    TerminalConclusion,
)
from app.service.multi_run_integration.audio_saver import ProductionAudioSaver
from app.service.run_persistence import (
    AudioPersistenceService,
    AudioPublication,
    AudioRepository,
    ProductionRunAudioAdapter,
    RunRepository,
)
from app.service.run_persistence.contracts import (
    IngressKind,
    RunInsert,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from tests.multi_run_coordinator_mvp._support import (
    Harness,
    frozen_imported,
    imported_request,
)
from tests.run_persistence.conftest import make_analysis_envelope
from tests.run_persistence.test_audio_repository import ready_runtime
from tests.run_persistence.test_audio_service import CountingRunRepository


@pytest.fixture
def prepared_database_path(tmp_path):
    return prepare_run_persistence_database(tmp_path / "run-v2.db")


@pytest.fixture
def now() -> str:
    return "2026-07-27T12:34:56.123456Z"


def _write_wav(path: Path, *, frame_count: int) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"\x01\x00" * frame_count)


def _insert_active(
    runtime,
    *,
    run_id: str,
    now: str,
    imported_source: Path | None = None,
    imported_source_identity: str | None = None,
    expected_frame_count: int | None = None,
) -> None:
    imported = imported_source is not None
    result = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=IngressKind.IMPORTED if imported else IngressKind.CAPTURED,
            trigger_source=TriggerSource.IMPORT if imported else TriggerSource.MANUAL,
            config_id=1,
            hardware_id=None if imported else 2,
            snapshots=freeze_run_inputs(
                make_analysis_envelope(config={"config": 1})
            ),
            accepted_at=now,
            imported_source_identity=imported_source_identity,
            imported_source_path=(
                str(imported_source.resolve(strict=False))
                if imported_source is not None
                else None
            ),
            imported_expected_frame_count=expected_frame_count,
        )
    )
    assert result.value is True


def _real_saver(runtime, now: str):
    run_repository = CountingRunRepository(RunRepository(runtime))
    authority = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            run_repository,
        ),
        utc_now=lambda: now,
    )
    return ProductionAudioSaver(authority), run_repository


def _captured_request(run_id: str, final_path: Path) -> CapturedAudioSaveRequest:
    source_identity = "capture:managed-input:7:reservation:9"
    return CapturedAudioSaveRequest(
        save_job_id=str(uuid4()),
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
            str(final_path.resolve(strict=False))
        ),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )


def _imported_request(
    run_id: str,
    source_path: Path,
    final_path: Path,
) -> ImportedAudioSaveRequest:
    source_identity = "sha256:exact-imported-source"
    return ImportedAudioSaveRequest(
        save_job_id=str(uuid4()),
        run_id=run_id,
        source_kind=AudioSaveSourceKind.IMPORTED_SOURCE,
        source_identity=source_identity,
        source=ImportedSourceReference(
            source_identity=source_identity,
            absolute_path=str(source_path.resolve(strict=False)),
            sample_rate_hz=8_000,
            channel_count=1,
            frame_count=2,
            format=ImportedSourceFormat.PCM16_WAV,
        ),
        final_path_request=FinalPathRequest(
            str(final_path.resolve(strict=False))
        ),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )


def test_captured_and_imported_savers_preserve_frozen_identity(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    captured_run_id = str(uuid4())
    imported_run_id = str(uuid4())
    source_path = tmp_path / "source.wav"
    _write_wav(source_path, frame_count=2)
    _insert_active(runtime, run_id=captured_run_id, now=now)
    _insert_active(
        runtime,
        run_id=imported_run_id,
        now=now,
        imported_source=source_path,
        imported_source_identity="sha256:exact-imported-source",
        expected_frame_count=2,
    )
    saver, run_repository = _real_saver(runtime, now)
    captured = _captured_request(captured_run_id, tmp_path / "captured.wav")
    imported = _imported_request(
        imported_run_id,
        source_path,
        tmp_path / "imported.wav",
    )

    captured_outcome = saver.save_captured(captured, SaveCancelToken())
    imported_outcome = saver.save_imported(imported, SaveCancelToken())

    assert captured_outcome.status is AudioSaveStatus.SAVED
    assert imported_outcome.status is AudioSaveStatus.SAVED
    for request, outcome in (
        (captured, captured_outcome),
        (imported, imported_outcome),
    ):
        assert outcome.save_job_id == request.save_job_id
        assert outcome.run_id == request.run_id
        assert outcome.source_kind is request.source_kind
        assert outcome.source_identity == request.source_identity
        assert outcome.persisted_audio is not None
        assert outcome.persisted_audio.save_job_id == request.save_job_id
        assert outcome.persisted_audio.source_identity == request.source_identity
        assert outcome.persisted_audio.content_revision == 1
        durable = AudioRepository(runtime).get_for_run(request.run_id)
        assert durable is not None
        assert durable.save_job_id == request.save_job_id
        assert durable.source_kind == request.source_kind.name
        assert durable.source_identity == request.source_identity
        assert durable.content_revision == 1
    assert run_repository.finish_calls == 0


def test_imported_saver_rejects_frame_mismatch_without_terminal_write(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    source_path = tmp_path / "three-frames.wav"
    final_path = tmp_path / "must-not-publish.wav"
    _write_wav(source_path, frame_count=3)
    _insert_active(
        runtime,
        run_id=run_id,
        now=now,
        imported_source=source_path,
        imported_source_identity="sha256:exact-imported-source",
        expected_frame_count=2,
    )
    saver, run_repository = _real_saver(runtime, now)

    outcome = saver.save_imported(
        _imported_request(run_id, source_path, final_path),
        SaveCancelToken(),
    )

    assert outcome.status is AudioSaveStatus.FAILED
    assert outcome.persisted_audio is None
    assert AudioRepository(runtime).get_for_run(run_id) is None
    assert not final_path.exists()
    assert run_repository.finish_calls == 0
    assert RunRepository(runtime).get(run_id).state == "ACTIVE"


def test_saver_failure_reaches_coordinator_once(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    source_path = tmp_path / "three-frames.wav"
    final_path = tmp_path / "must-not-publish.wav"
    _write_wav(source_path, frame_count=3)
    start_request = imported_request()
    _insert_active(
        runtime,
        run_id=start_request.run_id,
        now=now,
        imported_source=source_path,
        imported_source_identity="sha256:exact-imported-source",
        expected_frame_count=2,
    )
    saver, run_repository = _real_saver(runtime, now)
    dispatcher = BoundedAudioSaveDispatcher(
        captured_saver=saver,
        imported_saver=saver,
        worker_count=1,
        outstanding_capacity=1,
    )
    harness = Harness()
    harness.save_dispatcher = dispatcher
    harness.imported_freezer.result_factory = lambda request: replace(
        frozen_imported(request),
        source_reference=ImportedSourceReference(
            source_identity="sha256:exact-imported-source",
            absolute_path=str(source_path.resolve(strict=False)),
            sample_rate_hz=8_000,
            channel_count=1,
            frame_count=2,
            format=ImportedSourceFormat.PCM16_WAV,
        ),
        final_path_request=FinalPathRequest(
            str(final_path.resolve(strict=False))
        ),
        metadata=AudioMetadataConstraints(
            expected_sample_rate_hz=8_000,
            expected_channel_count=1,
            expected_frame_count=2,
            format=PersistedAudioFormat.PCM16_WAV,
        ),
    )
    coordinator = harness.coordinator()

    assert coordinator.start_imported(start_request).accepted
    report = dispatcher.shutdown(time.monotonic() + 3.0)

    assert report.completed_count == 1
    assert len(harness.persistence.terminal_calls) == 1
    assert (
        harness.persistence.terminal_calls[0].conclusion
        is TerminalConclusion.ERROR
    )
    assert AudioRepository(runtime).get_for_run(start_request.run_id) is None
    assert not final_path.exists()
    assert run_repository.finish_calls == 0
