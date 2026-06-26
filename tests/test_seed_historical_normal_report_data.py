from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_service import HistoricalMetricReportService
from model.audio_data_table import AudioDataTable
from model.historical_metric_cache_table import HistoricalMetricCacheTable
from scripts.seed_historical_normal_report_data import (
    MockNormalSeedOptions,
    seed_mock_normal_report_data,
)


def test_seed_mock_normal_report_data_creates_reusable_normal_distribution_rows(
    tmp_path,
):
    summary = seed_mock_normal_report_data(
        MockNormalSeedOptions(record_count=120, wav_dir=tmp_path / "wavs")
    )

    assert summary["record_count"] == 120
    assert summary["metric_row_count"] == 1080
    assert summary["spl_peak_db_mean"] == pytest.approx(55.0)
    assert summary["spl_peak_db_std"] == pytest.approx(1.8)
    assert summary["spl_average_db_mean"] == pytest.approx(53.8)
    assert summary["spl_average_db_std"] == pytest.approx(1.5)
    assert summary["loudness_peak_sone_mean"] == pytest.approx(64.0)
    assert summary["loudness_peak_sone_std"] == pytest.approx(4.2)
    assert summary["loudness_average_sone_mean"] == pytest.approx(60.0)
    assert summary["loudness_average_sone_std"] == pytest.approx(3.5)
    assert summary["stft_score_mean"] == pytest.approx(82.0)
    assert summary["vibration_main_frequency_hz_mean"] == pytest.approx(50.0)
    assert summary["vibration_peak_mean"] == pytest.approx(0.057)
    assert summary["vibration_peak_factor_mean"] == pytest.approx(1.77)
    assert summary["vibration_rms_mean"] == pytest.approx(0.032)
    assert AudioDataTable.select().count() == 120
    assert HistoricalMetricCacheTable.select().count() == 1080

    service = HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=SimpleNamespace(load_io_devices=lambda: {"mic_cal": {}}),
        judgment_engine=SimpleNamespace(),
    )
    result = service.generate_report(
        {
            "product_model": "ALL",
            "labels": ["OK", "NG"],
            "barcode": "MOCK-NORMAL",
        }
    )
    by_metric = {stat.metric_key: stat for stat in result.stats}

    assert result.total_records == 120
    assert set(by_metric) == {
        "spl_peak_db",
        "spl_average_db",
        "loudness_peak_sone",
        "loudness_average_sone",
        "stft_score",
        "vibration_main_frequency_hz",
        "vibration_peak",
        "vibration_peak_factor",
        "vibration_rms",
    }
    assert by_metric["spl_peak_db"].n == 120
    assert by_metric["spl_peak_db"].mean == pytest.approx(55.0)
    assert by_metric["spl_peak_db"].std == pytest.approx(1.8)
    assert by_metric["spl_average_db"].n == 120
    assert by_metric["spl_average_db"].mean == pytest.approx(53.8)
    assert by_metric["spl_average_db"].std == pytest.approx(1.5)
    assert by_metric["loudness_average_sone"].n == 120
    assert by_metric["loudness_average_sone"].mean == pytest.approx(60.0)
    assert by_metric["loudness_average_sone"].std == pytest.approx(3.5)
    assert by_metric["loudness_peak_sone"].n == 120
    assert by_metric["stft_score"].n == 120
    assert by_metric["vibration_main_frequency_hz"].n == 120
    assert by_metric["vibration_peak"].n == 120
    assert by_metric["vibration_peak_factor"].n == 120
    assert by_metric["vibration_rms"].n == 120


def test_seed_mock_normal_report_data_is_idempotent_for_prefix(tmp_path):
    options = MockNormalSeedOptions(record_count=12, wav_dir=tmp_path / "wavs")

    first = seed_mock_normal_report_data(options)
    second = seed_mock_normal_report_data(options)

    assert first["record_count"] == second["record_count"] == 12
    assert AudioDataTable.select().count() == 12
    assert HistoricalMetricCacheTable.select().count() == 108


def test_seed_mock_normal_report_data_can_clear_all_report_rows(tmp_path):
    first = MockNormalSeedOptions(
        record_count=8,
        prefix="first-demo",
        wav_dir=tmp_path / "wavs",
    )
    second = MockNormalSeedOptions(
        record_count=5,
        prefix="second-demo",
        wav_dir=tmp_path / "wavs",
        clear_all_report_data=True,
    )

    seed_mock_normal_report_data(first)
    seed_mock_normal_report_data(second)

    assert [row.audio_data_id for row in AudioDataTable.select()] == [
        f"second-demo-{index:04d}" for index in range(1, 6)
    ]
    assert HistoricalMetricCacheTable.select().count() == 45
