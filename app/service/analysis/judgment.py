"""Post-recording judgment engine.

Extracts the analysis window, runs per-channel limit checks, validates AI binding,
and produces a pass/fail verdict. AI execution is deferred to TestSession.
"""
from __future__ import annotations

import json
import logging

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.frames import (
    BandResult,
    ChannelJudgment,
    JudgmentResult,
)
from app.service.analysis.golden_reference import (
    GoldenReferenceCache,
    GoldenReferenceError,
)
from app.service.analysis.limit_tables import (
    LimitTable,
    evaluate_limit_tables,
    normalize_limit_tables,
    validate_limit_tables,
)
from app.service.golden_sample_service import GoldenSampleService
from app.service.config_service import _CHANNEL_DEFAULTS


_REF_PRESSURE = 20e-6
_REF_PRESSURE_DB = 20.0 * np.log10(_REF_PRESSURE)
_DEFAULT_LOUDNESS_UNIT = "sone"
_DEFAULT_LOUDNESS_LOWER_LIMIT = 0.0
_DEFAULT_LOUDNESS_UPPER_LIMIT = 60.0
_SPL_CHUNK_DURATION_SEC = 0.05
logger = logging.getLogger(__name__)


def _runtime_channel_defaults(channel_index: int) -> dict:
    """Convert _CHANNEL_DEFAULTS (DB-storage format) to runtime dict.

    _CHANNEL_DEFAULTS stores list/dict fields as JSON strings (for peewee).
    This parses them into Python objects for runtime use.
    """
    cfg = {"channel_index": channel_index}
    for k, v in _CHANNEL_DEFAULTS.items():
        if k in ("fft_band_limits", "oct_band_limits", "ai_config"):
            cfg[k] = json.loads(v) if isinstance(v, str) else v
        else:
            cfg[k] = v
    return cfg


def _configured_vibration_view_channel(config: dict, selected_channels) -> int | None:
    value = config.get("vibration_view_channel_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    selected = {
        channel
        for channel in selected_channels
        if isinstance(channel, int) and not isinstance(channel, bool) and channel >= 0
    }
    return value if value in selected else None


class JudgmentEngine:
    """Compute pass/fail verdict for a completed recording."""

    def __init__(
        self,
        ai_service,
        golden_sample_service: GoldenSampleService | None = None,
    ):
        self._ai = ai_service
        self._golden_sample_service = golden_sample_service

    def judge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        *,
        channel_column_map: dict[int, int] | None = None,
        loudness_trace_cache: dict[int, dict] | None = None,
        channel_features: dict[int, object] | None = None,
        run_ai: bool = True,
        allow_golden_runtime_resample: bool = False,
    ) -> JudgmentResult:
        """Run judgment on the configured automatic analysis window.

        Parameters
        ----------
        config : loaded config dict (from ConfigService.load_config)
        buffer : (total_samples, n_channels) recorded audio
        sample_rate : Hz
        mic_cal : mic calibration dict from DeviceTable (keys like "In1")
        audio_file_path : path to saved WAV for AI
        """
        analysis_dur = config.get("analysis_duration", 3.0)
        offset_ms = config.get("analysis_window_offset_ms", 0)
        total_samples = buffer.shape[0]
        total_sec = total_samples / sample_rate

        start_sec = float(offset_ms) / 1000.0
        end_sec = start_sec + analysis_dur
        if total_sec < end_sec:
            raise ValueError(
                "Captured audio does not fully cover the required analysis window "
                f"({start_sec:.3f}s to {end_sec:.3f}s)"
            )
        return self._run(
            config,
            buffer,
            sample_rate,
            mic_cal,
            audio_file_path,
            start_sec,
            end_sec,
            channel_column_map=channel_column_map,
            loudness_trace_cache=loudness_trace_cache,
            channel_features=channel_features,
            run_ai=run_ai,
            allow_golden_runtime_resample=allow_golden_runtime_resample,
        )

    def rejudge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
        *,
        channel_column_map: dict[int, int] | None = None,
        loudness_trace_cache: dict[int, dict] | None = None,
        channel_features: dict[int, object] | None = None,
        run_ai: bool = True,
        allow_golden_runtime_resample: bool = False,
    ) -> JudgmentResult:
        """Re-run judgment on a user-selected window."""
        total_sec = buffer.shape[0] / sample_rate
        start_sec = max(0.0, min(start_sec, total_sec))
        end_sec = max(start_sec, min(end_sec, total_sec))
        return self._run(
            config,
            buffer,
            sample_rate,
            mic_cal,
            audio_file_path,
            start_sec,
            end_sec,
            channel_column_map=channel_column_map,
            loudness_trace_cache=loudness_trace_cache,
            channel_features=channel_features,
            run_ai=run_ai,
            allow_golden_runtime_resample=allow_golden_runtime_resample,
        )

    def _run(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
        *,
        channel_column_map: dict[int, int] | None = None,
        loudness_trace_cache: dict[int, dict] | None = None,
        channel_features: dict[int, object] | None = None,
        run_ai: bool = True,
        allow_golden_runtime_resample: bool = False,
    ) -> JudgmentResult:
        start_idx = int(start_sec * sample_rate)
        end_idx = int(end_sec * sample_rate)
        end_idx = min(end_idx, buffer.shape[0])
        window = buffer[start_idx:end_idx]

        selected = config.get("selected_channels", [])
        vibration_channel_index = _configured_vibration_view_channel(config, selected)
        judged_channels = [
            ch_idx
            for ch_idx in selected
            if ch_idx != vibration_channel_index
        ]
        channels_cfg = {ch["channel_index"]: ch for ch in config.get("channels", [])}
        golden_cache = GoldenReferenceCache(
            golden_sample_service=self._golden_sample_service
        )
        configured_input_sample_rate = config.get("input_device_samplerate")

        channel_results: list[ChannelJudgment] = []
        all_pass = True

        for ch_idx in judged_channels:
            ch_cfg = channels_cfg.get(ch_idx)
            if ch_cfg is None:
                # Spec: use defaults when channel config is missing
                ch_cfg = _runtime_channel_defaults(ch_idx)

            precomputed = (
                channel_features.get(ch_idx)
                if isinstance(channel_features, dict)
                else None
            )
            column_index = self._buffer_column_for_channel(ch_idx, channel_column_map)

            # Extract single channel
            full_sig: np.ndarray | None = None
            if precomputed is not None:
                sig = np.asarray(precomputed.calibrated_signal, dtype=np.float64)
            elif buffer.ndim == 2 and 0 <= column_index < buffer.shape[1]:
                full_sig = buffer[:, column_index].copy()
                sig = full_sig[start_idx:end_idx].copy()
            elif buffer.ndim == 1:
                full_sig = buffer.copy()
                sig = full_sig[start_idx:end_idx].copy()
            elif window.ndim == 2 and 0 <= column_index < window.shape[1]:
                sig = window[:, column_index].copy()
            elif window.ndim == 1:
                sig = window.copy()
            else:
                continue

            # Apply mic calibration
            sensitivity = self._get_sensitivity(mic_cal, ch_idx)
            if precomputed is not None:
                calibrated_full = None
            else:
                calibrated_full = (
                    dsp.apply_mic_calibration(full_sig, sensitivity)
                    if full_sig is not None
                    else None
                )
                if calibrated_full is not None:
                    sig = calibrated_full[start_idx:end_idx].copy()
                else:
                    sig = dsp.apply_mic_calibration(sig, sensitivity)

            # SPL checks
            spl_weighting = ch_cfg.get("spl_weighting", "Z")
            spl_time_constant = ch_cfg.get("spl_time_constant", 0.125)
            spl_lower, spl_upper = self._enabled_bounds(
                ch_cfg,
                lower_enabled_key="spl_lower_enabled",
                lower_value_key="spl_lower_limit",
                upper_enabled_key="spl_upper_enabled",
                upper_value_key="spl_upper_limit",
            )
            spl_average_lower, spl_average_upper = self._enabled_bounds(
                ch_cfg,
                lower_enabled_key="spl_average_lower_enabled",
                lower_value_key="spl_average_lower_limit",
                upper_enabled_key="spl_average_upper_enabled",
                upper_value_key="spl_average_upper_limit",
            )
            spl_constrained = self._has_constraint(spl_lower, spl_upper)
            spl_average_constrained = self._has_constraint(spl_average_lower, spl_average_upper)
            spl_db: float | None = None
            spl_min_db: float | None = None
            spl_max_db: float | None = None
            spl_average_db: float | None = None
            spl_pass: bool | None = True
            spl_average_pass: bool | None = True
            spl_trace_available = False
            spl_trace_error_message: str | None = None
            spl_average_available = False
            spl_average_error_message: str | None = None
            spl_signal = calibrated_full if calibrated_full is not None else sig
            spl_start_sample = start_idx if calibrated_full is not None else 0
            spl_end_sample = end_idx if calibrated_full is not None else len(sig)
            try:
                if precomputed is not None:
                    if precomputed.spl_error is not None:
                        raise RuntimeError(precomputed.spl_error)
                    spl_trace_db = precomputed.spl_values
                else:
                    _, spl_trace_db = dsp.compute_spl_trace(
                        spl_signal,
                        sample_rate,
                        spl_weighting,
                        spl_time_constant,
                        chunk_duration=_SPL_CHUNK_DURATION_SEC,
                        output_start_sample=spl_start_sample,
                        output_end_sample=spl_end_sample,
                    )
                trace_values = list(spl_trace_db)
                if not trace_values:
                    trace_values = [-np.inf]
                spl_db = float(trace_values[-1])
                spl_min_db, spl_max_db = self._value_bounds(trace_values)
                spl_trace_available = True
                spl_pass = self._values_pass_limits(
                    trace_values,
                    spl_lower,
                    spl_upper,
                )
            except Exception as exc:
                spl_trace_error_message = str(exc) or exc.__class__.__name__
                spl_pass = None if spl_constrained else True
            try:
                if precomputed is not None:
                    if precomputed.spl_average_error is not None:
                        raise RuntimeError(precomputed.spl_average_error)
                    spl_average_db = float(precomputed.spl_average_db)
                else:
                    spl_average_db = float(
                        dsp.compute_spl_leq(
                            spl_signal,
                            sample_rate,
                            spl_weighting,
                            start_sample=spl_start_sample,
                            end_sample=spl_end_sample,
                        )
                    )
                spl_average_available = True
                spl_average_pass = self._values_pass_limits(
                    [spl_average_db],
                    spl_average_lower,
                    spl_average_upper,
                )
            except Exception as exc:
                spl_average_error_message = str(exc) or exc.__class__.__name__
                spl_average_pass = None if spl_average_constrained else True

            loudness_min_sone: float | None = None
            loudness_max_sone: float | None = None
            loudness_average_sone: float | None = None
            loudness_pass: bool | None = True
            loudness_average_pass: bool | None = True
            loudness_available = False
            loudness_error_message: str | None = None
            loudness_lower, loudness_upper = self._loudness_limits_sone(
                ch_cfg,
                lower_enabled_key="loudness_lower_enabled",
                lower_value_key="loudness_lower_limit",
                upper_enabled_key="loudness_upper_enabled",
                upper_value_key="loudness_upper_limit",
            )
            loudness_average_lower, loudness_average_upper = self._loudness_limits_sone(
                ch_cfg,
                lower_enabled_key="loudness_average_lower_enabled",
                lower_value_key="loudness_average_lower_limit",
                upper_enabled_key="loudness_average_upper_enabled",
                upper_value_key="loudness_average_upper_limit",
            )
            loudness_constrained = self._has_constraint(loudness_lower, loudness_upper)
            loudness_average_constrained = self._has_constraint(
                loudness_average_lower,
                loudness_average_upper,
            )
            try:
                if precomputed is not None:
                    if precomputed.loudness_error is not None:
                        raise RuntimeError(precomputed.loudness_error)
                    times_sec = precomputed.loudness_times
                    loudness_values_sone = precomputed.loudness_values
                    loudness_max_sone = precomputed.loudness_max_sone
                elif calibrated_full is not None and start_sec > 0.0:
                    times_sec, loudness_values_sone, loudness_max_sone = (
                        dsp.compute_loudness_trace_for_window(
                            calibrated_full,
                            sample_rate,
                            start_sec,
                            end_sec,
                        )
                    )
                else:
                    times_sec, loudness_values_sone, loudness_max_sone = (
                        dsp.compute_loudness_trace(sig, sample_rate)
                    )
                if loudness_trace_cache is not None:
                    loudness_trace_cache[ch_idx] = {
                        "times_sec": list(times_sec),
                        "values_sone": list(loudness_values_sone),
                        "max_sone": float(loudness_max_sone),
                        "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
                        "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
                    }
                loudness_min_sone, loudness_trace_max_sone = self._value_bounds(loudness_values_sone)
                if loudness_trace_max_sone is not None:
                    loudness_max_sone = loudness_trace_max_sone
                loudness_average_sone = self._mean_value(loudness_values_sone)
                loudness_pass = self._values_pass_limits(
                    loudness_values_sone,
                    loudness_lower,
                    loudness_upper,
                )
                loudness_average_pass = self._values_pass_limits(
                    [loudness_average_sone] if loudness_average_sone is not None else [],
                    loudness_average_lower,
                    loudness_average_upper,
                )
                loudness_available = True
            except Exception as exc:
                if loudness_trace_cache is not None:
                    loudness_trace_cache.pop(ch_idx, None)
                loudness_error_message = str(exc) or exc.__class__.__name__
                loudness_pass = None if loudness_constrained else True
                loudness_average_pass = None if loudness_average_constrained else True

            # FFT band checks
            fft_bands, fft_available, fft_error_message = self._check_fft_bands(
                channel_index=ch_idx,
                channel_config=ch_cfg,
                sig=sig,
                sample_rate=sample_rate,
                configured_input_sample_rate=configured_input_sample_rate,
                mic_sensitivity=sensitivity,
                golden_cache=golden_cache,
                allow_golden_runtime_resample=allow_golden_runtime_resample,
                precomputed=precomputed,
            )

            # Octave band checks
            oct_bands, octave_available, octave_error_message = self._check_octave_bands(
                channel_index=ch_idx,
                channel_config=ch_cfg,
                sig=sig,
                sample_rate=sample_rate,
                configured_input_sample_rate=configured_input_sample_rate,
                mic_sensitivity=sensitivity,
                golden_cache=golden_cache,
                allow_golden_runtime_resample=allow_golden_runtime_resample,
                precomputed=precomputed,
            )

            fft_required = bool(ch_cfg.get("fft_band_limits")) or str(
                ch_cfg.get("fft_golden_mode") or "off"
            ).lower() != "off"
            octave_required = bool(ch_cfg.get("oct_band_limits")) or str(
                ch_cfg.get("oct_golden_mode") or "off"
            ).lower() != "off"

            ch_pass = (
                spl_pass is True
                and spl_average_pass is True
                and loudness_pass is True
                and loudness_average_pass is True
                and (fft_available or not fft_required)
                and (octave_available or not octave_required)
                and all(b.passed for b in fft_bands)
                and all(b.passed for b in oct_bands)
            )
            if not ch_pass:
                all_pass = False

            channel_results.append(ChannelJudgment(
                channel_index=ch_idx,
                spl_db=spl_db,
                spl_pass=spl_pass,
                spl_min_db=spl_min_db,
                spl_max_db=spl_max_db,
                spl_average_db=spl_average_db,
                spl_average_pass=spl_average_pass,
                spl_trace_available=spl_trace_available,
                spl_trace_error_message=spl_trace_error_message,
                spl_average_available=spl_average_available,
                spl_average_error_message=spl_average_error_message,
                loudness_min_sone=loudness_min_sone,
                loudness_max_sone=loudness_max_sone,
                loudness_average_sone=loudness_average_sone,
                loudness_pass=loudness_pass,
                loudness_average_pass=loudness_average_pass,
                loudness_available=loudness_available,
                loudness_error_message=loudness_error_message,
                fft_available=fft_available,
                fft_error_message=fft_error_message,
                octave_available=octave_available,
                octave_error_message=octave_error_message,
                fft_band_results=fft_bands,
                oct_band_results=oct_bands,
            ))

        if judged_channels and not channel_results:
            all_pass = False

        # AI analysis
        if run_ai:
            ai_pass, ai_score = self._run_ai(config, audio_file_path)
        else:
            ai_pass, ai_score = None, None

        # Combined verdict
        limit_pass = all_pass
        if ai_pass is None:
            overall = "合格" if limit_pass else "不合格"
        else:
            overall = "合格" if (limit_pass and ai_pass) else "不合格"

        return JudgmentResult(
            overall=overall,
            limit_pass=limit_pass,
            ai_pass=ai_pass,
            ai_score=ai_score,
            channel_results=channel_results,
            analysis_window=(start_sec, end_sec),
        )

    @staticmethod
    def _buffer_column_for_channel(
        channel_index: int,
        channel_column_map: dict[int, int] | None,
    ) -> int:
        if isinstance(channel_column_map, dict) and channel_index in channel_column_map:
            try:
                return int(channel_column_map[channel_index])
            except (TypeError, ValueError):
                return channel_index
        return channel_index

    def _check_fft_bands(
        self,
        *,
        channel_index: int,
        channel_config: dict,
        sig: np.ndarray,
        sample_rate: float,
        configured_input_sample_rate,
        mic_sensitivity: float,
        golden_cache: GoldenReferenceCache,
        allow_golden_runtime_resample: bool = False,
        precomputed: object | None = None,
    ) -> tuple[list[BandResult], bool, str | None]:
        band_limits = channel_config.get("fft_band_limits", [])
        weighting = self._fft_weighting(channel_config.get("fft_weighting", "Z"))
        mode = self._golden_mode(channel_config.get("fft_golden_mode"))
        overlay_requested = bool(channel_config.get("fft_show_golden_overlay"))
        nfft = channel_config.get("fft_nfft", _CHANNEL_DEFAULTS["fft_nfft"])

        limit_tables, config_error = self._runtime_limit_tables(
            band_limits,
            label="FFT",
        )
        if config_error is not None:
            return [], False, config_error
        if not limit_tables:
            return [], True, None

        if mode != "off":
            try:
                golden_bundle = golden_cache.bundle_for_channel(
                    channel_index=channel_index,
                    channel_config=channel_config,
                    configured_input_sample_rate=configured_input_sample_rate,
                    runtime_sample_rate=sample_rate,
                    mic_sensitivity=mic_sensitivity,
                    allow_runtime_resample=allow_golden_runtime_resample,
                )
                golden_freqs, golden_levels = golden_cache.ensure_fft(
                    golden_bundle,
                    nfft=nfft,
                    weighting=weighting,
                )
            except GoldenReferenceError as exc:
                return [], False, str(exc)
        elif overlay_requested:
            try:
                golden_cache.bundle_for_channel(
                    channel_index=channel_index,
                    channel_config=channel_config,
                    configured_input_sample_rate=configured_input_sample_rate,
                    runtime_sample_rate=sample_rate,
                    mic_sensitivity=mic_sensitivity,
                    allow_runtime_resample=allow_golden_runtime_resample,
                )
            except GoldenReferenceError as exc:
                logger.warning("黄金样本加载失败，FFT 对比曲线已忽略: %s", exc)

        try:
            if precomputed is not None:
                if precomputed.fft_error is not None:
                    raise RuntimeError(precomputed.fft_error)
                measured_freqs = np.asarray(precomputed.fft_frequencies)
                measured_levels = np.asarray(precomputed.fft_magnitudes)
            else:
                measured_freqs, measured_levels = self._measure_fft(
                    sig,
                    sample_rate,
                    nfft=nfft,
                    weighting=weighting,
                )
        except Exception as exc:
            return [], False, str(exc) or exc.__class__.__name__
        if mode == "off":
            bands, active_count = self._build_fft_band_results(
                measured_freqs=measured_freqs,
                measured_levels=measured_levels,
                limit_tables=limit_tables,
                golden_levels=None,
                mode=mode,
            )
            if active_count == 0:
                return [], False, "FFT 频率范围内无可评估频点"
            return bands, True, None

        try:
            self._validate_matching_axis(measured_freqs, golden_freqs, "FFT")
        except GoldenReferenceError as exc:
            return [], False, str(exc)
        bands, active_count = self._build_fft_band_results(
            measured_freqs=measured_freqs,
            measured_levels=measured_levels,
            limit_tables=limit_tables,
            golden_levels=golden_levels,
            mode=mode,
        )
        if active_count == 0:
            return [], False, "FFT 频率范围内无可评估频点"

        return bands, True, None

    def _check_octave_bands(
        self,
        *,
        channel_index: int,
        channel_config: dict,
        sig: np.ndarray,
        sample_rate: float,
        configured_input_sample_rate,
        mic_sensitivity: float,
        golden_cache: GoldenReferenceCache,
        allow_golden_runtime_resample: bool = False,
        precomputed: object | None = None,
    ) -> tuple[list[BandResult], bool, str | None]:
        band_limits = channel_config.get("oct_band_limits", [])
        weighting = str(channel_config.get("oct_weighting", "Z") or "Z")
        mode = self._golden_mode(channel_config.get("oct_golden_mode"))
        overlay_requested = bool(channel_config.get("oct_show_golden_overlay"))

        limit_tables, config_error = self._runtime_limit_tables(
            band_limits,
            label="倍频程",
        )
        if config_error is not None:
            return [], False, config_error
        if not limit_tables:
            return [], True, None

        if mode != "off":
            try:
                golden_bundle = golden_cache.bundle_for_channel(
                    channel_index=channel_index,
                    channel_config=channel_config,
                    configured_input_sample_rate=configured_input_sample_rate,
                    runtime_sample_rate=sample_rate,
                    mic_sensitivity=mic_sensitivity,
                    allow_runtime_resample=allow_golden_runtime_resample,
                )
                golden_centers, golden_levels = golden_cache.ensure_octave(
                    golden_bundle,
                    weighting=weighting,
                )
            except GoldenReferenceError as exc:
                return [], False, str(exc)
        elif overlay_requested:
            try:
                golden_cache.bundle_for_channel(
                    channel_index=channel_index,
                    channel_config=channel_config,
                    configured_input_sample_rate=configured_input_sample_rate,
                    runtime_sample_rate=sample_rate,
                    mic_sensitivity=mic_sensitivity,
                    allow_runtime_resample=allow_golden_runtime_resample,
                )
            except GoldenReferenceError as exc:
                logger.warning("黄金样本加载失败，倍频程对比曲线已忽略: %s", exc)

        try:
            if precomputed is not None:
                if precomputed.octave_error is not None:
                    raise RuntimeError(precomputed.octave_error)
                measured_centers = np.asarray(precomputed.octave_centers)
                measured_levels = np.asarray(precomputed.octave_levels)
            else:
                measured_centers, measured_levels = self._measure_octave(
                    sig,
                    sample_rate,
                    weighting,
                )
        except Exception as exc:
            return [], False, str(exc) or exc.__class__.__name__
        if mode == "off":
            bands, active_count = self._build_octave_band_results(
                measured_centers=measured_centers,
                measured_levels=measured_levels,
                limit_tables=limit_tables,
                golden_levels=None,
                mode=mode,
            )
            if active_count == 0:
                return [], False, "倍频程频率范围内无可评估频点"
            return bands, True, None

        try:
            self._validate_matching_axis(measured_centers, golden_centers, "倍频程")
        except GoldenReferenceError as exc:
            return [], False, str(exc)

        bands, active_count = self._build_octave_band_results(
            measured_centers=measured_centers,
            measured_levels=measured_levels,
            limit_tables=limit_tables,
            golden_levels=golden_levels,
            mode=mode,
        )
        if active_count == 0:
            return [], False, "倍频程频率范围内无可评估频点"

        return bands, True, None

    def _measure_fft(
        self,
        sig: np.ndarray,
        sample_rate: float,
        *,
        nfft: int,
        weighting: str = "Z",
    ) -> tuple[np.ndarray, np.ndarray]:
        freqs, mag_db = dsp.compute_fft_spectrum(sig, sample_rate, nfft, weighting)
        return np.asarray(freqs, dtype=np.float64), self._absolute_levels_db(mag_db)

    def _measure_octave(
        self,
        sig: np.ndarray,
        sample_rate: float,
        weighting: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        centers, levels = dsp.compute_octave_bands(sig, sample_rate, weighting)
        return np.asarray(centers, dtype=np.float64), self._absolute_levels_db(levels)

    def _build_fft_band_results(
        self,
        *,
        measured_freqs: np.ndarray,
        measured_levels: np.ndarray,
        limit_tables: list[LimitTable],
        golden_levels: np.ndarray | None,
        mode: str,
    ) -> tuple[list[BandResult], int]:
        eval_freqs, eval_measured, eval_golden = self._finite_limit_inputs(
            frequencies=measured_freqs,
            levels=measured_levels,
            golden_levels=golden_levels,
        )
        if eval_freqs.size == 0:
            return [], 0

        evaluated = evaluate_limit_tables(
            limit_tables,
            eval_freqs,
            golden_levels=eval_golden,
            mode=mode,
        )
        judged_levels = self._judged_levels(
            measured_levels=eval_measured,
            golden_levels=eval_golden,
            mode=mode,
        )
        return self._band_results_from_evaluated_limits(
            frequencies=eval_freqs,
            levels=judged_levels,
            evaluated=evaluated,
            include_passes=False,
        )

    def _build_octave_band_results(
        self,
        *,
        measured_centers: np.ndarray,
        measured_levels: np.ndarray,
        limit_tables: list[LimitTable],
        golden_levels: np.ndarray | None,
        mode: str,
    ) -> tuple[list[BandResult], int]:
        eval_freqs, eval_measured, eval_golden = self._finite_limit_inputs(
            frequencies=measured_centers,
            levels=measured_levels,
            golden_levels=golden_levels,
        )
        if eval_freqs.size == 0:
            return [], 0

        evaluated = evaluate_limit_tables(
            limit_tables,
            eval_freqs,
            golden_levels=eval_golden,
            mode=mode,
        )
        judged_levels = self._judged_levels(
            measured_levels=eval_measured,
            golden_levels=eval_golden,
            mode=mode,
        )
        return self._band_results_from_evaluated_limits(
            frequencies=eval_freqs,
            levels=judged_levels,
            evaluated=evaluated,
            include_passes=True,
        )

    @staticmethod
    def _runtime_limit_tables(
        raw_limits,
        *,
        label: str,
    ) -> tuple[list[LimitTable], str | None]:
        errors = validate_limit_tables(raw_limits, label=label)
        if errors:
            return [], f"{label}配置错误: {'；'.join(errors)}"
        try:
            return normalize_limit_tables(raw_limits, label=label), None
        except ValueError as exc:
            return [], f"{label}配置错误: {exc}"

    @staticmethod
    def _finite_limit_inputs(
        *,
        frequencies: np.ndarray,
        levels: np.ndarray,
        golden_levels: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        frequency_axis = np.asarray(frequencies, dtype=np.float64)
        level_axis = np.asarray(levels, dtype=np.float64)
        finite_mask = np.isfinite(frequency_axis) & np.isfinite(level_axis)
        if golden_levels is None:
            return frequency_axis[finite_mask], level_axis[finite_mask], None

        golden_axis = np.asarray(golden_levels, dtype=np.float64)
        finite_mask &= np.isfinite(golden_axis)
        return (
            frequency_axis[finite_mask],
            level_axis[finite_mask],
            golden_axis[finite_mask],
        )

    @staticmethod
    def _judged_levels(
        *,
        measured_levels: np.ndarray,
        golden_levels: np.ndarray | None,
        mode: str,
    ) -> np.ndarray:
        if mode == "delta" and golden_levels is not None:
            return np.asarray(measured_levels, dtype=np.float64) - np.asarray(
                golden_levels,
                dtype=np.float64,
            )
        return np.asarray(measured_levels, dtype=np.float64)

    @classmethod
    def _band_results_from_evaluated_limits(
        cls,
        *,
        frequencies: np.ndarray,
        levels: np.ndarray,
        evaluated,
        include_passes: bool,
    ) -> tuple[list[BandResult], int]:
        results: list[BandResult] = []
        active_count = 0
        for freq, level, lower_value, upper_value, active in zip(
            frequencies,
            levels,
            evaluated.lower,
            evaluated.upper,
            evaluated.active,
            strict=False,
        ):
            if not bool(active):
                continue
            active_count += 1
            lower = cls._finite_bound_or_none(lower_value)
            upper = cls._finite_bound_or_none(upper_value)
            passed = cls._passes_one_sided_limit(
                level=float(level),
                lower=lower,
                upper=upper,
            )
            if include_passes or not passed:
                results.append(
                    BandResult(
                        freq=float(freq),
                        level=float(level),
                        lower=lower,
                        upper=upper,
                        passed=passed,
                    )
                )
        return results, active_count

    @staticmethod
    def _finite_bound_or_none(value) -> float | None:
        numeric = float(value)
        if np.isfinite(numeric):
            return numeric
        return None

    @staticmethod
    def _passes_one_sided_limit(
        *,
        level: float,
        lower: float | None,
        upper: float | None,
    ) -> bool:
        if lower is not None and level < lower:
            return False
        if upper is not None and level > upper:
            return False
        return True

    @staticmethod
    def _validate_matching_axis(
        measured_axis: np.ndarray,
        golden_axis: np.ndarray,
        label: str,
    ) -> None:
        if measured_axis.shape != golden_axis.shape:
            raise GoldenReferenceError(f"黄金样本{label}频轴不匹配")
        if not np.allclose(measured_axis, golden_axis):
            raise GoldenReferenceError(f"黄金样本{label}频轴不匹配")

    @staticmethod
    def _golden_mode(value) -> str:
        mode = str(value or "off").strip().lower()
        if mode in {"absolute", "delta"}:
            return mode
        return "off"

    @staticmethod
    def _fft_weighting(value) -> str:
        weighting = str(value or "Z").strip().upper()
        return weighting if weighting in {"A", "C", "Z"} else "Z"

    @staticmethod
    def _absolute_levels_db(levels_db: np.ndarray | list[float]) -> np.ndarray:
        levels = np.asarray(levels_db, dtype=np.float64)
        return levels - _REF_PRESSURE_DB

    @staticmethod
    def _loudness_limits_sone(
        channel_config: dict,
        *,
        lower_enabled_key: str = "loudness_lower_enabled",
        lower_value_key: str = "loudness_lower_limit",
        upper_enabled_key: str = "loudness_upper_enabled",
        upper_value_key: str = "loudness_upper_limit",
    ) -> tuple[float | None, float | None]:
        lower, upper = JudgmentEngine._enabled_bounds(
            channel_config,
            lower_enabled_key=lower_enabled_key,
            lower_value_key=lower_value_key,
            upper_enabled_key=upper_enabled_key,
            upper_value_key=upper_value_key,
        )
        unit = channel_config.get("loudness_unit", _DEFAULT_LOUDNESS_UNIT)
        if unit == "phon":
            return (
                dsp.phon_to_sone(lower) if lower is not None else None,
                dsp.phon_to_sone(upper) if upper is not None else None,
            )
        return (
            max(lower, 0.0) if lower is not None else None,
            max(upper, 0.0) if upper is not None else None,
        )

    @staticmethod
    def _enabled_bounds(
        channel_config: dict,
        *,
        lower_enabled_key: str,
        lower_value_key: str,
        upper_enabled_key: str,
        upper_value_key: str,
    ) -> tuple[float | None, float | None]:
        lower = (
            JudgmentEngine._coerce_limit(channel_config.get(lower_value_key), -np.inf)
            if channel_config.get(lower_enabled_key) is True
            else None
        )
        upper = (
            JudgmentEngine._coerce_limit(channel_config.get(upper_value_key), np.inf)
            if channel_config.get(upper_enabled_key) is True
            else None
        )
        return lower, upper

    @staticmethod
    def _has_constraint(lower: float | None, upper: float | None) -> bool:
        return lower is not None or upper is not None

    @staticmethod
    def _coerce_limit(value, default: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not np.isfinite(numeric):
            return float(default)
        return numeric

    @staticmethod
    def _value_bounds(values) -> tuple[float | None, float | None]:
        value_array = np.asarray(values, dtype=np.float64).reshape(-1)
        value_array = value_array[~np.isnan(value_array)]
        if value_array.size == 0:
            return None, None
        return float(np.min(value_array)), float(np.max(value_array))

    @staticmethod
    def _values_pass_limits(values, lower: float | None, upper: float | None) -> bool:
        value_array = np.asarray(values, dtype=np.float64).reshape(-1)
        value_array = value_array[~np.isnan(value_array)]
        if value_array.size == 0:
            return False
        if lower is not None and np.isfinite(lower) and np.any(value_array < lower):
            return False
        if upper is not None and np.isfinite(upper) and np.any(value_array > upper):
            return False
        return True

    @staticmethod
    def _mean_value(values) -> float | None:
        value_array = np.asarray(values, dtype=np.float64).reshape(-1)
        value_array = value_array[np.isfinite(value_array)]
        if value_array.size == 0:
            return None
        return float(np.mean(value_array))

    def _run_ai(self, config: dict, audio_file_path: str) -> tuple[bool | None, float | None]:
        """Validate AI binding and return (None, None) — actual call deferred to TestSession.

        JudgmentEngine only checks whether a binding exists/is enabled.
        The actual AiService.run_ai_analysis() call is made by TestSession,
        which owns the test_run_id, segment list, and ai_runs_dir context
        that AiService requires. TestSession patches ai_pass/ai_score into
        the JudgmentResult after calling AiService.
        """
        config_id = config.get("config_id")
        if config_id is None:
            return None, None

        try:
            binding = self._ai.resolve_binding(config_id)
        except Exception:
            return None, None
        if binding is None or not binding.get("enabled"):
            return None, None

        # AI integration will be called from TestSession which has the full
        # context (test_run_id, segments, etc.). For now, return None to indicate
        # "AI not yet run" — TestSession will fill this in.
        return None, None

    @staticmethod
    def _get_sensitivity(mic_cal: dict, channel_index: int) -> float:
        return dsp.mic_sensitivity_from_calibration(mic_cal, channel_index)
