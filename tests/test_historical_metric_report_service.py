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
    ReportCanceled,
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
