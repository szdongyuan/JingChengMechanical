from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
    METRIC_DEFINITIONS,
)


DEFAULT_PREFIX = "demo-normal-55-60"
DEFAULT_RECORD_COUNT = 360


@dataclass(frozen=True)
class MockNormalSeedOptions:
    record_count: int = DEFAULT_RECORD_COUNT
    prefix: str = DEFAULT_PREFIX
    wav_dir: Path | None = None
    rng_seed: int = 20260626
    clear_existing: bool = True
    clear_all_report_data: bool = False
    spl_mean: float = 55.0
    spl_std: float = 1.8
    spl_average_mean: float = 53.8
    spl_average_std: float = 1.5
    loudness_peak_mean: float = 64.0
    loudness_peak_std: float = 4.2
    loudness_mean: float = 60.0
    loudness_std: float = 3.5
    stft_score_mean: float = 82.0
    stft_score_std: float = 4.0
    vibration_frequency_mean: float = 50.0
    vibration_frequency_std: float = 1.2
    vibration_peak_mean: float = 0.057
    vibration_peak_std: float = 0.006
    vibration_peak_factor_mean: float = 1.77
    vibration_peak_factor_std: float = 0.18
    vibration_rms_mean: float = 0.032
    vibration_rms_std: float = 0.004


def seed_mock_normal_report_data(
    options: MockNormalSeedOptions | None = None,
) -> dict[str, object]:
    from model.audio_data_table import AudioDataTable
    from model.base_mode import db
    from model.historical_metric_cache_table import HistoricalMetricCacheTable

    options = options or MockNormalSeedOptions()
    if options.record_count <= 0:
        raise ValueError("record_count must be positive")

    wav_dir = options.wav_dir or _default_wav_dir()
    wav_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(options.rng_seed)
    metric_specs = _metric_specs(options)
    metric_values = {
        spec["metric_key"]: _scaled_normal_values(
            rng,
            options.record_count,
            mean=spec["mean"],
            sample_std=spec["std"],
        )
        for spec in metric_specs
    }
    config = _mock_config()
    config_content = json.dumps(config, ensure_ascii=False, sort_keys=True)
    config_hash = _stable_json_hash(config)
    calibration_hash = _stable_json_hash(_current_mic_calibration())

    with db.atomic():
        if options.clear_all_report_data:
            _clear_all_report_data(wav_dir)
        elif options.clear_existing:
            _clear_existing(options.prefix, wav_dir)
        for index in range(options.record_count):
            audio_data_id = f"{options.prefix}-{index + 1:04d}"
            wav_path = wav_dir / f"{audio_data_id}.wav"
            _write_dummy_wav(wav_path)
            stat = wav_path.stat()
            record_date = _record_date(index)
            product_model = (
                "DEMO-NORMAL-55-60-A"
                if index < options.record_count / 2
                else "DEMO-NORMAL-55-60-B"
            )
            label = "NG" if (index + 1) % 12 == 0 else "OK"
            AudioDataTable.create(
                audio_data_id=audio_data_id,
                file_path=str(wav_path.resolve()),
                product_model=product_model,
                sample_rate=8000,
                record_date=record_date,
                labels=label,
                label_source="mock",
                barcode=f"MOCK-NORMAL-{index + 1:04d}",
                analyse_config="Mock Normal Report",
                config_content=config_content,
            )
            for spec in metric_specs:
                metric_key = spec["metric_key"]
                value = metric_values[metric_key][index]
                metric_label, metric_unit = METRIC_DEFINITIONS[metric_key]
                HistoricalMetricCacheTable.create(
                    audio_data_id=audio_data_id,
                    file_path=str(wav_path.resolve()),
                    file_size=int(stat.st_size),
                    file_mtime_ns=int(stat.st_mtime_ns),
                    record_date=record_date,
                    product_model=product_model,
                    barcode=f"MOCK-NORMAL-{index + 1:04d}",
                    label=label,
                    label_source="mock",
                    analyse_config="Mock Normal Report",
                    config_hash=config_hash,
                    calibration_hash=calibration_hash,
                    algorithm_version=HISTORICAL_REPORT_ALGORITHM_VERSION,
                    channel_index=spec["channel_index"],
                    channel_label=spec["channel_label"],
                    metric_key=metric_key,
                    metric_label=metric_label,
                    metric_value=float(value),
                    metric_unit=metric_unit,
                    status="ok",
                    error_message=None,
                    computed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                )

    summary = {
        "prefix": options.prefix,
        "record_count": options.record_count,
        "metric_row_count": options.record_count * len(metric_specs),
        "wav_dir": str(wav_dir.resolve()),
    }
    for metric_key, values in metric_values.items():
        summary[f"{metric_key}_mean"] = float(np.mean(values))
        summary[f"{metric_key}_std"] = float(np.std(values, ddof=1))
    return summary


def _metric_specs(options: MockNormalSeedOptions) -> tuple[dict[str, object], ...]:
    return (
        _metric_spec("spl_peak_db", options.spl_mean, options.spl_std, 0),
        _metric_spec(
            "spl_average_db",
            options.spl_average_mean,
            options.spl_average_std,
            0,
        ),
        _metric_spec(
            "loudness_peak_sone",
            options.loudness_peak_mean,
            options.loudness_peak_std,
            0,
        ),
        _metric_spec(
            "loudness_average_sone",
            options.loudness_mean,
            options.loudness_std,
            0,
        ),
        _metric_spec("stft_score", options.stft_score_mean, options.stft_score_std, 0),
        _metric_spec(
            "vibration_main_frequency_hz",
            options.vibration_frequency_mean,
            options.vibration_frequency_std,
            1,
        ),
        _metric_spec(
            "vibration_peak",
            options.vibration_peak_mean,
            options.vibration_peak_std,
            1,
        ),
        _metric_spec(
            "vibration_peak_factor",
            options.vibration_peak_factor_mean,
            options.vibration_peak_factor_std,
            1,
        ),
        _metric_spec(
            "vibration_rms",
            options.vibration_rms_mean,
            options.vibration_rms_std,
            1,
        ),
    )


def _metric_spec(
    metric_key: str,
    mean: float,
    std: float,
    channel_index: int,
) -> dict[str, object]:
    return {
        "metric_key": metric_key,
        "mean": float(mean),
        "std": float(std),
        "channel_index": channel_index,
        "channel_label": f"CH{channel_index + 1}",
    }


def _clear_existing(prefix: str, wav_dir: Path) -> None:
    from model.audio_data_table import AudioDataTable
    from model.historical_metric_cache_table import HistoricalMetricCacheTable

    HistoricalMetricCacheTable.delete().where(
        HistoricalMetricCacheTable.audio_data_id.startswith(prefix)
    ).execute()
    AudioDataTable.delete().where(AudioDataTable.audio_data_id.startswith(prefix)).execute()
    for path in wav_dir.glob(f"{prefix}-*.wav"):
        if path.is_file():
            path.unlink()


def _clear_all_report_data(wav_dir: Path) -> None:
    from model.audio_data_table import AudioDataTable
    from model.historical_metric_cache_table import HistoricalMetricCacheTable

    HistoricalMetricCacheTable.delete().execute()
    AudioDataTable.delete().execute()
    for path in wav_dir.glob("*.wav"):
        if path.is_file():
            path.unlink()


def _scaled_normal_values(
    rng: np.random.Generator,
    count: int,
    *,
    mean: float,
    sample_std: float,
) -> np.ndarray:
    values = rng.normal(0.0, 1.0, size=count)
    if count >= 2:
        values = values - float(np.mean(values))
        current_std = float(np.std(values, ddof=1))
        if current_std > 0.0:
            values = values / current_std
    return (values * float(sample_std)) + float(mean)


def _write_dummy_wav(path: Path) -> None:
    sample_rate = 8000
    samples = np.zeros(sample_rate // 10, dtype=np.float32)
    wavfile.write(path, sample_rate, np.column_stack([samples, samples]))


def _mock_config() -> dict[str, object]:
    return {
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.1,
        "channels": [{"channel_index": 0}, {"channel_index": 1}],
    }


def _record_date(index: int) -> str:
    start = datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    return (start + timedelta(minutes=index * 3)).isoformat(timespec="seconds")


def _current_mic_calibration() -> dict:
    from model.device_table import DeviceTable

    row = DeviceTable.select().where(DeviceTable.id == 1).first()
    if row is None or not _has_saved_device(row):
        return {}
    try:
        mic_cal = json.loads(row.mic_cal or "{}")
    except Exception:
        return {}
    return mic_cal if isinstance(mic_cal, dict) else {}


def _has_saved_device(row: object) -> bool:
    input_values = (
        getattr(row, "input_selected_device_name", None),
        getattr(row, "input_selected_device_type", None),
        getattr(row, "input_selected_device_samplerate", None),
        getattr(row, "input_channel_count", None),
    )
    output_values = (
        getattr(row, "output_selected_device_name", None),
        getattr(row, "output_selected_device_type", None),
        getattr(row, "output_selected_device_samplerate", None),
        getattr(row, "output_channel_count", None),
    )
    return all(value not in (None, "") for value in input_values) or all(
        value not in (None, "") for value in output_values
    )


def _stable_json_hash(value: object) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _default_wav_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "mock_normal_report_wavs"


def _backup_database_file() -> str | None:
    from model.base_mode import DB_PATH

    if DB_PATH == ":memory:":
        return None
    db_path = Path(DB_PATH)
    if not db_path.is_file():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(
        f"{db_path.name}.before-mock-normal-55-60-{timestamp}.bak"
    )
    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed mock normal-distribution data for the historical report.",
    )
    parser.add_argument("--db", help="Optional jingchen.db path override.")
    parser.add_argument("--records", type=int, default=DEFAULT_RECORD_COUNT)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--wav-dir", type=Path)
    parser.add_argument("--seed", type=int, default=20260626)
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument(
        "--clear-all-report-data",
        action="store_true",
        help="Delete all audio_data_table and historical_metric_cache_table rows before seeding.",
    )
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)

    if args.db:
        os.environ["APP_DB_PATH"] = args.db

    from app.service.db_connection_service import DbConnectionService
    from model.base_mode import db

    backup_path = None if args.no_backup else _backup_database_file()
    connection = DbConnectionService()
    connection.connect()
    try:
        summary = seed_mock_normal_report_data(
            MockNormalSeedOptions(
                record_count=args.records,
                prefix=args.prefix,
                wav_dir=args.wav_dir,
                rng_seed=args.seed,
                clear_existing=not args.keep_existing,
                clear_all_report_data=args.clear_all_report_data,
            )
        )
    finally:
        if not db.is_closed():
            db.close()

    if backup_path:
        summary["backup_path"] = backup_path
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
