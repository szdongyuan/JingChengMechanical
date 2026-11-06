import json
from types import SimpleNamespace
import pytest
from app.service.offline_audio_import import ImportRequest
from app.service.offline_audio_import.snapshot import SnapshotPreparer
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone


def test_freezes_options_mapping_config_and_calibration_and_clamps_window(tmp_path):
    path = tmp_path / 'source.wav'; write_tone(path, 'float64')
    config = {'config_id': 9, 'selected_channels': [7], 'channels': [{'channel_index': 7}],
              'sample_rate': 48000, 'analysis_duration': 2}
    mic = {'In8': {'deviation_value': '2'}}
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: config),
        calibration_loader=lambda _: mic, toolchain_loader=lambda _: None)
    assignments = ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},)
    frozen = freezer.freeze(ImportRequest(str(path), 9, assignments, start_seconds=.25, duration_seconds=2))
    config['channels'][0]['spl_upper_limit'] = -100
    mic['In8']['deviation_value'] = '99'
    prepared = freezer.prepare(frozen)
    envelope = json.loads(prepared.inputs.config_snapshot_json)
    assert envelope['analysis']['window_end_seconds'] == 1.
    assert 'spl_upper_limit' not in envelope['config']['channels'][0]
    assert json.loads(prepared.inputs.calibration_snapshot_json)['In8']['deviation_value'] == '2'
    assert prepared.warnings
    assert envelope['config']['monitor_gain_db'] == 20.0


def test_duplicate_targets_are_rejected_before_acceptance(tmp_path):
    path = tmp_path / 'source.wav'; write_tone(path, 'float64', columns=2)
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: {
        'selected_channels': [7], 'channels': [{'channel_index': 7}]}),
        calibration_loader=lambda _: {}, toolchain_loader=lambda _: None)
    with pytest.raises(ValueError, match='unique|duplicate'):
        freezer.freeze(ImportRequest(str(path), 9, tuple(
            {'wav_channel_index': i, 'mode': 'config', 'channel_index': 7} for i in range(2))))


def test_real_saved_config_off_golden_defaults_require_no_golden_file(tmp_path):
    path = tmp_path / 'source.wav'; write_tone(path, 'float64')
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: {
        'selected_channels': [7], 'input_device_samplerate': 16000, 'analysis_duration': 1.,
        'channels': [{'channel_index': 7, 'fft_golden_mode': 'off', 'oct_golden_mode': 'off'}]}),
        calibration_loader=lambda _: {}, toolchain_loader=lambda _: None)
    snapshot = freezer.freeze(ImportRequest(str(path), 9,
        ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},)))
    assert freezer.prepare(snapshot).golden_artifacts == ()


def test_golden_freezes_at_original_rate_when_source_rate_differs(tmp_path):
    import numpy as np
    from scipy.io import wavfile
    from tests.test_golden_reference import _frozen_binding
    path = tmp_path / 'source.wav'; write_tone(path, 'float64')
    golden = tmp_path / 'golden.wav'
    wavfile.write(golden, 8000, np.ones(8000, dtype=np.int16))
    channel = dict(_frozen_binding(golden), channel_index=7, fft_golden_mode='upper', oct_golden_mode='off')
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: {
        'selected_channels': [7], 'input_device_samplerate': 16000, 'analysis_duration': 1., 'channels': [channel]}),
        calibration_loader=lambda _: {}, toolchain_loader=lambda _: None, golden_root=tmp_path)
    snapshot = freezer.freeze(ImportRequest(str(path), 9,
        ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},)))
    prepared = freezer.prepare(snapshot)
    assert prepared.metadata.sample_rate_hz == 48000
    assert prepared.golden_artifacts[0].sample_rate_hz == 8000
    golden.unlink()
    assert prepared.golden_artifacts[0].rehash() == prepared.golden_artifacts[0].sha256


def test_disabling_calibration_does_not_read_hardware_calibration(tmp_path):
    path = tmp_path / 'source.wav'; write_tone(path, 'float64')
    def forbidden(config):
        raise AssertionError('disabled calibration must not require hardware metadata')
    freezer = SnapshotPreparer(config_service=SimpleNamespace(load_config=lambda _: {
        'selected_channels': [7], 'channels': [{'channel_index': 7}]}),
        calibration_loader=forbidden, toolchain_loader=lambda _: None)
    snapshot = freezer.freeze(ImportRequest(str(path), 9,
        ({'wav_channel_index': 0, 'mode': 'config', 'channel_index': 7},), use_mic_calibration=False))
    assert json.loads(snapshot.inputs.calibration_snapshot_json) == {}
