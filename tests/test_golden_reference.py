from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.analysis import golden_reference as golden_reference_module
from app.service.analysis.golden_reference import (
    GoldenReferenceCache,
    GoldenReferenceError,
)
from app.service.golden_sample_service import GoldenSampleService


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    channels: int = 1,
) -> tuple[Path, np.ndarray]:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = np.linspace(-0.5, 0.5, samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack([base + (0.1 * idx) for idx in range(channels)]).astype(
            np.float32
        )
    wavfile.write(path, sample_rate, data)
    return path, data


def _binding(
    path: Path | str,
    *,
    sample_rate: float = 48_000.0,
    num_samples: int = 144_000,
) -> dict:
    return {
        "golden_sample_path": str(path),
        "golden_sample_sample_rate": sample_rate,
        "golden_sample_num_samples": num_samples,
    }


def test_bundle_for_channel_loads_managed_mono_wav_and_applies_mic_calibration(
    tmp_path,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, golden_data = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav"
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config=_binding(golden_path),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=2.5,
    )

    assert bundle.sample_rate == 48_000.0
    np.testing.assert_allclose(bundle.signal[:64], golden_data[:64] * 2.5)


@pytest.mark.parametrize(
    ("configured_input_sample_rate", "runtime_sample_rate"),
    [
        (44_100.0, 48_000.0),
        (48_000.0, 44_100.0),
    ],
)
def test_bundle_for_channel_rejects_sample_rate_mismatch_against_configured_and_runtime(
    tmp_path,
    configured_input_sample_rate,
    runtime_sample_rate,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="采样率"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(golden_path),
            configured_input_sample_rate=configured_input_sample_rate,
            runtime_sample_rate=runtime_sample_rate,
            mic_sensitivity=1.0,
        )


def test_bundle_for_channel_rejects_staged_and_multichannel_wavs(tmp_path):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    staged_path, _ = _write_temp_wav(tmp_path / "managed" / "staged" / "gold.wav")
    multi_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "multi.wav",
        channels=2,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="受管"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(staged_path),
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=48_000.0,
            mic_sensitivity=1.0,
        )

    with pytest.raises(GoldenReferenceError, match="单声道"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(multi_path),
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=48_000.0,
            mic_sensitivity=1.0,
        )


def test_bundle_for_channel_rejects_sample_count_mismatch_against_binding_metadata(
    tmp_path,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        samples=120_000,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="长度"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config={
                **_binding(golden_path),
                "golden_sample_num_samples": 144_000,
            },
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=48_000.0,
            mic_sensitivity=1.0,
        )


def test_ensure_fft_wraps_derivation_failures_in_golden_reference_error(
    tmp_path,
    monkeypatch,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    cache = GoldenReferenceCache(golden_sample_service=service)
    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config={
            **_binding(golden_path),
            "golden_sample_num_samples": 144_000,
        },
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=1.0,
    )

    def _raising_fft(*args, **kwargs):
        raise RuntimeError("fft backend exploded")

    monkeypatch.setattr(golden_reference_module.dsp, "compute_fft_spectrum", _raising_fft)

    with pytest.raises(GoldenReferenceError, match="fft backend exploded"):
        cache.ensure_fft(bundle, nfft=4096)


def test_ensure_octave_wraps_derivation_failures_in_golden_reference_error(
    tmp_path,
    monkeypatch,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    cache = GoldenReferenceCache(golden_sample_service=service)
    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config={
            **_binding(golden_path),
            "golden_sample_num_samples": 144_000,
        },
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=1.0,
    )

    def _raising_octave(*args, **kwargs):
        raise RuntimeError("octave backend exploded")

    monkeypatch.setattr(golden_reference_module.dsp, "compute_octave_bands", _raising_octave)

    with pytest.raises(GoldenReferenceError, match="octave backend exploded"):
        cache.ensure_octave(bundle, weighting="A")


def test_cache_reuses_loaded_bundle_and_derived_fft_by_nfft_and_octave_once_per_channel(
    tmp_path,
    monkeypatch,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    golden_path_b, _ = _write_temp_wav(tmp_path / "managed" / "config_2" / "gold.wav")
    cache = GoldenReferenceCache(golden_sample_service=service)

    read_calls: list[str] = []
    real_read = golden_reference_module.wavfile.read

    def fake_read(path):
        read_calls.append(str(path))
        return real_read(path)

    fft_calls: list[tuple[float, int]] = []
    octave_calls: list[tuple[float, str]] = []

    def fake_fft(signal, sample_rate, nfft):
        fft_calls.append((float(sample_rate), int(nfft)))
        axis = np.arange((int(nfft) // 2) + 1, dtype=np.float64)
        levels = np.full(axis.shape, 1.0, dtype=np.float64)
        return axis, levels

    def fake_octave(signal, sample_rate, weighting):
        octave_calls.append((float(sample_rate), str(weighting)))
        return np.array([1000.0], dtype=np.float64), np.array([2.0], dtype=np.float64)

    monkeypatch.setattr(golden_reference_module.wavfile, "read", fake_read)
    monkeypatch.setattr(golden_reference_module.dsp, "compute_fft_spectrum", fake_fft)
    monkeypatch.setattr(golden_reference_module.dsp, "compute_octave_bands", fake_octave)

    bundle1 = cache.bundle_for_channel(
        channel_index=0,
        channel_config=_binding(golden_path),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=1.0,
    )
    bundle2 = cache.bundle_for_channel(
        channel_index=0,
        channel_config=_binding(golden_path),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=1.0,
    )
    bundle_b = cache.bundle_for_channel(
        channel_index=1,
        channel_config=_binding(golden_path_b),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=48_000.0,
        mic_sensitivity=1.0,
    )

    assert bundle1 is bundle2

    fft_ref_1 = cache.ensure_fft(bundle1, nfft=4096)
    fft_ref_2 = cache.ensure_fft(bundle1, nfft=2048)
    fft_ref_3 = cache.ensure_fft(bundle1, nfft=4096)
    fft_ref_b = cache.ensure_fft(bundle_b, nfft=4096)
    octave_ref_1 = cache.ensure_octave(bundle1, weighting="A")
    octave_ref_2 = cache.ensure_octave(bundle1, weighting="A")

    assert len(fft_ref_1[0]) == 2049
    assert len(fft_ref_2[0]) == 1025
    assert fft_ref_1[0] is fft_ref_3[0]
    assert fft_ref_b[0] is not fft_ref_1[0]
    assert octave_ref_1[0].tolist() == octave_ref_2[0].tolist()
    assert len(read_calls) == 2
    assert fft_calls == [
        (48_000.0, 4096),
        (48_000.0, 2048),
        (48_000.0, 4096),
    ]
    assert octave_calls == [(48_000.0, "A")]
