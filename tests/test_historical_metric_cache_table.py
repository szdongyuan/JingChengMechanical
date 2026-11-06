import pytest

from model.historical_metric_cache_table import HistoricalMetricCacheTable


def test_historical_metric_cache_table_has_required_fields(setup_test_db):
    columns = set(HistoricalMetricCacheTable._meta.fields)

    assert {
        "cache_id",
        "audio_data_id",
        "file_path",
        "file_size",
        "file_mtime_ns",
        "record_date",
        "product_model",
        "barcode",
        "label",
        "label_source",
        "analyse_config",
        "config_hash",
        "calibration_hash",
        "algorithm_version",
        "channel_index",
        "channel_label",
        "metric_key",
        "metric_label",
        "metric_value",
        "metric_unit",
        "status",
        "error_message",
        "computed_at",
    } <= columns


def test_historical_metric_cache_table_sqlite_types_and_nullability(setup_test_db):
    cursor = setup_test_db.execute_sql(
        "PRAGMA table_info(historical_metric_cache_table)"
    )
    columns = {
        row[1]: {"type": row[2].upper(), "notnull": bool(row[3]), "pk": bool(row[5])}
        for row in cursor.fetchall()
    }

    assert columns["cache_id"] == {"type": "INTEGER", "notnull": True, "pk": True}
    assert columns["audio_data_id"] == {"type": "TEXT", "notnull": True, "pk": False}
    assert columns["file_path"] == {"type": "TEXT", "notnull": True, "pk": False}
    assert columns["file_size"] == {"type": "INTEGER", "notnull": True, "pk": False}
    assert columns["file_mtime_ns"] == {"type": "INTEGER", "notnull": True, "pk": False}
    assert columns["metric_value"] == {"type": "REAL", "notnull": False, "pk": False}
    assert columns["error_message"] == {"type": "TEXT", "notnull": False, "pk": False}
    assert columns["computed_at"] == {"type": "TEXT", "notnull": True, "pk": False}


def test_historical_metric_cache_unique_key_and_indexes(setup_test_db):
    cursor = setup_test_db.execute_sql(
        "PRAGMA index_list(historical_metric_cache_table)"
    )
    index_names = {row[1] for row in cursor.fetchall()}

    assert any("audio_data_id" in name for name in index_names)
    assert any("metric_key" in name for name in index_names)

    HistoricalMetricCacheTable.create(
        audio_data_id="a1",
        file_path="x.wav",
        file_size=10,
        file_mtime_ns=20,
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        config_hash="cfg",
        calibration_hash="cal",
        algorithm_version="2026-06-18.1",
        channel_index=0,
        channel_label="CH1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_value=60.0,
        metric_unit="dB",
        status="ok",
        computed_at="2026-06-18T10:01:00+08:00",
    )
    with pytest.raises(Exception):
        HistoricalMetricCacheTable.create(
            audio_data_id="a1",
            file_path="x.wav",
            file_size=10,
            file_mtime_ns=20,
            record_date="2026-06-18T10:00:00+08:00",
            product_model="M1",
            config_hash="cfg",
            calibration_hash="cal",
            algorithm_version="2026-06-18.1",
            channel_index=0,
            channel_label="CH1",
            metric_key="spl_peak_db",
            metric_label="声压级峰值",
            metric_value=60.0,
            metric_unit="dB",
            status="ok",
            computed_at="2026-06-18T10:01:00+08:00",
        )
