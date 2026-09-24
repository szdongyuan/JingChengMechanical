"""FFT/octave caches contain absolute dB and must not be converted twice."""
from dataclasses import replace
import json

import numpy as np
import pytest

from app.service.analysis import dsp
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.run_persistence import ProductionRunAudioAdapter
from tests.analysis_scheduler.test_imported_precision_analysis import write_tone, imported_job, import_mapping
from tests.analysis_scheduler.test_staged_production_executor import _Audio, _Token, _job


def assert_spectral_levels(chart, signal, sample_rate, channel_config):
    """Compare persisted chart values with direct DSP plus one pressure-reference conversion."""
    computations = {
        'fft': (dsp.compute_fft_spectrum(signal, sample_rate,
            channel_config.get('fft_nfft', 4096), channel_config.get('fft_weighting', 'Z')),
            'frequencies_hz'),
        'octave': (dsp.compute_octave_bands(signal, sample_rate,
            channel_config.get('oct_weighting', 'Z')), 'center_freqs_hz'),
    }
    for metric, ((axis, raw_levels), axis_name) in computations.items():
        actual = chart[metric]
        expected = np.asarray(raw_levels) - 20 * np.log10(20e-6)
        # Chart normalization may omit DC; compare each saved bin to its DSP bin.
        np.testing.assert_allclose(actual['levels_db'],
            np.interp(actual[axis_name], axis, expected), rtol=1e-6, atol=1e-4,
            err_msg=f'{metric}: absolute dB must have one reference conversion')


@pytest.mark.parametrize('metric', ['fft_magnitudes', 'octave_levels'])
def test_reusing_completed_spectral_features_keeps_absolute_levels(metric):
    rate = 48000
    samples = (np.sin(2 * np.pi * 2000 * np.arange(rate) / rate) * 2 ** -20)[:, None]
    kwargs = dict(samples=samples, sample_rate=rate,
        config={'selected_channels': [7], 'channels': [{'channel_index': 7}]},
        calibration={}, mapping={7: 0}, start_sec=0., end_sec=1.)
    executor = ProductionAnalysisExecutor()
    try:
        original = executor._build_channel_features(**kwargs)
        reused = original
        for _ in range(2):
            reused = executor._build_channel_features(**kwargs, existing_features=executor._feature_map(reused))
            np.testing.assert_array_equal(getattr(reused[0], metric), getattr(original[0], metric))
    finally:
        executor.close()


def constrained_channel(channel_index, required, *, nfft=4096):
    bands = [{'freq': 100., 'lower': -500., 'upper': 500.},
             {'freq': 4000., 'lower': -500., 'upper': 500.}] if required else []
    return {'channel_index': channel_index, 'fft_nfft': nfft, 'fft_weighting': 'Z',
        'oct_weighting': 'Z', 'fft_band_limits': bands, 'oct_band_limits': bands}


@pytest.mark.parametrize('required', [False, True])
@pytest.mark.parametrize('kind', ['pcm24', 'float64'])
def test_imported_decision_enrichment_keeps_mapped_and_chart_only_spectral_levels(tmp_path, required, kind):
    source = tmp_path / 'precision.wav'
    samples = write_tone(source, kind, columns=3)
    channel = constrained_channel(7, required)
    job = imported_job(source, columns=3, mapping=import_mapping(),
        config={'config_id': 11, 'selected_channels': [7], 'channels': [channel]})
    executor = ProductionAnalysisExecutor()
    audio = ProductionRunAudioAdapter(None).open(job.audio, _Token())
    try:
        decision = executor.execute_decision(job, audio, _Token())
        features = executor._require_bundle(decision.bundle_token).features
        assert bool(features[0].fft_magnitudes.size) is required
        assert bool(features[0].octave_levels.size) is required
        enriched = json.loads(executor.execute_enrichment(decision.bundle_token).canonical_result_json)['result']
        configs = {c['channel_index']: c for c in enriched['config_payload']['channels']}
        for logical, column in ((7, 2), (8, 0)):
            assert_spectral_levels(enriched['chart_payload'][str(logical)], samples[:, column], 48000, configs[logical])
    finally:
        executor.close()
        audio.close()


@pytest.mark.parametrize('required', [False, True])
def test_captured_pcm16_enrichment_keeps_legacy_scale_and_spectral_levels(tmp_path, required):
    audio = _Audio(sample_rate=48000, channel_count=1)
    integers = np.rint(audio.samples * 32767).astype('<i2')
    audio._samples = integers
    expected = np.where(integers >= 0, integers / 32767., integers / 32768.)[:, 0]
    job = _job(tmp_path, audio)
    config = json.loads(job.snapshot.config_snapshot_json)
    channel = dict(config['channels'][0])
    channel.update(constrained_channel(0, required, nfft=1024))
    config['channels'] = [channel]
    job = replace(job, audio=replace(job.audio, dtype='<i2'),
        snapshot=replace(job.snapshot, config_snapshot_json=json.dumps(config)))
    executor = ProductionAnalysisExecutor()
    try:
        decision = executor.execute_decision(job, audio, _Token())
        enriched = json.loads(executor.execute_enrichment(decision.bundle_token).canonical_result_json)['result']
        assert_spectral_levels(enriched['chart_payload']['0'], expected, 48000, channel)
    finally:
        executor.close()
