from dataclasses import replace
import json
import wave
from uuid import uuid4

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.audio_wav import decode_wav
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.scheduler import AudioStorageKind, PersistedAudioReference
from app.service.run_persistence import ProductionRunAudioAdapter, decode_analysis_result_payload
from tests.analysis_scheduler.test_production_executor import _job, _Token
from tests.analysis_scheduler.test_production_executor import _toolchain, _ResidentAiRuntime


def write_tone(path, kind, columns=1):
    rate = 48000
    times = np.arange(rate) / rate
    samples = np.column_stack([np.sin(2*np.pi*(1000+500*i)*times)*2**-20 for i in range(columns)])
    if kind == 'pcm24':
        integers = np.rint(samples * 2**23).astype(np.int32)
        packed = np.stack([integers & 255, (integers >> 8) & 255, (integers >> 16) & 255], axis=-1).astype(np.uint8)
        with wave.open(str(path), 'wb') as writer:
            writer.setparams((columns, 3, rate, 0, 'NONE', ''))
            writer.writeframes(packed.tobytes())
    else:
        wavfile.write(path, rate, samples.astype(kind))
    return decode_wav(path).samples


def imported_job(path, *, columns=1, config=None, mapping=None):
    identity = str(uuid4())
    config = config or {'config_id': 11, 'selected_channels': [0], 'channels': [
        {'channel_index': 0, 'spl_weighting': 'Z', 'spl_upper_enabled': True, 'spl_upper_limit': 100.0}]}
    job = _job(config=config, mapping=mapping, channel_count=columns, audio_dtype='<f8')
    reference = PersistedAudioReference(
        reference_id=identity, audio_data_id=identity,
        storage_kind=AudioStorageKind.IMPORTED_FINAL_AUDIO,
        absolute_path=str(path.resolve()), sample_rate_hz=48000,
        channel_count=columns, frame_count=48000, dtype='<f8',
        content_revision=f'{identity}:{path.stat().st_size}',
    )
    return replace(job, audio=reference, execution_timeout_seconds=60.0)


@pytest.mark.parametrize('kind', ['pcm24', 'float32', 'float64'])
def test_real_import_loader_and_engine_preserve_low_tone(tmp_path, kind):
    path = tmp_path / 'archive.wav'
    direct = write_tone(path, kind)
    job = imported_job(path)
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    np.testing.assert_array_equal(loaded.samples, direct)
    assert loaded.samples.dtype == np.dtype('float64')
    executor = ProductionAnalysisExecutor()
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        expected = 20*np.log10(np.sqrt(np.mean(direct[:, 0]**2))/2e-5)
        assert result['channel_results'][0]['spl_db'] == pytest.approx(expected, abs=.1)
    finally:
        executor.close()
        loaded.close()


def test_real_process_transport_retains_import_precision(tmp_path):
    path = tmp_path / 'archive.wav'
    direct = write_tone(path, 'float64')
    job = imported_job(path)
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    executor = ProcessIsolatedAnalysisExecutor(process_count=1)
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        expected = 20*np.log10(np.sqrt(np.mean(direct[:, 0]**2))/2e-5)
        assert result['channel_results'][0]['spl_db'] == pytest.approx(expected, abs=.1)
    finally:
        executor.close()
        loaded.close()


def import_mapping():
    return {'schema': 'audio-import-map/v2', 'source_channel_count': 3, 'assignments': [
        {'wav_channel_index': 0, 'mode': 'chart', 'channel_index': None},
        {'wav_channel_index': 1, 'mode': 'ignore', 'channel_index': None},
        {'wav_channel_index': 2, 'mode': 'config', 'channel_index': 7}]}


@pytest.mark.parametrize('calibration_db', [0.0, 6.0])
def test_sparse_mapping_window_and_calibration_use_target_channel(tmp_path, calibration_db):
    path = tmp_path / 'archive.wav'
    direct = write_tone(path, 'float64', columns=3)
    direct[36000:] *= 100
    wavfile.write(path, 48000, direct)
    config = {'config_id': 11, 'selected_channels': [7], 'channels': [
        {'channel_index': 7, 'spl_weighting': 'Z', 'spl_upper_enabled': True, 'spl_upper_limit': -28.0}]}
    job = imported_job(path, columns=3, config=config, mapping=import_mapping())
    job = replace(job, snapshot=replace(job.snapshot, analysis_window_start_seconds=.25,
        analysis_window_end_seconds=.75, calibration_snapshot_json=json.dumps({
            'In8': {'deviation_value': str(calibration_db)}, 'In9': {'deviation_value': '100'}})))
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    executor = ProductionAnalysisExecutor()
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        assert [c['channel_index'] for c in result['channel_results']] == [7]
        expected = 20*np.log10(np.sqrt(np.mean(direct[12000:36000, 2]**2))/2e-5) + (20*np.log10(calibration_db) if calibration_db else 0)
        assert result['channel_results'][0]['spl_db'] == pytest.approx(expected, abs=.1)
        assert result['analysis_window'] == [.25, .75]
        assert set(result['chart_payload']) == {'7', '8'}
        assert result['config_payload']['selected_channels'] == [8, 7]
        assert result['review']['import']['display_channel_labels']['8'] == '仅图表 / WAV 1'
        for channel, frequency in [('7', 2000), ('8', 1000)]:
            fft = result['chart_payload'][channel]['fft']
            peak = np.argmax(fft['levels_db'])
            assert fft['frequencies_hz'][peak] == pytest.approx(frequency, abs=15)
        assert result['limit_pass'] is (calibration_db == 0.0)
        assert max(result['chart_payload']['8']['spl']['values_db']) < -29
    finally:
        executor.close()
        loaded.close()


def test_mapped_ai_uses_original_column_and_reports_target_identity(tmp_path):
    path = tmp_path / 'archive.wav'
    write_tone(path, 'float64', columns=3)
    config = {'config_id': 11, 'selected_channels': [7], 'channels': [{'channel_index': 7, 'spl_weighting': 'Z'}]}
    job = imported_job(path, columns=3, config=config, mapping=import_mapping())
    job = replace(job, snapshot=replace(job.snapshot, ai_toolchain=_toolchain(execution_mode='resident_process')))
    runtime = _ResidentAiRuntime(lambda kwargs: {'module_status': 'success', 'response_payload': {
        'request_id': kwargs['request_id'], 'ai_result': {'ai_label': 'FAIL', 'score': .1,
        'extra': {'channel_index': 2}}}})
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    executor = ProductionAnalysisExecutor(ai_runtime=runtime)
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        segment = runtime.calls[0]['request_payload']['segments'][0]
        assert segment['channels'] == [2]
        assert segment['source_channels'] == [7]
        assert segment['channel_map'] == [{'logical_channel': 2, 'source_channel': 7}]
        assert result['ai_pass'] is False
        ai_row = next(row for row in result['summary_payload']['rows'] if row['item_key'] == 'ai')
        assert ai_row['channel_index'] == 7
    finally:
        executor.close()
        loaded.close()


def test_import_above_nyquist_constraint_is_unavailable_not_pass(tmp_path):
    path = tmp_path / 'archive.wav'
    write_tone(path, 'float64', columns=3)
    config = {'config_id': 11, 'selected_channels': [7], 'channels': [{
        'channel_index': 7, 'spl_weighting': 'Z', 'fft_band_limits': [
            {'freq': 25000., 'lower': -100., 'upper': 100.}, {'freq': 30000., 'lower': -100., 'upper': 100.}]}]}
    job = imported_job(path, columns=3, config=config, mapping=import_mapping())
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    executor = ProductionAnalysisExecutor()
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        row = next(row for row in result['summary_payload']['rows'] if row['item_key'] == 'fft')
        assert row['status'] == '无结果'
        assert result['limit_pass'] is False
    finally:
        executor.close()
        loaded.close()


@pytest.mark.parametrize('configured_ai', [False, True])
def test_all_chart_mapping_keeps_charts_without_judgment_channels(tmp_path, configured_ai):
    path = tmp_path / 'archive.wav'
    write_tone(path, 'float64', columns=3)
    mapping = import_mapping()
    mapping['assignments'][2] = {'wav_channel_index': 2, 'mode': 'chart', 'channel_index': None}
    job = imported_job(path, columns=3, mapping=mapping)
    if configured_ai:
        job = replace(job, snapshot=replace(job.snapshot, ai_toolchain=_toolchain(execution_mode='resident_process')))
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    runtime = _ResidentAiRuntime(lambda kwargs: pytest.fail('chart-only channels cannot enter AI'))
    executor = ProductionAnalysisExecutor(ai_runtime=runtime)
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        assert result['channel_results'] == []
        assert set(result['chart_payload']) == {'1', '2'}
        assert runtime.calls == []
    finally:
        executor.close()
        loaded.close()


def test_import_frozen_golden_resamples_at_source_rate_through_real_engine(tmp_path):
    from app.service.analysis.golden_reference import freeze_golden_artifact
    path = tmp_path / 'archive.wav'
    write_tone(path, 'float64', columns=3)
    golden = tmp_path / 'golden.wav'
    wavfile.write(golden, 8000, (np.sin(2*np.pi*1000*np.arange(8000)/8000)*1000).astype(np.int16))
    channel = {'channel_index': 7, 'spl_weighting': 'Z', 'fft_golden_mode': 'absolute',
        'fft_band_limits': [{'freq': 500., 'lower': -200., 'upper': 200.}, {'freq': 3000., 'lower': -200., 'upper': 200.}],
        'golden_sample_path': str(golden), 'golden_sample_source_channel': 7,
        'golden_sample_start_sample': 0, 'golden_sample_num_samples': 8000,
        'golden_sample_start_ms': 0, 'golden_sample_duration_ms': 1000, 'golden_sample_sample_rate': 8000.0}
    artifact = freeze_golden_artifact(config_id=11, channel_index=7, channel_config=channel,
        runtime_sample_rate=8000., analysis_duration=1., managed_root=str(tmp_path))
    config = {'config_id': 11, 'input_device_samplerate': 8000., 'selected_channels': [7], 'channels': [channel]}
    job = imported_job(path, columns=3, config=config, mapping=import_mapping())
    loaded = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    executor = ProductionAnalysisExecutor(frozen_golden_artifacts={(11, 7): artifact})
    golden.unlink()  # runtime must consume the frozen reference, independently of the path
    try:
        result = json.loads(decode_analysis_result_payload(executor.execute(job, loaded, _Token()).result_payload).result_payload_json)['result']
        assert result['channel_results'][0]['fft_available'] is True
        assert result['channel_results'][0]['fft_error_message'] is None
        assert next(row for row in result['summary_payload']['rows'] if row['item_key'] == 'fft')['status'] != '无结果'
    finally:
        executor.close()
        loaded.close()
