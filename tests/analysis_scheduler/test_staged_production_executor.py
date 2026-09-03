from __future__ import annotations

import json
import zlib
from copy import deepcopy
from dataclasses import asdict, fields, replace
from pathlib import Path

import numpy as np
import pytest

from app.service.analysis import dsp
from app.service.analysis import production_executor as production_executor_module
from app.service.analysis.production_executor import (
    ProductionAnalysisExecutor,
    _attach_stft_payload,
    _compact_chart_numeric_arrays,
)
from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.review_payloads import (
    build_chart_payload,
    build_summary_payload,
)
from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AudioStorageKind,
    PersistedAudioReference,
    canonical_toolchain_lane_key,
    AnalysisExecutionError,
)
from app.service.analysis.staged_execution import (
    AnalysisBundleBudget,
    AnalysisDecisionEnvelope,
    AnalysisEnrichmentReport,
    REQUIRED_ENRICHMENT_CHART_METRICS,
)


class _Audio:
    def __init__(self, *, sample_rate: int, channel_count: int, duration: float = 1.0):
        self._sample_rate = sample_rate
        frames = int(sample_rate * duration)
        time_axis = np.arange(frames, dtype=np.float64) / sample_rate
        channels = [
            (0.04 + channel * 0.01)
            * np.sin(2.0 * np.pi * (700.0 + 300.0 * channel) * time_axis)
            for channel in range(channel_count)
        ]
        self._samples = np.column_stack(channels).astype(np.float32)

    @property
    def samples(self):
        return self._samples

    @property
    def sample_rate_hz(self):
        return self._sample_rate

    @property
    def channel_count(self):
        return self._samples.shape[1]

    @property
    def frame_count(self):
        return self._samples.shape[0]


class _Token:
    is_cancelled = False
    reason = None


class _Runtime:
    def __init__(self, *, response_padding: int = 0, raises: bool = False):
        self.calls = []
        self.response_padding = response_padding
        self.raises = raises

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("resident exploded")
        self.response = {
            "module_status": "success",
            "response_payload": {
                "protocol_version": 1,
                "request_id": kwargs["request_id"],
                "ai_result": {
                    "ai_label": "PASS",
                    "score": 0.875,
                    "extra": {"decision_threshold": 0.5, "channel_index": 0},
                    "summary": "x" * self.response_padding,
                },
            },
        }
        return self.response

    def shutdown_all(self):
        pass


def _job(
    tmp_path: Path,
    audio: _Audio,
    *,
    ai: bool = False,
    window: tuple[float, float] = (0.0, 1.0),
    config_padding: int = 0,
) -> AnalysisJob:
    selected = list(range(audio.channel_count))
    config = {
        "config_id": 23,
        "selected_channels": selected,
        "input_device_samplerate": float(audio.sample_rate_hz),
        "channels": [
            {
                "channel_index": channel,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_lower_enabled": True,
                "spl_lower_limit": 50.0,
                "spl_upper_enabled": True,
                "spl_upper_limit": 120.0,
                "fft_nfft": 1024,
                "fft_weighting": "Z",
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "oct_weighting": "Z",
            }
            for channel in selected
        ],
    }
    if config_padding:
        config["retained_padding"] = "x" * config_padding
    toolchain = None
    if ai:
        toolchain = AiToolchainSnapshot(
            toolchain_id="resident-test",
            runtime_lane_key=canonical_toolchain_lane_key("resident-test"),
            execution_revision="resident-r1",
            execution_mode="resident_process",
            entrypoint="analyze.py",
            package_dir=str(tmp_path),
            params_json='{"decision_threshold":0.5}',
            timeout_seconds=1.0,
        )
    snapshot = AnalysisInputSnapshot(
        config_snapshot_json=json.dumps(config),
        calibration_snapshot_json=json.dumps(
            {
                f"In{channel + 1}": {"deviation_value": "0.0"}
                for channel in selected
            }
        ),
        workorder_snapshot_json='{"sn":"staged"}',
        channel_mapping_json=json.dumps(
            {"schema": "audio-channel-map/v1", "channel_order": selected}
        ),
        analysis_window_start_seconds=window[0],
        analysis_window_end_seconds=window[1],
        algorithm_revision="legacy-v1",
        ai_toolchain=toolchain,
    )
    return AnalysisJob(
        run_id="run-staged",
        analysis_attempt_id="attempt-staged",
        kind=AnalysisKind.AUTOMATIC,
        audio=PersistedAudioReference(
            reference_id="audio-staged",
            storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
            absolute_path=str((tmp_path / "run_audio" / "final.wav").resolve()),
            sample_rate_hz=audio.sample_rate_hz,
            channel_count=audio.channel_count,
            frame_count=audio.frame_count,
            dtype="float32",
            content_revision="sha256:staged-audio",
        ),
        snapshot=snapshot,
        execution_timeout_seconds=2.0,
    )


def _heterogeneous_job(
    tmp_path: Path,
    audio: _Audio,
    *,
    channel_settings: tuple[tuple[int, int, float], tuple[int, int, float]],
) -> AnalysisJob:
    job = _job(tmp_path, audio)
    config = json.loads(job.snapshot.config_snapshot_json)
    for channel, (fft_nfft, stft_nfft, stft_overlap) in zip(
        config["channels"],
        channel_settings,
        strict=True,
    ):
        channel["fft_nfft"] = fft_nfft
        channel["stft_nfft"] = stft_nfft
        channel["stft_overlap"] = stft_overlap
    return replace(
        job,
        snapshot=replace(
            job.snapshot,
            config_snapshot_json=json.dumps(config),
        ),
    )


def _assert_spec_close(actual, expected, path="root"):
    if isinstance(expected, dict):
        assert set(actual) == set(expected), path
        for key in expected:
            _assert_spec_close(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert len(actual) == len(expected), path
        if expected and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in expected
        ):
            if "loudness" in path and not path.endswith("times_sec"):
                rtol, atol = 1e-5, 1e-7
            elif any(label in path for label in ("db", "magnitude", "levels")):
                rtol, atol = 1e-6, 1e-4
            elif any(label in path for label in ("times", "frequencies", "freqs")):
                rtol, atol = 1e-10, 1e-9
            else:
                rtol, atol = 1e-7, 1e-9
            np.testing.assert_allclose(actual, expected, rtol=rtol, atol=atol)
        else:
            for index, item in enumerate(expected):
                _assert_spec_close(actual[index], item, f"{path}[{index}]")
        return
    if isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-7, abs=1e-9), path
        return
    assert actual == expected, path


def test_chart_numeric_compaction_preserves_topology_limits_and_spec_tolerances():
    original = {
        0: {
            "stft": {
                "times_sec": [0.1234567890123, 0.9876543210987],
                "frequencies_hz": [123.45678901234, 456.78901234567],
                "magnitude_db": [
                    [-12.3456789012, -98.7654321098],
                    [-23.4567890123, -87.6543210987],
                ],
                "stft_color_scale_min_db": -120.123456789,
            },
            "spl": {
                "times_sec": [0.1234567890123],
                "values_db": [61.123456789],
                "lower_limit_db": 40.123456789,
            },
            "loudness": {
                "times_sec": [0.1234567890123],
                "values": [1.1234567890123],
                "upper_limit": 3.123456789,
            },
            "fft": {
                "frequencies_hz": [1000.1234567890123],
                "levels_db": [-30.123456789],
                "limit_lines": [{"freq_hz": 1000.123456789, "limit_db": -20.123456789}],
            },
            "current_trace": {"values": [2.1234567890123]},
        }
    }
    compacted = deepcopy(original)

    _compact_chart_numeric_arrays(compacted)

    _assert_spec_close(compacted, original)
    assert compacted[0]["fft"]["limit_lines"] == original[0]["fft"]["limit_lines"]
    assert compacted[0]["spl"]["lower_limit_db"] == original[0]["spl"]["lower_limit_db"]
    assert compacted[0]["loudness"]["upper_limit"] == original[0]["loudness"]["upper_limit"]
    assert compacted[0]["current_trace"] == original[0]["current_trace"]
    assert len(json.dumps(compacted, separators=(",", ":"))) < len(
        json.dumps(original, separators=(",", ":"))
    )


def test_canonical_encoder_skips_redundant_cycle_scan_without_changing_bytes(
    monkeypatch,
):
    value = {"chart_payload": {"0": {"values_db": [1.25, 2.5]}}}
    expected = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    actual_dumps = json.dumps
    calls = []

    def capture(payload, **kwargs):
        calls.append(kwargs)
        return actual_dumps(payload, **kwargs)

    monkeypatch.setattr(production_executor_module.json, "dumps", capture)

    actual = ProductionAnalysisExecutor._canonical_json(value)

    assert actual == expected
    assert calls == [
        {
            "ensure_ascii": False,
            "allow_nan": False,
            "sort_keys": True,
            "separators": (",", ":"),
            "check_circular": False,
        }
    ]


def _legacy_oracle(job: AnalysisJob, audio: _Audio, *, ai: bool) -> dict:
    config = json.loads(job.snapshot.config_snapshot_json)
    calibration = json.loads(job.snapshot.calibration_snapshot_json)
    mapping_raw = json.loads(job.snapshot.channel_mapping_json)
    mapping = {
        channel_index: column
        for column, channel_index in enumerate(mapping_raw["channel_order"])
    }
    loudness_cache = {}
    result = JudgmentEngine(None).rejudge(
        config,
        audio.samples,
        float(audio.sample_rate_hz),
        calibration,
        "",
        job.snapshot.analysis_window_start_seconds,
        job.snapshot.analysis_window_end_seconds,
        channel_column_map=mapping,
        loudness_trace_cache=loudness_cache,
        run_ai=False,
        allow_golden_runtime_resample=False,
    )
    ai_review = (
        {
            "status": "success",
            "threshold": 0.5,
            "score": 0.875,
            "passed": True,
            "ai_label": "PASS",
            "channel_index": 0,
            "error_message": None,
            "summary": "",
        }
        if ai
        else {"status": "skipped"}
    )
    result.ai_pass = ai_review.get("passed")
    result.ai_score = ai_review.get("score")
    result.overall = (
        "合格"
        if result.limit_pass and result.ai_pass is not False
        else "不合格"
    )
    judgment_payload = json.loads(json.dumps(asdict(result)))
    chart_payload = build_chart_payload(
        buffer=audio.samples,
        config=config,
        sample_rate=float(audio.sample_rate_hz),
        mic_cal=calibration,
        analysis_window=(
            job.snapshot.analysis_window_start_seconds,
            job.snapshot.analysis_window_end_seconds,
        ),
        judgment_payload=judgment_payload,
        channel_column_map=mapping,
        loudness_trace_cache=loudness_cache,
        allow_golden_runtime_resample=False,
    )
    _attach_stft_payload(
        chart_payload=chart_payload,
        samples=audio.samples,
        config=config,
        calibration=calibration,
        sample_rate=float(audio.sample_rate_hz),
        channel_column_map=mapping,
    )
    return {
        "judgment": "PASS" if result.overall == "合格" else "FAIL",
        "judgment_payload": judgment_payload,
        "summary_payload": build_summary_payload(
            config=config,
            judgment_payload=judgment_payload,
            ai_review=ai_review,
            chart_payload=chart_payload,
        ),
        "chart_payload": {str(key): value for key, value in chart_payload.items()},
        "ai_label": ai_review.get("ai_label"),
    }
@pytest.mark.parametrize(
    ("sample_rate", "channel_count"),
    ((44_100, 1), (48_000, 2)),
)
def test_staged_executor_returns_lightweight_decision_then_compatible_enrichment(
    tmp_path,
    sample_rate,
    channel_count,
):
    audio = _Audio(sample_rate=sample_rate, channel_count=channel_count)
    executor = ProductionAnalysisExecutor()

    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())

    assert type(decision) is AnalysisDecisionEnvelope
    decision_root = json.loads(decision.canonical_result_json)
    assert decision_root["result"]["chart_payload"] == {}
    assert decision_root["result"]["channel_results"]
    assert decision_root["result"]["summary_payload"]["rows"]

    enriched = executor.execute_enrichment(decision.bundle_token)

    assert type(enriched) is AnalysisEnrichmentReport
    assert zlib.decompress(enriched.result_payload_zlib).decode("utf-8") == (
        enriched.canonical_result_json
    )
    enriched_root = json.loads(enriched.canonical_result_json)
    assert enriched_root["judgment"] == decision.judgment
    for decision_channel, enriched_channel in zip(
        decision_root["result"]["channel_results"],
        enriched_root["result"]["channel_results"],
        strict=True,
    ):
        for key, value in decision_channel.items():
            if key.endswith("_pass"):
                assert enriched_channel[key] == value
        assert enriched_channel["spl_trace_available"] is True
        assert enriched_channel["loudness_available"] is True
        assert enriched_channel["fft_available"] is True
        assert enriched_channel["octave_available"] is True
    assert set(enriched_root["result"]["chart_payload"]) == {
        str(channel) for channel in range(channel_count)
    }
    assert enriched.chart_manifest == tuple(
        (channel, REQUIRED_ENRICHMENT_CHART_METRICS)
        for channel in range(channel_count)
    )


def test_enrichment_uses_fast_artifact_compression(tmp_path, monkeypatch):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())
    actual_compress = zlib.compress
    levels = []

    def capture_level(payload, level=-1):
        levels.append(level)
        return actual_compress(payload, level=level)

    monkeypatch.setattr(
        production_executor_module.zlib,
        "compress",
        capture_level,
    )

    enrichment = executor.execute_enrichment(decision.bundle_token)

    assert levels == [0]
    assert zlib.decompress(enrichment.result_payload_zlib).decode("utf-8") == (
        enrichment.canonical_result_json
    )


def test_bundle_arrays_are_read_only_enumerated_and_within_explicit_budget(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())

    inventory = executor.bundle_array_inventory(decision.bundle_token)
    bundle = executor._require_bundle(decision.bundle_token)
    assert all(
        not isinstance(getattr(bundle, field.name), (dict, list, AnalysisJob))
        for field in fields(bundle)
    )
    assert type(bundle.config_snapshot_json) is bytes
    assert type(bundle.decision_canonical_result_json) is bytes

    expected_suffixes = {
        "source_audio",
        "calibrated_signal",
        "spl_times",
        "spl_values",
        "loudness_times",
        "loudness_values",
        "fft_frequencies",
        "fft_magnitudes",
        "stft_times",
        "stft_frequencies",
        "stft_magnitudes",
        "octave_centers",
        "octave_levels",
    }
    assert {name.split(".")[-1] for name, _array in inventory} == expected_suffixes
    discovered = {"source_audio": bundle.source_audio}
    for feature_index, feature in enumerate(bundle.features):
        discovered.update(
            {
                f"features[{feature_index}].{field.name}": getattr(
                    feature, field.name
                )
                for field in fields(feature)
                if isinstance(getattr(feature, field.name), np.ndarray)
            }
        )
    assert {name for name, _array in inventory} == set(discovered)
    assert all(not array.flags.writeable for _name, array in inventory)
    assert not discovered["source_audio"].flags.owndata
    assert discovered["features[0].calibrated_signal"].nbytes == (
        8 * audio.frame_count
    )
    assert discovered["features[0].spl_times"].nbytes == discovered[
        "features[0].spl_values"
    ].nbytes
    assert discovered["features[0].loudness_times"].nbytes == discovered[
        "features[0].loudness_values"
    ].nbytes
    assert discovered["features[0].fft_frequencies"].nbytes == discovered[
        "features[0].fft_magnitudes"
    ].nbytes
    assert discovered["features[0].stft_magnitudes"].shape == (
        discovered["features[0].stft_frequencies"].size,
        discovered["features[0].stft_times"].size,
    )
    assert discovered["features[0].octave_centers"].nbytes == discovered[
        "features[0].octave_levels"
    ].nbytes
    owning_bytes = sum(array.nbytes for _name, array in inventory if array.flags.owndata)
    budget = AnalysisBundleBudget(
        frame_count=audio.frame_count,
        channel_count=audio.channel_count,
        dtype_size=audio.samples.dtype.itemsize,
    )
    metadata_bytes = executor.bundle_retained_metadata_bytes(
        decision.bundle_token
    )
    assert 0 < metadata_bytes <= 256 * 1024
    assert owning_bytes + metadata_bytes <= budget.max_bytes


def test_bundle_rejects_oversized_retained_config_before_publication(
    tmp_path,
    monkeypatch,
):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    monkeypatch.setattr(
        executor,
        "_build_channel_features",
        lambda **_kwargs: pytest.fail("oversized config reached DSP allocation"),
    )

    with pytest.raises(AnalysisExecutionError):
        executor.execute_decision(
            _job(tmp_path, audio, config_padding=270 * 1024),
            audio,
            _Token(),
        )

    assert executor._bundles == {}


def test_retained_metadata_measure_tracks_actual_snapshot_bytes(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    base = executor.execute_decision(_job(tmp_path, audio), audio, _Token())
    padded = executor.execute_decision(
        _job(tmp_path, audio, config_padding=1024),
        audio,
        _Token(),
    )

    base_bytes = executor.bundle_retained_metadata_bytes(base.bundle_token)
    padded_bytes = executor.bundle_retained_metadata_bytes(padded.bundle_token)

    assert padded_bytes > base_bytes + 1024


def test_bundle_rejects_oversized_ai_diagnostic_before_publication(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor(
        ai_runtime=_Runtime(response_padding=270 * 1024)
    )

    with pytest.raises(AnalysisExecutionError):
        executor.execute_decision(
            _job(tmp_path, audio, ai=True), audio, _Token()
        )

    assert executor._bundles == {}
    response_path = tmp_path / "ai_runs" / "attempt-staged" / "response.json"
    assert len(response_path.read_bytes()) > 270 * 1024


@pytest.mark.parametrize(
    "channel_settings",
    (
        ((512, 512, 0.25), (2048, 1024, 0.75)),
        ((4096, 2048, 0.50), (1024, 512, 0.25)),
    ),
)
def test_heterogeneous_channel_shapes_fit_configured_per_channel_budget(
    tmp_path,
    channel_settings,
):
    audio = _Audio(sample_rate=48_000, channel_count=2)
    job = _heterogeneous_job(
        tmp_path,
        audio,
        channel_settings=channel_settings,
    )
    oracle = _legacy_oracle(job, audio, ai=False)
    executor = ProductionAnalysisExecutor()

    decision = executor.execute_decision(job, audio, _Token())
    assert executor.bundle_retained_metadata_bytes(decision.bundle_token) <= (
        256 * 1024
    )
    enriched = json.loads(
        executor.execute_enrichment(decision.bundle_token).canonical_result_json
    )

    assert enriched["judgment"] == oracle["judgment"]
    _assert_spec_close(
        enriched["result"]["chart_payload"],
        oracle["chart_payload"],
        "heterogeneous_chart_payload",
    )


def test_single_bundle_boundary_rejects_fft_shape_over_frozen_config(tmp_path):
    audio = _Audio(sample_rate=48_000, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())
    bundle = executor._require_bundle(decision.bundle_token)
    configured_bins = 1024 // 2 + 1
    oversized_axis = np.arange(configured_bins + 1, dtype=np.float64)
    oversized_levels = np.zeros(configured_bins + 1, dtype=np.float64)
    oversized_axis.setflags(write=False)
    oversized_levels.setflags(write=False)
    feature = replace(
        bundle.features[0],
        fft_frequencies=oversized_axis,
        fft_magnitudes=oversized_levels,
    )
    oversized_bundle = replace(bundle, features=(feature,))

    with pytest.raises(ValueError, match="FFT|fft"):
        executor._validate_bundle_budget(oversized_bundle)


def test_enrichment_reuses_decision_calibration_and_dsp_primitives(tmp_path, monkeypatch):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())

    for name in ("compute_spl_trace",):
        monkeypatch.setattr(
            dsp,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"Enrichment recomputed {_name}"
            ),
        )

    enriched = executor.execute_enrichment(decision.bundle_token)

    assert json.loads(enriched.canonical_result_json)["result"]["chart_payload"]


def test_decision_defers_unconstrained_chart_dsp_then_enrichment_restores_oracle(
    tmp_path,
    monkeypatch,
):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    base_job = _job(tmp_path, audio)
    config = json.loads(base_job.snapshot.config_snapshot_json)
    channel = config["channels"][0]
    for key in (
        "spl_lower_enabled",
        "spl_upper_enabled",
        "spl_average_lower_enabled",
        "spl_average_upper_enabled",
        "loudness_lower_enabled",
        "loudness_upper_enabled",
        "loudness_average_lower_enabled",
        "loudness_average_upper_enabled",
    ):
        channel[key] = False
    channel["fft_band_limits"] = []
    channel["oct_band_limits"] = []
    channel["fft_golden_mode"] = "off"
    channel["oct_golden_mode"] = "off"
    job = replace(
        base_job,
        snapshot=replace(
            base_job.snapshot,
            config_snapshot_json=json.dumps(config),
        ),
    )
    oracle = _legacy_oracle(job, audio, ai=False)
    calls = {
        name: 0
        for name in (
            "compute_spl_trace",
            "compute_spl_leq",
            "compute_loudness_trace",
            "compute_loudness_trace_for_window",
            "compute_fft_spectrum",
            "compute_stft",
            "compute_octave_bands",
        )
    }
    for name in tuple(calls):
        original = getattr(dsp, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(dsp, name, counted)

    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(job, audio, _Token())
    decision_root = json.loads(decision.canonical_result_json)

    assert calls == {name: 0 for name in calls}
    assert decision_root["judgment"] == oracle["judgment"]
    assert decision_root["result"]["limit_pass"] is True
    assert decision_root["result"]["summary_payload"]["overall_status"] == "pass"

    enriched_root = json.loads(
        executor.execute_enrichment(decision.bundle_token).canonical_result_json
    )
    assert calls == {
        "compute_spl_trace": 1,
        "compute_spl_leq": 1,
        "compute_loudness_trace": 1,
        "compute_loudness_trace_for_window": 0,
        "compute_fft_spectrum": 1,
        "compute_stft": 1,
        "compute_octave_bands": 1,
    }
    for field_name in ("judgment_payload", "summary_payload", "chart_payload"):
        _assert_spec_close(
            enriched_root["result"][field_name],
            oracle[field_name],
            f"deferred_full_result.{field_name}",
        )


def test_enabled_spl_limit_is_computed_once_in_decision_and_reused_in_enrichment(
    tmp_path,
    monkeypatch,
):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    calls = {"trace": 0, "average": 0}
    real_trace = dsp.compute_spl_trace
    real_average = dsp.compute_spl_leq

    def trace(*args, **kwargs):
        calls["trace"] += 1
        return real_trace(*args, **kwargs)

    def average(*args, **kwargs):
        calls["average"] += 1
        return real_average(*args, **kwargs)

    monkeypatch.setattr(dsp, "compute_spl_trace", trace)
    monkeypatch.setattr(dsp, "compute_spl_leq", average)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())

    assert calls == {"trace": 1, "average": 0}
    executor.execute_enrichment(decision.bundle_token)
    assert calls == {"trace": 1, "average": 1}


def test_enrichment_rejects_identity_mismatch_without_consuming_bundle(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(_job(tmp_path, audio), audio, _Token())
    mismatched = replace(
        decision.bundle_token,
        identity=replace(
            decision.bundle_token.identity,
            content_revision="sha256:different-audio",
        ),
    )

    with pytest.raises(AnalysisExecutionError):
        executor.execute_enrichment(mismatched)

    assert executor.execute_enrichment(decision.bundle_token).judgment == (
        decision.judgment
    )


def test_enriched_payload_matches_uncached_legacy_dsp_at_near_limit(tmp_path):
    audio = _Audio(sample_rate=48_000, channel_count=1)
    base_job = _job(tmp_path, audio)
    config = json.loads(base_job.snapshot.config_snapshot_json)
    _times, trace = dsp.compute_spl_trace(
        audio.samples[:, 0], 48_000.0, "Z", 0.125, chunk_duration=0.05
    )
    config["channels"][0]["spl_upper_limit"] = float(max(trace)) + 1e-9
    job = replace(
        base_job,
        snapshot=replace(
            base_job.snapshot,
            config_snapshot_json=json.dumps(config),
        ),
    )
    oracle = _legacy_oracle(job, audio, ai=False)
    calibration = json.loads(job.snapshot.calibration_snapshot_json)
    executor = ProductionAnalysisExecutor()
    decision = executor.execute_decision(job, audio, _Token())
    decision_root = json.loads(decision.canonical_result_json)

    legacy_chart = build_chart_payload(
        buffer=audio.samples,
        config=config,
        sample_rate=48_000.0,
        mic_cal=calibration,
        analysis_window=(0.0, 1.0),
        judgment_payload=decision_root["result"]["judgment_payload"],
        channel_column_map={0: 0},
        allow_golden_runtime_resample=False,
    )
    _attach_stft_payload(
        chart_payload=legacy_chart,
        samples=audio.samples,
        config=config,
        calibration=calibration,
        sample_rate=48_000.0,
        channel_column_map={0: 0},
    )
    legacy_summary = build_summary_payload(
        config=config,
        judgment_payload=decision_root["result"]["judgment_payload"],
        ai_review={"status": "skipped"},
        chart_payload=legacy_chart,
    )

    enriched_root = json.loads(
        executor.execute_enrichment(decision.bundle_token).canonical_result_json
    )

    assert enriched_root["judgment"] == decision.judgment
    assert enriched_root["result"]["limit_pass"] is True
    assert enriched_root["result"]["summary_payload"] == oracle["summary_payload"]
    _assert_spec_close(
        enriched_root["result"]["chart_payload"],
        oracle["chart_payload"],
    )


def test_nonzero_window_terminal_stft_matches_full_recording_legacy_oracle(
    tmp_path,
    monkeypatch,
):
    audio = _Audio(sample_rate=48_000, channel_count=1, duration=2.0)
    job = _job(tmp_path, audio, window=(0.5, 1.5))
    config = json.loads(job.snapshot.config_snapshot_json)
    calibration = json.loads(job.snapshot.calibration_snapshot_json)
    legacy_chart = {0: {}}
    _attach_stft_payload(
        chart_payload=legacy_chart,
        samples=audio.samples,
        config=config,
        calibration=calibration,
        sample_rate=48_000.0,
        channel_column_map={0: 0},
    )
    executor = ProductionAnalysisExecutor()
    stft_input_sizes = []
    original_compute_stft = dsp.compute_stft

    def recording_compute_stft(signal, *args, **kwargs):
        stft_input_sizes.append(np.asarray(signal).size)
        return original_compute_stft(signal, *args, **kwargs)

    monkeypatch.setattr(dsp, "compute_stft", recording_compute_stft)

    decision = executor.execute_decision(job, audio, _Token())
    assert stft_input_sizes == []
    retained_source = dict(
        executor.bundle_array_inventory(decision.bundle_token)
    )["source_audio"]
    assert retained_source.shape[0] == audio.frame_count
    assert not retained_source.flags.owndata
    enriched = json.loads(
        executor.execute_enrichment(decision.bundle_token).canonical_result_json
    )
    assert stft_input_sizes == [48_000, 96_000]
    actual = enriched["result"]["chart_payload"]["0"]["stft"]
    expected = legacy_chart[0]["stft"]

    assert len(actual["times_sec"]) == len(expected["times_sec"])
    assert len(actual["frequencies_hz"]) == len(expected["frequencies_hz"])
    assert np.asarray(actual["magnitude_db"]).shape == np.asarray(
        expected["magnitude_db"]
    ).shape
    assert actual["times_sec"][-1] == pytest.approx(
        expected["times_sec"][-1], rel=1e-10, abs=1e-9
    )
    np.testing.assert_allclose(
        actual["times_sec"],
        expected["times_sec"],
        rtol=1e-10,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        actual["frequencies_hz"],
        expected["frequencies_hz"],
        rtol=1e-10,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        actual["magnitude_db"],
        expected["magnitude_db"],
        rtol=1e-6,
        atol=1e-4,
    )


@pytest.mark.parametrize(
    ("sample_rate", "channel_count", "duration", "window", "near_limit", "ai"),
    (
        (44_100, 1, 1.0, (0.0, 1.0), False, False),
        (48_000, 2, 1.0, (0.0, 1.0), False, False),
        (48_000, 1, 2.0, (0.5, 1.5), False, False),
        (48_000, 1, 1.0, (0.0, 1.0), True, False),
        (44_100, 1, 1.0, (0.0, 1.0), False, True),
    ),
    ids=("one-channel-44k", "two-channel-48k", "nonzero-window", "near-limit", "resident-ai"),
)
def test_staged_parity_against_independent_uncached_legacy_oracle(
    tmp_path,
    sample_rate,
    channel_count,
    duration,
    window,
    near_limit,
    ai,
):
    audio = _Audio(
        sample_rate=sample_rate,
        channel_count=channel_count,
        duration=duration,
    )
    job = _job(tmp_path, audio, ai=ai, window=window)
    if near_limit:
        config = json.loads(job.snapshot.config_snapshot_json)
        _times, trace = dsp.compute_spl_trace(
            audio.samples[:, 0],
            float(sample_rate),
            "Z",
            0.125,
            chunk_duration=0.05,
        )
        config["channels"][0]["spl_upper_limit"] = float(max(trace)) + 1e-9
        job = replace(
            job,
            snapshot=replace(
                job.snapshot,
                config_snapshot_json=json.dumps(config),
            ),
        )

    oracle = _legacy_oracle(job, audio, ai=ai)
    runtime = _Runtime() if ai else None
    executor = ProductionAnalysisExecutor(ai_runtime=runtime)
    decision = executor.execute_decision(job, audio, _Token())
    decision_root = json.loads(decision.canonical_result_json)
    enriched_root = json.loads(
        executor.execute_enrichment(decision.bundle_token).canonical_result_json
    )

    assert decision.judgment == oracle["judgment"]
    assert enriched_root["judgment"] == oracle["judgment"]
    _assert_spec_close(
        enriched_root["result"]["judgment_payload"],
        oracle["judgment_payload"],
        "judgment_payload",
    )
    assert enriched_root["result"]["summary_payload"] == oracle[
        "summary_payload"
    ]
    _assert_spec_close(
        enriched_root["result"]["chart_payload"],
        oracle["chart_payload"],
        "chart_payload",
    )
    for actual_channel, expected_channel in zip(
        enriched_root["result"]["channel_results"],
        oracle["judgment_payload"]["channel_results"],
        strict=True,
    ):
        assert actual_channel["channel_index"] == expected_channel["channel_index"]
        for key in (
            "spl_pass",
            "spl_average_pass",
            "loudness_pass",
            "loudness_average_pass",
            "spl_trace_available",
            "spl_average_available",
            "loudness_available",
            "fft_available",
            "octave_available",
        ):
            assert actual_channel[key] is expected_channel[key]
    assert decision_root["result"]["ai_pass"] is (True if ai else None)
    if ai:
        response = json.loads(
            (
                tmp_path / "ai_runs" / "attempt-staged" / "response.json"
            ).read_text(encoding="utf-8")
        )
        assert response["ai_result"]["ai_label"] == oracle["ai_label"]


def test_ai_diagnostic_json_is_deferred_until_enrichment(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    runtime = _Runtime()
    executor = ProductionAnalysisExecutor(ai_runtime=runtime)
    decision = executor.execute_decision(
        _job(tmp_path, audio, ai=True), audio, _Token()
    )
    work_dir = tmp_path / "ai_runs" / "attempt-staged"

    assert len(runtime.calls) == 1
    assert not (work_dir / "request.json").exists()
    assert not (work_dir / "response.json").exists()
    assert decision.judgment == "PASS"

    enriched = executor.execute_enrichment(decision.bundle_token)

    request = json.loads((work_dir / "request.json").read_text(encoding="utf-8"))
    response = json.loads((work_dir / "response.json").read_text(encoding="utf-8"))
    assert request == runtime.calls[0]["request_payload"]
    assert response == runtime.response["response_payload"]
    root = json.loads(enriched.canonical_result_json)
    assert root["result"]["ai_pass"] is True
    assert root["result"]["ai_score"] == pytest.approx(0.875)


def test_ai_request_is_flushed_when_resident_call_raises(tmp_path):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor(ai_runtime=_Runtime(raises=True))
    job = _job(tmp_path, audio, ai=True)

    with pytest.raises(AnalysisExecutionError):
        executor.execute_decision(job, audio, _Token())

    request_path = tmp_path / "ai_runs" / "attempt-staged" / "request.json"
    assert request_path.exists()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["request_id"] == "attempt-staged"


def test_ai_diagnostics_flush_before_enrichment_chart_failure(tmp_path, monkeypatch):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    runtime = _Runtime()
    executor = ProductionAnalysisExecutor(ai_runtime=runtime)
    decision = executor.execute_decision(
        _job(tmp_path, audio, ai=True), audio, _Token()
    )
    monkeypatch.setattr(
        "app.service.analysis.production_executor.build_chart_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("chart failed")),
    )

    with pytest.raises(AnalysisExecutionError):
        executor.execute_enrichment(decision.bundle_token)

    assert decision.bundle_token.bundle_id in executor._bundles
    assert executor.bundle_array_inventory(decision.bundle_token)

    work_dir = tmp_path / "ai_runs" / "attempt-staged"
    assert json.loads(
        (work_dir / "request.json").read_text(encoding="utf-8")
    ) == runtime.calls[0]["request_payload"]
    assert json.loads(
        (work_dir / "response.json").read_text(encoding="utf-8")
    ) == runtime.response["response_payload"]


def test_compatibility_execute_releases_private_bundle_on_enrichment_failure(
    tmp_path,
    monkeypatch,
):
    audio = _Audio(sample_rate=44_100, channel_count=1)
    executor = ProductionAnalysisExecutor()
    monkeypatch.setattr(
        "app.service.analysis.production_executor.build_chart_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("chart failed")),
    )

    with pytest.raises(AnalysisExecutionError):
        executor.execute(_job(tmp_path, audio), audio, _Token())

    assert executor._bundles == {}
