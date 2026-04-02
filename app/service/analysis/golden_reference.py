from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
from scipy.io import wavfile

from app.service.analysis import dsp
from app.service.golden_sample_service import GoldenSampleService


_REF_PRESSURE = 20e-6
_REF_PRESSURE_DB = 20.0 * math.log10(_REF_PRESSURE)


class GoldenReferenceError(RuntimeError):
    """Raised when a runtime golden reference cannot be loaded or derived."""


@dataclass
class GoldenReferenceBundle:
    signal: np.ndarray
    sample_rate: float
    fft_frequencies_hz: np.ndarray | None = None
    fft_levels_db: np.ndarray | None = None
    octave_center_freqs_hz: np.ndarray | None = None
    octave_levels_db: np.ndarray | None = None


class GoldenReferenceCache:
    """Load and derive one shared golden reference bundle per channel per pass."""

    def __init__(self, *, golden_sample_service: GoldenSampleService | None = None):
        self._golden_samples = golden_sample_service or GoldenSampleService()
        self._bundles: dict[int, GoldenReferenceBundle] = {}

    def bundle_for_channel(
        self,
        *,
        channel_index: int,
        channel_config: dict,
        configured_input_sample_rate,
        runtime_sample_rate,
        mic_sensitivity: float,
    ) -> GoldenReferenceBundle:
        cached = self._bundles.get(channel_index)
        if cached is not None:
            return cached

        path = str(channel_config.get("golden_sample_path") or "").strip()
        if not path:
            raise GoldenReferenceError("黄金样本路径不存在或不可读")
        if (
            not self._golden_samples.is_managed_path(path)
            or self._golden_samples.is_managed_staged_path(path)
        ):
            raise GoldenReferenceError("黄金样本必须位于受管已提交目录")

        stored_sample_rate = _coerce_positive_float(
            channel_config.get("golden_sample_sample_rate")
        )
        configured_sample_rate = _coerce_positive_float(configured_input_sample_rate)
        runtime_sample_rate = _coerce_positive_float(runtime_sample_rate)
        if (
            stored_sample_rate is None
            or configured_sample_rate is None
            or runtime_sample_rate is None
        ):
            raise GoldenReferenceError("黄金样本采样率无效")
        if (
            stored_sample_rate != configured_sample_rate
            or stored_sample_rate != runtime_sample_rate
        ):
            raise GoldenReferenceError("黄金样本采样率与当前配置或运行采样率不一致")

        actual_sample_rate, data = _read_wav(path)
        if float(actual_sample_rate) != stored_sample_rate:
            raise GoldenReferenceError("黄金样本采样率与当前配置或运行采样率不一致")

        signal = np.asarray(data, dtype=np.float64)
        if signal.ndim != 1:
            raise GoldenReferenceError("黄金样本必须是单声道 WAV")
        signal = signal.reshape(-1)
        if signal.size <= 0:
            raise GoldenReferenceError("黄金样本为空")
        expected_num_samples = _coerce_positive_int(
            channel_config.get("golden_sample_num_samples")
        )
        if expected_num_samples is None:
            raise GoldenReferenceError("黄金样本长度无效")
        if signal.size != expected_num_samples:
            raise GoldenReferenceError("黄金样本文件长度与记录元数据不一致")

        calibrated = dsp.apply_mic_calibration(signal, mic_sensitivity)
        bundle = GoldenReferenceBundle(
            signal=np.asarray(calibrated, dtype=np.float64).reshape(-1),
            sample_rate=stored_sample_rate,
        )
        self._bundles[channel_index] = bundle
        return bundle

    def ensure_fft(
        self,
        bundle: GoldenReferenceBundle,
    ) -> tuple[np.ndarray, np.ndarray]:
        if bundle.fft_frequencies_hz is None or bundle.fft_levels_db is None:
            nfft = max(1, int(bundle.signal.size))
            try:
                frequencies_hz, magnitudes_db = dsp.compute_fft_spectrum(
                    bundle.signal,
                    bundle.sample_rate,
                    nfft,
                )
            except Exception as exc:
                message = str(exc) or "黄金样本 FFT 参考计算失败"
                raise GoldenReferenceError(message) from exc
            bundle.fft_frequencies_hz = np.asarray(frequencies_hz, dtype=np.float64)
            bundle.fft_levels_db = _absolute_levels_db(magnitudes_db)
        return bundle.fft_frequencies_hz, bundle.fft_levels_db

    def ensure_octave(
        self,
        bundle: GoldenReferenceBundle,
        *,
        weighting: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        if (
            bundle.octave_center_freqs_hz is None
            or bundle.octave_levels_db is None
        ):
            try:
                center_freqs_hz, levels_db = dsp.compute_octave_bands(
                    bundle.signal,
                    bundle.sample_rate,
                    str(weighting or "Z"),
                )
            except Exception as exc:
                message = str(exc) or "黄金样本倍频程参考计算失败"
                raise GoldenReferenceError(message) from exc
            bundle.octave_center_freqs_hz = np.asarray(
                center_freqs_hz,
                dtype=np.float64,
            )
            bundle.octave_levels_db = _absolute_levels_db(levels_db)
        return bundle.octave_center_freqs_hz, bundle.octave_levels_db


def _read_wav(path: str) -> tuple[int, object]:
    normalized = os.path.abspath(str(path))
    if not os.path.isfile(normalized):
        raise GoldenReferenceError("黄金样本路径不存在或不可读")
    try:
        return wavfile.read(normalized)
    except Exception as exc:  # pragma: no cover - scipy exception shape is unstable
        raise GoldenReferenceError("黄金样本路径不存在或不可读") from exc


def _absolute_levels_db(levels_db: np.ndarray | list[float]) -> np.ndarray:
    levels = np.asarray(levels_db, dtype=np.float64)
    return levels - _REF_PRESSURE_DB


def _coerce_positive_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        numeric = float(value)
        if math.isfinite(numeric) and numeric > 0.0:
            return numeric
    return None


def _coerce_positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, np.integer)):
        numeric = int(value)
        if numeric > 0:
            return numeric
    return None


__all__ = [
    "GoldenReferenceBundle",
    "GoldenReferenceCache",
    "GoldenReferenceError",
]
