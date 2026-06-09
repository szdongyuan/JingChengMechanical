import logging
import math

import numpy as np
import pytest

from app.service.analysis import imported_audio, stft_score
from app.service.analysis import judgment as judgment_module
from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.analysis.imported_audio import (
    ImportedAudioAnalysisRequest,
    ImportedChannelAssignment,
    ImportedWav,
)
from app.service.analysis.judgment import JudgmentEngine
from app.services_container import FactoryReviewWindow


def _channel_config(channel_index: int, **overrides):
    channel = {
        "channel_index": channel_index,
        "spl_lower_enabled": False,
        "spl_lower_limit": 0.0,
        "spl_upper_enabled": False,
        "spl_upper_limit": 120.0,
        "spl_average_lower_enabled": False,
        "spl_average_lower_limit": 0.0,
        "spl_average_upper_enabled": False,
        "spl_average_upper_limit": 120.0,
        "loudness_unit": "sone",
        "loudness_lower_enabled": False,
        "loudness_lower_limit": 0.0,
        "loudness_upper_enabled": False,
        "loudness_upper_limit": 60.0,
        "loudness_average_lower_enabled": False,
        "loudness_average_lower_limit": 0.0,
        "loudness_average_upper_enabled": False,
        "loudness_average_upper_limit": 60.0,
        "fft_nfft": 128,
        "fft_band_limits": [],
        "fft_golden_mode": "off",
        "fft_show_golden_overlay": False,
        "oct_band_limits": [],
        "oct_golden_mode": "off",
        "oct_show_golden_overlay": False,
        "stft_nfft": 256,
        "stft_overlap": 0.5,
    }
    channel.update(overrides)
    return channel


def _single_channel_config(
    *,
    config_id=101,
    selected_channels=None,
    analysis_window_offset_ms=0,
    analysis_duration=1.0,
    **channel_overrides,
):
    selected = [0] if selected_channels is None else list(selected_channels)
    return {
        "config_id": config_id,
        "selected_channels": selected,
        "channels": [_channel_config(0, **channel_overrides)],
        "input_device_samplerate": 48000.0,
        "analysis_window_offset_ms": analysis_window_offset_ms,
        "analysis_duration": analysis_duration,
    }


def _config_with_channels(
    *,
    config_id=101,
    selected_channels=None,
    channels=None,
):
    shaped_channels = []
    for channel in channels or [{"channel_index": 0}]:
        index = int(channel.get("channel_index", len(shaped_channels)))
        overrides = dict(channel)
        overrides.pop("channel_index", None)
        shaped_channels.append(_channel_config(index, **overrides))
    return {
        "config_id": config_id,
        "selected_channels": [0] if selected_channels is None else list(selected_channels),
        "channels": shaped_channels,
        "input_device_samplerate": 48000.0,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 1.0,
    }


class _FakeJudgmentEngine:
    def __init__(self):
        self.calls = []

    def rejudge(
        self,
        config,
        buffer,
        sample_rate,
        mic_cal,
        audio_file_path,
        start_sec,
        end_sec,
        **kwargs,
    ):
        self.calls.append(
            {
                "config": config,
                "buffer": buffer,
                "sample_rate": sample_rate,
                "mic_cal": mic_cal,
                "audio_file_path": audio_file_path,
                "start_sec": start_sec,
                "end_sec": end_sec,
                **kwargs,
            }
        )
        return JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(start_sec, end_sec),
            channel_results=[
                ChannelJudgment(
                    channel_index=channel_index,
                    spl_db=70.0,
                    spl_pass=True,
                    spl_min_db=70.0,
                    spl_max_db=70.0,
                    spl_average_db=70.0,
                    spl_average_pass=True,
                    spl_trace_available=True,
                    spl_average_available=True,
                    loudness_min_sone=1.0,
                    loudness_max_sone=1.0,
                    loudness_average_sone=1.0,
                    loudness_pass=True,
                    loudness_average_pass=True,
                    loudness_available=True,
                    fft_available=True,
                    octave_available=True,
                    fft_band_results=[],
                    oct_band_results=[],
                )
                for channel_index in config.get("selected_channels", [])
            ],
        )


def _summary_row(summary_payload, item_key):
    for row in summary_payload.get("rows", []):
        if row.get("item_key") == item_key:
            return row
    raise AssertionError(f"summary row {item_key!r} not found")


def _patch_imported_chart_backends(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl_trace",
        lambda signal, sample_rate, weighting, time_constant, *, chunk_duration: (
            [0.05],
            [70.0],
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft: (
            np.asarray([1000.0, 2000.0], dtype=np.float64),
            np.asarray([67.0, 45.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.asarray([500.0, 1000.0], dtype=np.float64),
            np.asarray([57.0, 43.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0, 1.0], [2.0, 3.0], 3.0),
    )
    monkeypatch.setattr(
        imported_audio.dsp,
        "compute_stft",
        lambda signal, sample_rate, nfft, overlap: (
            np.asarray([0.0], dtype=np.float64),
            np.asarray([100.0], dtype=np.float64),
            np.asarray([[1.0]], dtype=np.float64),
        ),
    )


def _run_import(
    monkeypatch,
    wav,
    config,
    request=None,
    *,
    mic_cal=None,
    judgment_engine=None,
):
    request = request or ImportedAudioAnalysisRequest(
        config_id=config["config_id"],
        wav_path=wav.path,
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    def fake_chart_payload(**kwargs):
        return {
            channel_index: {"spl": {}}
            for channel_index in kwargs["config"].get("selected_channels", [])
        }

    monkeypatch.setattr(imported_audio, "build_chart_payload", fake_chart_payload)
    return imported_audio.analyze_imported_wav(
        request=request,
        config=config,
        mic_cal={} if mic_cal is None else mic_cal,
        judgment_engine=judgment_engine or _FakeJudgmentEngine(),
    )


def _run_import_with_captured_configs(
    monkeypatch,
    wav,
    config,
    request,
    *,
    mic_cal=None,
):
    captured = {}
    engine = _FakeJudgmentEngine()
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    def fake_chart_payload(**kwargs):
        captured["chart"] = kwargs
        captured["chart_config"] = kwargs["config"]
        captured["chart_channel_column_map"] = kwargs.get("channel_column_map")
        captured["chart_channels_by_index"] = {
            channel["channel_index"]: channel
            for channel in kwargs["config"].get("channels", [])
        }
        return {
            channel_index: {"spl": {}}
            for channel_index in kwargs["config"].get("selected_channels", [])
        }

    monkeypatch.setattr(imported_audio, "build_chart_payload", fake_chart_payload)
    payload = imported_audio.analyze_imported_wav(
        request=request,
        config=config,
        mic_cal={} if mic_cal is None else mic_cal,
        judgment_engine=engine,
    )
    captured["payload"] = payload
    captured["judgment"] = engine.calls[0]
    captured["judgment_config"] = engine.calls[0]["config"]
    captured["display_channel_labels"] = payload.display_channel_labels
    return captured


def _run_import_and_return_shaped_channel(monkeypatch, wav, config, request):
    captured = _run_import_with_captured_configs(monkeypatch, wav, config, request)
    channel_index = request.assignments[0].channel_index
    channels_by_index = {
        channel["channel_index"]: channel
        for channel in captured["judgment_config"].get("channels", [])
    }
    return captured["payload"], channels_by_index[channel_index]


def test_normalize_int16_wav_to_float_columns():
    raw = np.array([[0, 32767], [-32768, 16384]], dtype=np.int16)

    normalized = imported_audio.normalize_wav_samples(raw)

    assert normalized.shape == (2, 2)
    assert normalized.dtype == np.float64
    assert normalized[0, 0] == 0.0
    assert normalized[0, 1] == pytest.approx(32767 / 32768.0)
    assert normalized[1, 0] == -1.0


def test_normalize_int32_wav_to_float_columns():
    raw = np.array([0, 2147483647, -2147483648], dtype=np.int32)

    normalized = imported_audio.normalize_wav_samples(raw)

    assert normalized.shape == (3, 1)
    assert normalized[0, 0] == 0.0
    assert normalized[1, 0] == pytest.approx(2147483647 / 2147483648.0)
    assert normalized[2, 0] == -1.0


def test_normalize_uint8_wav_centers_samples():
    raw = np.array([0, 128, 255], dtype=np.uint8)

    normalized = imported_audio.normalize_wav_samples(raw)

    assert normalized.shape == (3, 1)
    assert normalized[0, 0] == pytest.approx(-1.0)
    assert abs(normalized[1, 0]) < 0.01


def test_normalize_float_wav_preserves_values():
    raw = np.array([0.25, -0.5], dtype=np.float32)

    normalized = imported_audio.normalize_wav_samples(raw)

    assert normalized[:, 0].tolist() == pytest.approx([0.25, -0.5])


def test_load_wav_rejects_non_wav_extension(tmp_path):
    path = tmp_path / "input.mp3"
    path.write_bytes(b"not-a-wav")

    with pytest.raises(ValueError, match="WAV"):
        imported_audio.load_wav(str(path))


def test_load_wav_rejects_unreadable_wav(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not a valid wav")

    with pytest.raises(ValueError, match="WAV|读取"):
        imported_audio.load_wav(str(path))


@pytest.mark.parametrize("sample_rate", [0, -25600, None])
def test_load_wav_rejects_missing_or_non_positive_sample_rate(
    monkeypatch,
    tmp_path,
    sample_rate,
):
    path = tmp_path / "input.wav"
    path.write_bytes(b"stub")
    monkeypatch.setattr(
        imported_audio.wavfile,
        "read",
        lambda _: (sample_rate, np.zeros(4, dtype=np.int16)),
    )

    with pytest.raises(ValueError, match="采样率"):
        imported_audio.load_wav(str(path))


@pytest.mark.parametrize(
    "raw",
    [
        np.array([], dtype=np.int16),
        np.zeros((0, 2), dtype=np.int16),
    ],
)
def test_load_wav_rejects_zero_sample_wav(monkeypatch, tmp_path, raw):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"stub")
    monkeypatch.setattr(imported_audio.wavfile, "read", lambda _: (25600, raw))

    with pytest.raises(ValueError, match="空|样本"):
        imported_audio.load_wav(str(path))


def test_normalize_wav_rejects_unsupported_dtype():
    raw = np.asarray([1 + 2j], dtype=np.complex64)

    with pytest.raises(ValueError, match="格式|类型"):
        imported_audio.normalize_wav_samples(raw)


def test_normalize_wav_rejects_more_than_two_dimensions():
    raw = np.zeros((2, 2, 2), dtype=np.int16)

    with pytest.raises(ValueError, match="维度|通道"):
        imported_audio.normalize_wav_samples(raw)


def test_normalize_wav_rejects_zero_channel_array():
    raw = np.zeros((10, 0), dtype=np.int16)

    with pytest.raises(ValueError, match="通道"):
        imported_audio.normalize_wav_samples(raw)


def test_default_mapping_maps_selected_channels_and_chart_only_extras():
    config = {
        "selected_channels": [0, 2],
        "channels": [{"channel_index": 0}, {"channel_index": 2}],
    }

    assignments = imported_audio.default_channel_assignments(
        config,
        wav_channel_count=4,
    )

    assert assignments[0].mode == "config"
    assert assignments[0].channel_index == 0
    assert assignments[1].mode == "chart"
    assert assignments[2].mode == "config"
    assert assignments[2].channel_index == 2
    assert assignments[3].mode == "chart"


def test_duplicate_config_mapping_is_rejected():
    with pytest.raises(ValueError, match="重复"):
        imported_audio.normalize_channel_assignments(
            [
                ImportedChannelAssignment(0, "config", 0),
                ImportedChannelAssignment(1, "config", 0),
            ],
            wav_channel_count=2,
            allowed_config_channels=(0,),
        )


def test_mapping_rejects_all_ignored_channels():
    with pytest.raises(ValueError, match="至少|通道"):
        imported_audio.normalize_channel_assignments(
            [
                ImportedChannelAssignment(0, "ignore", None),
                ImportedChannelAssignment(1, "ignore", None),
            ],
            wav_channel_count=2,
            allowed_config_channels=(0,),
        )


def test_mapping_rejects_empty_assignment_set():
    with pytest.raises(ValueError, match="至少|通道"):
        imported_audio.normalize_channel_assignments(
            [],
            wav_channel_count=2,
            allowed_config_channels=(0,),
        )


def test_window_defaults_from_config_and_clamps_end():
    window, warnings = imported_audio.resolve_analysis_window(
        {"analysis_window_offset_ms": 500, "analysis_duration": 2.0},
        wav_duration_sec=1.0,
        requested_start_sec=None,
        requested_duration_sec=None,
    )

    assert window == (0.5, 1.0)
    assert any(w.code == "analysis_window_clamped" for w in warnings)


def test_window_short_after_clamp_warns():
    window, warnings = imported_audio.resolve_analysis_window(
        {"analysis_window_offset_ms": 1900, "analysis_duration": 1.0},
        wav_duration_sec=2.0,
        requested_start_sec=None,
        requested_duration_sec=None,
    )

    assert window == (1.9, 2.0)
    assert any(w.code == "analysis_window_short_after_clamp" for w in warnings)


@pytest.mark.parametrize(
    ("requested_start_sec", "requested_duration_sec", "message"),
    [
        (float("nan"), 1.0, "起始"),
        (-0.1, 1.0, "起始"),
        (0.0, 0.0, "时长"),
        (0.0, -1.0, "时长"),
        (0.0, float("inf"), "时长"),
    ],
)
def test_window_rejects_invalid_start_or_duration(
    requested_start_sec,
    requested_duration_sec,
    message,
):
    with pytest.raises(ValueError, match=message):
        imported_audio.resolve_analysis_window(
            {"analysis_window_offset_ms": 0, "analysis_duration": 1.0},
            wav_duration_sec=2.0,
            requested_start_sec=requested_start_sec,
            requested_duration_sec=requested_duration_sec,
        )


def test_window_rejects_start_at_or_after_wav_duration():
    with pytest.raises(ValueError, match="超出|起始"):
        imported_audio.resolve_analysis_window(
            {"analysis_window_offset_ms": 0, "analysis_duration": 1.0},
            wav_duration_sec=2.0,
            requested_start_sec=2.0,
            requested_duration_sec=0.5,
        )


def test_imported_analysis_uses_wav_sample_rate_and_skips_ai(monkeypatch):
    wav = ImportedWav(
        path="import.wav",
        sample_rate=25600.0,
        buffer=np.zeros((25600, 2), dtype=np.float64),
    )
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_window_offset_ms=250,
        analysis_duration=0.5,
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )
    captured = {}
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)
    monkeypatch.setattr(
        imported_audio,
        "build_chart_payload",
        lambda **kwargs: captured.setdefault("chart", kwargs) or {0: {"spl": {}}},
    )
    monkeypatch.setattr(
        imported_audio,
        "build_summary_payload",
        lambda **kwargs: {"overall_status": "pass", "rows": []},
    )
    engine = _FakeJudgmentEngine()

    payload = imported_audio.analyze_imported_wav(
        request=request,
        config=config,
        mic_cal={"In1": {"deviation_value": "2.0"}},
        judgment_engine=engine,
    )

    assert captured["chart"]["sample_rate"] == 25600.0
    assert captured["chart"]["analysis_window"] == (0.25, 0.75)
    assert payload.default_window == FactoryReviewWindow(0.25, 0.75)
    assert payload.fallback_window == FactoryReviewWindow(0.0, 1.0)
    assert payload.captured_duration_sec == pytest.approx(1.0)
    assert payload.manual_reanalysis_allowed is True
    assert payload.displayed_config_id == 101
    assert payload.run_id.startswith("import_101_")
    assert engine.calls[0]["run_ai"] is False
    assert engine.calls[0]["allow_golden_runtime_resample"] is True
    assert engine.calls[0]["start_sec"] == 0.25
    assert engine.calls[0]["end_sec"] == 0.75
    assert engine.calls[0]["channel_column_map"] == {0: 0}


def test_imported_analysis_disables_manual_reanalysis_for_too_short_wav(monkeypatch):
    wav = ImportedWav(
        path="import.wav",
        sample_rate=25600.0,
        buffer=np.zeros((1280, 1), dtype=np.float64),
    )
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_duration=0.05,
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )

    payload = _run_import(monkeypatch, wav, config, request)

    assert payload.captured_duration_sec == pytest.approx(0.05)
    assert payload.manual_reanalysis_allowed is False


def test_imported_analysis_passes_wav_to_config_channel_map_to_judgment(monkeypatch):
    wav = ImportedWav(
        path="import.wav",
        sample_rate=25600.0,
        buffer=np.zeros((25600, 2), dtype=np.float64),
    )
    config = _single_channel_config(config_id=101, selected_channels=[0])
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(1, "config", 0),),
    )
    engine = _FakeJudgmentEngine()
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)
    monkeypatch.setattr(imported_audio, "build_chart_payload", lambda **kwargs: {0: {"spl": {}}})
    monkeypatch.setattr(
        imported_audio,
        "build_summary_payload",
        lambda **kwargs: {"overall_status": "pass", "rows": []},
    )

    payload = imported_audio.analyze_imported_wav(
        request=request,
        config=config,
        mic_cal={},
        judgment_engine=engine,
    )

    assert engine.calls[0]["channel_column_map"] == {0: 1}
    assert payload.chart_payload


def test_chart_only_channels_are_in_chart_payload_but_not_judgment(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 2), dtype=np.float64))
    config = _single_channel_config(config_id=101, selected_channels=[0])
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
    )

    captured = _run_import_with_captured_configs(monkeypatch, wav, config, request)

    assert captured["judgment_config"]["selected_channels"] == [0]
    assert captured["chart_config"]["selected_channels"] == [0, 1]
    assert captured["chart_channels_by_index"][1]["fft_band_limits"] == []
    assert captured["chart_channels_by_index"][1]["fft_golden_mode"] == "off"
    assert captured["chart_channel_column_map"] == {0: 0, 1: 1}


def test_imported_audio_review_payload_includes_deep_copied_chart_config_for_vibration(
    monkeypatch,
):
    wav = ImportedWav(
        "import.wav",
        25600.0,
        np.zeros((1024, 2), dtype=np.float64),
    )
    config = _config_with_channels(
        config_id=101,
        selected_channels=[0, 1],
        channels=[
            {"channel_index": 0},
            {"channel_index": 1},
        ],
    )
    config["vibration_view_channel_index"] = 1
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path=wav.path,
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "config", 1),
        ),
    )

    captured = _run_import_with_captured_configs(monkeypatch, wav, config, request)
    payload = captured["payload"]

    assert payload.config_payload == captured["chart_config"]
    assert payload.config_payload is not captured["chart_config"]
    assert payload.config_payload["selected_channels"] == [0, 1]
    assert payload.config_payload["vibration_view_channel_index"] == 1

    payload.config_payload["selected_channels"].append(99)
    assert captured["chart_config"]["selected_channels"] == [0, 1]


def test_chart_only_channel_ids_do_not_collide_with_unselected_config_channels(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 2), dtype=np.float64))
    config = _config_with_channels(
        config_id=101,
        selected_channels=[0],
        channels=[
            {"channel_index": 0},
            {
                "channel_index": 1,
                "fft_band_limits": [{"freq": 1000.0, "lower": 1.0, "upper": 2.0}],
            },
        ],
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
    )

    captured = _run_import_with_captured_configs(monkeypatch, wav, config, request)

    assert captured["chart_config"]["selected_channels"] == [0, 2]
    assert captured["chart_channel_column_map"] == {0: 0, 2: 1}
    assert captured["display_channel_labels"][2] == "仅图表 / WAV 2"
    assert captured["chart_channels_by_index"][2]["fft_band_limits"] == []
    assert any(w.code == "chart_only_no_judgment" for w in captured["payload"].import_warnings)


def test_imported_analysis_adds_stft_payload_using_wav_sample_rate(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 2), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_window_offset_ms=250,
        analysis_duration=0.5,
        stft_nfft=512,
        stft_overlap=0.25,
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
    )
    captured = []

    def fake_compute_stft(signal, sample_rate, nfft, overlap):
        captured.append((signal.size, sample_rate, nfft, overlap))
        return (
            np.asarray([0.1, 0.2], dtype=np.float64),
            np.asarray([100.0, 200.0], dtype=np.float64),
            np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64),
        )

    monkeypatch.setattr(imported_audio.dsp, "compute_stft", fake_compute_stft)

    payload = _run_import(monkeypatch, wav, config, request)

    assert captured[0] == (25600, 25600.0, 512, 0.25)
    assert captured[1][1] == 25600.0
    assert captured[1][2] > 0
    assert payload.chart_payload[0]["stft"]["times_sec"] == pytest.approx([0.1, 0.2])
    assert payload.chart_payload[0]["stft"]["frequencies_hz"] == pytest.approx([100.0, 200.0])
    assert np.asarray(payload.chart_payload[0]["stft"]["magnitude_db"]).shape == (2, 2)
    chart_only_channel = next(ch for ch in payload.chart_payload if ch != 0)
    assert "stft" in payload.chart_payload[chart_only_channel]


def test_imported_analysis_adds_stft_score_with_mapped_config_and_window(monkeypatch):
    wav = ImportedWav(
        "import.wav",
        25600.0,
        np.column_stack(
            [
                np.full(25600, 0.25, dtype=np.float64),
                np.full(25600, 0.5, dtype=np.float64),
            ]
        ),
    )
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_window_offset_ms=250,
        analysis_duration=0.5,
        stft_nfft=512,
        stft_overlap=0.25,
        stft_score_summary_mode="mean",
        stft_score_loudness_reference_sone=12.5,
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(1, "config", 0),),
    )
    captured = {}

    monkeypatch.setattr(
        imported_audio.dsp,
        "compute_stft",
        lambda signal, sample_rate, nfft, overlap: (
            np.asarray([0.1], dtype=np.float64),
            np.asarray([100.0], dtype=np.float64),
            np.asarray([[1.0]], dtype=np.float64),
        ),
    )

    def fake_score(**kwargs):
        captured.update(kwargs)
        start_sec = float(kwargs["absolute_start_sec"])
        return {
            "enabled": True,
            "summary_score": 84.25,
            "times_sec": [start_sec, start_sec + 0.1],
            "scores": [85.0, 83.5],
            "summary_mode": "mean",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)

    payload = _run_import(
        monkeypatch,
        wav,
        config,
        request,
        mic_cal={"In1": {"deviation_value": "3.0"}},
    )

    assert payload.chart_payload[0]["stft_score"]["summary_score"] == 84.25
    assert _summary_row(payload.summary_payload, "stft_score")["actual_display"] == "84.25"
    assert payload.chart_payload[0]["stft_score"]["times_sec"][0] == pytest.approx(0.25)
    assert captured["sample_rate"] == 25600.0
    assert captured["stft_nfft"] == 512
    assert captured["stft_overlap"] == 0.25
    assert captured["absolute_start_sec"] == pytest.approx(0.25)
    assert captured["score_config"]["stft_score_summary_mode"] == "mean"
    assert captured["score_config"]["stft_score_loudness_reference_sone"] == pytest.approx(12.5)
    np.testing.assert_allclose(captured["signal"], np.full(12800, 1.5))
    np.testing.assert_allclose(captured["loudness_context_signal"], np.full(25600, 1.5))


def test_imported_chart_only_channel_uses_default_enabled_stft_score_config(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 2), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_window_offset_ms=250,
        analysis_duration=0.5,
        stft_nfft=512,
        stft_score_enabled=False,
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
    )
    calls = []

    monkeypatch.setattr(
        imported_audio.dsp,
        "compute_stft",
        lambda signal, sample_rate, nfft, overlap: (
            np.asarray([0.1], dtype=np.float64),
            np.asarray([100.0], dtype=np.float64),
            np.asarray([[1.0]], dtype=np.float64),
        ),
    )

    def fake_score(**kwargs):
        calls.append(kwargs)
        return {
            "enabled": bool(kwargs["score_config"]["stft_score_enabled"]),
            "summary_score": 80.0,
            "times_sec": [float(kwargs["absolute_start_sec"])],
            "scores": [80.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)

    payload = _run_import(monkeypatch, wav, config, request)

    chart_only_channel = next(ch for ch in payload.chart_payload if ch != 0)
    assert calls[0]["score_config"]["stft_score_enabled"] is False
    assert any(
        call["stft_nfft"] == 1024
        and call["score_config"]["stft_score_enabled"] is True
        for call in calls
    )
    assert payload.chart_payload[chart_only_channel]["stft_score"]["enabled"] is True
    assert payload.chart_payload[chart_only_channel]["stft_score"]["times_sec"][0] == pytest.approx(0.25)


def test_imported_stft_score_failure_keeps_stft_payload_and_adds_warning(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_window_offset_ms=250,
        analysis_duration=0.5,
        stft_nfft=512,
    )

    monkeypatch.setattr(
        imported_audio.dsp,
        "compute_stft",
        lambda signal, sample_rate, nfft, overlap: (
            np.asarray([0.1], dtype=np.float64),
            np.asarray([100.0], dtype=np.float64),
            np.asarray([[1.0]], dtype=np.float64),
        ),
    )

    def fail_score(**_kwargs):
        raise RuntimeError("score unavailable")

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fail_score)

    payload = _run_import(monkeypatch, wav, config)

    assert "stft" in payload.chart_payload[0]
    assert "stft_score" not in payload.chart_payload[0]
    assert any(
        warning.metric == "stft_score"
        and warning.code == "stft_score_unavailable"
        for warning in payload.import_warnings
    )


def test_calibration_enabled_filters_to_mapped_config_channels_only(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 2), dtype=np.float64))
    config = _single_channel_config(config_id=101, selected_channels=[0])
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
        use_mic_calibration=True,
    )

    captured = _run_import_with_captured_configs(
        monkeypatch,
        wav,
        config,
        request,
        mic_cal={
            "In1": {"deviation_value": "2.0"},
            "In2": {"deviation_value": "9.0"},
        },
    )

    assert captured["judgment"]["mic_cal"] == {"In1": {"deviation_value": "2.0"}}
    assert captured["chart"]["mic_cal"] == {"In1": {"deviation_value": "2.0"}}
    assert "In2" not in captured["chart"]["mic_cal"]


def test_calibration_disabled_passes_noop_calibration_to_judgment_and_charts(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(config_id=101, selected_channels=[0])
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
        use_mic_calibration=False,
    )

    captured = _run_import_with_captured_configs(
        monkeypatch,
        wav,
        config,
        request,
        mic_cal={"In1": {"deviation_value": "2.0"}},
    )

    assert captured["judgment"]["mic_cal"] == {}
    assert captured["chart"]["mic_cal"] == {}
    assert any(w.code == "calibration_disabled" for w in captured["payload"].import_warnings)


def test_above_nyquist_frequency_limits_are_removed_with_warning(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_band_limits=[
            {"freq": 1000.0, "lower": 20.0, "upper": 80.0},
            {"freq": 2000.0, "lower": 30.0, "upper": 90.0},
            {"freq": 20000.0, "lower": 20.0, "upper": 80.0},
        ],
        oct_band_limits=[
            {"freq": 1000.0, "lower": 20.0, "upper": 80.0},
            {"freq": 2000.0, "lower": 30.0, "upper": 90.0},
            {"freq": 16000.0, "lower": 20.0, "upper": 80.0},
        ],
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )

    result, shaped_channel = _run_import_and_return_shaped_channel(
        monkeypatch,
        wav,
        config,
        request,
    )

    assert shaped_channel["fft_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 20.0},
                {"freq": 2000.0, "limit": 30.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 80.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        },
    ]
    assert shaped_channel["oct_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 20.0},
                {"freq": 2000.0, "limit": 30.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 80.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        },
    ]
    assert any(w.code == "nyquist_skipped" for w in result.import_warnings)


def test_import_audio_filters_grouped_limits_above_nyquist(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_band_limits=[
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 90.0},
                    {"freq": 2000.0, "limit": 70.0},
                    {"freq": 20000.0, "limit": 80.0},
                ],
            }
        ],
        oct_band_limits=[
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 2000.0, "limit": 20.0},
                    {"freq": 16000.0, "limit": 30.0},
                ],
            }
        ],
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )

    result, shaped_channel = _run_import_and_return_shaped_channel(
        monkeypatch,
        wav,
        config,
        request,
    )

    assert shaped_channel["fft_band_limits"] == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
                {"freq": 2000.0, "limit": 70.0},
            ],
        }
    ]
    assert shaped_channel["oct_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 10.0},
                {"freq": 2000.0, "limit": 20.0},
            ],
        }
    ]
    warning_metrics = {
        warning.metric
        for warning in result.import_warnings
        if warning.code == "nyquist_skipped"
    }
    assert {"fft", "octave"} <= warning_metrics


@pytest.mark.parametrize(
    (
        "field_name",
        "metric",
        "summary_item_key",
        "no_evaluable_code",
    ),
    [
        (
            "fft_band_limits",
            "fft",
            "fft",
            "nyquist_no_evaluable_fft",
        ),
        (
            "oct_band_limits",
            "octave",
            "octave",
            "nyquist_no_evaluable_octave",
        ),
    ],
)
def test_import_audio_treats_grouped_empty_limits_as_disabled(
    monkeypatch,
    field_name,
    metric,
    summary_item_key,
    no_evaluable_code,
):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        **{field_name: [{"kind": "upper", "points": []}]},
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )

    result, shaped_channel = _run_import_and_return_shaped_channel(
        monkeypatch,
        wav,
        config,
        request,
    )

    assert shaped_channel[field_name] == []
    metric_warning_codes = {
        warning.code for warning in result.import_warnings if warning.metric == metric
    }
    assert "nyquist_skipped" not in metric_warning_codes
    assert no_evaluable_code not in metric_warning_codes
    assert _summary_row(result.summary_payload, summary_item_key)["status"] != "无结果"


@pytest.mark.parametrize(
    (
        "field_name",
        "metric",
        "no_evaluable_code",
        "malformed_limits",
    ),
    [
        (
            "fft_band_limits",
            "fft",
            "nyquist_no_evaluable_fft",
            [
                {
                    "kind": "ceiling",
                    "points": [
                        {"freq": 1000.0, "limit": 100.0},
                        {"freq": 20000.0, "limit": 80.0},
                    ],
                }
            ],
        ),
        (
            "oct_band_limits",
            "octave",
            "nyquist_no_evaluable_octave",
            [
                {
                    "kind": "ceiling",
                    "points": [
                        {"freq": 1000.0, "limit": 100.0},
                        {"freq": 16000.0, "limit": 80.0},
                    ],
                }
            ],
        ),
        (
            "fft_band_limits",
            "fft",
            "nyquist_no_evaluable_fft",
            [{"freq": 20000.0, "lower": 20.0}],
        ),
    ],
)
def test_import_audio_preserves_malformed_limits_without_nyquist_warnings(
    monkeypatch,
    field_name,
    metric,
    no_evaluable_code,
    malformed_limits,
):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        **{field_name: malformed_limits},
    )
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )

    result, shaped_channel = _run_import_and_return_shaped_channel(
        monkeypatch,
        wav,
        config,
        request,
    )

    assert shaped_channel[field_name] != []
    assert shaped_channel[field_name] == malformed_limits
    metric_warning_codes = {
        warning.code for warning in result.import_warnings if warning.metric == metric
    }
    assert "nyquist_skipped" not in metric_warning_codes
    assert no_evaluable_code not in metric_warning_codes


def test_grouped_limits_with_too_few_remaining_frequencies_force_no_result(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_band_limits=[
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 20000.0, "limit": 80.0},
                ],
            }
        ],
        oct_band_limits=[
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 10.0},
                    {"freq": 16000.0, "limit": 30.0},
                ],
            }
        ],
    )

    payload = _run_import(monkeypatch, wav, config)

    assert _summary_row(payload.summary_payload, "fft")["status"] == "无结果"
    assert _summary_row(payload.summary_payload, "octave")["status"] == "无结果"
    warning_codes = {warning.code for warning in payload.import_warnings}
    assert {"nyquist_no_evaluable_fft", "nyquist_no_evaluable_octave"} <= warning_codes


def test_all_fft_limits_above_nyquist_marks_fft_no_result_not_unconstrained(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_band_limits=[
            {"freq": 16000.0, "lower": 20.0, "upper": 80.0},
            {"freq": 20000.0, "lower": 20.0, "upper": 80.0},
        ],
    )

    payload = _run_import(monkeypatch, wav, config)

    row = _summary_row(payload.summary_payload, "fft")
    assert payload.judgment_payload["overall"] == "不合格"
    assert payload.judgment_payload["limit_pass"] is False
    assert payload.summary_payload["overall_status"] == "no_result"
    assert payload.summary_payload["overall_label"] == "无结果"
    assert payload.summary_payload["error_message"] == "导入 WAV 频率范围内无可评估 FFT 约束"
    assert row["status"] == "无结果"
    assert any(w.code == "nyquist_no_evaluable_fft" for w in payload.import_warnings)


def test_imported_fft_rule_no_result_keeps_measured_fft_chart(monkeypatch):
    _patch_imported_chart_backends(monkeypatch)
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_band_limits=[
            {"freq": 16000.0, "lower": 20.0, "upper": 80.0},
            {"freq": 20000.0, "lower": 20.0, "upper": 80.0},
        ],
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    payload = imported_audio.analyze_imported_wav(
        request=ImportedAudioAnalysisRequest(
            config_id=config["config_id"],
            wav_path=wav.path,
            assignments=(ImportedChannelAssignment(0, "config", 0),),
        ),
        config=config,
        mic_cal={},
        judgment_engine=_FakeJudgmentEngine(),
    )

    channel_result = payload.judgment_payload["channel_results"][0]
    assert channel_result["fft_available"] is True
    assert _summary_row(payload.summary_payload, "fft")["status"] == "无结果"
    assert payload.chart_payload[0]["fft"]["levels_db"] == [67.0, 45.0]


def test_partial_nyquist_fft_no_result_keeps_real_measured_fft_chart(monkeypatch):
    sample_rate = 25600.0
    duration_sec = 1.0
    time_axis = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    samples = (0.05 * np.sin(2.0 * np.pi * 1000.0 * time_axis)).reshape(-1, 1)
    wav = ImportedWav("import.wav", sample_rate, samples)
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        analysis_duration=duration_sec,
        fft_nfft=128,
        stft_nfft=128,
        fft_band_limits=[
            {"freq": 1000.0, "lower": 20.0, "upper": 120.0},
            {"freq": 20000.0, "lower": 20.0, "upper": 120.0},
        ],
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    payload = imported_audio.analyze_imported_wav(
        request=ImportedAudioAnalysisRequest(
            config_id=config["config_id"],
            wav_path=wav.path,
            assignments=(ImportedChannelAssignment(0, "config", 0),),
        ),
        config=config,
        mic_cal={},
        judgment_engine=JudgmentEngine(ai_service=None),
    )

    assert _summary_row(payload.summary_payload, "fft")["status"] == "无结果"
    fft_warning_codes = {
        warning.code
        for warning in payload.import_warnings
        if warning.metric == "fft"
    }
    assert {"nyquist_skipped", "nyquist_no_evaluable_fft"} <= fft_warning_codes
    fft_payload = payload.chart_payload[0]["fft"]
    frequencies_hz = fft_payload["frequencies_hz"]
    levels_db = fft_payload["levels_db"]
    assert frequencies_hz
    assert levels_db
    assert len(frequencies_hz) == len(levels_db)
    assert frequencies_hz[-1] == pytest.approx(sample_rate / 2.0)
    assert any(np.isfinite(level) for level in levels_db)


def test_all_octave_limits_above_nyquist_marks_octave_no_result_not_unconstrained(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        oct_band_limits=[
            {"freq": 16000.0, "lower": 20.0, "upper": 80.0},
            {"freq": 20000.0, "lower": 20.0, "upper": 80.0},
        ],
    )

    payload = _run_import(monkeypatch, wav, config)

    row = _summary_row(payload.summary_payload, "octave")
    assert payload.judgment_payload["overall"] == "不合格"
    assert payload.judgment_payload["limit_pass"] is False
    assert payload.summary_payload["overall_status"] == "no_result"
    assert payload.summary_payload["overall_label"] == "无结果"
    assert payload.summary_payload["error_message"] == "导入 WAV 频率范围内无可评估倍频程约束"
    assert row["status"] == "无结果"
    assert any(w.code == "nyquist_no_evaluable_octave" for w in payload.import_warnings)


def test_golden_sample_resample_adds_import_warning(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_golden_mode="absolute",
        golden_sample_sample_rate=48000.0,
    )

    payload = _run_import(monkeypatch, wav, config)

    assert any(w.code == "golden_resampled" for w in payload.import_warnings)


def test_golden_failure_adds_warning_and_only_metric_is_no_result(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_golden_mode="absolute",
        golden_sample_path="missing.wav",
        fft_band_limits=[{"freq": 1000.0, "lower": -3.0, "upper": 3.0}],
    )

    payload = _run_import(monkeypatch, wav, config)

    assert _summary_row(payload.summary_payload, "fft")["status"] == "无结果"
    assert _summary_row(payload.summary_payload, "spl")["status"] != "无结果"
    assert any(w.code == "golden_unavailable" for w in payload.import_warnings)


@pytest.mark.parametrize(
    ("metric", "mode_key", "chart_key"),
    [
        ("fft", "fft_golden_mode", "fft"),
        ("octave", "oct_golden_mode", "octave"),
    ],
)
def test_golden_dependent_chart_omission_adds_unavailable_warning(
    monkeypatch,
    tmp_path,
    metric,
    mode_key,
    chart_key,
):
    _patch_imported_chart_backends(monkeypatch)
    golden_path = tmp_path / f"invalid_{metric}.wav"
    golden_path.write_bytes(b"not managed")
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        **{
            mode_key: "absolute",
            "golden_sample_path": str(golden_path),
            "golden_sample_sample_rate": 25600.0,
            "golden_sample_num_samples": 1024,
        },
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    payload = imported_audio.analyze_imported_wav(
        request=ImportedAudioAnalysisRequest(
            config_id=config["config_id"],
            wav_path=wav.path,
            assignments=(ImportedChannelAssignment(0, "config", 0),),
        ),
        config=config,
        mic_cal={},
        judgment_engine=_FakeJudgmentEngine(),
    )

    assert chart_key not in payload.chart_payload[0]
    warnings = [
        warning
        for warning in payload.import_warnings
        if warning.code == "golden_unavailable"
        and warning.channel_index == 0
        and warning.wav_channel_index == 0
        and warning.metric == metric
    ]
    assert len(warnings) == 1


def test_fft_off_mode_overlay_failure_adds_warning_and_keeps_measured_chart(
    monkeypatch,
    tmp_path,
):
    _patch_imported_chart_backends(monkeypatch)
    golden_path = tmp_path / "gold.wav"
    golden_path.write_bytes(b"not managed")
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_golden_mode="off",
        fft_show_golden_overlay=True,
        golden_sample_path=str(golden_path),
        golden_sample_sample_rate=25600.0,
        golden_sample_num_samples=1024,
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    payload = imported_audio.analyze_imported_wav(
        request=ImportedAudioAnalysisRequest(
            config_id=config["config_id"],
            wav_path=wav.path,
            assignments=(ImportedChannelAssignment(0, "config", 0),),
        ),
        config=config,
        mic_cal={},
        judgment_engine=_FakeJudgmentEngine(),
    )

    fft_payload = payload.chart_payload[0]["fft"]
    assert fft_payload["golden_mode"] == "off"
    assert fft_payload["levels_db"] == [67.0, 45.0]
    assert "golden_overlay_levels_db" not in fft_payload
    warnings = [
        warning
        for warning in payload.import_warnings
        if warning.code == "golden_unavailable"
        and warning.channel_index == 0
        and warning.wav_channel_index == 0
        and warning.metric == "fft"
    ]
    assert len(warnings) == 1


def test_imported_summary_marks_ai_not_run(monkeypatch):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 1), dtype=np.float64))
    config = _single_channel_config(config_id=101, selected_channels=[0])

    payload = _run_import(monkeypatch, wav, config)

    ai_row = _summary_row(payload.summary_payload, "ai")
    assert ai_row["status"] == "未运行"
    assert ai_row["constrained"] is False


def test_import_warnings_are_logged(monkeypatch, caplog):
    wav = ImportedWav("import.wav", 25600.0, np.zeros((1024, 2), dtype=np.float64))
    config = _single_channel_config(config_id=101, selected_channels=[0])
    request = ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(
            ImportedChannelAssignment(0, "config", 0),
            ImportedChannelAssignment(1, "chart", None),
        ),
        use_mic_calibration=False,
    )

    with caplog.at_level(logging.INFO, logger="app.service.analysis.imported_audio"):
        payload = _run_import(monkeypatch, wav, config, request)

    warning_codes = {warning.code for warning in payload.import_warnings}
    assert "chart_only_no_judgment" in warning_codes
    assert "calibration_disabled" in warning_codes
    assert "chart_only_no_judgment" in caplog.text
    assert "calibration_disabled" in caplog.text


def test_imported_forced_fft_no_result_summary_identifies_affected_channel(
    monkeypatch,
):
    _patch_imported_chart_backends(monkeypatch)
    wav = ImportedWav("import.wav", 25600.0, np.zeros((25600, 2), dtype=np.float64))
    config = _config_with_channels(
        config_id=101,
        selected_channels=[0, 1],
        channels=[
            {
                "channel_index": 0,
                "fft_band_limits": [
                    {"freq": 16000.0, "lower": 20.0, "upper": 80.0},
                    {"freq": 20000.0, "lower": 20.0, "upper": 80.0},
                ],
            },
            {
                "channel_index": 1,
                "fft_band_limits": [
                    {"freq": 1000.0, "lower": 80.0, "upper": 90.0},
                    {"freq": 2000.0, "lower": 80.0, "upper": 90.0},
                ],
            },
        ],
    )
    monkeypatch.setattr(imported_audio, "load_wav", lambda path: wav)

    payload = imported_audio.analyze_imported_wav(
        request=ImportedAudioAnalysisRequest(
            config_id=config["config_id"],
            wav_path=wav.path,
            assignments=(
                ImportedChannelAssignment(0, "config", 0),
                ImportedChannelAssignment(1, "config", 1),
            ),
        ),
        config=config,
        mic_cal={},
        judgment_engine=JudgmentEngine(ai_service=None),
    )

    fft_row = _summary_row(payload.summary_payload, "fft")
    assert fft_row["status"] == "无结果"
    assert fft_row["channel_index"] == 0
    assert fft_row["channel_label"] == "CH1"
    assert any(
        warning.code == "nyquist_no_evaluable_fft"
        and warning.channel_index == 0
        and warning.wav_channel_index == 0
        for warning in payload.import_warnings
    )


def test_judgment_engine_run_ai_false_does_not_resolve_ai_binding():
    class AiShouldNotRun:
        def resolve_binding(self, config_id):
            raise AssertionError("AI binding should not be resolved")

    engine = JudgmentEngine(AiShouldNotRun())

    result = engine.rejudge(
        {"config_id": 101, "selected_channels": [], "channels": []},
        np.zeros((10, 1), dtype=np.float64),
        10.0,
        {},
        "import.wav",
        0.0,
        1.0,
        run_ai=False,
    )

    assert result.ai_pass is None
    assert result.ai_score is None


def test_judgment_engine_passes_runtime_resample_option_to_golden_cache(monkeypatch):
    captured = []

    class GoldenCacheStub:
        def bundle_for_channel(self, **kwargs):
            captured.append(kwargs["allow_runtime_resample"])
            return object()

        def ensure_fft(self, bundle, *, nfft):
            return (
                np.asarray([1000.0, 2000.0], dtype=np.float64),
                np.asarray([60.0, 60.0], dtype=np.float64),
            )

    monkeypatch.setattr(
        judgment_module,
        "GoldenReferenceCache",
        lambda golden_sample_service=None: GoldenCacheStub(),
    )
    monkeypatch.setattr(judgment_module.dsp, "compute_spl", lambda *args, **kwargs: 70.0)
    monkeypatch.setattr(
        judgment_module.dsp,
        "compute_spl_trace",
        lambda *args, **kwargs: ([0.0], [70.0]),
    )
    monkeypatch.setattr(judgment_module.dsp, "compute_spl_leq", lambda *args, **kwargs: 70.0)
    monkeypatch.setattr(
        judgment_module.dsp,
        "compute_loudness_trace",
        lambda *args, **kwargs: ([0.0], [1.0], 1.0),
    )
    monkeypatch.setattr(
        judgment_module.dsp,
        "compute_fft_spectrum",
        lambda *args, **kwargs: (
            np.asarray([1000.0, 2000.0], dtype=np.float64),
            np.asarray([60.0, 60.0], dtype=np.float64),
        ),
    )

    engine = JudgmentEngine(ai_service=None)
    config = _single_channel_config(
        config_id=101,
        selected_channels=[0],
        fft_golden_mode="absolute",
        fft_band_limits=[
            {"freq": 1000.0, "lower": -100.0, "upper": 100.0},
            {"freq": 2000.0, "lower": -100.0, "upper": 100.0},
        ],
    )

    engine.rejudge(
        config,
        np.zeros((100, 1), dtype=np.float64),
        100.0,
        {},
        "import.wav",
        0.0,
        1.0,
        run_ai=False,
        allow_golden_runtime_resample=True,
    )

    assert captured == [True]
