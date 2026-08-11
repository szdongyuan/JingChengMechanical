from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import PureWindowsPath

import numpy as np
import pytest

from app.service.analysis import dsp
from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.analysis.production_executor import (
    ProductionAnalysisExecutor,
    frozen_ai_snapshot_identity,
)
from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionStatus,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AnalysisStage,
    AudioStorageKind,
    FrozenResultObject,
    PersistedAudioReference,
    canonical_toolchain_lane_key,
)


class _Audio:
    def __init__(self, channel_count: int = 1):
        self._channel_count = channel_count
        self._samples = np.zeros(
            (48_000, channel_count),
            dtype=np.float32,
        )

    @property
    def samples(self):
        return self._samples

    @property
    def sample_rate_hz(self):
        return 48_000

    @property
    def channel_count(self):
        return self._channel_count

    @property
    def frame_count(self):
        return 48_000

    def close(self):
        pass


class _Token:
    is_cancelled = False
    reason = None

    def wait(self, timeout_seconds=None):
        return False


class _Judgment:
    def __init__(self, *, overall: str = "合格", limit_pass: bool = True):
        self.calls = []
        self.overall = overall
        self.limit_pass = limit_pass

    def rejudge(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return JudgmentResult(
            overall=self.overall,
            limit_pass=self.limit_pass,
            ai_pass=None,
            ai_score=None,
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=60.0,
                    spl_pass=True,
                    spl_min_db=58.0,
                    spl_max_db=61.0,
                    spl_average_db=59.0,
                    spl_average_pass=True,
                    spl_trace_available=True,
                    spl_average_available=True,
                    loudness_min_sone=1.0,
                    loudness_max_sone=1.2,
                    loudness_average_sone=1.1,
                    loudness_pass=True,
                    loudness_average_pass=True,
                    loudness_available=True,
                )
            ],
            analysis_window=(0.0, 1.0),
        )


def _complete_channel_result(channel_index: int = 0) -> ChannelJudgment:
    return ChannelJudgment(
        channel_index=channel_index,
        spl_db=60.0,
        spl_pass=True,
        spl_min_db=58.0,
        spl_max_db=61.0,
        spl_average_db=59.0,
        spl_average_pass=True,
        spl_trace_available=True,
        spl_trace_error_message=None,
        spl_average_available=True,
        spl_average_error_message=None,
        loudness_min_sone=1.0,
        loudness_max_sone=1.2,
        loudness_average_sone=1.1,
        loudness_pass=True,
        loudness_average_pass=True,
        loudness_available=True,
        loudness_error_message=None,
        fft_available=True,
        fft_error_message=None,
        octave_available=True,
        octave_error_message=None,
    )


class _ResultJudgment(_Judgment):
    def __init__(self, channel_results: list[ChannelJudgment]):
        super().__init__()
        self._channel_results = channel_results

    def rejudge(self, *args, **kwargs):
        result = super().rejudge(*args, **kwargs)
        result.channel_results = self._channel_results
        return result


def _toolchain(**changes):
    values = {
        "toolchain_id": "工具链-α",
        "runtime_lane_key": canonical_toolchain_lane_key("工具链-α"),
        "execution_revision": "revision-01",
        "execution_mode": "resident",
        "entrypoint": "analyze.py",
        "package_dir": "C:/工具/package",
        "params_json": '{"z":1,"a":"值"}',
        "timeout_seconds": 1.25,
    }
    values.update(changes)
    return AiToolchainSnapshot(**values)


def _job(
    *,
    toolchain=None,
    config=None,
    mapping=None,
    channel_count=1,
    audio_dtype="float32",
    current_trace_json=None,
):
    config = config or {
        "config_id": 11,
        "selected_channels": [0],
        "input_device_samplerate": 48_000.0,
        "channels": [{"channel_index": 0, "spl_weighting": "Z"}],
    }
    snapshot = AnalysisInputSnapshot(
        config_snapshot_json=json.dumps(config),
        calibration_snapshot_json=json.dumps(
            {"In1": {"deviation_value": "0.0"}}
        ),
        workorder_snapshot_json='{"sn":"A"}',
        channel_mapping_json=json.dumps(
            mapping
            or {
                "schema": "audio-channel-map/v1",
                "channel_order": [0],
            }
        ),
        analysis_window_start_seconds=0.0,
        analysis_window_end_seconds=1.0,
        algorithm_revision="legacy-v1",
        current_trace_json=current_trace_json,
        ai_toolchain=toolchain,
    )
    return AnalysisJob(
        run_id="run-1",
        analysis_attempt_id="attempt-1",
        kind=AnalysisKind.AUTOMATIC,
        audio=PersistedAudioReference(
            reference_id="audio-1",
            storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
            absolute_path=str(PureWindowsPath("C:/audio/final.wav")),
            sample_rate_hz=48_000,
            channel_count=channel_count,
            frame_count=48_000,
            dtype=audio_dtype,
            content_revision="revision-1",
        ),
        snapshot=snapshot,
        execution_timeout_seconds=2.0,
    )


def _required_loudness_config() -> dict:
    return {
        "config_id": 11,
        "selected_channels": [0],
        "input_device_samplerate": 48_000.0,
        "channels": [
            {
                "channel_index": 0,
                "spl_weighting": "Z",
                "loudness_lower_enabled": True,
                "loudness_lower_limit": 0.0,
                "loudness_upper_enabled": True,
                "loudness_upper_limit": 100.0,
                "loudness_average_lower_enabled": True,
                "loudness_average_lower_limit": 0.0,
                "loudness_average_upper_enabled": True,
                "loudness_average_upper_limit": 100.0,
            }
        ],
    }


def test_executor_uses_only_frozen_snapshot_weighting(monkeypatch):
    judgment = _Judgment()
    executor = ProductionAnalysisExecutor(judgment_engine=judgment)
    result = executor.execute(_job(), _Audio(), _Token())
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert len(judgment.calls) == 1
    args, kwargs = judgment.calls[0]
    assert args[0]["channels"][0]["spl_weighting"] == "Z"
    assert kwargs["channel_column_map"] == {0: 0}
    assert kwargs["run_ai"] is False
    assert kwargs["allow_golden_runtime_resample"] is False


def test_executor_restores_pcm16_audio_to_live_float_scale_before_all_dsp():
    class _Pcm16Audio(_Audio):
        def __init__(self):
            super().__init__()
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            live_signal = 0.25 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            self.expected = np.where(
                live_signal >= 0.0,
                np.floor(live_signal * 32767.0 + 0.5) / 32767.0,
                np.ceil(live_signal * 32768.0 - 0.5) / 32768.0,
            )
            self._samples = np.where(
                live_signal >= 0.0,
                np.floor(live_signal * 32767.0 + 0.5),
                np.ceil(live_signal * 32768.0 - 0.5),
            ).astype("<i2").reshape(-1, 1)

    audio = _Pcm16Audio()
    judgment = _Judgment()
    config = {
        "config_id": 11,
        "selected_channels": [0],
        "input_device_samplerate": 48_000.0,
        "channels": [
            {
                "channel_index": 0,
                "spl_weighting": "Z",
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "stft_color_scale_enabled": True,
                "stft_color_scale_min_db": -88.0,
                "stft_color_scale_max_db": -8.0,
            }
        ],
    }

    result = ProductionAnalysisExecutor(judgment_engine=judgment).execute(
        _job(config=config, audio_dtype="<i2"),
        audio,
        _Token(),
    )

    judgment_samples = judgment.calls[0][0][1]
    assert judgment_samples.dtype == np.float64
    np.testing.assert_allclose(
        judgment_samples[:, 0],
        audio.expected,
        rtol=0.0,
        atol=0.0,
    )

    root = dict(result.result_payload.items)
    nested = dict(root["result"].items)
    chart_payload = dict(nested["chart_payload"].items)
    channel_payload = dict(chart_payload["0"].items)
    stft_payload = dict(channel_payload["stft"].items)
    terminal_magnitude = np.asarray(stft_payload["magnitude_db"])
    assert stft_payload["stft_color_scale_enabled"] is True
    assert stft_payload["stft_color_scale_min_db"] == -88.0
    assert stft_payload["stft_color_scale_max_db"] == -8.0

    _times, _frequencies, expected_magnitude = dsp.compute_stft(
        audio.expected,
        48_000.0,
        1024,
        0.5,
    )
    time_indexes = np.linspace(
        0,
        expected_magnitude.shape[1] - 1,
        min(expected_magnitude.shape[1], 512),
        dtype=int,
    )
    frequency_indexes = np.linspace(
        0,
        expected_magnitude.shape[0] - 1,
        min(expected_magnitude.shape[0], 1025),
        dtype=int,
    )
    np.testing.assert_allclose(
        terminal_magnitude,
        expected_magnitude[np.ix_(frequency_indexes, time_indexes)],
        rtol=0.0,
        atol=1e-12,
    )


def test_executor_success_payload_has_exact_closed_judgment_result_root():
    result = ProductionAnalysisExecutor(
        judgment_engine=_Judgment()
    ).execute(_job(), _Audio(), _Token())

    assert type(result.result_payload) is FrozenResultObject
    root = dict(result.result_payload.items)
    assert set(root) == {"judgment", "result"}
    assert root["judgment"] == "PASS"
    assert type(root["result"]) is FrozenResultObject


def test_executor_attaches_persisted_current_trace_to_chart_payload():
    current_trace_json = json.dumps(
        {
            "schema": "run-current-trace/v1",
            "unit": "A",
            "samples": [[0.0, 1.25], [0.5, 1.75]],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    result = ProductionAnalysisExecutor(
        judgment_engine=_Judgment()
    ).execute(
        _job(current_trace_json=current_trace_json),
        _Audio(),
        _Token(),
    )

    nested = dict(dict(result.result_payload.items)["result"].items)
    channel_payload = dict(
        dict(nested["chart_payload"].items)["0"].items
    )
    current_trace = dict(channel_payload["current_trace"].items)
    assert current_trace["times_sec"] == (0.0, 0.5)
    assert current_trace["values_a"] == (1.25, 1.75)
    assert current_trace["unit"] == "A"


@pytest.mark.parametrize(
    ("legacy_overall", "limit_pass", "expected_judgment"),
    (
        ("合格", True, "PASS"),
        ("不合格", False, "FAIL"),
    ),
)
def test_executor_maps_established_legacy_verdict_and_nests_review_facts_under_result(
    legacy_overall,
    limit_pass,
    expected_judgment,
):
    result = ProductionAnalysisExecutor(
        judgment_engine=_Judgment(
            overall=legacy_overall,
            limit_pass=limit_pass,
        )
    ).execute(_job(), _Audio(), _Token())

    root = dict(result.result_payload.items)
    assert root["judgment"] == expected_judgment
    nested = dict(root["result"].items)
    assert set(nested) == {
        "legacy_overall",
        "limit_pass",
        "ai_pass",
        "ai_score",
        "analysis_window",
        "channel_results",
        "review",
        "displayed_config_id",
        "config_payload",
        "captured_duration_sec",
        "default_window",
        "fallback_window",
        "manual_reanalysis_allowed",
        "judgment_payload",
        "summary_payload",
        "chart_payload",
    }
    assert nested["legacy_overall"] == legacy_overall
    assert nested["limit_pass"] is limit_pass
    assert type(nested["review"]) is FrozenResultObject
    review = dict(nested["review"].items)
    assert set(review) == {
        "algorithm_revision",
        "workorder_snapshot_json",
        "workorder",
    }
    assert type(nested["summary_payload"]) is FrozenResultObject
    assert type(nested["chart_payload"]) is FrozenResultObject
    assert type(nested["config_payload"]) is FrozenResultObject


def test_executor_accepts_configured_weighting_and_rejects_mapping_drift():
    judgment = _Judgment()
    executor = ProductionAnalysisExecutor(judgment_engine=judgment)
    config = {
        "config_id": 11,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "spl_weighting": "A"}],
    }
    result = executor.execute(_job(config=config), _Audio(), _Token())
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert len(judgment.calls) == 1
    args, _kwargs = judgment.calls[0]
    assert args[0]["channels"][0]["spl_weighting"] == "A"

    with pytest.raises(AnalysisExecutionError):
        executor.execute(
            _job(
                mapping={
                    "schema": "audio-channel-map/v1",
                    "channel_order": [1],
                }
            ),
            _Audio(),
            _Token(),
        )
    assert len(judgment.calls) == 1


@pytest.mark.parametrize(
    "channel_result",
    (
        replace(_complete_channel_result(), spl_average_available=False),
        replace(
            _complete_channel_result(),
            fft_error_message="FFT backend failed",
        ),
        replace(_complete_channel_result(), spl_db=float("nan")),
        replace(_complete_channel_result(), spl_average_db=None),
        replace(
            _complete_channel_result(),
            spl_average_db=float("nan"),
        ),
        replace(_complete_channel_result(), spl_average_pass=None),
        replace(_complete_channel_result(), spl_average_pass=1),
    ),
    ids=(
        "unavailable",
        "reported-error",
        "nonfinite-spl",
        "missing-leq",
        "nonfinite-leq",
        "missing-leq-pass",
        "non-bool-leq-pass",
    ),
)
def test_executor_fails_closed_for_invalid_required_legacy_calculation(
    channel_result,
):
    executor = ProductionAnalysisExecutor(
        judgment_engine=_ResultJudgment([channel_result])
    )

    with pytest.raises(AnalysisExecutionError) as captured:
        executor.execute(_job(), _Audio(), _Token())

    assert (
        captured.value.error.category
        is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    )


def test_executor_fails_closed_when_a_selected_channel_result_is_missing():
    config = {
        "config_id": 11,
        "selected_channels": [0, 1],
        "input_device_samplerate": 48_000.0,
        "channels": [
            {"channel_index": 0, "spl_weighting": "Z"},
            {"channel_index": 1, "spl_weighting": "Z"},
        ],
    }
    executor = ProductionAnalysisExecutor(
        judgment_engine=_ResultJudgment(
            [_complete_channel_result(channel_index=0)]
        )
    )

    with pytest.raises(AnalysisExecutionError) as captured:
        executor.execute(
            _job(
                config=config,
                mapping={
                    "schema": "audio-channel-map/v1",
                    "channel_order": [0, 1],
                },
                channel_count=2,
            ),
            _Audio(channel_count=2),
            _Token(),
        )

    assert (
        captured.value.error.category
        is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    )


def test_real_judgment_engine_excludes_selected_vibration_channel_from_results():
    class _MixedAudio(_Audio):
        def __init__(self):
            super().__init__(channel_count=2)
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            signal = (
                0.1 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            ).astype(np.float32)
            self._samples = np.column_stack((signal, signal))

    config = {
        "config_id": 11,
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "input_device_samplerate": 48_000.0,
        "channels": [
            {"channel_index": 0, "spl_weighting": "Z"},
            {"channel_index": 1, "spl_weighting": "A"},
        ],
    }

    result = ProductionAnalysisExecutor().execute(
        _job(
            config=config,
            mapping={
                "schema": "audio-channel-map/v1",
                "channel_order": [0, 1],
            },
            channel_count=2,
        ),
        _MixedAudio(),
        _Token(),
    )

    root = dict(result.result_payload.items)
    nested = dict(root["result"].items)
    channel_indexes = [
        dict(item.items)["channel_index"]
        for item in nested["channel_results"]
    ]
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert channel_indexes == [0]


def test_real_judgment_engine_allows_vibration_only_empty_results():
    class _SineAudio(_Audio):
        def __init__(self):
            super().__init__()
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            self._samples = (
                0.1 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            ).astype(np.float32).reshape(-1, 1)

    config = {
        "config_id": 11,
        "selected_channels": [0],
        "vibration_view_channel_index": 0,
        "input_device_samplerate": 48_000.0,
        "channels": [{"channel_index": 0, "spl_weighting": "A"}],
    }

    result = ProductionAnalysisExecutor().execute(
        _job(config=config),
        _SineAudio(),
        _Token(),
    )

    root = dict(result.result_payload.items)
    nested = dict(root["result"].items)
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert root["judgment"] == "PASS"
    assert nested["channel_results"] == ()


def test_real_judgment_engine_dsp_failure_never_emits_pass(monkeypatch):
    class _SineAudio(_Audio):
        def __init__(self):
            super().__init__()
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            self._samples = (
                0.1 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            ).astype(np.float32).reshape(-1, 1)

    monkeypatch.setattr(
        dsp,
        "compute_spl_leq",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("forced DSP failure")
        ),
    )

    with pytest.raises(AnalysisExecutionError) as captured:
        ProductionAnalysisExecutor().execute(
            _job(),
            _SineAudio(),
            _Token(),
        )

    assert (
        captured.value.error.category
        is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    )


@pytest.mark.parametrize(
    "channel_result",
    (
        replace(_complete_channel_result(), loudness_available=False),
        replace(
            _complete_channel_result(),
            loudness_error_message="loudness backend failed",
        ),
        replace(
            _complete_channel_result(),
            loudness_average_sone=float("nan"),
        ),
        replace(_complete_channel_result(), loudness_pass=None),
    ),
    ids=("unavailable", "reported-error", "nonfinite", "missing-pass"),
)
def test_executor_fails_closed_for_invalid_configured_loudness(
    channel_result,
):
    executor = ProductionAnalysisExecutor(
        judgment_engine=_ResultJudgment([channel_result])
    )

    with pytest.raises(AnalysisExecutionError) as captured:
        executor.execute(
            _job(config=_required_loudness_config()),
            _Audio(),
            _Token(),
        )

    assert (
        captured.value.error.category
        is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    )


def test_real_judgment_engine_required_loudness_failure_fails_execution(
    monkeypatch,
):
    class _SineAudio(_Audio):
        def __init__(self):
            super().__init__()
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            self._samples = (
                0.1 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            ).astype(np.float32).reshape(-1, 1)

    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("forced loudness DSP failure")
        ),
    )

    with pytest.raises(AnalysisExecutionError) as captured:
        ProductionAnalysisExecutor().execute(
            _job(config=_required_loudness_config()),
            _SineAudio(),
            _Token(),
        )

    assert (
        captured.value.error.category
        is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    )


def test_real_judgment_engine_optional_loudness_failure_stays_optional(
    monkeypatch,
):
    class _SineAudio(_Audio):
        def __init__(self):
            super().__init__()
            time_axis = np.arange(48_000, dtype=np.float64) / 48_000.0
            self._samples = (
                0.1 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)
            ).astype(np.float32).reshape(-1, 1)

    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("optional loudness DSP failure")
        ),
    )

    result = ProductionAnalysisExecutor().execute(
        _job(),
        _SineAudio(),
        _Token(),
    )

    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert dict(result.result_payload.items)["judgment"] == "PASS"


def test_executor_never_reopens_or_resamples_frozen_golden_artifact(monkeypatch):
    judgment = _Judgment()
    executor = ProductionAnalysisExecutor(judgment_engine=judgment)
    monkeypatch.setattr(
        "app.service.analysis.golden_reference._read_wav",
        lambda *_: (_ for _ in ()).throw(AssertionError("path reopened")),
    )
    result = executor.execute(_job(), _Audio(), _Token())
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
    assert judgment.calls[0][1]["allow_golden_runtime_resample"] is False


def test_nonempty_ai_snapshot_fails_before_execution_as_frozen_ai_unsupported():
    toolchain = _toolchain()
    with pytest.raises(AnalysisExecutionError) as captured:
        ProductionAnalysisExecutor(judgment_engine=_Judgment()).execute(
            _job(toolchain=toolchain), _Audio(), _Token()
        )
    error = captured.value.error
    assert error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    assert error.stage is AnalysisStage.EXECUTE
    assert error.summary == (
        f"FROZEN_AI_UNSUPPORTED:{frozen_ai_snapshot_identity(toolchain)}"
    )


def test_nonempty_ai_snapshot_never_calls_any_ai_runtime_or_legacy_executor():
    judgment = _Judgment()
    executor = ProductionAnalysisExecutor(judgment_engine=judgment)
    with pytest.raises(AnalysisExecutionError):
        executor.execute(_job(toolchain=_toolchain()), _Audio(), _Token())
    assert judgment.calls == []


def test_frozen_ai_failure_summary_binds_canonical_snapshot_identity():
    toolchain = _toolchain()
    canonical = {
        "toolchain_id": toolchain.toolchain_id,
        "runtime_lane_key": toolchain.runtime_lane_key,
        "execution_revision": toolchain.execution_revision,
        "execution_mode": toolchain.execution_mode,
        "entrypoint": toolchain.entrypoint,
        "package_dir": toolchain.package_dir,
        "params_json": toolchain.params_json,
        "timeout_seconds_hex": toolchain.timeout_seconds.hex(),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert frozen_ai_snapshot_identity(toolchain) == (
        "sha256:" + hashlib.sha256(encoded).hexdigest()
    )


def test_post_prepare_golden_path_or_file_mutation_uses_only_frozen_digest_and_bytes(
    monkeypatch,
):
    judgment = _Judgment()
    executor = ProductionAnalysisExecutor(judgment_engine=judgment)
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: pytest.fail("runtime file open"),
    )
    result = executor.execute(_job(), _Audio(), _Token())
    assert result.status is AnalysisExecutionStatus.SUCCEEDED
