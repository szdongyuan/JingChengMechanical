"""Frozen-snapshot production adapter for the legacy non-AI judgment path."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.golden_reference import FrozenGoldenArtifact
from app.service.analysis.judgment import JudgmentEngine, _runtime_channel_defaults
from app.service.ai_resident_runtime import ResidentToolchainRuntime
from app.service.analysis.review_payloads import (
    build_chart_payload,
    build_summary_payload,
)
from app.service.current_meter_service import build_current_trace_payload
from app.service.analysis.scheduler import (
    AiToolchainSnapshot,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisJob,
    AnalysisStage,
    FrozenResultObject,
)

_TERMINAL_STFT_MAX_TIME_COLUMNS = 512
_TERMINAL_STFT_MAX_FREQUENCY_ROWS = 1025


def frozen_ai_snapshot_identity(snapshot: AiToolchainSnapshot) -> str:
    if type(snapshot) is not AiToolchainSnapshot:
        raise TypeError("snapshot must be an exact AiToolchainSnapshot")
    payload = {
        "toolchain_id": snapshot.toolchain_id,
        "runtime_lane_key": snapshot.runtime_lane_key,
        "execution_revision": snapshot.execution_revision,
        "execution_mode": snapshot.execution_mode,
        "entrypoint": snapshot.entrypoint,
        "package_dir": snapshot.package_dir,
        "params_json": snapshot.params_json,
        "timeout_seconds_hex": snapshot.timeout_seconds.hex(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _failure(summary: str) -> AnalysisExecutionError:
    return AnalysisExecutionError(
        AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            stage=AnalysisStage.EXECUTE,
            summary=summary[:1024],
        )
    )


def _decode_object(raw: str, name: str) -> dict:
    def reject_constant(value: str) -> None:
        raise ValueError(value)

    try:
        value = json.loads(raw, parse_constant=reject_constant)
    except (TypeError, ValueError) as exc:
        raise _failure(f"frozen {name} snapshot is invalid") from exc
    if type(value) is not dict:
        raise _failure(f"frozen {name} snapshot must be an object")
    return value


def _frozen_scalar(value: object):
    if isinstance(value, np.ndarray):
        return _frozen_scalar(value.tolist())
    if isinstance(value, np.generic):
        return _frozen_scalar(value.item())
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite result")
        return value
    if type(value) is list:
        return tuple(_frozen_scalar(item) for item in value)
    if type(value) is tuple:
        return tuple(_frozen_scalar(item) for item in value)
    if type(value) is dict:
        return _unchecked_frozen_result_object(
            tuple(
                (str(key), _frozen_scalar(item))
                for key, item in sorted(value.items())
            )
        )
    if hasattr(value, "__dataclass_fields__"):
        return _unchecked_frozen_result_object(
            tuple(
                (field.name, _frozen_scalar(getattr(value, field.name)))
                for field in fields(value)
            )
        )
    raise TypeError(f"unsupported result value {type(value).__name__}")


def _unchecked_frozen_result_object(items: tuple) -> FrozenResultObject:
    """Build nested nodes; AnalysisExecutionResult validates the full tree once."""

    value = object.__new__(FrozenResultObject)
    object.__setattr__(value, "items", items)
    return value


def _plain_result(value: object):
    """Convert analysis values to ordinary finite JSON-compatible values."""

    if is_dataclass(value):
        return _plain_result(asdict(value))
    if isinstance(value, np.ndarray):
        return _plain_result(value.tolist())
    if isinstance(value, np.generic):
        return _plain_result(value.item())
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite result")
        return value
    if type(value) in (list, tuple):
        return [_plain_result(item) for item in value]
    if type(value) is dict:
        return {str(key): _plain_result(item) for key, item in value.items()}
    raise TypeError(f"unsupported result value {type(value).__name__}")


def _mic_sensitivity(calibration: dict, channel_index: int) -> float:
    return dsp.mic_sensitivity_from_calibration(
        calibration,
        channel_index,
    )


def _restore_dsp_sample_scale(
    samples: np.ndarray,
    persisted_dtype: str,
) -> np.ndarray:
    """Restore persisted PCM samples to the float scale used by live DSP."""

    if persisted_dtype != "<i2":
        return samples
    if samples.dtype != np.dtype("<i2"):
        raise ValueError("loaded audio dtype differs from frozen metadata")
    pcm = samples.astype(np.float64)
    return np.where(pcm >= 0.0, pcm / 32767.0, pcm / 32768.0)


def _attach_stft_payload(
    *,
    chart_payload: dict,
    samples: np.ndarray,
    config: dict,
    calibration: dict,
    sample_rate: float,
    channel_column_map: dict[int, int],
) -> None:
    """Persist a bounded terminal STFT so cached history remains inspectable."""

    channels = {
        item.get("channel_index"): item
        for item in config.get("channels", [])
        if isinstance(item, dict)
    }
    for channel_index in config.get("selected_channels", []):
        if not isinstance(channel_index, int) or isinstance(channel_index, bool):
            continue
        column = channel_column_map.get(channel_index, channel_index)
        if samples.ndim == 1:
            signal = np.asarray(samples, dtype=np.float64)
        elif 0 <= column < samples.shape[1]:
            signal = np.asarray(samples[:, column], dtype=np.float64)
        else:
            continue
        if signal.size == 0:
            continue
        channel_config = _runtime_channel_defaults(channel_index)
        if isinstance(channels.get(channel_index), dict):
            channel_config.update(channels[channel_index])
        try:
            calibrated = dsp.apply_mic_calibration(
                signal,
                _mic_sensitivity(calibration, channel_index),
            )
            times, frequencies, magnitudes = dsp.compute_stft(
                calibrated,
                sample_rate,
                int(channel_config.get("stft_nfft", 1024)),
                float(channel_config.get("stft_overlap", 0.5)),
            )
        except Exception:
            continue
        times = np.asarray(times, dtype=np.float64)
        frequencies = np.asarray(frequencies, dtype=np.float64)
        magnitudes = np.asarray(magnitudes, dtype=np.float64)
        if (
            times.ndim != 1
            or frequencies.ndim != 1
            or magnitudes.shape != (frequencies.size, times.size)
            or times.size == 0
            or frequencies.size == 0
        ):
            continue

        # Keep the terminal image bounded without visibly coarsening the
        # configured STFT.  The default 1024-point STFT now keeps all 513
        # frequency bins, matching the live image; longer recordings are only
        # thinned once they exceed the display-oriented time-column budget.
        time_indexes = np.linspace(
            0,
            times.size - 1,
            min(times.size, _TERMINAL_STFT_MAX_TIME_COLUMNS),
            dtype=int,
        )
        frequency_indexes = np.linspace(
            0,
            frequencies.size - 1,
            min(frequencies.size, _TERMINAL_STFT_MAX_FREQUENCY_ROWS),
            dtype=int,
        )
        chart_payload.setdefault(channel_index, {})["stft"] = {
            "times_sec": times[time_indexes].tolist(),
            "frequencies_hz": frequencies[frequency_indexes].tolist(),
            "magnitude_db": magnitudes[
                np.ix_(frequency_indexes, time_indexes)
            ].tolist(),
            "stft_color_scale_enabled": bool(
                channel_config.get("stft_color_scale_enabled", False)
            ),
            "stft_color_scale_min_db": channel_config.get(
                "stft_color_scale_min_db",
                -120.0,
            ),
            "stft_color_scale_max_db": channel_config.get(
                "stft_color_scale_max_db",
                0.0,
            ),
        }


def _map_legacy_verdict(overall: object, limit_pass: object) -> str:
    if type(overall) is not str or type(limit_pass) is not bool:
        raise ValueError("legacy judgment verdict is invalid")
    if overall == "合格" and limit_pass:
        return "PASS"
    if overall == "不合格" and not limit_pass:
        return "FAIL"
    raise ValueError("legacy judgment verdict is incoherent")


def _require_finite_number(value: object, name: str) -> None:
    if (
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, (int, float, np.integer, np.floating))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"legacy {name} is missing or nonfinite")


def _validate_legacy_result(
    result: object,
    selected: list[int],
    channel_configs: Mapping[int, dict],
) -> None:
    channel_results = getattr(result, "channel_results", None)
    if type(channel_results) not in (list, tuple):
        raise ValueError("legacy channel results are missing")

    by_index = {}
    for channel_result in channel_results:
        index = getattr(channel_result, "channel_index", None)
        if type(index) is not int or index in by_index:
            raise ValueError("legacy channel result identity is invalid")
        by_index[index] = channel_result

        for field_name in (
            "spl_trace_available",
            "spl_average_available",
            "fft_available",
            "octave_available",
        ):
            if getattr(channel_result, field_name, None) is not True:
                raise ValueError(
                    f"required legacy calculation {field_name} is unavailable"
                )
        for field_name in (
            "spl_trace_error_message",
            "spl_average_error_message",
            "fft_error_message",
            "octave_error_message",
        ):
            if getattr(channel_result, field_name, None) is not None:
                raise ValueError(
                    f"required legacy calculation {field_name} reported an error"
                )
        _require_finite_number(
            getattr(channel_result, "spl_db", None),
            "spl_db",
        )
        if type(getattr(channel_result, "spl_pass", None)) is not bool:
            raise ValueError("legacy spl_pass is missing")
        _require_finite_number(
            getattr(channel_result, "spl_average_db", None),
            "spl_average_db",
        )
        if type(getattr(channel_result, "spl_average_pass", None)) is not bool:
            raise ValueError("legacy spl_average_pass is missing")

        channel_config = channel_configs[index]
        loudness_trace_required = any(
            channel_config.get(key) is True
            for key in (
                "loudness_lower_enabled",
                "loudness_upper_enabled",
            )
        )
        loudness_average_required = any(
            channel_config.get(key) is True
            for key in (
                "loudness_average_lower_enabled",
                "loudness_average_upper_enabled",
            )
        )
        if loudness_trace_required or loudness_average_required:
            if getattr(channel_result, "loudness_available", None) is not True:
                raise ValueError(
                    "required legacy calculation loudness is unavailable"
                )
            if getattr(channel_result, "loudness_error_message", None) is not None:
                raise ValueError(
                    "required legacy calculation loudness reported an error"
                )
        if loudness_trace_required:
            _require_finite_number(
                getattr(channel_result, "loudness_min_sone", None),
                "loudness_min_sone",
            )
            _require_finite_number(
                getattr(channel_result, "loudness_max_sone", None),
                "loudness_max_sone",
            )
            if type(getattr(channel_result, "loudness_pass", None)) is not bool:
                raise ValueError("legacy loudness_pass is missing")
        if loudness_average_required:
            _require_finite_number(
                getattr(channel_result, "loudness_average_sone", None),
                "loudness_average_sone",
            )
            if (
                type(
                    getattr(
                        channel_result,
                        "loudness_average_pass",
                        None,
                    )
                )
                is not bool
            ):
                raise ValueError("legacy loudness_average_pass is missing")

    if set(by_index) != set(selected) or len(by_index) != len(selected):
        raise ValueError("legacy channel results are missing or unexpected")


class ProductionAnalysisExecutor:
    def __init__(
        self,
        *,
        judgment_engine: object | None = None,
        frozen_golden_artifacts: Mapping[
            tuple[int, int], FrozenGoldenArtifact
        ] | None = None,
        ai_runtime: object | None = None,
    ) -> None:
        self._judgment = judgment_engine or JudgmentEngine(None)
        self._golden = dict(frozen_golden_artifacts or {})
        self._ai_runtime = ai_runtime or ResidentToolchainRuntime()

    def close(self) -> None:
        self._ai_runtime.shutdown_all()

    def _run_frozen_ai(
        self,
        *,
        job: AnalysisJob,
        config: dict,
        workorder: dict,
        channel_order: list[int],
    ) -> dict:
        toolchain = job.snapshot.ai_toolchain
        if toolchain is None:
            return {"status": "skipped"}
        if toolchain.execution_mode != "resident_process":
            return {
                "status": "process_error",
                "error_message": "AI execution mode is not supported",
            }

        params = _decode_object(toolchain.params_json, "AI params")
        request_id = job.analysis_attempt_id
        duration_ms = int(
            round(
                (
                    job.snapshot.analysis_window_end_seconds
                    - job.snapshot.analysis_window_start_seconds
                )
                * 1000.0
            )
        )
        audio_parent = Path(job.audio.absolute_path).parent
        ai_runs_root = (
            audio_parent.parent / "ai_runs"
            if audio_parent.name.lower() == "run_audio"
            else audio_parent / "ai_runs"
        )
        work_dir = ai_runs_root / job.analysis_attempt_id
        artifacts_dir = work_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "protocol_version": 1,
            "request_id": request_id,
            "test_run_id": job.run_id,
            "toolchain_id": toolchain.toolchain_id,
            "segments": [
                {
                    "segment_id": (
                        job.audio.audio_data_id or job.audio.reference_id
                    ),
                    "role": "device_1",
                    "audio_path": job.audio.absolute_path,
                    "channels": list(range(job.audio.channel_count)),
                    "source_channels": list(channel_order),
                    "channel_map": [
                        {
                            "logical_channel": column,
                            "source_channel": source,
                        }
                        for column, source in enumerate(channel_order)
                    ],
                    "duration_ms": duration_ms,
                    "sample_rate": job.audio.sample_rate_hz,
                    "metadata": {},
                }
            ],
            "context": {
                "config_id": config.get("config_id"),
                "workorder": workorder,
            },
            "params": params,
            "window_info": {
                "final_window_offset_ms": int(
                    round(
                        job.snapshot.analysis_window_start_seconds
                        * 1000.0
                    )
                ),
                "final_window_duration_ms": duration_ms,
            },
        }
        (work_dir / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        transport = self._ai_runtime.analyze(
            toolchain_id=toolchain.toolchain_id,
            entrypoint=toolchain.entrypoint,
            package_dir=toolchain.package_dir,
            request_id=request_id,
            request_payload=request,
            artifacts_dir=str(artifacts_dir),
            timeout_ms=max(
                1,
                int(round(toolchain.timeout_seconds * 1000.0)),
            ),
        )
        if transport.get("module_status") != "success":
            return {
                "status": str(
                    transport.get("module_status") or "process_error"
                ),
                "error_message": str(
                    transport.get("error_summary") or "AI analysis failed"
                ),
            }
        response = transport.get("response_payload")
        if isinstance(response, dict):
            (work_dir / "response.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request_id
        ):
            return {
                "status": "protocol_error",
                "error_message": "AI response is invalid",
            }
        ai_result = response.get("ai_result")
        if not isinstance(ai_result, dict):
            return {
                "status": "protocol_error",
                "error_message": "AI result is missing",
            }
        score_value = ai_result.get("score")
        score = (
            float(score_value)
            if type(score_value) in (int, float)
            and math.isfinite(float(score_value))
            else None
        )
        extra = ai_result.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        threshold_value = extra.get(
            "decision_threshold",
            params.get("decision_threshold", 0.5),
        )
        threshold = (
            float(threshold_value)
            if type(threshold_value) in (int, float)
            and math.isfinite(float(threshold_value))
            else 0.5
        )
        label = str(ai_result.get("ai_label") or "").strip()
        normalized_label = label.upper()
        if normalized_label in {"PASS", "OK"}:
            passed = True
        elif normalized_label in {"FAIL", "NG"}:
            passed = False
        else:
            passed = score is not None and score >= threshold
        reported_channel = extra.get("channel_index")
        channel_index = None
        if (
            type(reported_channel) is int
            and 0 <= reported_channel < len(channel_order)
        ):
            channel_index = channel_order[reported_channel]
        return {
            "status": "success",
            "threshold": threshold,
            "score": score,
            "passed": passed,
            "ai_label": label or None,
            "channel_index": channel_index,
            "error_message": None,
            "summary": ai_result.get("summary"),
        }

    def execute(self, job: AnalysisJob, audio, cancel_token) -> AnalysisExecutionResult:
        try:
            config = _decode_object(
                job.snapshot.config_snapshot_json,
                "config",
            )
            calibration = _decode_object(
                job.snapshot.calibration_snapshot_json,
                "calibration",
            )
            workorder = _decode_object(
                job.snapshot.workorder_snapshot_json,
                "workorder",
            )
            mapping_raw = _decode_object(
                job.snapshot.channel_mapping_json,
                "channel mapping",
            )
            selected = config.get("selected_channels")
            channels = config.get("channels")
            if (
                type(selected) is not list
                or not selected
                or any(type(index) is not int or index < 0 for index in selected)
                or len(set(selected)) != len(selected)
                or type(channels) is not list
            ):
                raise ValueError("selected channels are invalid")
            by_index = {}
            for channel in channels:
                if type(channel) is not dict:
                    raise ValueError("channel row is invalid")
                index = channel.get("channel_index")
                if type(index) is not int or index in by_index:
                    raise ValueError("channel row identity is invalid")
                by_index[index] = channel
            if set(by_index) != set(selected):
                raise ValueError("channel rows do not match selected channels")
            vibration_channel = config.get("vibration_view_channel_index")
            judged_channels = (
                [
                    index
                    for index in selected
                    if index != vibration_channel
                ]
                if (
                    type(vibration_channel) is int
                    and vibration_channel >= 0
                    and vibration_channel in selected
                )
                else list(selected)
            )
            if any(
                str(by_index[index].get("spl_weighting") or "").strip().upper()
                not in {"A", "C", "Z"}
                for index in selected
            ):
                raise ValueError("SPL weighting must be A, C, or Z")
            if (
                set(mapping_raw) != {"schema", "channel_order"}
                or mapping_raw.get("schema") != "audio-channel-map/v1"
                or type(mapping_raw.get("channel_order")) is not list
            ):
                raise ValueError("channel mapping is invalid")
            channel_order = mapping_raw["channel_order"]
            if (
                not channel_order
                or any(
                    type(channel_index) is not int or channel_index < 0
                    for channel_index in channel_order
                )
                or len(set(channel_order)) != len(channel_order)
            ):
                raise ValueError("channel mapping is invalid")
            mapping = {
                channel_index: column_index
                for column_index, channel_index in enumerate(channel_order)
            }
            if (
                set(mapping) != set(selected)
                or len(set(mapping.values())) != len(mapping)
                or any(value >= audio.channel_count for value in mapping.values())
            ):
                raise ValueError("channel mapping differs from frozen channels")
            samples = np.asarray(audio.samples)
            if (
                samples.ndim not in (1, 2)
                or samples.shape[0] != audio.frame_count
                or audio.sample_rate_hz != job.audio.sample_rate_hz
                or audio.channel_count != job.audio.channel_count
                or audio.frame_count != job.audio.frame_count
            ):
                raise ValueError("loaded audio differs from frozen metadata")
            samples = _restore_dsp_sample_scale(samples, job.audio.dtype)
            config_id = config.get("config_id")
            for index in selected:
                channel = by_index[index]
                golden_enabled = any(
                    str(channel.get(key) or "off").lower() != "off"
                    for key in ("fft_golden_mode", "oct_golden_mode")
                )
                if golden_enabled:
                    artifact = self._golden.get((config_id, index))
                    if type(artifact) is not FrozenGoldenArtifact:
                        raise ValueError("frozen golden artifact is missing")
                    if artifact.rehash() != artifact.sha256:
                        raise ValueError("frozen golden artifact digest changed")
                    channel["_frozen_golden_artifact"] = artifact
            if bool(getattr(cancel_token, "is_cancelled", False)):
                raise AnalysisExecutionError(
                    AnalysisError(
                        AnalysisErrorCategory.ANALYSIS_CANCELED,
                        AnalysisStage.CANCEL,
                        getattr(cancel_token, "reason", None)
                        or "analysis canceled",
                    )
                )
            loudness_trace_cache: dict[int, dict] = {}
            result = self._judgment.rejudge(
                config,
                samples,
                float(audio.sample_rate_hz),
                calibration,
                "",
                job.snapshot.analysis_window_start_seconds,
                job.snapshot.analysis_window_end_seconds,
                channel_column_map=mapping,
                loudness_trace_cache=loudness_trace_cache,
                run_ai=False,
                allow_golden_runtime_resample=False,
            )
            _validate_legacy_result(result, judged_channels, by_index)
            if (
                tuple(result.analysis_window)
                != (
                    job.snapshot.analysis_window_start_seconds,
                    job.snapshot.analysis_window_end_seconds,
                )
                or any(
                    item.channel_index not in judged_channels
                    for item in result.channel_results
                )
            ):
                raise ValueError("legacy judgment result is incoherent")
            ai_review = self._run_frozen_ai(
                job=job,
                config=config,
                workorder=workorder,
                channel_order=channel_order,
            )
            ai_pass = ai_review.get("passed")
            result.ai_pass = ai_pass if type(ai_pass) is bool else None
            ai_score = ai_review.get("score")
            result.ai_score = (
                float(ai_score) if type(ai_score) is float else None
            )
            result.overall = (
                "合格"
                if result.limit_pass and result.ai_pass is not False
                else "不合格"
            )
            judgment = "PASS" if result.overall == "合格" else "FAIL"
            judgment_payload = _plain_result(result)
            current_trace_payload = None
            if job.snapshot.current_trace_json:
                stored_trace = _decode_object(
                    job.snapshot.current_trace_json,
                    "current trace",
                )
                current_config = dict(
                    config.get("current_meter")
                    if isinstance(config.get("current_meter"), dict)
                    else {}
                )
                current_config["unit"] = stored_trace.get("unit")
                current_trace_payload = build_current_trace_payload(
                    [
                        {
                            "time_sec": sample[0],
                            "current_a": sample[1],
                        }
                        for sample in stored_trace.get("samples", [])
                        if isinstance(sample, list) and len(sample) == 2
                    ],
                    analysis_window=tuple(result.analysis_window),
                    config=current_config,
                )
            chart_payload = build_chart_payload(
                buffer=samples,
                config=config,
                sample_rate=float(audio.sample_rate_hz),
                mic_cal=calibration,
                analysis_window=tuple(result.analysis_window),
                judgment_payload=judgment_payload,
                channel_column_map=mapping,
                loudness_trace_cache=loudness_trace_cache,
                allow_golden_runtime_resample=False,
                current_trace_payload=current_trace_payload,
            )
            _attach_stft_payload(
                chart_payload=chart_payload,
                samples=samples,
                config=config,
                calibration=calibration,
                sample_rate=float(audio.sample_rate_hz),
                channel_column_map=mapping,
            )
            summary_payload = build_summary_payload(
                config=config,
                judgment_payload=judgment_payload,
                ai_review=ai_review,
                chart_payload=chart_payload,
            )
            duration_sec = float(audio.frame_count) / float(audio.sample_rate_hz)
            legacy_result = _frozen_scalar(
                {
                    "legacy_overall": str(result.overall),
                    "limit_pass": bool(result.limit_pass),
                    "ai_pass": result.ai_pass,
                    "ai_score": result.ai_score,
                    "analysis_window": [
                        float(item) for item in result.analysis_window
                    ],
                    "channel_results": judgment_payload["channel_results"],
                    "review": {
                        "algorithm_revision": job.snapshot.algorithm_revision,
                        "workorder_snapshot_json": (
                            job.snapshot.workorder_snapshot_json
                        ),
                        "workorder": workorder,
                    },
                    "displayed_config_id": config_id,
                    "config_payload": config,
                    "captured_duration_sec": duration_sec,
                    "default_window": {
                        "start_sec": float(result.analysis_window[0]),
                        "end_sec": float(result.analysis_window[1]),
                    },
                    "fallback_window": {
                        "start_sec": 0.0,
                        "end_sec": duration_sec,
                    },
                    "manual_reanalysis_allowed": True,
                    "judgment_payload": judgment_payload,
                    "summary_payload": summary_payload,
                    "chart_payload": chart_payload,
                }
            )
            payload = _unchecked_frozen_result_object(
                (
                    ("judgment", judgment),
                    ("result", legacy_result),
                )
            )
            return AnalysisExecutionResult(
                AnalysisExecutionStatus.SUCCEEDED,
                payload,
            )
        except AnalysisExecutionError:
            raise
        except Exception as exc:
            raise _failure(
                f"frozen legacy analysis failed: {type(exc).__name__}"
            ) from exc


__all__ = [
    "ProductionAnalysisExecutor",
    "frozen_ai_snapshot_identity",
]
