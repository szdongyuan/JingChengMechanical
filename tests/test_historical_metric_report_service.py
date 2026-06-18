import json
from types import SimpleNamespace

from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
)
from app.service.historical_metric_report_service import (
    HistoricalMetricReportService,
)
from model.audio_data_table import AudioDataTable


def _create_audio_row(**overrides):
    audio_data_id = overrides.pop("audio_data_id", "audio-1")
    payload = {
        "audio_data_id": audio_data_id,
        "file_path": overrides.pop("file_path", f"missing-{audio_data_id}.wav"),
        "product_model": overrides.pop("product_model", "M1"),
        "sample_rate": overrides.pop("sample_rate", 48000),
        "record_date": overrides.pop("record_date", "2026-06-18T10:00:00+08:00"),
        "labels": overrides.pop("labels", "OK"),
        "label_source": overrides.pop("label_source", "auto"),
        "barcode": overrides.pop("barcode", "SN-1"),
        "analyse_config": overrides.pop("analyse_config", "Config A"),
        "config_content": overrides.pop("config_content", "{}"),
    }
    payload.update(overrides)
    return AudioDataTable.create(**payload)


def _service(
    mic_cal=None,
    judgment_engine=None,
    algorithm_version=HISTORICAL_REPORT_ALGORITHM_VERSION,
):
    hss = SimpleNamespace(
        load_io_devices=lambda: {"mic_cal": {} if mic_cal is None else mic_cal}
    )
    return HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=hss,
        judgment_engine=judgment_engine,
        algorithm_version=algorithm_version,
    )


def test_report_service_reuses_audio_data_filter_semantics():
    _create_audio_row(
        audio_data_id="ok",
        product_model="M1",
        labels="OK",
        barcode="ABC",
        record_date="2026-06-18T10:00:00+08:00",
    )
    _create_audio_row(
        audio_data_id="ng",
        product_model="M2",
        labels="NG",
        barcode="DEF",
        record_date="2026-06-18T10:01:00+08:00",
    )
    _create_audio_row(
        audio_data_id="blank",
        product_model="M1",
        labels="",
        barcode="ABC-X",
        record_date="2026-06-18T10:02:00+08:00",
    )

    rows = _service().list_source_records(
        {
            "product_model": "M1",
            "start_time": "",
            "end_time": "",
            "barcode": "ABC",
            "labels": ["OK", "not_labeled"],
        }
    )

    assert [row.audio_data_id for row in rows] == ["blank", "ok"]


def test_report_service_applies_time_range_filter_semantics():
    _create_audio_row(
        audio_data_id="early",
        record_date="2026-06-18T09:59:59+08:00",
    )
    _create_audio_row(
        audio_data_id="inside",
        record_date="2026-06-18T10:30:00+08:00",
    )
    _create_audio_row(
        audio_data_id="late",
        record_date="2026-06-18T11:00:01+08:00",
    )

    rows = _service().list_source_records(
        {
            "product_model": "ALL",
            "start_time": "2026-06-18 10:00:00",
            "end_time": "2026-06-18 11:00:00",
            "barcode": "",
            "labels": ["OK", "NG", "not_labeled"],
        }
    )

    assert [row.audio_data_id for row in rows] == ["inside"]


def test_config_hash_is_deterministic_for_json_content():
    service = _service()

    left = service.config_hash(json.dumps({"b": 2, "a": 1}, ensure_ascii=False))
    right = service.config_hash(json.dumps({"a": 1, "b": 2}, ensure_ascii=False))

    assert left == right


def test_calibration_hash_changes_with_mic_calibration():
    assert _service({"In1": {"sensitivity": 1.0}}).current_calibration_hash() != (
        _service({"In1": {"sensitivity": 2.0}}).current_calibration_hash()
    )
