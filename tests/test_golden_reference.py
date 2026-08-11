from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pytest
from scipy import signal as scipy_signal
from scipy.io import wavfile

from app.service.analysis import golden_reference as golden_reference_module
from app.service.analysis.golden_reference import (
    FrozenGoldenArtifact,
    GoldenReferenceCache,
    GoldenReferenceError,
    freeze_golden_artifact,
)
from app.service.golden_sample_service import GoldenSampleService


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    channels: int = 1,
    amplitude: float = 0.5,
) -> tuple[Path, np.ndarray]:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = np.linspace(-amplitude, amplitude, samples, dtype=np.float32)
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


def _frozen_binding(path: Path, *, sample_rate: int = 8_000) -> dict:
    return {
        "golden_sample_path": str(path),
        "golden_sample_source_channel": 3,
        "golden_sample_start_sample": 0,
        "golden_sample_num_samples": sample_rate,
        "golden_sample_start_ms": 0,
        "golden_sample_duration_ms": 1000,
        "golden_sample_sample_rate": float(sample_rate),
    }


def _freeze_one_second_artifact(path: Path) -> FrozenGoldenArtifact:
    return freeze_golden_artifact(
        config_id=1,
        channel_index=0,
        channel_config=_frozen_binding(path),
        runtime_sample_rate=8_000.0,
        analysis_duration=1.0,
        managed_root=str(path.parent),
    )


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


def test_golden_reference_can_resample_to_import_runtime_rate(tmp_path):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, golden_data = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=48_000,
        samples=480,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config=_binding(
            golden_path,
            sample_rate=48_000.0,
            num_samples=480,
        ),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=25_600.0,
        mic_sensitivity=0.0,
        allow_runtime_resample=True,
    )

    expected = scipy_signal.resample_poly(golden_data.astype(np.float64), 16, 30)
    assert bundle.sample_rate == 25_600.0
    assert bundle.signal.size == pytest.approx(256, abs=1)
    np.testing.assert_allclose(bundle.signal, expected, rtol=1e-12, atol=1e-12)


def test_golden_reference_strict_mode_still_rejects_rate_mismatch(tmp_path):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=48_000,
        samples=480,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="采样率"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(
                golden_path,
                sample_rate=48_000.0,
                num_samples=480,
            ),
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=25_600.0,
            mic_sensitivity=0.0,
        )


def test_golden_reference_resample_mode_uses_noop_calibration_when_zero(tmp_path):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=48_000,
        samples=480,
        amplitude=0.5,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config=_binding(
            golden_path,
            sample_rate=48_000.0,
            num_samples=480,
        ),
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=25_600.0,
        mic_sensitivity=0.0,
        allow_runtime_resample=True,
    )

    assert np.max(np.abs(bundle.signal)) <= 1.0


def test_golden_reference_resample_mode_still_validates_actual_wav_sample_rate(
    tmp_path,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=44_100,
        samples=441,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="采样率"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(
                golden_path,
                sample_rate=48_000.0,
                num_samples=441,
            ),
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=25_600.0,
            mic_sensitivity=0.0,
            allow_runtime_resample=True,
        )


def test_golden_reference_resample_mode_still_validates_sample_count(tmp_path):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=48_000,
        samples=480,
    )
    cache = GoldenReferenceCache(golden_sample_service=service)

    with pytest.raises(GoldenReferenceError, match="长度"):
        cache.bundle_for_channel(
            channel_index=0,
            channel_config=_binding(
                golden_path,
                sample_rate=48_000.0,
                num_samples=960,
            ),
            configured_input_sample_rate=48_000.0,
            runtime_sample_rate=25_600.0,
            mic_sensitivity=0.0,
            allow_runtime_resample=True,
        )


def test_golden_reference_resample_mode_does_not_rewrite_managed_file_or_binding(
    tmp_path,
):
    service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    golden_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        sample_rate=48_000,
        samples=480,
    )
    original_bytes = golden_path.read_bytes()
    binding = _binding(golden_path, sample_rate=48_000.0, num_samples=480)
    original_binding = dict(binding)
    cache = GoldenReferenceCache(golden_sample_service=service)

    bundle = cache.bundle_for_channel(
        channel_index=0,
        channel_config=binding,
        configured_input_sample_rate=48_000.0,
        runtime_sample_rate=25_600.0,
        mic_sensitivity=0.0,
        allow_runtime_resample=True,
    )

    assert bundle.sample_rate == 25_600.0
    assert golden_path.read_bytes() == original_bytes
    assert binding == original_binding


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


def test_cache_reuses_loaded_bundles_and_derived_metrics(
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

    fft_calls: list[tuple[float, int, str]] = []
    octave_calls: list[tuple[float, str]] = []

    def fake_fft(signal, sample_rate, nfft, weighting="Z"):
        fft_calls.append((float(sample_rate), int(nfft), str(weighting)))
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

    fft_ref_z = cache.ensure_fft(bundle1, nfft=4096, weighting="Z")
    fft_ref_a = cache.ensure_fft(bundle1, nfft=4096, weighting="A")
    fft_ref_z_again = cache.ensure_fft(bundle1, nfft=4096, weighting="Z")
    fft_ref_2048 = cache.ensure_fft(bundle1, nfft=2048, weighting="Z")
    fft_ref_b = cache.ensure_fft(bundle_b, nfft=8192, weighting="Z")
    octave_ref_1 = cache.ensure_octave(bundle1, weighting="A")
    octave_ref_2 = cache.ensure_octave(bundle1, weighting="A")

    assert len(fft_ref_z[0]) == 2049
    assert len(fft_ref_2048[0]) == 1025
    assert fft_ref_z[0] is fft_ref_z_again[0]
    assert fft_ref_a[0] is not fft_ref_z[0]
    assert fft_ref_b[0] is not fft_ref_z[0]
    assert octave_ref_1[0].tolist() == octave_ref_2[0].tolist()
    assert len(read_calls) == 2
    assert fft_calls.count((48_000.0, 4096, "Z")) == 1
    assert fft_calls.count((48_000.0, 4096, "A")) == 1
    assert fft_calls == [
        (48_000.0, 4096, "Z"),
        (48_000.0, 4096, "A"),
        (48_000.0, 2048, "Z"),
        (48_000.0, 8192, "Z"),
    ]
    assert octave_calls == [(48_000.0, "A")]


def test_freeze_golden_artifact_rejects_truncated_wav(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    path = root / "truncated.wav"
    wavfile.write(path, 8_000, np.ones(8_000, dtype=np.int16))
    path.write_bytes(path.read_bytes()[:-2])

    with pytest.raises(GoldenReferenceError):
        _freeze_one_second_artifact(path)


def test_freeze_golden_artifact_rejects_decoder_warning(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    path = root / "warning.wav"
    wavfile.write(path, 8_000, np.ones(8_000, dtype=np.int16))
    real_read = golden_reference_module.wavfile.read

    def warning_read(source):
        warnings.warn("decoder warning", UserWarning)
        return real_read(source)

    monkeypatch.setattr(
        golden_reference_module.wavfile,
        "read",
        warning_read,
    )

    with pytest.raises(GoldenReferenceError):
        _freeze_one_second_artifact(path)


@pytest.mark.parametrize(
    ("decoded_rate", "decoded"),
    (
        (16_000, np.ones(8_000, dtype=np.int16)),
        (8_000, np.ones((8_000, 1), dtype=np.int16)),
        (8_000, np.ones(8_000, dtype=np.float32)),
        (8_000, np.ones(8_000, dtype=np.int32)),
        (8_000, np.ones(7_999, dtype=np.int16)),
    ),
    ids=(
        "sample-rate",
        "mono-shape",
        "integer-pcm-dtype",
        "sample-width-dtype",
        "sample-count",
    ),
)
def test_freeze_golden_artifact_requires_exact_header_and_decoded_facts(
    tmp_path,
    monkeypatch,
    decoded_rate,
    decoded,
):
    root = tmp_path / "managed"
    root.mkdir()
    path = root / "facts.wav"
    wavfile.write(path, 8_000, np.ones(8_000, dtype=np.int16))
    monkeypatch.setattr(
        golden_reference_module.wavfile,
        "read",
        lambda _source: (decoded_rate, decoded),
    )

    with pytest.raises(GoldenReferenceError):
        _freeze_one_second_artifact(path)


def test_freeze_golden_artifact_rejects_decoder_eof(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    path = root / "eof.wav"
    wavfile.write(path, 8_000, np.ones(8_000, dtype=np.int16))
    monkeypatch.setattr(
        golden_reference_module.wavfile,
        "read",
        lambda _source: (_ for _ in ()).throw(EOFError("decoder EOF")),
    )

    with pytest.raises(GoldenReferenceError):
        _freeze_one_second_artifact(path)


def test_frozen_bundle_rehashes_bytes_without_reopening_path(tmp_path, monkeypatch):
    import numpy as np
    from scipy.io import wavfile

    from app.service.analysis.golden_reference import (
        FrozenGoldenArtifact,
        GoldenReferenceCache,
        freeze_golden_artifact,
    )

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "committed.wav"
    wavfile.write(path, 8_000, np.zeros(8_000, dtype=np.int16))
    artifact = freeze_golden_artifact(
        config_id=1,
        channel_index=0,
        channel_config={
            "golden_sample_path": str(path),
            "golden_sample_source_channel": 0,
            "golden_sample_start_sample": 0,
            "golden_sample_num_samples": 8_000,
            "golden_sample_start_ms": 0,
            "golden_sample_duration_ms": 1000,
            "golden_sample_sample_rate": 8_000.0,
        },
        runtime_sample_rate=8_000.0,
        analysis_duration=1.0,
        managed_root=str(root),
    )
    assert type(artifact) is FrozenGoldenArtifact
    monkeypatch.setattr(
        "app.service.analysis.golden_reference._read_wav",
        lambda *_: pytest.fail("runtime path reopen"),
    )
    bundle = GoldenReferenceCache().bundle_for_channel(
        channel_index=0,
        channel_config={"_frozen_golden_artifact": artifact},
        configured_input_sample_rate=8_000.0,
        runtime_sample_rate=8_000.0,
        mic_sensitivity=0.0,
        allow_runtime_resample=False,
    )
    assert bundle.sample_rate == 8_000.0
    assert artifact.rehash() == artifact.sha256
