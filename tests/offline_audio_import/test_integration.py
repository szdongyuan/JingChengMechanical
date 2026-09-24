import json
from types import SimpleNamespace
import pytest
from app.service.offline_audio_import import ImportRequest, ImportPhase, OfflineAudioImportService
from app.service.offline_audio_import.snapshot import SnapshotPreparer
from app.service.offline_audio_import.runtime import OfflineImportRuntime
from app.service.run_persistence import RunPersistenceDatabase, settle_startup, AnalysisResultArtifactStore, RunRepository
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from app.service.audio_wav import decode_wav
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone


@pytest.mark.parametrize('kind', ['pcm24', 'float64'])
def test_real_archive_process_engine_and_history(tmp_path, kind):
    source = tmp_path / 'source.wav'; write_tone(source, kind)
    original = source.read_bytes()
    from app.service.run_persistence.prepare_database import prepare_run_persistence_database
    prepare_run_persistence_database(tmp_path / 'runs.db')
    db = RunPersistenceDatabase(tmp_path / 'runs.db')
    assert settle_startup(db, '2026-09-09T00:00:00.000000Z').ready
    artifacts = AnalysisResultArtifactStore(tmp_path / 'results')
    config = {'config_id': 9, 'fault_types': [], 'selected_channels': [7], 'sample_rate': 48000,
        'analysis_duration': 1., 'channels': [{'channel_index': 7, 'spl_weighting': 'Z', 'spl_upper_enabled': True, 'spl_upper_limit': 100.}]}
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: config),
        calibration_loader=lambda _: {}, toolchain_loader=lambda _: None)
    service = OfflineAudioImportService(freeze=freezer.freeze, prepare=freezer.prepare,
        runtime_factory=lambda busy: OfflineImportRuntime(database=db, artifact_store=artifacts,
            output_directory=tmp_path / 'run_audio', production_busy=busy))
    try:
        receipt = service.submit(ImportRequest(str(source), 9, ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},)))
        assert receipt.accepted, receipt.error
        with service._condition:
            assert service._condition.wait_for(lambda: service.status.finished, 40)
        assert service.status.phase is ImportPhase.COMPLETED, service.status
        assert service.status.result_ready and service.status.charts_ready
        snapshot = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        assert RunRepository(db).get(receipt.run_id).hardware_id is None
        assert snapshot.run.ingress_kind == 'IMPORTED'
        source.unlink()
        assert snapshot.audio is not None
        from pathlib import Path
        archive = Path(snapshot.audio.source_wav_path)
        assert archive.read_bytes() == original
        assert decode_wav(archive).samples.max() > 0
        result = snapshot.review
        assert result['channel_results'][0]['spl_db'] < -20
        assert result['review']['import']['current_available'] is False
        from app.service.run_persistence import Judgment
        original_result = snapshot.review
        assert RunRepository(db).set_manual_judgment(receipt.run_id, Judgment.FAIL,
            '2026-09-09T23:59:59.000000Z').value
        relabeled = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        assert relabeled.run.judgment == 'FAIL'
        assert relabeled.original_analysis_judgment == 'PASS'
        assert relabeled.review == original_result
    finally:
        assert service.close()
        db.close_after_upstream_stopped()


def make_service(tmp_path, *, prepare=None, toolchain=None, sink=None, busy=lambda: False):
    from app.service.run_persistence.prepare_database import prepare_run_persistence_database
    db = RunPersistenceDatabase(prepare_run_persistence_database(tmp_path / 'runs.db'))
    assert settle_startup(db, '2026-09-09T00:00:00.000000Z').ready
    artifacts = AnalysisResultArtifactStore(tmp_path / 'results')
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: {
        'fault_types': [], 'selected_channels': [7], 'sample_rate': 48000, 'analysis_duration': 1.,
        'channels': [{'channel_index': 7}]}), calibration_loader=lambda _: {}, toolchain_loader=lambda _: toolchain)
    service = OfflineAudioImportService(freeze=freezer.freeze, prepare=prepare or freezer.prepare,
        runtime_factory=lambda production_busy: OfflineImportRuntime(database=db, artifact_store=artifacts,
            output_directory=tmp_path / 'run_audio', production_busy=production_busy), status_sink=sink, production_busy=busy)
    source = tmp_path / 'source.wav'; write_tone(source, 'float64')
    request = ImportRequest(str(source), 9, ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},))
    return service, db, artifacts, request


def test_postaccept_preparation_failure_is_a_durable_failed_run(tmp_path):
    def broken(snapshot):
        raise ValueError('golden artifact is unreadable')
    service, db, _, request = make_service(tmp_path, prepare=broken)
    try:
        receipt = service.submit(request)
        service.wait_for_phase(ImportPhase.FAILED, 5)
        run = RunRepository(db).get(receipt.run_id)
        assert run is not None and run.state == 'FAILED'
        assert 'golden artifact' in run.terminal_summary
        from app.service.run_persistence.data_management import RunDataManagementService
        assert RunDataManagementService(db).delete_one(receipt.run_id).database_committed
        service._worker.join(2)
        second = service.submit(request)
        assert second.accepted
        service.wait_for_phase(ImportPhase.FAILED, 5)
        assert RunRepository(db).get(second.run_id).state == 'FAILED'
        assert service._runtime.record_failure(
            receipt.run_id, service._freeze(request), ValueError('late failure handler')
        ) is False
        assert RunRepository(db).get(receipt.run_id) is None
    finally:
        assert service.close()
        db.close_after_upstream_stopped()


def test_enabled_ai_uses_real_resident_runtime_and_reuses_frozen_lane(tmp_path):
    from tests.test_ai_resident_runtime import _write_fake_resident_script, _entrypoint
    toolchain = {'toolchain_id': 'tc-1', 'execution_revision': 'test-revision', 'execution_mode': 'resident_process',
        'entrypoint': _entrypoint(_write_fake_resident_script(tmp_path)), 'package_dir': str(tmp_path), 'params': {}, 'timeout_seconds': 2.}
    service, db, artifacts, request = make_service(tmp_path, toolchain=toolchain)
    try:
        first = service.submit(request)
        with service._condition:
            assert service._condition.wait_for(lambda: service.status.finished, 40)
        assert service.status.phase is ImportPhase.COMPLETED, service.status
        lane = service._runtime.executor
        history = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(first.run_id)
        assert history.review['ai_pass'] is True
        response_path = tmp_path / 'ai_runs' / history.successful_attempt_id / 'response.json'
        assert json.loads(response_path.read_text())['ai_result']['process_instance_id']
        service._worker.join(2)
        second = service.submit(request)
        assert second.accepted
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        assert service._runtime.executor is lane
        assert second.run_id != first.run_id
    finally:
        assert service.close()
        db.close_after_upstream_stopped()


def test_missing_enabled_ai_model_fails_truthfully_after_acceptance(tmp_path):
    toolchain = {'toolchain_id': 'missing-model', 'execution_revision': 'test-revision', 'execution_mode': 'resident_process',
        'entrypoint': 'missing-model.exe', 'package_dir': str(tmp_path), 'params': {}, 'timeout_seconds': 2.}
    service, db, _, request = make_service(tmp_path, toolchain=toolchain)
    try:
        receipt = service.submit(request)
        assert receipt.accepted
        failure = service.wait_for_phase(ImportPhase.FAILED, 40)
        assert failure.error and ('missing' in failure.error.lower() or 'failed' in failure.error.lower())
        assert RunRepository(db).get(receipt.run_id).state == 'FAILED'
    finally:
        assert service.close()
        db.close_after_upstream_stopped()


def test_production_defers_charts_and_relabel_does_not_lose_enrichment(tmp_path):
    import threading
    from app.service.run_persistence import Judgment
    busy = threading.Event()
    def observe(status):
        if status.phase is ImportPhase.ANALYZING:
            busy.set()  # Production begins during the decision phase.
    service, db, artifacts, request = make_service(tmp_path, sink=observe, busy=busy.is_set)
    try:
        receipt = service.submit(request)
        service.wait_for_phase(ImportPhase.COMPLETING_CHARTS, 40)
        before = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        assert before.charts_message is not None
        assert RunRepository(db).set_manual_judgment(receipt.run_id, Judgment.FAIL,
            '2026-09-09T23:59:59.000000Z').value
        assert not service.submit(request).accepted
        busy.clear()
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        after = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        assert after.run.judgment == 'FAIL'
        assert after.original_analysis_judgment == before.original_analysis_judgment
        assert after.charts_message is None
    finally:
        busy.clear()
        assert service.close()
        db.close_after_upstream_stopped()


def test_staged_import_ai_enrichment_keeps_decision_fields(tmp_path):
    from dataclasses import replace
    from app.service.analysis.production_executor import ProductionAnalysisExecutor
    from app.service.run_persistence import ProductionRunAudioAdapter
    from tests.analysis_scheduler.test_imported_precision_analysis import imported_job
    from tests.analysis_scheduler.test_production_executor import _Token, _toolchain, _ResidentAiRuntime
    source = tmp_path / 'source.wav'; write_tone(source, 'float64')
    job = imported_job(source, config={'config_id': 9, 'selected_channels': [7], 'channels': [{'channel_index': 7}]},
        mapping={'schema': 'audio-import-map/v2', 'source_channel_count': 1, 'assignments': [
            {'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7}]})
    job = replace(job, snapshot=replace(job.snapshot, ai_toolchain=_toolchain(execution_mode='resident_process')))
    ai = _ResidentAiRuntime(lambda call: {'module_status': 'success', 'response_payload': {
        'request_id': call['request_id'], 'ai_result': {'ai_label': 'ok'}}})
    executor = ProductionAnalysisExecutor(ai_runtime=ai)
    audio = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    try:
        decision = executor.execute_decision(job, audio, _Token())
        executor.execute_enrichment(decision.bundle_token)
    finally:
        executor.close(); audio.close()


@pytest.mark.parametrize('phase', [ImportPhase.ANALYZING, ImportPhase.COMPLETING_CHARTS])
def test_shutdown_retires_analysis_and_chart_workers_without_active_run(tmp_path, phase):
    import threading
    entered = threading.Event()
    release = threading.Event()
    busy = threading.Event()
    def observe(status):
        if status.phase is ImportPhase.ANALYZING:
            busy.set()
        if status.phase is phase:
            entered.set()
            release.wait(3)
    service, db, _, request = make_service(tmp_path, sink=observe, busy=busy.is_set)
    try:
        receipt = service.submit(request)
        assert entered.wait(15)
        # Release the deterministic phase barrier as shutdown requests cancellation.
        release.set()
        assert service.close(timeout_seconds=5)
        assert not service._worker.is_alive()
        assert service._runtime.executor is None
        assert RunRepository(db).get(receipt.run_id).state != 'ACTIVE'
        assert service.status.phase is ImportPhase.CANCELED
        if phase is ImportPhase.COMPLETING_CHARTS:
            assert service.status.result_ready
    finally:
        release.set(); busy.clear()
        service.close()
        db.close_after_upstream_stopped()


def test_any_real_saved_config_imports_and_replays_without_input_hardware(tmp_path):
    import time
    from model.test_config_table import TestConfigTable
    from model.channel_config_table import ChannelConfigTable
    from app.service.config_service import ConfigService
    from app.service.run_persistence import AudioRepository, ProductionRunAudioAdapter
    from app.service.run_scoped_playback.service import RunScopedPlaybackService
    from app.service.run_scoped_playback.contracts import RunPlaybackRequest, PlaybackStatus
    from tests.run_scoped_playback.conftest import FakeBackend
    config = TestConfigTable.create(config_name='Saved offline only', selected_channels='[7]',
        analysis_duration=1., output_device_name='test speaker', output_device_hostapi='test output')
    ChannelConfigTable.create(config=config, channel_index=7, spl_weighting='Z')
    service, db, artifacts, request = make_service(tmp_path)
    freezer = SnapshotPreparer(config_service=ConfigService(None), calibration_loader=lambda _: {}, toolchain_loader=lambda _: None)
    service._freeze, service._prepare = freezer.freeze, freezer.prepare
    request = ImportRequest(request.source_path, config.config_id, request.assignments)
    playback = None
    try:
        receipt = service.submit(request)
        assert receipt.accepted, receipt.error
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        from pathlib import Path
        Path(request.source_path).unlink()
        backend = FakeBackend()
        playback = RunScopedPlaybackService(run_repository=RunRepository(db), audio_repository=AudioRepository(db),
            audio_loader=ProductionRunAudioAdapter(None), backend=backend, monotonic=time.monotonic)
        result = playback.play(RunPlaybackRequest(receipt.run_id, 0, 0., 1.))
        assert result.status is PlaybackStatus.STARTED, result
        assert backend.start_arguments[0][4] == 20.0
        assert backend.start_arguments[0][0].max() > 0
    finally:
        if playback:
            playback.shutdown(2.)
        assert service.close()
        db.close_after_upstream_stopped()


def test_deleted_pending_import_releases_lane_without_resurrecting_record(tmp_path):
    import threading
    from app.service.run_persistence.data_management import RunDataManagementService
    busy = threading.Event()
    block_first_decision = True

    def observe(status):
        nonlocal block_first_decision
        if status.phase is ImportPhase.ANALYZING and block_first_decision:
            block_first_decision = False
            busy.set()

    service, db, artifacts, request = make_service(tmp_path, sink=observe, busy=busy.is_set)
    try:
        first = service.submit(request)
        service.wait_for_phase(ImportPhase.COMPLETING_CHARTS, 40)
        deleted = RunDataManagementService(db, result_artifact_store=artifacts).delete_one(first.run_id)
        assert deleted.database_committed, deleted
        busy.clear()
        with service._condition:
            assert service._condition.wait_for(lambda: service.status.finished, 10), service.status
        assert service.status.phase is ImportPhase.CANCELED
        assert not service.status.result_ready
        assert not service.status.charts_ready
        assert 'discarded' in service.status.error
        service._worker.join(2)
        assert RunRepository(db).get(first.run_id) is None
        second = service.submit(request)
        assert second.accepted, second.error
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        assert RunRepository(db).get(second.run_id).state == 'SUCCEEDED'
        assert RunRepository(db).get(first.run_id) is None
    finally:
        busy.clear()
        assert service.close()
        db.close_after_upstream_stopped()


def test_deleted_pending_import_shutdown_and_repeated_failure_do_not_recreate_record(tmp_path):
    import threading
    from app.service.run_persistence.data_management import RunDataManagementService
    busy = threading.Event()

    def observe(status):
        if status.phase is ImportPhase.ANALYZING:
            busy.set()

    service, db, artifacts, request = make_service(tmp_path, sink=observe, busy=busy.is_set)
    frozen = service._freeze(request)
    try:
        receipt = service.submit(request)
        service.wait_for_phase(ImportPhase.COMPLETING_CHARTS, 40)
        deleted = RunDataManagementService(db, result_artifact_store=artifacts).delete_one(receipt.run_id)
        assert deleted.database_committed
        assert RunRepository(db).get(receipt.run_id) is None
        assert service.close(timeout_seconds=5)
        assert RunRepository(db).get(receipt.run_id) is None
        assert service.status.phase is ImportPhase.CANCELED
        assert not service.status.result_ready
        assert not service.status.charts_ready
        # Both runtime and application handlers retire this job during close.
        # Repeating that same central operation remains non-creating.
        for _ in range(2):
            assert service._runtime.record_failure(
                receipt.run_id, frozen, InterruptedError('application shutdown')
            ) is False
            assert RunRepository(db).get(receipt.run_id) is None
    finally:
        busy.clear()
        assert service.close()
        db.close_after_upstream_stopped()
