import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
    METRIC_DEFINITIONS,
    HistoricalMetricValue,
    ReportCanceled,
)
from app.service.historical_metric_report_statistics import (
    compute_outliers,
    compute_statistics,
)
from app.service.historical_metric_report_service import (
    HistoricalMetricReportService,
)
from model.audio_data_table import AudioDataTable
from model.historical_metric_cache_table import HistoricalMetricCacheTable


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


def _metric_row(audio_data_id, value, *, product_model="M1", metric_key="spl_peak_db"):
    return HistoricalMetricValue(
        audio_data_id=audio_data_id,
        record_date="2026-06-18T10:00:00+08:00",
        product_model=product_model,
        barcode=None,
        label="OK",
        label_source="auto",
        analyse_config="Cfg",
        channel_index=0,
        channel_label="CH1",
        metric_key=metric_key,
        metric_label="声压级峰值",
        metric_value=value,
        metric_unit="dB",
        status="ok",
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


def test_cache_calibration_hash_matches_computation_snapshot(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    first_mic_cal = {"In1": {"sensitivity": 1.0}}
    second_mic_cal = {"In1": {"sensitivity": 2.0}}
    calibration_snapshots = [first_mic_cal, second_mic_cal]

    def alternating_devices():
        mic_cal = calibration_snapshots.pop(0)
        calibration_snapshots.append(mic_cal)
        return {"mic_cal": mic_cal}

    seen_mic_cal = {}

    class FakeJudgment:
        def rejudge(self, config, buffer, sample_rate, mic_cal, *args, **kwargs):
            seen_mic_cal["judgment"] = mic_cal
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=0,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                    )
                ],
            )

    def chart_payload(**kwargs):
        seen_mic_cal["chart"] = kwargs["mic_cal"]
        return {}

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        chart_payload,
    )
    service = HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=SimpleNamespace(load_io_devices=alternating_devices),
        judgment_engine=FakeJudgment(),
    )

    service.generate_report({"labels": ["OK", "NG"]})

    row = HistoricalMetricCacheTable.get()
    assert seen_mic_cal["judgment"] is seen_mic_cal["chart"]
    assert row.calibration_hash == _stable_json_hash(seen_mic_cal["judgment"])


def test_missing_wav_writes_failed_record_cache_row():
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.1,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(config_content=json.dumps(config, ensure_ascii=False))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert result.failures[0].channel_index == -1
    assert result.failures[0].metric_key == "__record__"
    assert (
        "WAV" in result.failures[0].error_message
        or "文件" in result.failures[0].error_message
    )


def test_invalid_config_writes_failed_record_cache_row():
    _create_audio_row(config_content="{bad json")

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert result.failures[0].status == "failed"
    assert "配置" in result.failures[0].error_message


def test_invalid_selected_channels_writes_failed_row_and_continues_to_cached_record():
    bad_config = {
        "selected_channels": None,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.1,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(
        audio_data_id="bad-selected-channels",
        record_date="2026-06-18T10:01:00+08:00",
        config_content=json.dumps(bad_config),
    )
    cached_record = _create_audio_row(
        audio_data_id="cached-ok",
        record_date="2026-06-18T10:00:00+08:00",
    )
    service = _service()
    current_signature = service.source_signature_for_record(cached_record)
    HistoricalMetricCacheTable.create(
        audio_data_id=cached_record.audio_data_id,
        file_path=current_signature.file_path,
        file_size=current_signature.file_size,
        file_mtime_ns=current_signature.file_mtime_ns,
        record_date=cached_record.record_date,
        product_model=cached_record.product_model,
        barcode=cached_record.barcode,
        label=cached_record.labels,
        label_source=cached_record.label_source,
        analyse_config=cached_record.analyse_config,
        config_hash=current_signature.config_hash,
        calibration_hash=current_signature.calibration_hash,
        algorithm_version=current_signature.algorithm_version,
        channel_index=0,
        channel_label="CH1",
        metric_key="spl_peak_db",
        metric_label=METRIC_DEFINITIONS["spl_peak_db"][0],
        metric_value=60.0,
        metric_unit=METRIC_DEFINITIONS["spl_peak_db"][1],
        status="ok",
        error_message=None,
        computed_at="2026-06-18T10:05:00+08:00",
    )

    result = service.generate_report({"labels": ["OK", "NG"]})

    assert result.total_records == 2
    assert result.valid_record_count == 1
    assert result.failed_record_count == 1
    assert result.failures[0].audio_data_id == "bad-selected-channels"
    assert result.failures[0].status == "failed"
    assert result.failures[0].channel_index == -1
    assert result.failures[0].metric_key == "__record__"
    assert "配置未包含有效通道" in result.failures[0].error_message
    assert any(
        row.audio_data_id == "cached-ok" and row.status == "ok"
        for row in result.metric_rows
    )


def test_projected_runtime_wav_maps_selected_channel_to_column_zero(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [3],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 3}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    seen = {}

    class FakeJudgment:
        def rejudge(self, *args, channel_column_map=None, **kwargs):
            seen["channel_column_map"] = channel_column_map
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=3,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                        loudness_max_sone=2.0,
                        loudness_average_sone=1.5,
                    )
                ],
            )

    service = HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=SimpleNamespace(load_io_devices=lambda: {"mic_cal": {}}),
        judgment_engine=FakeJudgment(),
    )
    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {3: {"stft_score": {"summary_score": 88.0}}},
    )

    result = service.generate_report({"labels": ["OK", "NG"]})

    assert seen["channel_column_map"] == {3: 0}
    assert {row.metric_key for row in result.metric_rows if row.status == "ok"} >= {
        "spl_peak_db",
        "spl_average_db",
        "loudness_peak_sone",
        "loudness_average_sone",
        "stft_score",
    }


def test_wav_channel_count_must_match_selected_channels(tmp_path):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [3],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 3}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert "通道" in result.failures[0].error_message


def test_vibration_metrics_are_extracted_for_valid_vibration_channel(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}, {"channel_index": 1}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {
            1: {
                "vibration": {
                    "overall_rms": 1.25,
                    "overall_peak": 2.5,
                    "overall_crest_factor": 2.0,
                    "dominant_frequency_hz": 60.0,
                }
            }
        },
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    by_key = {row.metric_key: row for row in result.metric_rows if row.status == "ok"}
    assert by_key["vibration_rms"].metric_label == "振动 RMS"
    assert by_key["vibration_rms"].metric_value == pytest.approx(1.25)
    assert by_key["vibration_peak"].metric_label == "振动峰值"
    assert by_key["vibration_peak"].metric_value == pytest.approx(2.5)
    assert by_key["vibration_peak_factor"].metric_label == "振动峰值因子"
    assert by_key["vibration_peak_factor"].metric_value == pytest.approx(2.0)
    assert by_key["vibration_main_frequency_hz"].metric_label == "振动主频"
    assert by_key["vibration_main_frequency_hz"].metric_unit == "Hz"
    assert by_key["vibration_main_frequency_hz"].metric_value == pytest.approx(60.0)


def test_vibration_metrics_are_omitted_without_valid_vibration_channel(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "vibration_view_channel_index": 3,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {0: {"vibration": {"overall_rms": 1.25}}},
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert all(
        not row.metric_key.startswith("vibration_")
        for row in result.metric_rows
    )


def test_acoustic_metrics_exclude_configured_vibration_channel(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}, {"channel_index": 1}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=0,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                        loudness_max_sone=2.0,
                        loudness_average_sone=1.5,
                    ),
                    ChannelJudgment(
                        channel_index=1,
                        spl_db=90.0,
                        spl_pass=True,
                        spl_max_db=91.0,
                        spl_average_db=90.5,
                        loudness_max_sone=9.0,
                        loudness_average_sone=8.5,
                    ),
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {
            0: {"stft_score": {"summary_score": 88.0}},
            1: {"stft_score": {"summary_score": 1.0}},
        },
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    acoustic_keys = {
        "spl_peak_db",
        "spl_average_db",
        "loudness_peak_sone",
        "loudness_average_sone",
        "stft_score",
    }
    assert all(
        row.channel_index != 1
        for row in result.metric_rows
        if row.metric_key in acoustic_keys
    )


def test_cache_recomputes_when_calibration_hash_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    _service({"In1": {"sensitivity": 1.0}}, judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )
    _service({"In1": {"sensitivity": 2.0}}, judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert len(calls) == 2


def test_cache_reuses_valid_rows_without_reanalysis(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 1


def test_cache_recomputes_when_file_identity_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    before = wav_path.stat()
    wavfile.write(wav_path, 1000, np.zeros((2000, 1), dtype=np.float32))
    after = wav_path.stat()
    assert (after.st_size, after.st_mtime_ns) != (
        before.st_size,
        before.st_mtime_ns,
    )
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 2


def test_cache_recomputes_when_config_hash_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    row = _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    config["analysis_duration"] = 0.25
    AudioDataTable.update(config_content=json.dumps(config)).where(
        AudioDataTable.audio_data_id == row.audio_data_id
    ).execute()
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 2


def test_cache_recomputes_and_deletes_stale_rows_when_algorithm_version_changes(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )

    _service(judgment_engine=FakeJudgment(), algorithm_version="test.v1").generate_report(
        {"labels": ["OK", "NG"]}
    )
    _service(judgment_engine=FakeJudgment(), algorithm_version="test.v2").generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert len(calls) == 2
    assert {
        row.algorithm_version for row in HistoricalMetricCacheTable.select()
    } == {"test.v2"}


def test_generate_report_raises_report_canceled_when_cancel_requested():
    _create_audio_row(audio_data_id="a1")

    with pytest.raises(ReportCanceled):
        _service().generate_report(
            {"labels": ["OK", "NG"]},
            cancel_check=lambda: True,
        )


def test_invalid_wav_read_failure_writes_sentinel_file_identity(tmp_path):
    wav_path = tmp_path / "bad.wav"
    wav_path.write_bytes(b"not a wav")
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    row = HistoricalMetricCacheTable.get()
    assert row.status == "failed"
    assert row.file_path.endswith("bad.wav")
    assert row.file_size == -1
    assert row.file_mtime_ns == -1


def test_rejudge_failure_writes_failed_row_and_continues_to_next_record(tmp_path):
    first_wav = tmp_path / "first.wav"
    second_wav = tmp_path / "second.wav"
    wavfile.write(first_wav, 1000, np.zeros((1000, 1), dtype=np.float32))
    wavfile.write(second_wav, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(
        audio_data_id="bad",
        file_path=str(first_wav),
        config_content=json.dumps(config),
    )
    _create_audio_row(
        audio_data_id="good",
        file_path=str(second_wav),
        config_content=json.dumps(config),
    )

    class FakeJudgment:
        def rejudge(self, config, buffer, sample_rate, mic_cal, audio_file_path, *args, **kwargs):
            if audio_file_path == str(first_wav):
                raise RuntimeError("judgment failed")
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)
                ],
            )

    result = _service(judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert result.failed_record_count == 1
    assert result.valid_record_count == 1
    assert any("judgment failed" in (row.error_message or "") for row in result.failures)


def test_chart_payload_failure_preserves_judgment_metrics_and_records_failure(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=0,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                    )
                ],
            )

    def fail_chart(**kwargs):
        raise RuntimeError("chart failed")

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        fail_chart,
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    ok_keys = {row.metric_key for row in result.metric_rows if row.status == "ok"}
    assert {"spl_peak_db", "spl_average_db"} <= ok_keys
    assert any("chart failed" in (row.error_message or "") for row in result.failures)


def test_lazy_backfill_real_small_wav_generates_cache_rows(tmp_path):
    sample_rate = 2000
    duration_sec = 0.5
    times = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    signal = (0.01 * np.sin(2 * np.pi * 100.0 * times)).astype(np.float32)
    wav_path = tmp_path / "small.wav"
    wavfile.write(wav_path, sample_rate, signal.reshape(-1, 1))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": duration_sec,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    ok_keys = {row.metric_key for row in result.metric_rows if row.status == "ok"}
    assert {"spl_peak_db", "spl_average_db"} <= ok_keys
    assert result.valid_record_count == 1
    assert HistoricalMetricCacheTable.select().where(
        HistoricalMetricCacheTable.status == "ok"
    ).count() >= 2


def test_generate_report_summary_counts_are_distinct_records_not_metric_rows():
    ok_record = _create_audio_row(audio_data_id="ok", labels="OK")
    ng_record = _create_audio_row(audio_data_id="ng", labels="NG")
    blank_record = _create_audio_row(audio_data_id="blank", labels="")
    service = _service()
    for record, status, metric_key in (
        (ok_record, "ok", "spl_peak_db"),
        (ok_record, "ok", "spl_average_db"),
        (ng_record, "failed", "__record__"),
        (blank_record, "skipped", "__record__"),
    ):
        current_signature = service.source_signature_for_record(record)
        HistoricalMetricCacheTable.create(
            audio_data_id=record.audio_data_id,
            file_path=current_signature.file_path,
            file_size=current_signature.file_size,
            file_mtime_ns=current_signature.file_mtime_ns,
            record_date=record.record_date,
            product_model=record.product_model,
            barcode=record.barcode,
            label=record.labels,
            label_source=record.label_source,
            analyse_config=record.analyse_config,
            config_hash=current_signature.config_hash,
            calibration_hash=current_signature.calibration_hash,
            algorithm_version=current_signature.algorithm_version,
            channel_index=-1 if metric_key == "__record__" else 0,
            channel_label="记录" if metric_key == "__record__" else "CH1",
            metric_key=metric_key,
            metric_label=(
                "记录状态"
                if metric_key == "__record__"
                else METRIC_DEFINITIONS[metric_key][0]
            ),
            metric_value=None if status != "ok" else 60.0,
            metric_unit="" if metric_key == "__record__" else METRIC_DEFINITIONS[metric_key][1],
            status=status,
            error_message="测试原因" if status != "ok" else None,
            computed_at="2026-06-18T10:05:00+08:00",
        )

    result = service.generate_report({"labels": ["OK", "NG", "not_labeled"]})

    assert result.total_records == 3
    assert result.valid_record_count == 1
    assert result.failed_record_count == 1
    assert result.skipped_record_count == 1
    assert result.label_counts == {"OK": 1, "NG": 1, "not_labeled": 1}


def test_generate_report_returns_computed_statistics_and_outliers():
    service = _service()
    for value in range(20):
        record = _create_audio_row(
            audio_data_id=f"cached-{value}",
            record_date=f"2026-06-18T10:{value:02d}:00+08:00",
            product_model="M1",
        )
        current_signature = service.source_signature_for_record(record)
        HistoricalMetricCacheTable.create(
            audio_data_id=record.audio_data_id,
            file_path=current_signature.file_path,
            file_size=current_signature.file_size,
            file_mtime_ns=current_signature.file_mtime_ns,
            record_date=record.record_date,
            product_model=record.product_model,
            barcode=record.barcode,
            label=record.labels,
            label_source=record.label_source,
            analyse_config=record.analyse_config,
            config_hash=current_signature.config_hash,
            calibration_hash=current_signature.calibration_hash,
            algorithm_version=current_signature.algorithm_version,
            channel_index=0,
            channel_label="CH1",
            metric_key="spl_peak_db",
            metric_label=METRIC_DEFINITIONS["spl_peak_db"][0],
            metric_value=float(value),
            metric_unit=METRIC_DEFINITIONS["spl_peak_db"][1],
            status="ok",
            error_message=None,
            computed_at="2026-06-18T10:05:00+08:00",
        )

    result = service.generate_report({"labels": ["OK", "NG"]})
    stat = next(item for item in result.stats if item.product_model == "M1")

    assert stat.n == 20
    assert stat.mean == pytest.approx(9.5)
    assert {row.audio_data_id for row in result.outliers} == {
        "cached-0",
        "cached-19",
    }


def test_statistics_use_sample_std_and_linear_percentiles():
    rows = [
        HistoricalMetricValue(
            audio_data_id=f"a{i}",
            record_date="2026-06-18T10:00:00+08:00",
            product_model="M1",
            barcode=None,
            label="OK",
            label_source="auto",
            analyse_config="Cfg",
            channel_index=0,
            channel_label="CH1",
            metric_key="spl_peak_db",
            metric_label="声压级峰值",
            metric_value=float(value),
            metric_unit="dB",
            status="ok",
        )
        for i, value in enumerate([1, 2, 3, 4, 5], start=1)
    ]

    stats = compute_statistics(rows)
    stat = next(item for item in stats if item.product_model == "M1")

    assert stat.n == 5
    assert stat.mean == pytest.approx(3.0)
    assert stat.std == pytest.approx(np.std([1, 2, 3, 4, 5], ddof=1))
    assert stat.p5 == pytest.approx(
        np.percentile([1, 2, 3, 4, 5], 5, method="linear")
    )
    assert stat.normal_available is True


def test_statistics_ignore_non_finite_values():
    rows = [
        _metric_row("finite-1", 1.0),
        _metric_row("finite-2", 2.0),
        _metric_row("nan", float("nan")),
        _metric_row("inf", float("inf")),
    ]

    stat = next(item for item in compute_statistics(rows) if item.product_model == "M1")

    assert stat.n == 2
    assert stat.mean == pytest.approx(1.5)


def test_statistics_suppress_normal_curve_for_small_samples():
    stat = next(
        item
        for item in compute_statistics(
            [_metric_row(f"a{i}", float(i)) for i in range(4)]
        )
        if item.product_model == "M1"
    )

    assert stat.n == 4
    assert stat.normal_available is False
    assert stat.normal_x == []
    assert stat.normal_y == []


def test_statistics_include_empty_model_bucket_and_all_models_aggregate():
    rows = [
        _metric_row("blank", 1.0, product_model=""),
        _metric_row("m1", 3.0, product_model="M1"),
    ]

    stats = compute_statistics(rows)
    by_model = {item.product_model: item for item in stats}

    assert by_model["未填写型号"].n == 1
    assert by_model["未填写型号"].mean == pytest.approx(1.0)
    assert by_model["全部型号"].n == 2
    assert by_model["全部型号"].mean == pytest.approx(2.0)


def test_statistics_keep_display_label_collisions_as_separate_groups():
    rows = [
        _metric_row("all-model-1", 100.0, product_model="全部型号"),
        _metric_row("all-model-2", 110.0, product_model="全部型号"),
        _metric_row("real-blank-label-1", 10.0, product_model="未填写型号"),
        _metric_row("real-blank-label-2", 20.0, product_model="未填写型号"),
        _metric_row("blank-model", 1.0, product_model=""),
        _metric_row("m1", 3.0, product_model="M1"),
    ]

    stats = compute_statistics(rows)
    all_model_stats = {
        stat.n: stat for stat in stats if stat.product_model == "全部型号"
    }
    blank_label_stats = {
        stat.n: stat for stat in stats if stat.product_model == "未填写型号"
    }

    assert len(stats) == 5
    assert len({stat.group_key for stat in stats}) == len(stats)
    assert set(all_model_stats) == {2, 6}
    assert all_model_stats[2].mean == pytest.approx(105.0)
    assert all_model_stats[6].mean == pytest.approx(244.0 / 6.0)
    assert set(blank_label_stats) == {1, 2}
    assert blank_label_stats[1].mean == pytest.approx(1.0)
    assert blank_label_stats[2].mean == pytest.approx(15.0)


def test_outliers_do_not_use_all_models_aggregate_groups():
    rows = [
        _metric_row(f"same-{i}", 0.0, product_model="M1")
        for i in range(19)
    ] + [_metric_row("single-high", 1000.0, product_model="全部型号")]

    assert compute_outliers(rows) == []


def test_outliers_use_three_std_and_percentile_rules():
    three_std_rows = [
        _metric_row(f"std-{i}", 10.0, product_model="STD")
        for i in range(19)
    ] + [_metric_row("std-high", 100.0, product_model="STD")]
    percentile_rows = [
        _metric_row(f"pct-{i}", float(i), product_model="PCTL")
        for i in range(20)
    ]

    outliers = compute_outliers(three_std_rows + percentile_rows)
    outlier_ids = {row.audio_data_id for row in outliers}

    assert "std-high" in outlier_ids
    assert {"pct-0", "pct-19"} <= outlier_ids


def _stable_json_hash(value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
