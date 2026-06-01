from __future__ import annotations

import copy
import logging
import math
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from app.service.analysis import dsp, stft_score
from app.service.analysis.judgment import JudgmentEngine, _runtime_channel_defaults
from app.service.analysis.review_payloads import (
    build_chart_payload,
    build_summary_payload,
    canonical_channel_label,
)
from app.services_container import (
    FactoryReviewPayload,
    FactoryReviewWindow,
    ImportAudioWarning,
)

_SHORT_CLAMPED_WINDOW_THRESHOLD_SEC = 0.25
_MIN_MANUAL_REANALYSIS_DURATION_SEC = 0.1
_FFT_SINGLE_CONTROL_POINT_ERROR = "FFT 至少需要两个不同频率的控制点"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportedWav:
    path: str
    sample_rate: float
    buffer: np.ndarray

    @property
    def channel_count(self) -> int:
        return int(self.buffer.shape[1])

    @property
    def duration_sec(self) -> float:
        return float(self.buffer.shape[0]) / float(self.sample_rate)


@dataclass(frozen=True)
class ImportedChannelAssignment:
    wav_channel_index: int
    mode: str
    channel_index: int | None = None


@dataclass(frozen=True)
class ImportedAudioAnalysisRequest:
    config_id: int
    wav_path: str
    assignments: tuple[ImportedChannelAssignment, ...]
    start_sec: float | None = None
    duration_sec: float | None = None
    use_mic_calibration: bool = True


@dataclass(frozen=True)
class ImportedAudioAnalysisInputs:
    config: dict
    mic_cal: dict
    wav: ImportedWav


def normalize_wav_samples(data: np.ndarray) -> np.ndarray:
    """Return a finite float64 ``(samples, channels)`` buffer."""
    array = np.asarray(data)
    if array.ndim > 2:
        raise ValueError("WAV 数据维度或通道格式不支持")
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    if array.ndim != 2:
        raise ValueError("WAV 数据维度或通道格式不支持")
    if array.shape[1] <= 0:
        raise ValueError("WAV 通道数量无效")

    dtype = array.dtype
    if np.issubdtype(dtype, np.floating):
        normalized = array.astype(np.float64, copy=False)
    elif np.issubdtype(dtype, np.signedinteger):
        scale = float(2 ** (np.iinfo(dtype).bits - 1))
        normalized = array.astype(np.float64) / scale
    elif dtype == np.dtype("uint8"):
        normalized = (array.astype(np.float64) - 128.0) / 128.0
    else:
        raise ValueError("WAV 数据格式或类型不支持")

    if not np.all(np.isfinite(normalized)):
        raise ValueError("WAV 数据包含非有限数值")
    return np.asarray(normalized, dtype=np.float64)


def load_wav(path: str) -> ImportedWav:
    """Read a WAV file and return normalized audio plus metadata."""
    wav_path = Path(path)
    if wav_path.suffix.lower() != ".wav":
        raise ValueError("请选择 WAV 文件")

    try:
        sample_rate, data = wavfile.read(str(wav_path))
    except Exception as exc:  # pragma: no cover - scipy exception type varies
        raise ValueError("WAV 文件读取失败") from exc

    try:
        sample_rate_value = float(sample_rate)
    except (TypeError, ValueError):
        raise ValueError("WAV 采样率无效") from None
    if not math.isfinite(sample_rate_value) or sample_rate_value <= 0.0:
        raise ValueError("WAV 采样率无效")

    buffer = normalize_wav_samples(np.asarray(data))
    if buffer.shape[0] <= 0:
        raise ValueError("WAV 文件为空或没有样本")

    return ImportedWav(
        path=str(wav_path),
        sample_rate=sample_rate_value,
        buffer=buffer,
    )


def selected_config_channels(config: dict) -> tuple[int, ...]:
    """Return normalized non-duplicate selected config channel indexes."""
    selected: list[int] = []
    seen: set[int] = set()
    for value in config.get("selected_channels", []):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        if value in seen:
            continue
        selected.append(value)
        seen.add(value)
    return tuple(selected)


def default_channel_assignments(
    config: dict,
    wav_channel_count: int,
) -> tuple[ImportedChannelAssignment, ...]:
    """Map matching selected config channels and mark remaining WAV channels chart-only."""
    wav_channel_count = _validate_wav_channel_count(wav_channel_count)
    selected = set(selected_config_channels(config))
    assignments: list[ImportedChannelAssignment] = []
    for wav_channel_index in range(wav_channel_count):
        if wav_channel_index in selected:
            assignments.append(
                ImportedChannelAssignment(
                    wav_channel_index=wav_channel_index,
                    mode="config",
                    channel_index=wav_channel_index,
                )
            )
        else:
            assignments.append(
                ImportedChannelAssignment(
                    wav_channel_index=wav_channel_index,
                    mode="chart",
                    channel_index=None,
                )
            )
    return tuple(assignments)


def normalize_channel_assignments(
    assignments,
    *,
    wav_channel_count: int,
    allowed_config_channels: tuple[int, ...],
) -> tuple[ImportedChannelAssignment, ...]:
    """Validate one-off WAV channel assignments for this import request."""
    wav_channel_count = _validate_wav_channel_count(wav_channel_count)
    allowed = set(allowed_config_channels)
    normalized: list[ImportedChannelAssignment] = []
    used_wav_channels: set[int] = set()
    used_config_channels: set[int] = set()
    active_count = 0

    for raw_assignment in assignments or ():
        assignment = _coerce_channel_assignment(raw_assignment)
        wav_channel_index = _coerce_non_negative_int(
            assignment.wav_channel_index,
            message="WAV 通道索引无效",
        )
        if wav_channel_index < 0 or wav_channel_index >= wav_channel_count:
            raise ValueError("WAV 通道索引超出范围")
        if wav_channel_index in used_wav_channels:
            raise ValueError("WAV 通道映射重复")
        used_wav_channels.add(wav_channel_index)

        mode = str(assignment.mode or "").strip().lower()
        if mode not in {"config", "chart", "ignore"}:
            raise ValueError("WAV 通道映射模式无效")

        channel_index = assignment.channel_index
        if mode == "config":
            channel_index = _coerce_non_negative_int(
                channel_index,
                message="配置通道索引无效",
            )
            if channel_index not in allowed:
                raise ValueError("只能映射到已选择的配置通道")
            if channel_index in used_config_channels:
                raise ValueError("配置通道映射重复")
            used_config_channels.add(channel_index)
            active_count += 1
        elif mode == "chart":
            channel_index = None
            active_count += 1
        else:
            channel_index = None

        normalized.append(
            ImportedChannelAssignment(
                wav_channel_index=wav_channel_index,
                mode=mode,
                channel_index=channel_index,
            )
        )

    if active_count <= 0:
        raise ValueError("请至少选择一个 WAV 通道用于分析")
    normalized.sort(key=lambda item: item.wav_channel_index)
    return tuple(normalized)


def resolve_analysis_window(
    config,
    *,
    wav_duration_sec,
    requested_start_sec,
    requested_duration_sec,
) -> tuple[tuple[float, float], tuple[ImportAudioWarning, ...]]:
    """Resolve and validate the effective import analysis window."""
    wav_duration = _coerce_finite_float(wav_duration_sec, label="WAV 时长")
    if wav_duration <= 0.0:
        raise ValueError("WAV 时长无效")

    if requested_start_sec is None:
        offset_ms = _coerce_finite_float(
            config.get("analysis_window_offset_ms", 0),
            label="起始时间",
        )
        start_sec = _coerce_finite_float(
            offset_ms / 1000.0,
            label="起始时间",
        )
    else:
        start_sec = _coerce_finite_float(requested_start_sec, label="起始时间")
    if start_sec < 0.0:
        raise ValueError("起始时间不能小于 0")
    if start_sec >= wav_duration:
        raise ValueError("起始时间超出 WAV 时长")

    if requested_duration_sec is None:
        duration_sec = _coerce_finite_float(
            config.get("analysis_duration", 0),
            label="分析时长",
        )
    else:
        duration_sec = _coerce_finite_float(
            requested_duration_sec,
            label="分析时长",
        )
    if duration_sec <= 0.0:
        raise ValueError("分析时长必须大于 0")

    requested_end = start_sec + duration_sec
    end_sec = min(requested_end, wav_duration)
    warnings: list[ImportAudioWarning] = []
    if end_sec < requested_end:
        warnings.append(
            ImportAudioWarning(
                code="analysis_window_clamped",
                message="分析窗口结束时间已裁剪到 WAV 文件末尾",
                severity="warning",
            )
        )
        if end_sec - start_sec < _SHORT_CLAMPED_WINDOW_THRESHOLD_SEC:
            warnings.append(
                ImportAudioWarning(
                    code="analysis_window_short_after_clamp",
                    message="裁剪后的分析窗口过短，结果可能不稳定",
                    severity="warning",
                )
            )

    return (float(start_sec), float(end_sec)), tuple(warnings)


def analyze_imported_wav(
    *,
    request: ImportedAudioAnalysisRequest,
    config: dict,
    mic_cal: dict,
    judgment_engine: JudgmentEngine,
) -> FactoryReviewPayload:
    """Build a temporary imported-audio review payload without persistence."""
    wav = load_wav(request.wav_path)
    warnings: list[ImportAudioWarning] = []

    analysis_window, window_warnings = resolve_analysis_window(
        config,
        wav_duration_sec=wav.duration_sec,
        requested_start_sec=request.start_sec,
        requested_duration_sec=request.duration_sec,
    )
    warnings.extend(window_warnings)
    start_sec, end_sec = analysis_window

    assignments = normalize_channel_assignments(
        request.assignments,
        wav_channel_count=wav.channel_count,
        allowed_config_channels=selected_config_channels(config),
    )
    shaped = _shape_import_configs(
        source_config=config,
        assignments=assignments,
        sample_rate=wav.sample_rate,
        warnings=warnings,
    )
    effective_mic_cal = _effective_import_mic_cal(
        mic_cal,
        mapped_config_channels=tuple(shaped["judgment_selected_channels"]),
        use_mic_calibration=request.use_mic_calibration,
        warnings=warnings,
    )

    loudness_trace_cache: dict[int, dict] = {}
    judgment_result = judgment_engine.rejudge(
        shaped["judgment_config"],
        wav.buffer,
        wav.sample_rate,
        effective_mic_cal,
        wav.path,
        start_sec,
        end_sec,
        run_ai=False,
        allow_golden_runtime_resample=True,
        channel_column_map=shaped["judgment_channel_column_map"],
        loudness_trace_cache=loudness_trace_cache,
    )
    judgment_payload = _serialize_judgment_result(judgment_result)
    _apply_forced_no_results(
        judgment_payload,
        forced_no_results=shaped["forced_no_results"],
    )
    _collect_golden_unavailable_from_judgment(
        judgment_payload=judgment_payload,
        channel_configs=shaped["judgment_channels_by_index"],
        channel_column_map=shaped["judgment_channel_column_map"],
        warnings=warnings,
        forced_no_results=shaped["forced_no_results"],
    )

    summary_payload = build_summary_payload(
        config=shaped["judgment_config"],
        judgment_payload=judgment_payload,
        ai_review={"status": "skipped"},
    )
    _mark_forced_no_result_summary_rows(
        summary_payload,
        forced_no_results=shaped["forced_no_results"],
    )
    _mark_imported_ai_not_run(summary_payload)
    chart_judgment_payload = _judgment_payload_for_measured_import_charts(
        judgment_payload,
        measured_chart_forced_no_results=shaped["measured_chart_forced_no_results"],
    )

    chart_payload = build_chart_payload(
        buffer=wav.buffer,
        config=shaped["chart_config"],
        sample_rate=wav.sample_rate,
        mic_cal=effective_mic_cal,
        analysis_window=analysis_window,
        judgment_payload=chart_judgment_payload,
        allow_golden_runtime_resample=True,
        channel_column_map=shaped["chart_channel_column_map"],
        loudness_trace_cache=loudness_trace_cache,
    )
    _collect_golden_unavailable_from_chart(
        chart_payload=chart_payload,
        channel_configs=shaped["chart_channels_by_index"],
        channel_column_map=shaped["chart_channel_column_map"],
        warnings=warnings,
    )
    _add_imported_stft_payload(
        chart_payload=chart_payload,
        chart_config=shaped["chart_config"],
        chart_channel_column_map=shaped["chart_channel_column_map"],
        wav=wav,
        analysis_window=analysis_window,
        mic_cal=effective_mic_cal,
        warnings=warnings,
    )

    payload = FactoryReviewPayload(
        displayed_config_id=request.config_id,
        config_payload=copy.deepcopy(shaped["chart_config"]),
        run_id=f"import_{request.config_id}_{uuid.uuid4().hex}",
        trigger_source="import_audio",
        captured_duration_sec=wav.duration_sec,
        default_window=FactoryReviewWindow(start_sec, end_sec),
        fallback_window=FactoryReviewWindow(0.0, wav.duration_sec),
        manual_reanalysis_allowed=(
            wav.duration_sec >= _MIN_MANUAL_REANALYSIS_DURATION_SEC
        ),
        judgment_payload=judgment_payload,
        summary_payload=summary_payload,
        chart_payload=chart_payload,
        import_warnings=tuple(warnings),
        display_channel_labels=shaped["display_channel_labels"],
    )
    _log_import_warnings(
        warnings,
        config_id=request.config_id,
    )
    return payload


def _shape_import_configs(
    *,
    source_config: dict,
    assignments: tuple[ImportedChannelAssignment, ...],
    sample_rate: float,
    warnings: list[ImportAudioWarning],
) -> dict:
    source_config = copy.deepcopy(source_config)
    source_channels = _source_channels_by_index(source_config)
    judgment_selected: list[int] = []
    judgment_channels: list[dict] = []
    judgment_channel_map: dict[int, int] = {}
    chart_selected: list[int] = []
    chart_channels: list[dict] = []
    chart_channel_map: dict[int, int] = {}
    labels: dict[int, str] = {}
    forced_no_results: dict[int, set[str]] = {}
    measured_chart_forced_no_results: dict[int, set[str]] = {}

    used_chart_ids = _source_channel_ids(source_config)
    next_chart_only_id = max(used_chart_ids | set(selected_config_channels(source_config)) | {-1}) + 1

    for assignment in assignments:
        if assignment.mode == "config":
            channel_index = int(assignment.channel_index)
            channel_config = _copy_source_channel_config(
                source_channels,
                channel_index,
            )
            no_result_metrics, measured_chart_metrics = _shape_frequency_limits_for_import(
                channel_config=channel_config,
                sample_rate=sample_rate,
                channel_index=channel_index,
                wav_channel_index=assignment.wav_channel_index,
                warnings=warnings,
            )
            _inspect_golden_configuration(
                channel_config=channel_config,
                sample_rate=sample_rate,
                channel_index=channel_index,
                wav_channel_index=assignment.wav_channel_index,
                warnings=warnings,
                forced_no_results=forced_no_results,
            )
            if no_result_metrics:
                forced_no_results.setdefault(channel_index, set()).update(no_result_metrics)
            if measured_chart_metrics:
                measured_chart_forced_no_results.setdefault(channel_index, set()).update(
                    measured_chart_metrics
                )
            judgment_selected.append(channel_index)
            judgment_channels.append(copy.deepcopy(channel_config))
            judgment_channel_map[channel_index] = assignment.wav_channel_index
            chart_selected.append(channel_index)
            chart_channels.append(copy.deepcopy(channel_config))
            chart_channel_map[channel_index] = assignment.wav_channel_index
            labels[channel_index] = f"CH{channel_index + 1} / WAV {assignment.wav_channel_index + 1}"
        elif assignment.mode == "chart":
            chart_only_id = _allocate_chart_only_id(used_chart_ids, next_chart_only_id)
            next_chart_only_id = chart_only_id + 1
            chart_config = _chart_only_channel_config(chart_only_id)
            chart_selected.append(chart_only_id)
            chart_channels.append(chart_config)
            chart_channel_map[chart_only_id] = assignment.wav_channel_index
            labels[chart_only_id] = f"仅图表 / WAV {assignment.wav_channel_index + 1}"
            warnings.append(
                ImportAudioWarning(
                    code="chart_only_no_judgment",
                    message="仅图表通道不参与规则判定",
                    severity="info",
                    channel_index=chart_only_id,
                    wav_channel_index=assignment.wav_channel_index,
                )
            )

    judgment_config = copy.deepcopy(source_config)
    judgment_config["selected_channels"] = judgment_selected
    judgment_config["channels"] = judgment_channels

    chart_config = copy.deepcopy(source_config)
    chart_config["selected_channels"] = chart_selected
    chart_config["channels"] = chart_channels

    return {
        "judgment_config": judgment_config,
        "chart_config": chart_config,
        "judgment_selected_channels": tuple(judgment_selected),
        "judgment_channel_column_map": judgment_channel_map,
        "chart_channel_column_map": chart_channel_map,
        "display_channel_labels": labels,
        "forced_no_results": forced_no_results,
        "measured_chart_forced_no_results": measured_chart_forced_no_results,
        "judgment_channels_by_index": {
            channel["channel_index"]: channel
            for channel in judgment_channels
            if isinstance(channel.get("channel_index"), int)
        },
        "chart_channels_by_index": {
            channel["channel_index"]: channel
            for channel in chart_channels
            if isinstance(channel.get("channel_index"), int)
        },
    }


def _source_channels_by_index(config: dict) -> dict[int, dict]:
    channels: dict[int, dict] = {}
    for channel in config.get("channels", []):
        if not isinstance(channel, dict):
            continue
        channel_index = channel.get("channel_index")
        if isinstance(channel_index, bool) or not isinstance(channel_index, int):
            continue
        channels[channel_index] = copy.deepcopy(channel)
    return channels


def _source_channel_ids(config: dict) -> set[int]:
    ids: set[int] = set()
    for channel in config.get("channels", []):
        if isinstance(channel, dict):
            channel_index = channel.get("channel_index")
            if isinstance(channel_index, int) and not isinstance(channel_index, bool):
                ids.add(channel_index)
    return ids


def _copy_source_channel_config(source_channels: dict[int, dict], channel_index: int) -> dict:
    channel_config = _runtime_channel_defaults(channel_index)
    if isinstance(source_channels.get(channel_index), dict):
        channel_config.update(copy.deepcopy(source_channels[channel_index]))
    channel_config["channel_index"] = channel_index
    return channel_config


def _allocate_chart_only_id(used_ids: set[int], start_at: int) -> int:
    candidate = int(start_at)
    while candidate in used_ids:
        candidate += 1
    used_ids.add(candidate)
    return candidate


def _chart_only_channel_config(channel_index: int) -> dict:
    channel_config = _runtime_channel_defaults(channel_index)
    channel_config["channel_index"] = channel_index
    channel_config["fft_band_limits"] = []
    channel_config["oct_band_limits"] = []
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
        channel_config[key] = False
    channel_config["fft_golden_mode"] = "off"
    channel_config["oct_golden_mode"] = "off"
    channel_config["fft_show_golden_overlay"] = False
    channel_config["oct_show_golden_overlay"] = False
    return channel_config


def _shape_frequency_limits_for_import(
    *,
    channel_config: dict,
    sample_rate: float,
    channel_index: int,
    wav_channel_index: int,
    warnings: list[ImportAudioWarning],
) -> tuple[set[str], set[str]]:
    no_result_metrics: set[str] = set()
    measured_chart_metrics: set[str] = set()
    nyquist = float(sample_rate) / 2.0
    original_fft_limits = channel_config.get("fft_band_limits", [])
    original_fft_had_above_nyquist = _limits_include_above_nyquist(
        original_fft_limits,
        nyquist=nyquist,
    )
    fft_limits = _filter_limits_above_nyquist(
        original_fft_limits,
        nyquist=nyquist,
        channel_index=channel_index,
        wav_channel_index=wav_channel_index,
        metric="fft",
        warnings=warnings,
    )
    if _is_non_empty_list(channel_config.get("fft_band_limits")):
        distinct = {
            float(limit["freq"])
            for limit in fft_limits
            if _coerce_positive_number(limit.get("freq")) is not None
        }
        if len(distinct) < 2:
            no_result_metrics.add("fft")
            if original_fft_had_above_nyquist and distinct:
                measured_chart_metrics.add("fft")
            warnings.append(
                ImportAudioWarning(
                    code="nyquist_no_evaluable_fft",
                    message="FFT 约束在当前 WAV 采样率下没有足够可评估频点",
                    severity="warning",
                    channel_index=channel_index,
                    wav_channel_index=wav_channel_index,
                    metric="fft",
                )
            )
    channel_config["fft_band_limits"] = fft_limits

    oct_limits = _filter_limits_above_nyquist(
        channel_config.get("oct_band_limits", []),
        nyquist=nyquist,
        channel_index=channel_index,
        wav_channel_index=wav_channel_index,
        metric="octave",
        warnings=warnings,
    )
    if _is_non_empty_list(channel_config.get("oct_band_limits")) and not oct_limits:
        no_result_metrics.add("octave")
        warnings.append(
            ImportAudioWarning(
                code="nyquist_no_evaluable_octave",
                message="倍频程约束在当前 WAV 采样率下没有可评估频点",
                severity="warning",
                channel_index=channel_index,
                wav_channel_index=wav_channel_index,
                metric="octave",
            )
            )
    channel_config["oct_band_limits"] = oct_limits
    return no_result_metrics, measured_chart_metrics


def _limits_include_above_nyquist(limits, *, nyquist: float) -> bool:
    if not isinstance(limits, list):
        return False
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        freq = _coerce_positive_number(limit.get("freq"))
        if freq is not None and freq > nyquist:
            return True
    return False


def _filter_limits_above_nyquist(
    limits,
    *,
    nyquist: float,
    channel_index: int,
    wav_channel_index: int,
    metric: str,
    warnings: list[ImportAudioWarning],
) -> list[dict]:
    if not isinstance(limits, list):
        return []
    filtered: list[dict] = []
    skipped = False
    for limit in limits:
        if not isinstance(limit, dict):
            filtered.append(copy.deepcopy(limit))
            continue
        freq = _coerce_positive_number(limit.get("freq"))
        if freq is not None and freq > nyquist:
            skipped = True
            continue
        filtered.append(copy.deepcopy(limit))
    if skipped:
        warnings.append(
            ImportAudioWarning(
                code="nyquist_skipped",
                message="已跳过超过 WAV 奈奎斯特频率的约束",
                severity="warning",
                channel_index=channel_index,
                wav_channel_index=wav_channel_index,
                metric=metric,
            )
        )
    return filtered


def _inspect_golden_configuration(
    *,
    channel_config: dict,
    sample_rate: float,
    channel_index: int,
    wav_channel_index: int,
    warnings: list[ImportAudioWarning],
    forced_no_results: dict[int, set[str]],
) -> None:
    for metric in ("fft", "octave"):
        if not _golden_requested(channel_config, metric):
            continue
        stored_rate = _coerce_positive_number(
            channel_config.get("golden_sample_sample_rate")
        )
        if stored_rate is not None and stored_rate != float(sample_rate):
            warnings.append(
                ImportAudioWarning(
                    code="golden_resampled",
                    message="黄金样本已按导入 WAV 采样率临时重采样",
                    severity="info",
                    channel_index=channel_index,
                    wav_channel_index=wav_channel_index,
                    metric=metric,
                )
            )
        golden_path = str(channel_config.get("golden_sample_path") or "").strip()
        if not golden_path or not Path(golden_path).is_file():
            _append_import_warning_once(
                warnings,
                ImportAudioWarning(
                    code="golden_unavailable",
                    message=_golden_unavailable_message(channel_config, metric),
                    severity="warning",
                    channel_index=channel_index,
                    wav_channel_index=wav_channel_index,
                    metric=metric,
                )
            )
            if _golden_metric_requires_judgment(channel_config, metric):
                forced_no_results.setdefault(channel_index, set()).add(metric)


def _golden_requested(channel_config: dict, metric: str) -> bool:
    return (
        _golden_metric_requires_judgment(channel_config, metric)
        or _golden_overlay_requested(channel_config, metric)
    )


def _golden_metric_requires_judgment(channel_config: dict, metric: str) -> bool:
    return _golden_mode_for_metric(channel_config, metric) in {"absolute", "delta"}


def _golden_overlay_requested(channel_config: dict, metric: str) -> bool:
    prefix = "oct" if metric == "octave" else metric
    return (
        bool(channel_config.get(f"{prefix}_show_golden_overlay"))
        and _golden_mode_for_metric(channel_config, metric) != "delta"
    )


def _golden_mode_for_metric(channel_config: dict, metric: str) -> str:
    prefix = "oct" if metric == "octave" else metric
    mode = str(channel_config.get(f"{prefix}_golden_mode") or "off").lower()
    return mode if mode in {"absolute", "delta"} else "off"


def _golden_unavailable_message(channel_config: dict, metric: str) -> str:
    if _golden_metric_requires_judgment(channel_config, metric):
        return "黄金样本不可用，相关指标无结果"
    return "黄金样本不可用，黄金对比曲线已忽略"


def _append_import_warning_once(
    warnings: list[ImportAudioWarning],
    warning: ImportAudioWarning,
) -> None:
    for existing in warnings:
        if (
            existing.code == warning.code
            and existing.channel_index == warning.channel_index
            and existing.wav_channel_index == warning.wav_channel_index
            and existing.metric == warning.metric
        ):
            return
    warnings.append(warning)


def _effective_import_mic_cal(
    mic_cal: dict,
    *,
    mapped_config_channels: tuple[int, ...],
    use_mic_calibration: bool,
    warnings: list[ImportAudioWarning],
) -> dict:
    if not use_mic_calibration:
        warnings.append(
            ImportAudioWarning(
                code="calibration_disabled",
                message="导入分析已禁用麦克风校准",
                severity="info",
            )
        )
        return {}
    if not isinstance(mic_cal, dict):
        return {}
    effective = {}
    for channel_index in mapped_config_channels:
        key = f"In{channel_index + 1}"
        if isinstance(mic_cal.get(key), dict):
            effective[key] = copy.deepcopy(mic_cal[key])
    return effective


def _serialize_judgment_result(judgment_result) -> dict:
    if isinstance(judgment_result, dict):
        return copy.deepcopy(judgment_result)
    if is_dataclass(judgment_result):
        return asdict(judgment_result)
    raise RuntimeError("analysis result unavailable")


def _apply_forced_no_results(
    judgment_payload: dict,
    *,
    forced_no_results: dict[int, set[str]],
) -> None:
    if not forced_no_results:
        return
    channel_results = judgment_payload.get("channel_results", [])
    if not isinstance(channel_results, list):
        return
    for channel_result in channel_results:
        if not isinstance(channel_result, dict):
            continue
        channel_index = channel_result.get("channel_index")
        metrics = forced_no_results.get(channel_index, set())
        if "fft" in metrics:
            if not channel_result.get("fft_error_message"):
                channel_result["fft_error_message"] = _forced_no_result_error_message("fft")
            channel_result["fft_band_results"] = []
        if "octave" in metrics:
            if not channel_result.get("octave_error_message"):
                channel_result["octave_error_message"] = _forced_no_result_error_message("octave")
            channel_result["oct_band_results"] = []
    judgment_payload["limit_pass"] = False
    if judgment_payload.get("overall") == "合格":
        judgment_payload["overall"] = "不合格"


def _judgment_payload_for_measured_import_charts(
    judgment_payload: dict,
    *,
    measured_chart_forced_no_results: dict[int, set[str]],
) -> dict:
    if not measured_chart_forced_no_results:
        return judgment_payload
    chart_judgment_payload = copy.deepcopy(judgment_payload)
    channel_results = chart_judgment_payload.get("channel_results", [])
    if not isinstance(channel_results, list):
        return judgment_payload

    changed = False
    for channel_result in channel_results:
        if not isinstance(channel_result, dict):
            continue
        channel_index = channel_result.get("channel_index")
        metrics = measured_chart_forced_no_results.get(channel_index, set())
        if "fft" not in metrics:
            continue
        if (
            channel_result.get("fft_available") is False
            and channel_result.get("fft_error_message") == _FFT_SINGLE_CONTROL_POINT_ERROR
        ):
            channel_result["fft_available"] = True
            channel_result["fft_error_message"] = None
            channel_result["fft_band_results"] = []
            changed = True
    return chart_judgment_payload if changed else judgment_payload


def _forced_no_result_error_message(metric: str | None) -> str:
    if metric == "octave":
        return "导入 WAV 频率范围内无可评估倍频程约束"
    return "导入 WAV 频率范围内无可评估 FFT 约束"


def _collect_golden_unavailable_from_judgment(
    *,
    judgment_payload: dict,
    channel_configs: dict[int, dict],
    channel_column_map: dict[int, int],
    warnings: list[ImportAudioWarning],
    forced_no_results: dict[int, set[str]],
) -> None:
    for channel_result in judgment_payload.get("channel_results", []):
        if not isinstance(channel_result, dict):
            continue
        channel_index = channel_result.get("channel_index")
        channel_config = channel_configs.get(channel_index)
        if not isinstance(channel_config, dict):
            continue
        if (
            channel_result.get("fft_available") is False
            and _golden_requested(channel_config, "fft")
        ):
            forced_no_results.setdefault(channel_index, set()).add("fft")
            _append_import_warning_once(
                warnings,
                ImportAudioWarning(
                    code="golden_unavailable",
                    message="黄金样本 FFT 参考不可用",
                    severity="warning",
                    channel_index=channel_index,
                    wav_channel_index=channel_column_map.get(channel_index),
                    metric="fft",
                )
            )
        if (
            channel_result.get("octave_available") is False
            and _golden_requested(channel_config, "octave")
        ):
            forced_no_results.setdefault(channel_index, set()).add("octave")
            _append_import_warning_once(
                warnings,
                ImportAudioWarning(
                    code="golden_unavailable",
                    message="黄金样本倍频程参考不可用",
                    severity="warning",
                    channel_index=channel_index,
                    wav_channel_index=channel_column_map.get(channel_index),
                    metric="octave",
                )
            )


def _collect_golden_unavailable_from_chart(
    *,
    chart_payload: dict,
    channel_configs: dict[int, dict],
    channel_column_map: dict[int, int],
    warnings: list[ImportAudioWarning],
) -> None:
    if not isinstance(chart_payload, dict):
        return
    for channel_index, channel_config in channel_configs.items():
        channel_payload = chart_payload.get(channel_index)
        if not isinstance(channel_payload, dict):
            continue
        _collect_metric_golden_warning_from_chart(
            channel_payload=channel_payload,
            channel_config=channel_config,
            channel_index=channel_index,
            wav_channel_index=channel_column_map.get(channel_index),
            metric="fft",
            payload_key="fft",
            overlay_key="golden_overlay_levels_db",
            warnings=warnings,
        )
        _collect_metric_golden_warning_from_chart(
            channel_payload=channel_payload,
            channel_config=channel_config,
            channel_index=channel_index,
            wav_channel_index=channel_column_map.get(channel_index),
            metric="octave",
            payload_key="octave",
            overlay_key="golden_overlay_levels_db",
            warnings=warnings,
        )


def _collect_metric_golden_warning_from_chart(
    *,
    channel_payload: dict,
    channel_config: dict,
    channel_index: int,
    wav_channel_index: int | None,
    metric: str,
    payload_key: str,
    overlay_key: str,
    warnings: list[ImportAudioWarning],
) -> None:
    metric_payload = channel_payload.get(payload_key)
    if _golden_metric_requires_judgment(channel_config, metric) and not isinstance(
        metric_payload,
        dict,
    ):
        _append_import_warning_once(
            warnings,
            ImportAudioWarning(
                code="golden_unavailable",
                message=_golden_unavailable_message(channel_config, metric),
                severity="warning",
                channel_index=channel_index,
                wav_channel_index=wav_channel_index,
                metric=metric,
            ),
        )
        return
    if not _golden_overlay_requested(channel_config, metric):
        return
    if not isinstance(metric_payload, dict) or overlay_key in metric_payload:
        return
    _append_import_warning_once(
        warnings,
        ImportAudioWarning(
            code="golden_unavailable",
            message=_golden_unavailable_message(channel_config, metric),
            severity="warning",
            channel_index=channel_index,
            wav_channel_index=wav_channel_index,
            metric=metric,
        ),
    )


def _mark_forced_no_result_summary_rows(
    summary_payload: dict,
    *,
    forced_no_results: dict[int, set[str]],
) -> None:
    if not forced_no_results:
        return
    forced_metric_channels: dict[str, int] = {}
    for channel_index in sorted(
        channel for channel in forced_no_results if isinstance(channel, int)
    ):
        for metric in sorted(forced_no_results.get(channel_index, set())):
            forced_metric_channels.setdefault(metric, channel_index)
    metrics = set(forced_metric_channels)
    changed = False
    first_error_message = None
    for row in summary_payload.get("rows", []):
        if not isinstance(row, dict) or row.get("item_key") not in metrics:
            continue
        metric = row.get("item_key")
        first_error_message = first_error_message or _forced_no_result_error_message(metric)
        row["status"] = "无结果"
        row["deviation_value"] = None
        row["deviation_display"] = "--"
        row["channel_index"] = forced_metric_channels[metric]
        row["channel_label"] = canonical_channel_label(forced_metric_channels[metric])
        row["constrained"] = True
        changed = True
    if changed:
        _refresh_summary_aggregate(
            summary_payload,
            fallback_error_message=first_error_message,
        )


def _refresh_summary_aggregate(
    summary_payload: dict,
    *,
    fallback_error_message: str | None,
) -> None:
    rows = [row for row in summary_payload.get("rows", []) if isinstance(row, dict)]
    if any(row.get("status") == "无结果" for row in rows):
        overall_status = "no_result"
    elif any(row.get("status") == "NG" for row in rows):
        overall_status = "fail"
    else:
        overall_status = "pass"

    summary_payload["overall_status"] = overall_status
    summary_payload["overall_label"] = {
        "pass": "PASS",
        "fail": "FAIL",
        "no_result": "无结果",
    }[overall_status]
    if overall_status == "no_result":
        summary_payload["error_message"] = (
            summary_payload.get("error_message") or fallback_error_message
        )
    else:
        summary_payload["error_message"] = None


def _mark_imported_ai_not_run(summary_payload: dict) -> None:
    for row in summary_payload.get("rows", []):
        if isinstance(row, dict) and row.get("item_key") == "ai":
            row["status"] = "未运行"
            row["deviation_value"] = None
            row["deviation_display"] = "--"
            row["constrained"] = False
            return


def _add_imported_stft_payload(
    *,
    chart_payload: dict | None,
    chart_config: dict,
    chart_channel_column_map: dict[int, int],
    wav: ImportedWav,
    analysis_window: tuple[float, float],
    mic_cal: dict,
    warnings: list[ImportAudioWarning],
) -> None:
    if not isinstance(chart_payload, dict):
        return
    start_sec, end_sec = analysis_window
    start_idx = max(0, min(wav.buffer.shape[0], int(start_sec * wav.sample_rate)))
    end_idx = max(start_idx, min(wav.buffer.shape[0], int(end_sec * wav.sample_rate)))
    selected_window = wav.buffer[start_idx:end_idx]
    channels_by_index = _source_channels_by_index(chart_config)

    for channel_index, channel_payload in chart_payload.items():
        if not isinstance(channel_payload, dict):
            continue
        try:
            column_index = int(chart_channel_column_map.get(channel_index, channel_index))
            if wav.buffer.ndim == 1:
                stft_signal = np.asarray(wav.buffer, dtype=np.float64)
                score_signal = np.asarray(selected_window, dtype=np.float64)
            else:
                stft_signal = np.asarray(wav.buffer[:, column_index], dtype=np.float64)
                score_signal = np.asarray(selected_window[:, column_index], dtype=np.float64)
            channel_config = _copy_source_channel_config(channels_by_index, int(channel_index))
            sensitivity = _mic_sensitivity(mic_cal, int(channel_index))
            stft_signal = dsp.apply_mic_calibration(stft_signal, sensitivity)
            score_signal = dsp.apply_mic_calibration(score_signal, sensitivity)
            nfft = _positive_int_or_default(
                channel_config.get("stft_nfft"),
                _runtime_channel_defaults(int(channel_index)).get("stft_nfft", 1024),
            )
            overlap = _overlap_or_default(channel_config.get("stft_overlap"))
            times_sec, frequencies_hz, magnitude_db = dsp.compute_stft(
                stft_signal,
                wav.sample_rate,
                nfft,
                overlap,
            )
            channel_payload["stft"] = {
                "times_sec": [
                    float(time_value)
                    for time_value in np.asarray(times_sec, dtype=np.float64)
                ],
                "frequencies_hz": [
                    float(freq)
                    for freq in np.asarray(frequencies_hz, dtype=np.float64)
                ],
                "magnitude_db": np.asarray(magnitude_db, dtype=np.float64),
            }
            if "stft_score" not in channel_payload:
                try:
                    score_payload = stft_score.compute_stft_score_payload(
                        signal=score_signal,
                        sample_rate=wav.sample_rate,
                        stft_nfft=nfft,
                        stft_overlap=overlap,
                        score_config=stft_score.normalize_score_config(channel_config),
                        absolute_start_sec=start_sec,
                        loudness_context_signal=stft_signal,
                    )
                except Exception as score_exc:
                    warnings.append(
                        ImportAudioWarning(
                            code="stft_score_unavailable",
                            message=str(score_exc) or "STFT 得分不可用",
                            severity="warning",
                            channel_index=(
                                int(channel_index)
                                if isinstance(channel_index, int) and not isinstance(channel_index, bool)
                                else None
                            ),
                            wav_channel_index=column_index,
                            metric="stft_score",
                        )
                    )
                else:
                    if isinstance(score_payload, dict):
                        channel_payload["stft_score"] = score_payload
        except Exception as exc:
            warnings.append(
                ImportAudioWarning(
                    code="stft_unavailable",
                    message=str(exc) or "STFT 图表不可用",
                    severity="warning",
                    channel_index=(
                        int(channel_index)
                        if isinstance(channel_index, int) and not isinstance(channel_index, bool)
                        else None
                    ),
                    metric="stft",
                )
            )


def _positive_int_or_default(value, default) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return int(default)
    return numeric if numeric > 0 else int(default)


def _overlap_or_default(value) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(numeric) or numeric < 0.0 or numeric >= 1.0:
        return 0.5
    return numeric


def _mic_sensitivity(mic_cal: dict, channel_index: int) -> float:
    if not isinstance(mic_cal, dict):
        return 0.0
    entry = mic_cal.get(f"In{channel_index + 1}", {})
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("deviation_value", "0"))
    except (TypeError, ValueError):
        return 0.0


def _is_non_empty_list(value) -> bool:
    return isinstance(value, list) and bool(value)


def _coerce_positive_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= 0.0:
        return None
    return numeric


def _log_import_warnings(
    warnings: list[ImportAudioWarning],
    *,
    config_id: int,
) -> None:
    for warning in warnings:
        logger.info(
            "import_audio_warning code=%s severity=%s config_id=%s channel_index=%s wav_channel_index=%s metric=%s message=%s",
            warning.code,
            warning.severity,
            config_id,
            warning.channel_index,
            warning.wav_channel_index,
            warning.metric,
            warning.message,
        )


def _coerce_channel_assignment(raw_assignment) -> ImportedChannelAssignment:
    if isinstance(raw_assignment, ImportedChannelAssignment):
        return raw_assignment
    if isinstance(raw_assignment, dict):
        return ImportedChannelAssignment(
            wav_channel_index=raw_assignment.get("wav_channel_index"),
            mode=raw_assignment.get("mode"),
            channel_index=raw_assignment.get("channel_index"),
        )
    raise ValueError("WAV 通道映射格式无效")


def _validate_wav_channel_count(wav_channel_count: int) -> int:
    if isinstance(wav_channel_count, bool) or not isinstance(wav_channel_count, int):
        raise ValueError("WAV 通道数量无效")
    if wav_channel_count <= 0:
        raise ValueError("WAV 通道数量无效")
    return wav_channel_count


def _coerce_non_negative_int(value, *, message: str) -> int:
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        raise ValueError(message) from None
    if numeric < 0:
        raise ValueError(message)
    return numeric


def _coerce_finite_float(value, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数值")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是有限数值") from None
    if not math.isfinite(numeric):
        raise ValueError(f"{label}必须是有限数值")
    return numeric


__all__ = [
    "ImportedAudioAnalysisInputs",
    "ImportedAudioAnalysisRequest",
    "ImportedChannelAssignment",
    "ImportedWav",
    "_SHORT_CLAMPED_WINDOW_THRESHOLD_SEC",
    "analyze_imported_wav",
    "default_channel_assignments",
    "load_wav",
    "normalize_wav_samples",
    "normalize_channel_assignments",
    "resolve_analysis_window",
    "selected_config_channels",
    "wavfile",
]
