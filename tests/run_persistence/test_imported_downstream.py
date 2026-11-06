import json
from dataclasses import replace
from uuid import uuid4
import pytest
from app.service.run_persistence import (
    AudioPersistenceService, AudioPublication, AudioRepository, RunRepository,
    ProductionRunAudioAdapter, ImportedAudioSaveRequest,
)
from app.service.run_persistence.contracts import AttemptKind, RunInsert, IngressKind, TriggerSource, freeze_run_inputs
from app.service.run_persistence.run_persistence_scheduler_bridge import prepare_analysis_facts, _audio_reference_matches_record
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from app.service.run_persistence.export_reader import RunExportReader
from tests.run_persistence.test_audio_service import insert_imported_run
from tests.run_persistence.test_export_reader import _ready_runtime, _finish, _analysis_envelope, ACCEPTED_AT
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone, import_mapping
from tests.analysis_scheduler.test_production_executor import _Token
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.scheduler import AnalysisJob
from app.service.run_persistence import decode_analysis_result_payload


@pytest.mark.parametrize('kind', ['pcm24', 'float32', 'float64'])
def test_real_archive_bridge_history_and_export_keep_original_columns(prepared_database_path, tmp_path, kind):
    runtime = _ready_runtime(prepared_database_path)
    source = tmp_path / 'external.wav'
    write_tone(source, kind, columns=3)
    run_id = insert_imported_run(runtime, ACCEPTED_AT, source, expected_frame_count=48000)
    envelope = _analysis_envelope()
    envelope['config'].update(selected_channels=[7], channels=[{'channel_index': 7, 'spl_weighting': 'Z'}])
    envelope['channel_mapping'] = import_mapping()
    envelope['analysis']['window_end_seconds'] = 1.0
    with runtime.operation() as database:
        database.execute_sql('UPDATE run SET config_snapshot_json=? WHERE run_id=?', (json.dumps(envelope), run_id))
    adapter = ProductionRunAudioAdapter(AudioPersistenceService(AudioPublication(), AudioRepository(runtime), RunRepository(runtime)))
    archive = tmp_path / 'archive.wav'
    result = adapter.persist_imported(ImportedAudioSaveRequest(
        save_job_id=str(uuid4()), run_id=run_id, source_identity='sha256:immutable-source', content_revision=1,
        source_path=str(source), final_path=str(archive), expected_sample_rate_hz=48000,
        expected_channel_count=3, expected_frame_count=48000, now=ACCEPTED_AT))
    assert result.reference is not None, result
    with pytest.raises(ValueError):
        replace(result, reference=replace(result.reference, source_container_format=(
            'WAV_IEEE_FLOAT' if kind == 'pcm24' else 'WAV_PCM')))
    assert archive.read_bytes() == source.read_bytes()
    record = AudioRepository(runtime).get_for_run(run_id)
    facts = prepare_analysis_facts(RunRepository(runtime).get(run_id), record, AttemptKind.INITIAL)
    assert facts.audio_reference.dtype == '<f8'
    assert _audio_reference_matches_record(facts.audio_reference, record, 'IMPORTED')
    loaded = adapter.open(facts.audio_reference, _Token())
    executor = ProductionAnalysisExecutor()
    try:
        job = AnalysisJob(run_id=run_id, analysis_attempt_id=str(uuid4()), kind=facts.scheduler_kind,
            audio=facts.audio_reference, snapshot=facts.snapshot, execution_timeout_seconds=60.)
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)
        assert [row['channel_index'] for row in result['result']['channel_results']] == [7]
        from app.service.run_persistence.analysis_result_artifact import AnalysisResultArtifactStore
        from tests.run_persistence.test_historical_analysis_reader import _save_attempt
        store = AnalysisResultArtifactStore(tmp_path / 'results')
        _save_attempt((runtime, run_id, record.audio_id, archive, store, result['result']))
    finally:
        loaded.close()
        executor.close()
    _finish(runtime, run_id)
    history = HistoricalAnalysisReader(runtime, result_artifact_store=store).read(run_id)
    assert history.audio.channel_order == (0, 1, 2)
    assert set(history.review['chart_payload']) == {'7', '8'}
    exported = RunExportReader(runtime).read_many((run_id,))[0]
    assert exported.channel_order == (0, 1, 2)
    assert exported.source_wav_path == str(archive.resolve())
    with runtime.operation() as database:
        database.execute_sql('UPDATE audio SET container_format=? WHERE run_id=?',
            ('WAV_IEEE_FLOAT' if kind == 'pcm24' else 'WAV_PCM', run_id))
    assert HistoricalAnalysisReader(runtime, result_artifact_store=store).read(run_id).audio is None
    runtime.close_after_upstream_stopped()


def test_explicit_import_mapping_is_accepted_only_for_imported_runs(tmp_path):
    envelope = _analysis_envelope()
    envelope['config']['selected_channels'] = [7]
    envelope['channel_mapping'] = import_mapping()
    snapshots = freeze_run_inputs(envelope)
    values = dict(run_id=str(uuid4()), config_id=7, snapshots=snapshots, accepted_at=ACCEPTED_AT)
    assert RunInsert(**values, ingress_kind=IngressKind.IMPORTED, trigger_source=TriggerSource.IMPORT,
        hardware_id=None, imported_source_identity='source:1', imported_source_path=str(tmp_path / 'source.wav'),
        imported_expected_frame_count=48000)
    with pytest.raises(ValueError, match='import'):
        RunInsert(**values, ingress_kind=IngressKind.CAPTURED, trigger_source=TriggerSource.MANUAL, hardware_id=1)
