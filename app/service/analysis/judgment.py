"""Post-recording judgment engine.

Extracts the analysis window, runs per-channel limit checks, validates AI binding,
and produces a pass/fail verdict. AI execution is deferred to TestSession.
"""
from __future__ import annotations

import json

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.frames import (
    BandResult,
    ChannelJudgment,
    JudgmentResult,
)
from app.service.config_service import _CHANNEL_DEFAULTS


_REF_PRESSURE = 20e-6
_DEFAULT_LOUDNESS_UNIT = "sone"
_DEFAULT_LOUDNESS_LOWER_LIMIT = 0.0
_DEFAULT_LOUDNESS_UPPER_LIMIT = 60.0


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


class JudgmentEngine:
    """Compute pass/fail verdict for a completed recording."""

    def __init__(self, ai_service):
        self._ai = ai_service

    def judge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
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
        return self._run(config, buffer, sample_rate, mic_cal,
                         audio_file_path, start_sec, end_sec)

    def rejudge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
    ) -> JudgmentResult:
        """Re-run judgment on a user-selected window."""
        total_sec = buffer.shape[0] / sample_rate
        start_sec = max(0.0, min(start_sec, total_sec))
        end_sec = max(start_sec, min(end_sec, total_sec))
        return self._run(config, buffer, sample_rate, mic_cal,
                         audio_file_path, start_sec, end_sec)

    def _run(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
    ) -> JudgmentResult:
        start_idx = int(start_sec * sample_rate)
        end_idx = int(end_sec * sample_rate)
        end_idx = min(end_idx, buffer.shape[0])
        window = buffer[start_idx:end_idx]

        selected = config.get("selected_channels", [])
        channels_cfg = {ch["channel_index"]: ch for ch in config.get("channels", [])}

        channel_results: list[ChannelJudgment] = []
        all_pass = True

        for ch_idx in selected:
            ch_cfg = channels_cfg.get(ch_idx)
            if ch_cfg is None:
                # Spec: use defaults when channel config is missing
                ch_cfg = _runtime_channel_defaults(ch_idx)

            # Extract single channel
            if window.ndim == 2 and ch_idx < window.shape[1]:
                sig = window[:, ch_idx].copy()
            elif window.ndim == 1:
                sig = window.copy()
            else:
                continue

            # Apply mic calibration
            sensitivity = self._get_sensitivity(mic_cal, ch_idx)
            if sensitivity != 0.0:
                sig = dsp.apply_mic_calibration(sig, sensitivity)

            # SPL check
            spl_db = float(dsp.compute_spl(
                sig, sample_rate,
                ch_cfg.get("spl_weighting", "Z"),
                ch_cfg.get("spl_time_constant", 0.125),
            ))
            spl_lo = ch_cfg.get("spl_lower_limit", 0.0)
            spl_hi = ch_cfg.get("spl_upper_limit", 100.0)
            spl_pass = bool(spl_lo <= spl_db <= spl_hi)

            loudness_max_sone: float | None = None
            loudness_pass: bool | None = None
            loudness_available = False
            loudness_error_message: str | None = None
            try:
                _, _, loudness_max_sone = dsp.compute_loudness_trace(sig, sample_rate)
                loudness_lo, loudness_hi = self._loudness_limits_sone(ch_cfg)
                loudness_pass = bool(loudness_lo <= loudness_max_sone <= loudness_hi)
                loudness_available = True
            except Exception as exc:
                loudness_error_message = str(exc) or exc.__class__.__name__

            # FFT band checks
            nfft = ch_cfg.get("stft_nfft", 1024)
            fft_bands = self._check_bands(
                sig, sample_rate, nfft,
                ch_cfg.get("fft_band_limits", []),
            )

            # Octave band checks
            oct_bands = self._check_octave_bands(
                sig, sample_rate,
                ch_cfg.get("oct_weighting", "Z"),
                ch_cfg.get("oct_band_limits", []),
            )

            ch_pass = (
                spl_pass
                and loudness_pass is True
                and all(b.passed for b in fft_bands)
                and all(b.passed for b in oct_bands)
            )
            if not ch_pass:
                all_pass = False

            channel_results.append(ChannelJudgment(
                channel_index=ch_idx,
                spl_db=spl_db,
                spl_pass=spl_pass,
                loudness_max_sone=loudness_max_sone,
                loudness_pass=loudness_pass,
                loudness_available=loudness_available,
                loudness_error_message=loudness_error_message,
                fft_band_results=fft_bands,
                oct_band_results=oct_bands,
            ))

        if selected and not channel_results:
            all_pass = False

        # AI analysis
        ai_pass, ai_score = self._run_ai(config, audio_file_path)

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

    def _check_bands(
        self,
        sig: np.ndarray,
        sample_rate: float,
        nfft: int,
        band_limits: list[dict],
    ) -> list[BandResult]:
        """Check FFT band limits."""
        if not band_limits:
            return []
        freqs, mag_db = dsp.compute_fft_spectrum(sig, sample_rate, nfft)
        results = []
        for bl in band_limits:
            target_freq = bl["freq"]
            # Find nearest frequency bin
            idx = np.argmin(np.abs(freqs - target_freq))
            level = float(mag_db[idx] - 20.0 * np.log10(_REF_PRESSURE))
            lo = bl.get("lower", -np.inf)
            hi = bl.get("upper", np.inf)
            results.append(BandResult(
                freq=float(freqs[idx]),
                level=level,
                lower=lo,
                upper=hi,
                passed=lo <= level <= hi,
            ))
        return results

    def _check_octave_bands(
        self,
        sig: np.ndarray,
        sample_rate: float,
        weighting: str,
        band_limits: list[dict],
    ) -> list[BandResult]:
        """Check 1/3-octave band limits."""
        if not band_limits:
            return []
        centers, levels = dsp.compute_octave_bands(sig, sample_rate, weighting)
        results = []
        for bl in band_limits:
            target_freq = bl["freq"]
            idx = np.argmin(np.abs(centers - target_freq))
            level = float(levels[idx] - 20.0 * np.log10(_REF_PRESSURE))
            lo = bl.get("lower", -np.inf)
            hi = bl.get("upper", np.inf)
            results.append(BandResult(
                freq=float(centers[idx]),
                level=level,
                lower=lo,
                upper=hi,
                passed=lo <= level <= hi,
            ))
        return results

    @staticmethod
    def _loudness_limits_sone(channel_config: dict) -> tuple[float, float]:
        unit = channel_config.get("loudness_unit", _DEFAULT_LOUDNESS_UNIT)
        lower = float(channel_config.get("loudness_lower_limit", _DEFAULT_LOUDNESS_LOWER_LIMIT))
        upper = float(channel_config.get("loudness_upper_limit", _DEFAULT_LOUDNESS_UPPER_LIMIT))
        if unit == "phon":
            return dsp.phon_to_sone(lower), dsp.phon_to_sone(upper)
        return max(lower, 0.0), max(upper, 0.0)

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
        """Extract mic sensitivity from mic_cal dict.

        mic_cal format: {"In1": {"deviation_value": "0.5", ...}, ...}
        """
        key = f"In{channel_index + 1}"
        entry = mic_cal.get(key, {})
        raw = entry.get("deviation_value", "0")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0
