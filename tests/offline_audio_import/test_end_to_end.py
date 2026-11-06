"""Synthetic v2 migration -> real import -> full close/reopen -> existing UI."""
import json
import os
from pathlib import Path
import time
import wave
from types import SimpleNamespace
from zipfile import ZipFile
from scipy.io import wavfile

import numpy as np
import pytest
from PyQt6 import sip

from app.service.offline_audio_import import ImportRequest, ImportPhase, OfflineAudioImportService
from app.service.offline_audio_import.snapshot import SnapshotPreparer
from app.service.offline_audio_import.runtime import OfflineImportRuntime
from app.service.run_persistence import (RunPersistenceDatabase, settle_startup,
    AnalysisResultArtifactStore, RunRepository, AudioRepository, ProductionRunAudioAdapter)
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.export_reader import RunExportReader
from app.service.run_scoped_playback.service import RunScopedPlaybackService
from app.service.run_scoped_playback.contracts import BackendJoinReport, BackendJoinStatus
from tests.run_scoped_playback.conftest import FakeBackend
from tests.run_persistence.conftest import raw_schema_database, now
from tests.run_persistence.test_audio_format_migration import populated_v2, rows
from tests.run_persistence.test_historical_analysis_integration import screenshot_style
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone, import_mapping
from tests.analysis_scheduler.test_cached_spectral_units import assert_spectral_levels
from tests.test_ai_resident_runtime import _write_fake_resident_script, _entrypoint
from ui.modules.data_management import DataManagementModule


def write_windowed_tones(source, kind):
    samples = write_tone(source, kind, columns=3)
    samples[36000:] *= 100
    if kind == 'pcm24':
        integers = np.rint(samples * 2 ** 23).astype(np.int32)
        packed = np.stack([integers & 255, (integers >> 8) & 255, (integers >> 16) & 255], axis=-1).astype(np.uint8)
        with wave.open(str(source), 'wb') as writer:
            writer.setparams((3, 3, 48000, 0, 'NONE', ''))
            writer.writeframes(packed.tobytes())
    else:
        wavfile.write(source, 48000, samples.astype(kind))
    from app.service.audio_wav import decode_wav
    return decode_wav(source).samples


class OutputBackend(FakeBackend):
    """Only the physical output boundary is fake; playback service stays real."""
    def join(self, timeout_seconds):
        self.join_calls.append(timeout_seconds)
        return BackendJoinReport(BackendJoinStatus.CLEAN, False)


@pytest.mark.parametrize('kind', ['pcm24', 'float32', 'float64'])
@pytest.mark.parametrize('calibrated', [False, True])
def test_precision_import_survives_complete_runtime_reopen(
    tmp_path, populated_v2, kind, calibrated, qtbot, screenshot_style, monkeypatch,
):
    legacy_rows = rows(populated_v2)
    source = tmp_path / 'external.wav'
    direct = write_windowed_tones(source, kind)
    original_bytes = source.read_bytes()
    from app.service.config_service import ConfigService
    from model.test_config_table import TestConfigTable
    from model.channel_config_table import ChannelConfigTable
    saved_config = TestConfigTable.create(config_id=9, config_name='离线精度验证',
        selected_channels='[7]', analysis_duration=1., output_device_name='Synthetic output',
        output_device_hostapi='Test')
    ChannelConfigTable.create(config=saved_config, channel_index=7, spl_weighting='Z',
        spl_upper_enabled=True, spl_upper_limit=100.)
    configs = ConfigService(None)
    config = configs.load_config(9)
    assert config['result_dashboard_layout']
    # Add the mapped channel and scored result to the existing resident protocol fixture.
    from tests import test_ai_resident_runtime as resident_fixture
    monkeypatch.setattr(resident_fixture, '_FAKE_RESIDENT_SCRIPT',
        resident_fixture._FAKE_RESIDENT_SCRIPT.replace('"ai_label": "ok",',
            '"ai_label": "ok", "score": 0.98, "extra": {"channel_index": 2},'))
    toolchain = {'toolchain_id': 'tc-reopen', 'execution_revision': 'test-revision',
        'execution_mode': 'resident_process', 'entrypoint': _entrypoint(_write_fake_resident_script(tmp_path)),
        'package_dir': str(tmp_path), 'params': {}, 'timeout_seconds': 5.}
    freezer = SnapshotPreparer(config_service=configs,
        calibration_loader=lambda _: {'In8': {'deviation_value': '2'}}, toolchain_loader=lambda _: toolchain)
    db = RunPersistenceDatabase(populated_v2)
    assert settle_startup(db, '2026-09-09T00:00:00.000000Z').ready
    assert rows(populated_v2) == legacy_rows
    backups = list(tmp_path.glob('*.v2-backup-*.sqlite3'))
    assert len(backups) == 1 and rows(backups[0]) == legacy_rows
    artifacts = AnalysisResultArtifactStore(tmp_path / 'results')
    service = OfflineAudioImportService(freeze=freezer.freeze, prepare=freezer.prepare,
        runtime_factory=lambda busy: OfflineImportRuntime(database=db, artifact_store=artifacts,
            output_directory=tmp_path / 'run_audio', production_busy=busy))
    try:
        receipt = service.submit(ImportRequest(str(source), 9, tuple(import_mapping()['assignments']),
            start_seconds=.25, duration_seconds=.5, use_mic_calibration=calibrated))
        assert receipt.accepted, receipt.error
        service.wait_for_phase(ImportPhase.COMPLETED, 40)
        before = HistoricalAnalysisReader(db, result_artifact_store=artifacts).read(receipt.run_id)
        review = before.review
        assert review['ai_pass'] is True
        ai_row = next(row for row in review['summary_payload']['rows'] if row['item_key'] == 'ai')
        assert ai_row['status'] == 'OK' and ai_row['channel_index'] == 7
        response = tmp_path / 'ai_runs' / before.successful_attempt_id / 'response.json'
        assert json.loads(response.read_text())['ai_result']['process_instance_id']
        ai_request = json.loads(response.with_name('request.json').read_text())
        segment = ai_request['segments'][0]
        assert segment['channels'] == [2] and segment['source_channels'] == [7]
        assert segment['channel_map'] == [{'logical_channel': 2, 'source_channel': 7}]
        assert ai_request['window_info'] == {'final_window_offset_ms': 250, 'final_window_duration_ms': 500}
        assert [row['channel_index'] for row in review['channel_results']] == [7]
        expected = 20 * np.log10(np.sqrt(np.mean(direct[12000:36000, 2] ** 2)) / 2e-5)
        assert review['channel_results'][0]['spl_db'] == pytest.approx(
            expected + (20 * np.log10(2) if calibrated else 0), abs=.1)
        assert review['analysis_window'] == [.25, .75]
        assert set(review['chart_payload']) == {'7', '8'}
        for channel, frequency in [('7', 2000), ('8', 1000)]:
            fft = review['chart_payload'][channel]['fft']
            assert fft['frequencies_hz'][np.argmax(fft['levels_db'])] == pytest.approx(frequency, abs=15)
            column = 2 if channel == '7' else 0
            sensitivity = 2 if calibrated and channel == '7' else 1
            channel_config = next(c for c in review['config_payload']['channels'] if str(c['channel_index']) == channel)
            assert_spectral_levels(review['chart_payload'][channel],
                direct[12000:36000, column] * sensitivity, 48000, channel_config)
        assert review['review']['import']['current_available'] is False
        run = RunRepository(db).get(receipt.run_id)
        assert run.hardware_id is None and run.current_trace_json is None
        assert run.ingress_kind == 'IMPORTED' and run.trigger_source == 'IMPORT'
        archive = Path(before.audio.source_wav_path)
        assert archive != source and archive.read_bytes() == original_bytes
        artifact_bytes = {p: p.read_bytes() for p in (tmp_path / 'results').rglob('*') if p.is_file()}
    finally:
        assert service.close()
        assert not service._worker.is_alive()
        assert service._runtime.executor is None
        db.close_after_upstream_stopped()

    moved_source = source.rename(tmp_path / 'user-moved-original.wav')
    reopened = RunPersistenceDatabase(populated_v2)
    assert settle_startup(reopened, '2026-09-09T23:00:00.000000Z').ready
    reader = HistoricalAnalysisReader(reopened,
        result_artifact_store=AnalysisResultArtifactStore(tmp_path / 'results'))
    backend = OutputBackend()
    playback = RunScopedPlaybackService(run_repository=RunRepository(reopened),
        audio_repository=AudioRepository(reopened), audio_loader=ProductionRunAudioAdapter(None),
        backend=backend, monotonic=time.monotonic)
    reopened_import = OfflineAudioImportService(freeze=freezer.freeze, prepare=freezer.prepare,
        runtime_factory=lambda busy: OfflineImportRuntime(database=reopened,
            artifact_store=AnalysisResultArtifactStore(tmp_path / 'results'),
            output_directory=tmp_path / 'run_audio', production_busy=busy))
    services = SimpleNamespace(historical_analysis_reader=reader, historical_playback=playback,
        offline_audio_import_service=reopened_import, config_service=configs,
        run_data_management_service=RunDataManagementService(reopened,
            result_artifact_store=AnalysisResultArtifactStore(tmp_path / 'results')))
    module = DataManagementModule(services)
    qtbot.addWidget(module)
    widget = None
    try:
        after = reader.read(receipt.run_id)
        assert after.review == review
        assert after.audio.channel_order == (0, 1, 2)
        row = next(i for i in range(module.table.rowCount()) if module.table.item(i, 1).text() == receipt.run_id)
        module.table.cellWidget(row, module._ANALYSIS_COLUMN).click()
        widget = module._analysis_dialogs[receipt.run_id]
        qtbot.waitUntil(lambda: widget.snapshot is not None and widget.replay_panel is not None, timeout=10000)
        assert '导入' in widget.metadata_label.text() and '无电流数据' in widget.metadata_label.text()
        result_module = widget.result_view.result_module
        assert result_module._metric_selected_channel_index == 7
        assert result_module.lb_spl_metric_value.text() != '--'
        assert result_module.lb_loudness_metric_value.text() != '--'
        assert result_module.table.item(5, 1).text() == '0.98'
        from ui.modules.spectrum import SpectrumModule
        spectrum = next(p for p in widget.result_view.panel_widgets.values() if isinstance(p, SpectrumModule))
        assert spectrum.current_render_channel() == 7
        displayed = widget.result_view.display_services.factory_mode.review_payload
        assert displayed.config_payload['selected_channels'] == [8, 7]
        assert displayed.display_channel_labels[8] == '仅图表 / WAV 1'
        panel = widget.replay_panel
        assert panel.channel_combo.currentData() == 0
        assert panel.gain_spin.value() == 20.
        for column in (0, 2, 1):  # Includes the ignored original WAV column.
            panel.channel_combo.setCurrentIndex(panel.channel_combo.findData(column))
            qtbot.waitUntil(panel.play_button.isEnabled)
            count = len(backend.start_arguments)
            panel.play_button.click()
            qtbot.waitUntil(lambda: len(backend.start_arguments) > count)
            segment, rate, _, _, gain, _ = backend.start_arguments[-1]
            assert rate == 48000 and gain == 20.
            np.testing.assert_array_equal(segment, direct[:, column].astype(np.float32))
        widget.label_combo.setCurrentIndex(2)
        widget.save_label_button.click()
        qtbot.waitUntil(lambda: widget.snapshot.run.judgment_source == 'MANUAL')
        relabeled = reader.read(receipt.run_id)
        assert relabeled.run.judgment == 'FAIL'
        assert relabeled.original_analysis_judgment == before.original_analysis_judgment == 'PASS'
        assert relabeled.review == review
        assert relabeled.run.config_snapshot_json == before.run.config_snapshot_json
        assert archive.read_bytes() == original_bytes
        assert all(p.read_bytes() == content for p, content in artifact_bytes.items())

        if os.environ.get('HISTORICAL_REPLAY_SCREENSHOTS') == '1' and kind == 'float64' and calibrated:
            output = Path('outputs'); output.mkdir(exist_ok=True)
            panel = widget.replay_panel
            panel.channel_combo.setCurrentIndex(panel.channel_combo.findData(0))
            widget.show(); qtbot.wait(100)
            assert widget.grab().save(str(output / 'offline-import-reopened-history.png'))
            monkeypatch.setattr(module, '_refresh_fault_type_options_cache', lambda: None)
            module.resize(1550, 500); module.show(); qtbot.wait(100)
            assert module.btn_import_audio.isEnabled()
            assert module.grab().save(str(output / 'offline-import-data-management.png'))
            from ui.popups.import_audio_analysis_dialog import ImportAudioAnalysisDialog
            setup = ImportAudioAnalysisDialog(configs=[config], default_config_id=9)
            qtbot.addWidget(setup)
            setup.load_wav_path(str(moved_source))
            for combo, assignment in zip(setup._mapping_combos, import_mapping()['assignments'], strict=True):
                choice = (assignment['mode'], assignment['channel_index'])
                combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i) == choice))
            setup.show(); qtbot.wait(100)
            assert setup.grab().save(str(output / 'offline-import-setup.png'))
            setup.close()

        from app.service.run_record_export.contracts import RunDataExportOptions
        from app.service.run_record_export.package import write_run_data_export
        export_reader = RunExportReader(reopened)
        record = export_reader.read_many((receipt.run_id,))[0]
        assert record.source_wav_path == str(archive) and record.channel_order == (0, 1, 2)
        result = write_run_data_export(export_reader.read_configurable_report((receipt.run_id,)),
            str(tmp_path / 'export.zip'), ai_values={}, options=RunDataExportOptions(False, True, False, False))
        assert result.counts.audio_files_emitted == 3, result
        with ZipFile(tmp_path / 'export.zip') as package:
            assert len([n for n in package.namelist() if n.endswith('.wav')]) == 3
        widget.close()
        qtbot.waitUntil(lambda: receipt.run_id not in module._analysis_dialogs)
        deleted = services.run_data_management_service.delete_one(receipt.run_id)
        assert deleted.database_committed
        assert not archive.exists()
        assert moved_source.read_bytes() == original_bytes
        assert RunRepository(reopened).get(receipt.run_id) is None
    finally:
        if widget is not None and not sip.isdeleted(widget):
            widget.close()
        module.close()
        from ui.popups.historical_analysis_dialog import _ACTIVE_READERS
        qtbot.waitUntil(lambda: not _ACTIVE_READERS, timeout=5000)
        assert playback.shutdown(2.).status.value == 'CLEAN'
        assert reopened_import.close()
        reopened.close_after_upstream_stopped()
