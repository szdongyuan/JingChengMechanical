"""Frozen-snapshot production adapter for the legacy non-AI judgment path."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.golden_reference import FrozenGoldenArtifact
from app.service.analysis.judgment import JudgmentEngine, _runtime_channel_defaults
from app.service.ai_resident_runtime import ResidentToolchainRuntime
from app.service.analysis.review_payloads import (
    build_chart_payload,
    build_summary_feature_chart_payload,
    build_summary_payload,
)
from app.service.analysis.performance_diagnostics import (
    AnalysisStageTimings,
    AnalysisTimingBoundary,
    StageTimer,
)
from app.service.analysis.staged_execution import (
    AnalysisBundleBudget,
    AnalysisBundleIdentity,
    AnalysisBundleToken,
    AnalysisDecisionEnvelope,
    AnalysisEnrichmentReport,
    REQUIRED_ENRICHMENT_CHART_METRICS,
)
from app.service.analysis.staged_result_payload import (
    build_lightweight_canonical_result,
    validate_enrichment_compatibility,
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
_BUNDLE_METADATA_MAX_BYTES = 256 * 1024
_DEFERRED_FEATURE = "deferred-to-enrichment"


def _enabled(channel: dict, *keys: str) -> bool:
    return any(channel.get(key) is True for key in keys)


def _decision_feature_requirements(channel: dict) -> dict[str, bool]:
    return {
        "spl": _enabled(channel, "spl_lower_enabled", "spl_upper_enabled"),
        "spl_average": _enabled(
            channel,
            "spl_average_lower_enabled",
            "spl_average_upper_enabled",
        ),
        "loudness": _enabled(
            channel,
            "loudness_lower_enabled",
            "loudness_upper_enabled",
            "loudness_average_lower_enabled",
            "loudness_average_upper_enabled",
        ),
        "fft": bool(channel.get("fft_band_limits"))
        or str(channel.get("fft_golden_mode") or "off").lower() != "off",
        "stft": False,
        "octave": bool(channel.get("oct_band_limits"))
        or str(channel.get("oct_golden_mode") or "off").lower() != "off",
    }


def _has_deferred_features(features: tuple[_ChannelDspFeatures, ...]) -> bool:
    return any(
        error == _DEFERRED_FEATURE
        for feature in features
        for error in (
            feature.spl_error,
            feature.spl_average_error,
            feature.loudness_error,
            feature.fft_error,
            feature.stft_error,
            feature.octave_error,
        )
    )


def _readonly_array(value, *, copy: bool = False) -> np.ndarray:
    array = np.array(value, dtype=np.float64, copy=copy)
    array.setflags(write=False)
    return array


def _pretty_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def _flush_diagnostic_bytes(
    work_dir: str | Path,
    request_json: bytes | None,
    response_json: bytes | None,
) -> None:
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if request_json is not None:
        (directory / "request.json").write_bytes(request_json)
    if response_json is not None:
        (directory / "response.json").write_bytes(response_json)


@dataclass(frozen=True, slots=True)
class _ChannelDspFeatures:
    channel_index: int
    calibrated_signal: np.ndarray
    spl_times: np.ndarray
    spl_values: np.ndarray
    spl_average_db: float | None
    spl_error: str | None
    spl_average_error: str | None
    loudness_times: np.ndarray
    loudness_values: np.ndarray
    loudness_max_sone: float | None
    loudness_error: str | None
    fft_frequencies: np.ndarray
    fft_magnitudes: np.ndarray
    fft_error: str | None
    stft_times: np.ndarray
    stft_frequencies: np.ndarray
    stft_magnitudes: np.ndarray
    stft_error: str | None
    octave_centers: np.ndarray
    octave_levels: np.ndarray
    octave_error: str | None

@dataclass(frozen=True, slots=True)
class _AnalysisBundle:
    token: AnalysisBundleToken
    source_audio: np.ndarray
    sample_rate_hz: float
    frame_count: int
    channel_count: int
    dtype_size: int
    persisted_dtype: str
    config_snapshot_json: bytes
    calibration_snapshot_json: bytes
    channel_mapping_json: bytes
    current_trace_json: bytes | None
    features: tuple[_ChannelDspFeatures, ...]
    judgment: str
    decision_canonical_result_json: bytes
    ai_review_json: bytes
    diagnostic_work_dir: str | None
    diagnostic_request_json: bytes | None
    diagnostic_response_json: bytes | None

    def retained_metadata_bytes(self) -> int:
        return _retained_metadata_size(self)


def _bundle_ndarray_inventory(
    bundle: _AnalysisBundle,
) -> tuple[tuple[str, np.ndarray], ...]:
    """Recursively enumerate every ndarray retained by one bundle exactly once."""

    inventory: list[tuple[str, np.ndarray]] = []
    active: set[int] = set()

    def visit(value: object, path: str) -> None:
        if isinstance(value, np.ndarray):
            inventory.append((path, value))
            return
        if value is None or type(value) in (bool, int, float, str, bytes):
            return
        value_id = id(value)
        if value_id in active:
            raise ValueError(f"bundle inventory cycle at {path}")
        active.add(value_id)
        try:
            if is_dataclass(value):
                for field in fields(value):
                    child_path = f"{path}.{field.name}" if path else field.name
                    visit(getattr(value, field.name), child_path)
                return
            if type(value) is tuple:
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")
                return
            raise TypeError(
                f"unsupported bundle inventory type at {path}: "
                f"{type(value).__name__}"
            )
        finally:
            active.remove(value_id)

    visit(bundle, "")
    return tuple(inventory)


def _retained_metadata_size(value: object) -> int:
    """Deterministically measure all bundle-owned objects except ndarray data."""

    seen: set[int] = set()
    active: set[int] = set()

    def measure(item: object, path: str) -> int:
        if item is None or type(item) in (bool, int, float, str, bytes):
            return sys.getsizeof(item)
        if isinstance(item, np.ndarray):
            header = sys.getsizeof(item)
            return max(0, header - (item.nbytes if item.flags.owndata else 0))

        item_id = id(item)
        if item_id in active:
            raise ValueError(f"retained metadata cycle at {path}")
        if item_id in seen:
            return 0
        active.add(item_id)
        seen.add(item_id)
        try:
            if is_dataclass(item):
                return sys.getsizeof(item) + sum(
                    measure(getattr(item, field.name), f"{path}.{field.name}")
                    for field in fields(item)
                )
            if type(item) is tuple:
                return sys.getsizeof(item) + sum(
                    measure(child, f"{path}[{index}]")
                    for index, child in enumerate(item)
                )
            raise TypeError(
                f"unsupported retained metadata type at {path}: "
                f"{type(item).__name__}"
            )
        finally:
            active.remove(item_id)

    return measure(value, "bundle")


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
    channel_features: dict[int, object] | None = None,
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
        precomputed = (
            channel_features.get(channel_index)
            if isinstance(channel_features, dict)
            else None
        )
        channel_config = _runtime_channel_defaults(channel_index)
        if isinstance(channels.get(channel_index), dict):
            channel_config.update(channels[channel_index])
        try:
            if precomputed is not None:
                if precomputed.stft_error is not None:
                    raise RuntimeError(precomputed.stft_error)
                times = precomputed.stft_times
                frequencies = precomputed.stft_frequencies
                magnitudes = precomputed.stft_magnitudes
            else:
                column = channel_column_map.get(channel_index, channel_index)
                if samples.ndim == 1:
                    signal = np.asarray(samples, dtype=np.float64)
                elif 0 <= column < samples.shape[1]:
                    signal = np.asarray(samples[:, column], dtype=np.float64)
                else:
                    continue
                if signal.size == 0:
                    continue
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


def _compact_chart_numeric_arrays(chart_payload: dict) -> None:
    """Bound chart-only float precision within the documented parity budget."""

    axis_keys = {
        "times_sec",
        "frequency_times_sec",
        "frequencies_hz",
        "center_freqs_hz",
        "fundamental_frequency_values_hz",
        "dominant_frequency_values_hz",
        "spectral_centroid_values_hz",
        "frequency_bandwidth_values_hz",
        "x_axis_range_sec",
    }
    db_keys = {
        "magnitude_db",
        "levels_db",
        "values_db",
        "golden_overlay_levels_db",
    }

    def round_array(value, digits: int):
        if isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = round_array(item, digits)
            return value
        if type(value) is float:
            return round(value, digits)
        return value

    def visit(value, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if isinstance(item, list) and key_text in axis_keys:
                    round_array(item, 10)
                elif isinstance(item, list) and key_text in db_keys:
                    round_array(item, 5)
                elif (
                    isinstance(item, list)
                    and key_text in {"values", "values_sone"}
                    and "loudness" in path
                ):
                    round_array(item, 8)
                else:
                    visit(item, (*path, key_text))
        elif isinstance(value, list):
            for item in value:
                visit(item, path)

    visit(chart_payload)


def _chart_manifest(
    chart_payload: dict,
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    required = frozenset(REQUIRED_ENRICHMENT_CHART_METRICS)
    return tuple(
        sorted(
            (
                channel_index,
                tuple(
                    sorted(
                        metric_name
                        for metric_name in channel_payload
                        if metric_name in required
                    )
                ),
            )
            for channel_index, channel_payload in chart_payload.items()
            if type(channel_index) is int and type(channel_payload) is dict
        )
    )


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
    *,
    require_all: bool = False,
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
        channel_config = channel_configs[index]
        requirements = _decision_feature_requirements(channel_config)
        if require_all:
            requirements = {
                **requirements,
                "spl": True,
                "spl_average": True,
                "loudness": True,
                "fft": True,
                "octave": True,
            }
        for metric, available_field, error_field in (
            ("spl", "spl_trace_available", "spl_trace_error_message"),
            (
                "spl_average",
                "spl_average_available",
                "spl_average_error_message",
            ),
            ("fft", "fft_available", "fft_error_message"),
            ("octave", "octave_available", "octave_error_message"),
        ):
            if not requirements[metric]:
                continue
            if getattr(channel_result, available_field, None) is not True:
                raise ValueError(
                    f"required legacy calculation {available_field} is unavailable"
                )
            if getattr(channel_result, error_field, None) is not None:
                raise ValueError(
                    f"required legacy calculation {error_field} reported an error"
                )
        if requirements["spl"]:
            _require_finite_number(
                getattr(channel_result, "spl_db", None),
                "spl_db",
            )
            if type(getattr(channel_result, "spl_pass", None)) is not bool:
                raise ValueError("legacy spl_pass is missing")
        if requirements["spl_average"]:
            _require_finite_number(
                getattr(channel_result, "spl_average_db", None),
                "spl_average_db",
            )
            if type(getattr(channel_result, "spl_average_pass", None)) is not bool:
                raise ValueError("legacy spl_average_pass is missing")
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
    BUNDLE_METADATA_MAX_BYTES = _BUNDLE_METADATA_MAX_BYTES

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
        self._bundles: dict[str, _AnalysisBundle] = {}

    def close(self) -> None:
        self._bundles.clear()
        self._ai_runtime.shutdown_all()

    @staticmethod
    def _identity_digest(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _bundle_identity(cls, job: AnalysisJob) -> AnalysisBundleIdentity:
        return AnalysisBundleIdentity(
            run_id=job.run_id,
            analysis_attempt_id=job.analysis_attempt_id,
            content_revision=job.audio.content_revision,
            algorithm_revision=job.snapshot.algorithm_revision,
            config_identity=cls._identity_digest(
                job.snapshot.config_snapshot_json
            ),
            calibration_identity=cls._identity_digest(
                job.snapshot.calibration_snapshot_json
            ),
            channel_mapping_identity=cls._identity_digest(
                job.snapshot.channel_mapping_json
            ),
            window_start_seconds=float(
                job.snapshot.analysis_window_start_seconds
            ),
            window_end_seconds=float(job.snapshot.analysis_window_end_seconds),
        )
        if require_all:
            loudness_trace_required = True
            loudness_average_required = True

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            check_circular=False,
        )

    @staticmethod
    def _feature_map(
        features: tuple[_ChannelDspFeatures, ...],
    ) -> dict[int, _ChannelDspFeatures]:
        return {feature.channel_index: feature for feature in features}

    def _validate_bundle_budget(self, bundle: _AnalysisBundle) -> int:
        inventory = _bundle_ndarray_inventory(bundle)
        allowed_array_names = {
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
        if {name.split(".")[-1] for name, _array in inventory} != (
            allowed_array_names
        ):
            raise ValueError("analysis bundle ndarray inventory is not closed")
        if any(array.flags.writeable for _name, array in inventory):
            raise ValueError("analysis bundle arrays must be read-only")
        for name, array in inventory:
            if name != "source_audio" and array.dtype != np.dtype(np.float64):
                raise ValueError(f"bundle array {name} must be float64")
        if bundle.source_audio.flags.owndata:
            raise ValueError("analysis source audio must be a non-owning view")
        if bundle.source_audio.shape[0] != bundle.frame_count:
            raise ValueError("analysis source audio frame count changed")

        window_frames = max(
            0,
            min(
                bundle.frame_count,
                int(
                    bundle.token.identity.window_end_seconds
                    * bundle.sample_rate_hz
                ),
            )
            - max(
                0,
                int(
                    bundle.token.identity.window_start_seconds
                    * bundle.sample_rate_hz
                ),
            ),
        )
        config = _decode_object(
            bundle.config_snapshot_json.decode("utf-8"),
            "config",
        )
        configured_channels = {
            channel.get("channel_index"): channel
            for channel in config.get("channels", [])
            if isinstance(channel, dict)
        }
        configured_array_max = 0
        for feature in bundle.features:
            channel_config = _runtime_channel_defaults(feature.channel_index)
            channel_config.update(
                configured_channels.get(feature.channel_index, {})
            )
            if feature.calibrated_signal.shape != (window_frames,):
                raise ValueError("calibrated signal shape exceeds configured window")

            spl_chunk_frames = max(
                1,
                int(round(bundle.sample_rate_hz * 0.05)),
            )
            spl_max = math.ceil(window_frames / spl_chunk_frames) + 2
            if (
                feature.spl_times.ndim != 1
                or feature.spl_values.shape != feature.spl_times.shape
                or feature.spl_times.size > spl_max
            ):
                raise ValueError("SPL arrays exceed the configured window bound")

            loudness_max = (
                math.ceil(
                    window_frames
                    * dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ
                    / bundle.sample_rate_hz
                )
                + 2
            )
            if (
                feature.loudness_times.ndim != 1
                or feature.loudness_values.shape != feature.loudness_times.shape
                or feature.loudness_times.size > loudness_max
            ):
                raise ValueError(
                    "loudness arrays exceed the configured window bound"
                )

            fft_nfft = int(channel_config.get("fft_nfft", 4096))
            fft_bins_max = fft_nfft // 2 + 1
            if (
                feature.fft_frequencies.ndim != 1
                or feature.fft_magnitudes.shape
                != feature.fft_frequencies.shape
                or feature.fft_frequencies.size > fft_bins_max
            ):
                raise ValueError("FFT arrays exceed configured fft_nfft")

            stft_nfft = int(channel_config.get("stft_nfft", 1024))
            stft_overlap = float(channel_config.get("stft_overlap", 0.5))
            stft_bins_max = stft_nfft // 2 + 1
            actual_nperseg = min(stft_nfft, window_frames)
            if actual_nperseg <= 0:
                stft_columns_max = 0
            else:
                noverlap = int(actual_nperseg * stft_overlap)
                hop = max(1, actual_nperseg - noverlap)
                stft_columns_max = (
                    2
                    + max(
                        0,
                        math.ceil(
                            (window_frames - actual_nperseg) / hop
                        ),
                    )
                )
            if feature.stft_times.ndim != 1 or feature.stft_frequencies.ndim != 1:
                raise ValueError("STFT axes must be one-dimensional")
            if feature.stft_magnitudes.shape != (
                feature.stft_frequencies.size,
                feature.stft_times.size,
            ):
                raise ValueError("STFT magnitude matrix has an invalid shape")
            if (
                feature.stft_frequencies.size > stft_bins_max
                or feature.stft_times.size > stft_columns_max
            ):
                raise ValueError(
                    "STFT arrays exceed configured nfft/overlap bounds"
                )

            octave_max = 128 if window_frames > 0 else 0
            if (
                feature.octave_centers.ndim != 1
                or feature.octave_levels.shape != feature.octave_centers.shape
                or feature.octave_centers.size > octave_max
            ):
                raise ValueError(
                    "octave arrays exceed the configured window bound"
                )

            configured_array_max += (
                8 * window_frames
                + 16 * spl_max
                + 16 * loudness_max
                + 16 * fft_bins_max
                + 8 * stft_columns_max
                + 8 * stft_bins_max
                + 8 * stft_bins_max * stft_columns_max
                + 16 * octave_max
            )

        owning_arrays: dict[int, np.ndarray] = {
            id(array): array
            for _name, array in inventory
            if array.flags.owndata
        }
        owning_bytes = sum(array.nbytes for array in owning_arrays.values())
        if owning_bytes > configured_array_max:
            raise ValueError("analysis bundle exceeds configured array budgets")

        metadata_bytes = bundle.retained_metadata_bytes()
        if metadata_bytes > _BUNDLE_METADATA_MAX_BYTES:
            raise ValueError("analysis bundle metadata exceeds 256 KiB")
        actual_bytes = owning_bytes + metadata_bytes
        AnalysisBundleBudget(
            frame_count=bundle.frame_count,
            channel_count=bundle.channel_count,
            dtype_size=bundle.dtype_size,
        ).require_within(actual_bytes)
        return actual_bytes

    def _require_bundle(self, token: AnalysisBundleToken) -> _AnalysisBundle:
        if type(token) is not AnalysisBundleToken:
            raise TypeError("bundle token must be an exact AnalysisBundleToken")
        bundle = self._bundles.get(token.bundle_id)
        if bundle is None or bundle.token.slot_id != token.slot_id:
            raise ValueError("analysis bundle token is missing or expired")
        token.require_identity(bundle.token.identity)
        return bundle

    def bundle_array_inventory(
        self,
        token: AnalysisBundleToken,
    ) -> tuple[tuple[str, np.ndarray], ...]:
        """Return the closed ndarray inventory for contract tests/diagnostics."""

        return _bundle_ndarray_inventory(self._require_bundle(token))

    def bundle_retained_metadata_bytes(
        self,
        token: AnalysisBundleToken,
    ) -> int:
        return self._require_bundle(token).retained_metadata_bytes()

    def _build_channel_features(
        self,
        *,
        samples: np.ndarray,
        sample_rate: float,
        config: dict,
        calibration: dict,
        mapping: dict[int, int],
        start_sec: float,
        end_sec: float,
        required_only: bool = False,
        existing_features: dict[int, _ChannelDspFeatures] | None = None,
    ) -> tuple[_ChannelDspFeatures, ...]:
        start_sample = max(0, int(start_sec * sample_rate))
        end_sample = min(samples.shape[0], int(end_sec * sample_rate))
        channels = {
            item.get("channel_index"): item
            for item in config.get("channels", [])
            if isinstance(item, dict)
        }
        result: list[_ChannelDspFeatures] = []
        empty = lambda: _readonly_array(np.empty(0, dtype=np.float64))

        for channel_index in config.get("selected_channels", []):
            previous = (
                existing_features.get(channel_index)
                if isinstance(existing_features, dict)
                else None
            )
            column = mapping[channel_index]
            source_full = (
                samples if samples.ndim == 1 else samples[:, column]
            )
            channel_config = _runtime_channel_defaults(channel_index)
            channel_config.update(channels.get(channel_index, {}))
            sensitivity = _mic_sensitivity(calibration, channel_index)
            calibrated_full = dsp.apply_mic_calibration(
                source_full,
                sensitivity,
            )
            calibrated_signal = (
                previous.calibrated_signal
                if previous is not None
                else _readonly_array(
                    calibrated_full[start_sample:end_sample],
                    copy=True,
                )
            )

            def capture(call):
                try:
                    return call(), None
                except Exception as exc:
                    return None, str(exc) or exc.__class__.__name__

            available_calls = {
                "spl": lambda: dsp.compute_spl_trace(
                    calibrated_full,
                    sample_rate,
                    channel_config.get("spl_weighting", "Z"),
                    channel_config.get("spl_time_constant", 0.125),
                    chunk_duration=0.05,
                    output_start_sample=start_sample,
                    output_end_sample=end_sample,
                ),
                "spl_average": lambda: dsp.compute_spl_leq(
                    calibrated_full,
                    sample_rate,
                    channel_config.get("spl_weighting", "Z"),
                    start_sample=start_sample,
                    end_sample=end_sample,
                ),
                "loudness": lambda: (
                    dsp.compute_loudness_trace_for_window(
                        calibrated_full,
                        sample_rate,
                        start_sec,
                        end_sec,
                    )
                    if start_sec > 0.0
                    else dsp.compute_loudness_trace(
                        calibrated_signal,
                        sample_rate,
                    )
                ),
                "fft": lambda: dsp.compute_fft_spectrum(
                    calibrated_signal,
                    sample_rate,
                    channel_config.get("fft_nfft", 4096),
                    channel_config.get("fft_weighting", "Z"),
                ),
                "stft": lambda: dsp.compute_stft(
                    calibrated_signal,
                    sample_rate,
                    int(channel_config.get("stft_nfft", 1024)),
                    float(channel_config.get("stft_overlap", 0.5)),
                ),
                "octave": lambda: dsp.compute_octave_bands(
                    calibrated_signal,
                    sample_rate,
                    channel_config.get("oct_weighting", "Z"),
                ),
            }
            previous_values = (
                {
                    "spl": (
                        (previous.spl_times, previous.spl_values),
                        previous.spl_error,
                    ),
                    "spl_average": (
                        previous.spl_average_db,
                        previous.spl_average_error,
                    ),
                    "loudness": (
                        (
                            previous.loudness_times,
                            previous.loudness_values,
                            previous.loudness_max_sone,
                        ),
                        previous.loudness_error,
                    ),
                    "fft": (
                        (previous.fft_frequencies, previous.fft_magnitudes),
                        previous.fft_error,
                    ),
                    "stft": (
                        (
                            previous.stft_times,
                            previous.stft_frequencies,
                            previous.stft_magnitudes,
                        ),
                        previous.stft_error,
                    ),
                    "octave": (
                        (previous.octave_centers, previous.octave_levels),
                        previous.octave_error,
                    ),
                }
                if previous is not None
                else {}
            )
            requirements = _decision_feature_requirements(channel_config)
            completed = {}
            calls = {}
            for name, call in available_calls.items():
                previous_value = previous_values.get(name)
                if (
                    previous_value is not None
                    and previous_value[1] != _DEFERRED_FEATURE
                ):
                    completed[name] = previous_value
                elif required_only and not requirements[name]:
                    completed[name] = (None, _DEFERRED_FEATURE)
                else:
                    calls[name] = call
            if calls:
                with ThreadPoolExecutor(
                    max_workers=len(calls),
                    thread_name_prefix=f"analysis-features-ch{channel_index}",
                ) as feature_pool:
                    futures = {
                        name: feature_pool.submit(capture, call)
                        for name, call in calls.items()
                    }
                    completed.update(
                        {
                            name: future.result()
                            for name, future in futures.items()
                        }
                    )

            spl_times = empty()
            spl_values = empty()
            spl_result, spl_error = completed["spl"]
            if spl_error is None:
                raw_times, raw_values = spl_result
                spl_times = _readonly_array(raw_times, copy=True)
                spl_values = _readonly_array(raw_values, copy=True)
            spl_average_result, spl_average_error = completed["spl_average"]
            spl_average_db = (
                float(spl_average_result)
                if spl_average_error is None
                else None
            )

            loudness_times = empty()
            loudness_values = empty()
            loudness_max_sone = None
            loudness_raw, loudness_error = completed["loudness"]
            if loudness_error is None:
                loudness_times = _readonly_array(loudness_raw[0], copy=True)
                loudness_values = _readonly_array(loudness_raw[1], copy=True)
                loudness_max_sone = float(loudness_raw[2])

            fft_frequencies = empty()
            fft_magnitudes = empty()
            fft_raw, fft_error = completed["fft"]
            if fft_error is None:
                fft_frequencies = _readonly_array(fft_raw[0], copy=True)
                fft_magnitudes = _readonly_array(
                    np.asarray(fft_raw[1], dtype=np.float64)
                    - 20.0 * math.log10(20e-6),
                    copy=True,
                )

            stft_times = empty()
            stft_frequencies = empty()
            stft_magnitudes = _readonly_array(
                np.empty((0, 0), dtype=np.float64)
            )
            stft_raw, stft_error = completed["stft"]
            if stft_error is None:
                stft_times = _readonly_array(stft_raw[0], copy=True)
                stft_frequencies = _readonly_array(stft_raw[1], copy=True)
                stft_magnitudes = _readonly_array(stft_raw[2], copy=True)

            octave_centers = empty()
            octave_levels = empty()
            octave_raw, octave_error = completed["octave"]
            if octave_error is None:
                octave_centers = _readonly_array(octave_raw[0], copy=True)
                octave_levels = _readonly_array(
                    np.asarray(octave_raw[1], dtype=np.float64)
                    - 20.0 * math.log10(20e-6),
                    copy=True,
                )

            for field_name, value in (
                ("fft_frequencies", fft_frequencies),
                ("stft_times", stft_times),
                ("stft_frequencies", stft_frequencies),
                ("octave_centers", octave_centers),
            ):
                for existing in result:
                    existing_value = getattr(existing, field_name)
                    if np.array_equal(value, existing_value):
                        shared = existing_value.view()
                        shared.setflags(write=False)
                        if field_name == "fft_frequencies":
                            fft_frequencies = shared
                        elif field_name == "stft_times":
                            stft_times = shared
                        elif field_name == "stft_frequencies":
                            stft_frequencies = shared
                        else:
                            octave_centers = shared
                        break

            result.append(
                _ChannelDspFeatures(
                    channel_index=channel_index,
                    calibrated_signal=calibrated_signal,
                    spl_times=spl_times,
                    spl_values=spl_values,
                    spl_average_db=spl_average_db,
                    spl_error=spl_error,
                    spl_average_error=spl_average_error,
                    loudness_times=loudness_times,
                    loudness_values=loudness_values,
                    loudness_max_sone=loudness_max_sone,
                    loudness_error=loudness_error,
                    fft_frequencies=fft_frequencies,
                    fft_magnitudes=fft_magnitudes,
                    fft_error=fft_error,
                    stft_times=stft_times,
                    stft_frequencies=stft_frequencies,
                    stft_magnitudes=stft_magnitudes,
                    stft_error=stft_error,
                    octave_centers=octave_centers,
                    octave_levels=octave_levels,
                    octave_error=octave_error,
                )
            )
        return tuple(result)

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
        request_json = _pretty_json_bytes(request)
        if len(request_json) > _BUNDLE_METADATA_MAX_BYTES:
            _flush_diagnostic_bytes(work_dir, request_json, None)
            raise ValueError("AI request diagnostic exceeds 256 KiB")
        try:
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
        except Exception:
            _flush_diagnostic_bytes(work_dir, request_json, None)
            raise
        if transport.get("module_status") != "success":
            return {
                "status": str(
                    transport.get("module_status") or "process_error"
                ),
                "error_message": str(
                    transport.get("error_summary") or "AI analysis failed"
                ),
                "_diagnostic_work_dir": work_dir,
                "_diagnostic_request_json": request_json,
                "_diagnostic_response_json": None,
            }
        response = transport.get("response_payload")
        try:
            response_json = (
                _pretty_json_bytes(response)
                if isinstance(response, dict)
                else None
            )
        except Exception:
            _flush_diagnostic_bytes(work_dir, request_json, None)
            raise
        if response_json is not None and len(response_json) > (
            _BUNDLE_METADATA_MAX_BYTES
        ):
            _flush_diagnostic_bytes(work_dir, request_json, response_json)
            raise ValueError("AI response diagnostic exceeds 256 KiB")
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request_id
        ):
            return {
                "status": "protocol_error",
                "error_message": "AI response is invalid",
                "_diagnostic_work_dir": work_dir,
                "_diagnostic_request_json": request_json,
                "_diagnostic_response_json": response_json,
            }
        ai_result = response.get("ai_result")
        if not isinstance(ai_result, dict):
            return {
                "status": "protocol_error",
                "error_message": "AI result is missing",
                "_diagnostic_work_dir": work_dir,
                "_diagnostic_request_json": request_json,
                "_diagnostic_response_json": response_json,
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
        raw_label = ai_result.get("ai_label")
        label = raw_label if isinstance(raw_label, str) else None
        normalized_label = (label or "").strip().upper()
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
            "ai_label": label,
            "channel_index": channel_index,
            "error_message": None,
            "summary": ai_result.get("summary"),
            "_diagnostic_work_dir": work_dir,
            "_diagnostic_request_json": request_json,
            "_diagnostic_response_json": response_json,
        }

    def execute_decision(
        self,
        job: AnalysisJob,
        audio,
        cancel_token,
        *,
        eager_features: bool = False,
    ) -> AnalysisDecisionEnvelope:
        diagnostic_work_dir: Path | None = None
        diagnostic_request_json: bytes | None = None
        diagnostic_response_json: bytes | None = None
        stage_timer: StageTimer | None = None
        try:
            initial_snapshot_bytes = sum(
                len(value.encode("utf-8"))
                for value in (
                    job.snapshot.config_snapshot_json,
                    job.snapshot.calibration_snapshot_json,
                    job.snapshot.workorder_snapshot_json,
                    job.snapshot.channel_mapping_json,
                    job.snapshot.current_trace_json or "",
                )
            )
            if initial_snapshot_bytes > _BUNDLE_METADATA_MAX_BYTES:
                raise ValueError("frozen snapshots exceed bundle metadata limit")
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
            source_audio = np.asarray(audio.samples)
            if (
                source_audio.ndim not in (1, 2)
                or source_audio.shape[0] != audio.frame_count
                or audio.sample_rate_hz != job.audio.sample_rate_hz
                or audio.channel_count != job.audio.channel_count
                or audio.frame_count != job.audio.frame_count
            ):
                raise ValueError("loaded audio differs from frozen metadata")
            samples = _restore_dsp_sample_scale(source_audio, job.audio.dtype)
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
            identity = self._bundle_identity(job)
            stage_timer = StageTimer(identity=identity)
            stage_timer.start(AnalysisTimingBoundary.LEGACY_DECISION_DSP)
            stage_timer.start(
                AnalysisTimingBoundary.DECISION_FEATURE_EXTRACTION
            )
            features = self._build_channel_features(
                samples=samples,
                sample_rate=float(audio.sample_rate_hz),
                config=config,
                calibration=calibration,
                mapping=mapping,
                start_sec=float(job.snapshot.analysis_window_start_seconds),
                end_sec=float(job.snapshot.analysis_window_end_seconds),
                required_only=not eager_features,
            )
            stage_timer.finish(
                AnalysisTimingBoundary.DECISION_FEATURE_EXTRACTION
            )
            feature_map = self._feature_map(features)
            loudness_trace_cache: dict[int, dict] = {}
            stage_timer.start(AnalysisTimingBoundary.DECISION_JUDGMENT)
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
                channel_features=feature_map,
                run_ai=False,
                allow_golden_runtime_resample=False,
            )
            _validate_legacy_result(
                result,
                judged_channels,
                by_index,
                require_all=eager_features,
            )
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
            stage_timer.finish(AnalysisTimingBoundary.DECISION_JUDGMENT)
            stage_timer.finish(AnalysisTimingBoundary.LEGACY_DECISION_DSP)
            stage_timer.start(AnalysisTimingBoundary.RESIDENT_AI_RESPONSE)
            ai_review = self._run_frozen_ai(
                job=job,
                config=config,
                workorder=workorder,
                channel_order=channel_order,
            )
            stage_timer.finish(AnalysisTimingBoundary.RESIDENT_AI_RESPONSE)
            stage_timer.start(AnalysisTimingBoundary.DECISION_PROJECTION)
            diagnostic_work_dir = ai_review.pop("_diagnostic_work_dir", None)
            diagnostic_request_json = ai_review.pop(
                "_diagnostic_request_json", None
            )
            diagnostic_response_json = ai_review.pop(
                "_diagnostic_response_json", None
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
                        {"time_sec": sample[0], "current_a": sample[1]}
                        for sample in stored_trace.get("samples", [])
                        if isinstance(sample, list) and len(sample) == 2
                    ],
                    analysis_window=tuple(result.analysis_window),
                    config=current_config,
                )
            summary_chart_payload = build_summary_feature_chart_payload(
                config=config,
                sample_rate=float(audio.sample_rate_hz),
                analysis_window_start_sec=float(result.analysis_window[0]),
                channel_features=feature_map,
                current_trace_payload=current_trace_payload,
            )
            summary_payload = build_summary_payload(
                config=config,
                judgment_payload=judgment_payload,
                ai_review=ai_review,
                chart_payload=summary_chart_payload,
            )
            if _has_deferred_features(features):
                summary_payload["overall_status"] = (
                    "pass" if judgment == "PASS" else "fail"
                )
                summary_payload["overall_label"] = judgment
                summary_payload["error_message"] = None
            duration_sec = float(audio.frame_count) / float(audio.sample_rate_hz)
            result_without_charts = _plain_result(
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
                        "ai_result": {
                            "status": ai_review.get("status"),
                            "score": ai_review.get("score"),
                            "ai_label": ai_review.get("ai_label"),
                        },
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
                    "chart_payload": {},
                }
            )
            token = AnalysisBundleToken(
                slot_id=0,
                bundle_id=uuid.uuid4().hex,
                identity=identity,
            )
            canonical = build_lightweight_canonical_result(
                judgment=judgment,
                result=result_without_charts,
            )
            source_audio_view = source_audio.view()
            source_audio_view.setflags(write=False)
            bundle = _AnalysisBundle(
                token=token,
                source_audio=source_audio_view,
                sample_rate_hz=float(job.audio.sample_rate_hz),
                frame_count=job.audio.frame_count,
                channel_count=job.audio.channel_count,
                dtype_size=np.dtype(job.audio.dtype).itemsize,
                persisted_dtype=job.audio.dtype,
                config_snapshot_json=(
                    job.snapshot.config_snapshot_json.encode("utf-8")
                ),
                calibration_snapshot_json=(
                    job.snapshot.calibration_snapshot_json.encode("utf-8")
                ),
                channel_mapping_json=(
                    job.snapshot.channel_mapping_json.encode("utf-8")
                ),
                current_trace_json=(
                    job.snapshot.current_trace_json.encode("utf-8")
                    if job.snapshot.current_trace_json
                    else None
                ),
                features=features,
                judgment=judgment,
                decision_canonical_result_json=canonical.encode("utf-8"),
                ai_review_json=self._canonical_json(ai_review).encode("utf-8"),
                diagnostic_work_dir=(
                    str(diagnostic_work_dir)
                    if isinstance(diagnostic_work_dir, Path)
                    else None
                ),
                diagnostic_request_json=(
                    diagnostic_request_json
                    if type(diagnostic_request_json) is bytes
                    else None
                ),
                diagnostic_response_json=(
                    diagnostic_response_json
                    if type(diagnostic_response_json) is bytes
                    else None
                ),
            )
            bundle_bytes = self._validate_bundle_budget(bundle)
            self._bundles[token.bundle_id] = bundle
            stage_timer.finish(
                AnalysisTimingBoundary.DECISION_PROJECTION,
                byte_count=bundle_bytes,
            )
            return AnalysisDecisionEnvelope(
                identity=identity,
                judgment=judgment,
                canonical_result_json=canonical,
                summary_payload_json=self._canonical_json(summary_payload),
                bundle_token=token,
                timings=stage_timer.snapshot(),
            )
        except AnalysisExecutionError:
            raise
        except Exception as exc:
            if diagnostic_work_dir is not None:
                _flush_diagnostic_bytes(
                    diagnostic_work_dir,
                    diagnostic_request_json,
                    diagnostic_response_json,
                )
            raise _failure(
                f"frozen decision analysis failed: {type(exc).__name__}"
            ) from exc

    def execute_enrichment(
        self,
        bundle_token: AnalysisBundleToken,
    ) -> AnalysisEnrichmentReport:
        try:
            bundle = self._require_bundle(bundle_token)
            stage_timer = StageTimer(identity=bundle.token.identity)
            stage_timer.start(AnalysisTimingBoundary.ENRICHMENT_DSP)
            if bundle.diagnostic_work_dir is not None:
                _flush_diagnostic_bytes(
                    bundle.diagnostic_work_dir,
                    bundle.diagnostic_request_json,
                    bundle.diagnostic_response_json,
                )
            config = _decode_object(
                bundle.config_snapshot_json.decode("utf-8"),
                "config",
            )
            calibration = _decode_object(
                bundle.calibration_snapshot_json.decode("utf-8"),
                "calibration",
            )
            mapping_raw = _decode_object(
                bundle.channel_mapping_json.decode("utf-8"),
                "channel mapping",
            )
            channel_mapping = {
                channel_index: column_index
                for column_index, channel_index in enumerate(
                    mapping_raw["channel_order"]
                )
            }
            config_id = config.get("config_id")
            for channel in config.get("channels", []):
                channel_index = channel.get("channel_index")
                golden_enabled = any(
                    str(channel.get(key) or "off").lower() != "off"
                    for key in ("fft_golden_mode", "oct_golden_mode")
                )
                if golden_enabled:
                    artifact = self._golden.get((config_id, channel_index))
                    if type(artifact) is not FrozenGoldenArtifact:
                        raise ValueError("frozen golden artifact is missing")
                    if artifact.rehash() != artifact.sha256:
                        raise ValueError("frozen golden artifact digest changed")
                    channel["_frozen_golden_artifact"] = artifact
            decision_canonical = bundle.decision_canonical_result_json.decode(
                "utf-8"
            )
            decision_root = json.loads(decision_canonical)
            result_without_charts = decision_root["result"]
            restored_samples = _restore_dsp_sample_scale(
                bundle.source_audio,
                bundle.persisted_dtype,
            )
            full_features = self._build_channel_features(
                samples=restored_samples,
                sample_rate=bundle.sample_rate_hz,
                config=config,
                calibration=calibration,
                mapping=channel_mapping,
                start_sec=bundle.token.identity.window_start_seconds,
                end_sec=bundle.token.identity.window_end_seconds,
                existing_features=self._feature_map(bundle.features),
            )
            feature_map = self._feature_map(full_features)
            deferred_features = _has_deferred_features(bundle.features)
            if deferred_features:
                full_judgment = self._judgment.rejudge(
                    config,
                    restored_samples,
                    bundle.sample_rate_hz,
                    calibration,
                    "",
                    bundle.token.identity.window_start_seconds,
                    bundle.token.identity.window_end_seconds,
                    channel_column_map=channel_mapping,
                    loudness_trace_cache={},
                    channel_features=feature_map,
                    run_ai=False,
                    allow_golden_runtime_resample=False,
                )
                selected = config.get("selected_channels", [])
                by_index = {
                    channel["channel_index"]: channel
                    for channel in config.get("channels", [])
                }
                vibration_channel = config.get("vibration_view_channel_index")
                judged_channels = (
                    [index for index in selected if index != vibration_channel]
                    if type(vibration_channel) is int
                    and vibration_channel in selected
                    else list(selected)
                )
                _validate_legacy_result(
                    full_judgment,
                    judged_channels,
                    by_index,
                    require_all=True,
                )
                full_judgment.ai_pass = result_without_charts["ai_pass"]
                full_judgment.ai_score = result_without_charts["ai_score"]
                full_judgment.overall = (
                    "合格"
                    if full_judgment.limit_pass
                    and full_judgment.ai_pass is not False
                    else "不合格"
                )
                if (
                    "PASS" if full_judgment.overall == "合格" else "FAIL"
                ) != bundle.judgment:
                    raise ValueError("enrichment changed the Decision judgment")
                judgment_payload = _plain_result(full_judgment)
            else:
                judgment_payload = result_without_charts["judgment_payload"]
            ai_review = _decode_object(
                bundle.ai_review_json.decode("utf-8"),
                "AI review",
            )
            loudness_trace_cache = {
                feature.channel_index: {
                    "times_sec": feature.loudness_times,
                    "values_sone": feature.loudness_values,
                    "max_sone": feature.loudness_max_sone,
                    "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
                    "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
                }
                for feature in full_features
                if feature.loudness_error is None
                and feature.loudness_values.size > 0
            }
            current_trace_payload = None
            if bundle.current_trace_json is not None:
                stored_trace = _decode_object(
                    bundle.current_trace_json.decode("utf-8"),
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
                        {"time_sec": sample[0], "current_a": sample[1]}
                        for sample in stored_trace.get("samples", [])
                        if isinstance(sample, list) and len(sample) == 2
                    ],
                    analysis_window=(
                        bundle.token.identity.window_start_seconds,
                        bundle.token.identity.window_end_seconds,
                    ),
                    config=current_config,
                )
            if deferred_features:
                summary_chart_payload = build_summary_feature_chart_payload(
                    config=config,
                    sample_rate=bundle.sample_rate_hz,
                    analysis_window_start_sec=(
                        bundle.token.identity.window_start_seconds
                    ),
                    channel_features=feature_map,
                    current_trace_payload=current_trace_payload,
                )
                summary_payload = build_summary_payload(
                    config=config,
                    judgment_payload=judgment_payload,
                    ai_review=ai_review,
                    chart_payload=summary_chart_payload,
                )
            else:
                summary_payload = result_without_charts["summary_payload"]
            chart_payload = build_chart_payload(
                buffer=bundle.source_audio,
                config=config,
                sample_rate=bundle.sample_rate_hz,
                mic_cal=calibration,
                analysis_window=(
                    bundle.token.identity.window_start_seconds,
                    bundle.token.identity.window_end_seconds,
                ),
                judgment_payload=judgment_payload,
                channel_column_map=channel_mapping,
                loudness_trace_cache=loudness_trace_cache,
                channel_features=feature_map,
                allow_golden_runtime_resample=False,
                current_trace_payload=current_trace_payload,
            )
            _attach_stft_payload(
                chart_payload=chart_payload,
                samples=restored_samples,
                config=config,
                calibration=calibration,
                sample_rate=bundle.sample_rate_hz,
                channel_column_map=channel_mapping,
                channel_features=(
                    feature_map
                    if (
                        math.isclose(
                            bundle.token.identity.window_start_seconds,
                            0.0,
                            abs_tol=1e-12,
                        )
                        and math.isclose(
                            bundle.token.identity.window_end_seconds,
                            float(bundle.frame_count) / bundle.sample_rate_hz,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    )
                    else None
                ),
            )
            _compact_chart_numeric_arrays(chart_payload)
            chart_manifest = _chart_manifest(chart_payload)
            stage_timer.finish(AnalysisTimingBoundary.ENRICHMENT_DSP)
            stage_timer.start(AnalysisTimingBoundary.CANONICAL_ENCODING)
            enriched_result = dict(result_without_charts)
            if deferred_features:
                enriched_result["legacy_overall"] = str(full_judgment.overall)
                enriched_result["limit_pass"] = bool(full_judgment.limit_pass)
                enriched_result["channel_results"] = judgment_payload[
                    "channel_results"
                ]
                enriched_result["judgment_payload"] = judgment_payload
                enriched_result["summary_payload"] = summary_payload
            enriched_result["chart_payload"] = chart_payload
            canonical = self._canonical_json(
                {"judgment": bundle.judgment, "result": enriched_result}
            )
            validate_enrichment_compatibility(
                decision_identity=bundle.token.identity,
                enrichment_identity=bundle.token.identity,
                decision_canonical_result_json=(
                    decision_canonical
                ),
                enrichment_canonical_result_json=canonical,
            )
            canonical_bytes = canonical.encode("utf-8")
            stage_timer.finish(
                AnalysisTimingBoundary.CANONICAL_ENCODING,
                byte_count=len(canonical_bytes),
            )
            stage_timer.start(AnalysisTimingBoundary.CANONICAL_COMPRESSION)
            compressed = zlib.compress(canonical_bytes, level=0)
            stage_timer.finish(
                AnalysisTimingBoundary.CANONICAL_COMPRESSION,
                byte_count=len(compressed),
            )
            report = AnalysisEnrichmentReport(
                identity=bundle.token.identity,
                judgment=bundle.judgment,
                canonical_result_json=canonical,
                result_payload_zlib=compressed,
                chart_manifest=chart_manifest,
                timings=stage_timer.snapshot(),
            )
            del self._bundles[bundle.token.bundle_id]
            return report
        except AnalysisExecutionError:
            raise
        except Exception as exc:
            raise _failure(
                f"frozen enrichment analysis failed: {type(exc).__name__}"
            ) from exc

    def execute(self, job: AnalysisJob, audio, cancel_token) -> AnalysisExecutionResult:
        decision = self.execute_decision(
            job,
            audio,
            cancel_token,
            eager_features=True,
        )
        try:
            enriched = self.execute_enrichment(decision.bundle_token)
        except Exception:
            self._bundles.pop(decision.bundle_token.bundle_id, None)
            raise
        root = json.loads(enriched.canonical_result_json)
        return AnalysisExecutionResult(
            AnalysisExecutionStatus.SUCCEEDED,
            _frozen_scalar(root),
        )


__all__ = [
    "ProductionAnalysisExecutor",
    "frozen_ai_snapshot_identity",
]
