from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from app.service import golden_sample_service as golden_sample_module
from app.service.golden_sample_service import (
    GoldenSampleService,
    StagedGoldenSample,
)


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    channels: int = 1,
) -> tuple[Path, np.ndarray]:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = np.arange(samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack([base + (10.0 * i) for i in range(channels)]).astype(
            np.float32
        )
    wavfile.write(path, sample_rate, data)
    return path, data


def _binding(path: Path | str, *, num_samples: int = 144_000) -> dict:
    return {
        "golden_sample_path": str(path),
        "golden_sample_label": "gold.wav",
        "golden_sample_source_channel": 1,
        "golden_sample_start_sample": 24_000,
        "golden_sample_num_samples": num_samples,
        "golden_sample_start_ms": 500,
        "golden_sample_duration_ms": round(num_samples * 1000 / 48_000.0),
        "golden_sample_sample_rate": 48_000.0,
    }


def _staged(path: Path | str, *, source_channel: int = 1) -> StagedGoldenSample:
    return StagedGoldenSample(
        staged_path=str(path),
        label="gold.wav",
        source_channel=source_channel,
        start_sample=24_000,
        num_samples=144_000,
        start_ms=500,
        duration_ms=3000,
        sample_rate=48_000.0,
    )


def test_create_staged_sample_extracts_mono_clip_and_normalizes_metadata(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    source_path, source_data = _write_temp_wav(
        tmp_path / "source.wav",
        samples=200_000,
        channels=2,
    )

    staged = svc.create_staged_sample(
        source_path=str(source_path),
        source_channel=1,
        start_sample=24_000,
        num_samples=144_000,
        sample_rate=48_000.0,
    )
    saved_sample_rate, staged_data = wavfile.read(staged.staged_path)

    assert staged.label == "source.wav"
    assert staged.source_channel == 1
    assert staged.start_sample == 24_000
    assert staged.num_samples == 144_000
    assert staged.start_ms == 500
    assert staged.duration_ms == 3000
    assert saved_sample_rate == 48_000
    assert staged_data.ndim == 1
    assert staged_data.shape[0] == 144_000
    np.testing.assert_allclose(
        staged_data[:32],
        source_data[24_000 : 24_000 + 144_000, 1][:32],
    )


def test_create_staged_sample_at_zero_keeps_original_frame_zero(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    source_path, source_data = _write_temp_wav(
        tmp_path / "source-from-zero.wav",
        sample_rate=100,
        samples=20,
        channels=1,
    )

    staged = svc.create_staged_sample(
        source_path=str(source_path),
        source_channel=0,
        start_sample=0,
        num_samples=10,
        sample_rate=100.0,
    )
    saved_sample_rate, staged_data = wavfile.read(staged.staged_path)

    assert saved_sample_rate == 100
    np.testing.assert_array_equal(staged_data, source_data[:10])
    assert staged_data[0] == source_data[0]
    assert not hasattr(golden_sample_module, "SESSION_WARMUP_SECONDS")


def test_promote_staged_samples_allocates_unique_committed_paths_per_binding(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    staged_path, _ = _write_temp_wav(tmp_path / "staged.wav")

    promoted = svc.promote_staged_samples(
        config_id=17,
        staged_by_channel={
            0: _staged(staged_path, source_channel=0),
            1: _staged(staged_path, source_channel=1),
        },
    )
    promoted_paths = [Path(meta["golden_sample_path"]) for meta in promoted.values()]

    assert set(promoted) == {0, 1}
    assert promoted_paths[0] != promoted_paths[1]
    assert all(path.exists() for path in promoted_paths)
    assert all(path.resolve().is_relative_to((tmp_path / "managed").resolve()) for path in promoted_paths)

    svc.rollback_promoted_samples([str(path) for path in promoted_paths])

    assert all(not path.exists() for path in promoted_paths)


def test_promote_staged_samples_leaves_no_committed_files_when_a_stage_is_invalid(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    staged_path, _ = _write_temp_wav(tmp_path / "valid.wav")

    with pytest.raises(ValueError):
        svc.promote_staged_samples(
            config_id=9,
            staged_by_channel={
                0: _staged(staged_path),
                1: _staged(tmp_path / "missing.wav"),
            },
        )

    assert list((tmp_path / "managed").rglob("*.wav")) == []


def test_restore_imported_sample_copies_wav_into_committed_dir_and_validates(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    source_path, _ = _write_temp_wav(tmp_path / "package" / "channel_0.wav")

    restored = svc.restore_imported_sample(
        config_id=17,
        channel_index=0,
        source_path=str(source_path),
        metadata=_binding(
            source_path,
            num_samples=144_000,
        ),
    )
    restored_path = Path(restored["golden_sample_path"])

    assert restored_path.exists()
    assert restored_path != source_path
    assert restored_path.name.startswith("channel_0_")
    assert restored_path.resolve().is_relative_to(
        (tmp_path / "managed" / "config_17").resolve()
    )
    assert restored["golden_sample_label"] == "gold.wav"
    assert restored["golden_sample_source_channel"] == 1
    assert svc.validate_committed_binding(
        channel_config=restored,
        analysis_duration=3.0,
        sample_rate=48_000.0,
    ) == []


def test_restore_imported_sample_rolls_back_copied_file_on_validation_failure(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    source_path, _ = _write_temp_wav(tmp_path / "package" / "channel_0.wav")
    metadata = _binding(source_path)
    metadata["golden_sample_sample_rate"] = 44_100.0

    with pytest.raises(ValueError, match="黄金样本"):
        svc.restore_imported_sample(
            config_id=17,
            channel_index=0,
            source_path=str(source_path),
            metadata=metadata,
        )

    assert list((tmp_path / "managed").rglob("*.wav")) == []
    assert source_path.exists()


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (
            lambda metadata: metadata.__setitem__("golden_sample_sample_rate", 0),
            "采样率",
        ),
        (
            lambda metadata: metadata.__setitem__("golden_sample_sample_rate", "bad"),
            "采样率",
        ),
        (
            lambda metadata: metadata.pop("golden_sample_num_samples"),
            "长度",
        ),
        (
            lambda metadata: metadata.__setitem__("golden_sample_num_samples", "bad"),
            "长度",
        ),
    ],
)
def test_restore_imported_sample_normalizes_invalid_metadata_to_value_error(
    tmp_path,
    mutate,
    expected_fragment,
):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    source_path, _ = _write_temp_wav(tmp_path / "package" / "channel_0.wav")
    metadata = _binding(source_path)
    mutate(metadata)

    with pytest.raises(ValueError, match=expected_fragment):
        svc.restore_imported_sample(
            config_id=17,
            channel_index=0,
            source_path=str(source_path),
            metadata=metadata,
        )

    assert list((tmp_path / "managed").rglob("*.wav")) == []
    assert source_path.exists()


def test_discard_staged_samples_only_deletes_managed_staged_files(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    managed_staged_path, _ = _write_temp_wav(tmp_path / "managed" / "staged" / "clip.wav")
    external_staged_path, _ = _write_temp_wav(tmp_path / "external.wav")

    svc.discard_staged_samples([str(managed_staged_path), str(external_staged_path)])

    assert not managed_staged_path.exists()
    assert external_staged_path.exists()


def test_validate_committed_binding_accepts_valid_binding(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    managed_path, _ = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")

    errors = svc.validate_committed_binding(
        channel_config=_binding(managed_path),
        analysis_duration=3.0,
        sample_rate=48_000.0,
    )

    assert errors == []


def test_validate_committed_binding_rejects_managed_staged_root_path(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    staged_path, _ = _write_temp_wav(tmp_path / "managed" / "staged" / "gold.wav")

    errors = svc.validate_committed_binding(
        channel_config=_binding(staged_path),
        analysis_duration=3.0,
        sample_rate=48_000.0,
    )

    assert any("已提交目录" in err for err in errors)


def test_validate_committed_binding_rejects_stereo_wav(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    managed_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        channels=2,
    )

    errors = svc.validate_committed_binding(
        channel_config=_binding(managed_path),
        analysis_duration=3.0,
        sample_rate=48_000.0,
    )

    assert any("单声道" in err for err in errors)


@pytest.mark.parametrize(
    ("binding_updates", "path_factory", "expected_fragment"),
    [
        ({}, lambda root, tmp: tmp / "outside.wav", "受管目录"),
        ({"golden_sample_start_sample": -1}, lambda root, tmp: root / "gold.wav", "起始采样"),
        ({"golden_sample_source_channel": -1}, lambda root, tmp: root / "gold.wav", "源通道"),
        ({"golden_sample_sample_rate": 44_100.0}, lambda root, tmp: root / "gold.wav", "采样率"),
        ({"golden_sample_start_ms": 499}, lambda root, tmp: root / "gold.wav", "起始时间"),
        ({"golden_sample_duration_ms": 2999}, lambda root, tmp: root / "gold.wav", "时长元数据"),
    ],
)
def test_validate_committed_binding_rejects_invalid_metadata(
    tmp_path,
    binding_updates,
    path_factory,
    expected_fragment,
):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    root_file, _ = _write_temp_wav(tmp_path / "managed" / "gold.wav")
    outside_file, _ = _write_temp_wav(tmp_path / "outside.wav")
    path = path_factory(root_file.parent, tmp_path)
    if path == outside_file:
        file_path = outside_file
    else:
        file_path = root_file
    binding = _binding(file_path)
    binding.update(binding_updates)

    errors = svc.validate_committed_binding(
        channel_config=binding,
        analysis_duration=3.0,
        sample_rate=48_000.0,
    )

    assert any(expected_fragment in err for err in errors)


def test_validate_committed_binding_rejects_actual_sample_count_mismatch(tmp_path):
    svc = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    managed_path, _ = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        samples=143_999,
    )

    errors = svc.validate_committed_binding(
        channel_config=_binding(managed_path),
        analysis_duration=3.0,
        sample_rate=48_000.0,
    )

    assert any("文件长度" in err for err in errors)
