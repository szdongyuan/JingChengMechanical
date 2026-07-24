"""Continuous frequency weighting for sound-pressure-level analysis."""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy import signal as scipy_signal


_BREAK_FREQUENCIES_HZ = (
    20.598997,
    107.65265,
    737.86223,
    12194.217,
)
_REFERENCE_FREQUENCY_HZ = 1_000.0


def a_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Return IEC A-weighting gains in dB for the given frequencies."""
    values = np.asarray(frequencies, dtype=np.float64)
    squared = values**2
    f1, f2, f3, f4 = _BREAK_FREQUENCIES_HZ
    with np.errstate(divide="ignore", invalid="ignore"):
        numerator = f4**2 * squared**2
        denominator = (
            (squared + f1**2)
            * np.sqrt((squared + f2**2) * (squared + f3**2))
            * (squared + f4**2)
        )
        response = numerator / denominator
    reference = _a_weight_linear(_REFERENCE_FREQUENCY_HZ)
    with np.errstate(divide="ignore"):
        gains_db = (
            20.0 * np.log10(np.maximum(response, 1e-30))
            - 20.0 * np.log10(reference)
        )
    return np.where(np.isfinite(gains_db), gains_db, -np.inf)


def _a_weight_linear(frequency: float) -> float:
    squared = frequency**2
    f1, f2, f3, f4 = _BREAK_FREQUENCIES_HZ
    numerator = f4**2 * squared**2
    denominator = (
        (squared + f1**2)
        * np.sqrt((squared + f2**2) * (squared + f3**2))
        * (squared + f4**2)
    )
    return float(numerator / denominator)


def c_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Return IEC C-weighting gains in dB for the given frequencies."""
    values = np.asarray(frequencies, dtype=np.float64)
    squared = values**2
    f1, _f2, _f3, f4 = _BREAK_FREQUENCIES_HZ
    with np.errstate(divide="ignore", invalid="ignore"):
        numerator = f4**2 * squared
        denominator = (squared + f1**2) * (squared + f4**2)
        response = numerator / denominator
    reference = _c_weight_linear(_REFERENCE_FREQUENCY_HZ)
    with np.errstate(divide="ignore"):
        gains_db = (
            20.0 * np.log10(np.maximum(response, 1e-30))
            - 20.0 * np.log10(reference)
        )
    return np.where(np.isfinite(gains_db), gains_db, -np.inf)


def _c_weight_linear(frequency: float) -> float:
    squared = frequency**2
    f1, _f2, _f3, f4 = _BREAK_FREQUENCIES_HZ
    numerator = f4**2 * squared
    denominator = (squared + f1**2) * (squared + f4**2)
    return float(numerator / denominator)


def spl_weighting_sos(
    sample_rate: float,
    weighting: str,
) -> np.ndarray | None:
    """Return cached digital SOS coefficients for A/C SPL weighting.

    Z and unknown compatibility values use the flat path and return ``None``.
    """
    normalized_sample_rate = float(sample_rate)
    if normalized_sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")
    normalized_weighting = str(weighting or "Z").strip().upper()
    if normalized_weighting not in ("A", "C"):
        return None
    return _cached_spl_weighting_sos(
        normalized_sample_rate,
        normalized_weighting,
    )


@lru_cache(maxsize=None)
def _cached_spl_weighting_sos(
    sample_rate: float,
    normalized_weighting: str,
) -> np.ndarray:
    nyquist = sample_rate / 2.0
    if all(frequency < nyquist for frequency in _BREAK_FREQUENCIES_HZ):
        angular_breaks = np.asarray(
            [
                2.0
                * sample_rate
                * np.tan(np.pi * frequency / sample_rate)
                for frequency in _BREAK_FREQUENCIES_HZ
            ],
            dtype=np.float64,
        )
    else:
        angular_breaks = 2.0 * np.pi * np.asarray(
            _BREAK_FREQUENCIES_HZ,
            dtype=np.float64,
        )

    first, second, third, fourth = angular_breaks
    if normalized_weighting == "A":
        analog_zeros = np.zeros(4, dtype=np.float64)
        analog_poles = -np.asarray(
            [first, first, second, third, fourth, fourth],
            dtype=np.float64,
        )
    else:
        analog_zeros = np.zeros(2, dtype=np.float64)
        analog_poles = -np.asarray(
            [first, first, fourth, fourth],
            dtype=np.float64,
        )

    analog_gain = 1.0
    _, analog_response = scipy_signal.freqs_zpk(
        analog_zeros,
        analog_poles,
        analog_gain,
        worN=np.asarray([2.0 * np.pi * _REFERENCE_FREQUENCY_HZ]),
    )
    analog_gain /= float(np.abs(analog_response[0]))

    digital_zeros, digital_poles, digital_gain = scipy_signal.bilinear_zpk(
        analog_zeros,
        analog_poles,
        analog_gain,
        fs=sample_rate,
    )
    if _REFERENCE_FREQUENCY_HZ < nyquist:
        _, digital_response = scipy_signal.freqz_zpk(
            digital_zeros,
            digital_poles,
            digital_gain,
            worN=np.asarray([_REFERENCE_FREQUENCY_HZ]),
            fs=sample_rate,
        )
        digital_gain /= float(np.abs(digital_response[0]))

    sos = scipy_signal.zpk2sos(
        digital_zeros,
        digital_poles,
        digital_gain,
    )
    sos.setflags(write=False)
    return sos


class SplFrequencyWeightingFilter:
    """Stateful A/C weighting filter for one continuous signal stream."""

    def __init__(self, sample_rate: float, weighting: str):
        self.sample_rate = float(sample_rate)
        self.weighting = str(weighting or "Z").strip().upper()
        cached_sos = spl_weighting_sos(self.sample_rate, self.weighting)
        # scipy.signal.sosfilt requires a writable coefficient buffer on some
        # supported builds, so keep the shared cache immutable and copy it
        # once for this filter instance.
        self._sos = None if cached_sos is None else cached_sos.copy()
        self._zi = self._zero_state()

    def _zero_state(self) -> np.ndarray | None:
        if self._sos is None:
            return None
        return np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Filter one arbitrary-sized chunk while preserving delay state."""
        values = np.asarray(samples, dtype=np.float64).reshape(-1)
        if values.size == 0 or self._sos is None:
            return values
        filtered, self._zi = scipy_signal.sosfilt(
            self._sos,
            values,
            zi=self._zi,
        )
        return filtered

    def reset(self) -> None:
        """Reset all filter delays to deterministic zero state."""
        self._zi = self._zero_state()
