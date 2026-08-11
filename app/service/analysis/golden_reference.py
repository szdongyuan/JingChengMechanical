from __future__ import annotations

import math
import os
import hashlib
import io
import wave
import warnings
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as scipy_signal
from scipy.io import wavfile

from app.service.analysis import dsp
from app.service.golden_sample_service import GoldenSampleService


_REF_PRESSURE = 20e-6
_REF_PRESSURE_DB = 20.0 * math.log10(_REF_PRESSURE)


class GoldenReferenceError(RuntimeError):
    """Raised when a runtime golden reference cannot be loaded or derived."""


@dataclass(frozen=True, slots=True)
class FrozenGoldenArtifact:
    config_id: int
    channel_index: int
    canonical_source_path: str
    sha256: str
    byte_size: int
    sample_rate_hz: int
    sample_width_bytes: int
    channel_count: int
    frame_count: int
    file_bytes: bytes = field(repr=False)
    signal: np.ndarray = field(repr=False, compare=False)

    def rehash(self) -> str:
        return hashlib.sha256(self.file_bytes).hexdigest()


def _file_identity(handle) -> tuple[int, int, int, int]:
    stat = os.fstat(handle.fileno())
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def freeze_golden_artifact(
    *,
    config_id: int,
    channel_index: int,
    channel_config: dict,
    runtime_sample_rate: float,
    analysis_duration: float,
    managed_root: str,
) -> FrozenGoldenArtifact:
    """Read and validate one committed mono PCM WAV exactly once."""
    path = str(channel_config.get("golden_sample_path") or "").strip()
    canonical = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(os.path.abspath(managed_root))
    try:
        within_root = os.path.commonpath((root, canonical)) == root
    except ValueError:
        within_root = False
    staged = os.path.join(root, "staged")
    try:
        within_staged = os.path.commonpath((staged, canonical)) == staged
    except ValueError:
        within_staged = False
    if not path or not within_root or within_staged:
        raise GoldenReferenceError(
            "golden artifact is not a managed committed path"
        )
    try:
        with open(canonical, "rb") as handle:
            before = _file_identity(handle)
            raw = handle.read()
            after = _file_identity(handle)
    except OSError as exc:
        raise GoldenReferenceError("golden artifact is unreadable") from exc
    if before != after or os.path.realpath(os.path.abspath(path)) != canonical:
        raise GoldenReferenceError("golden artifact changed during read")
    if len(raw) != before[2]:
        raise GoldenReferenceError("golden artifact size changed during read")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise GoldenReferenceError("golden artifact must be PCM")
            channel_count = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
    except (EOFError, wave.Error) as exc:
        raise GoldenReferenceError("golden artifact is not a valid WAV") from exc
    if channel_count != 1 or sample_width not in (1, 2, 3, 4):
        raise GoldenReferenceError(
            "golden artifact must be a mono integer PCM WAV"
        )
    source_channel = channel_config.get("golden_sample_source_channel")
    expected_rate = channel_config.get("golden_sample_sample_rate")
    expected_frames = channel_config.get("golden_sample_num_samples")
    start_sample = channel_config.get("golden_sample_start_sample")
    start_ms = channel_config.get("golden_sample_start_ms")
    duration_ms = channel_config.get("golden_sample_duration_ms")
    if (
        type(source_channel) is not int
        or source_channel < 0
        or type(expected_frames) is not int
        or expected_frames <= 0
        or type(start_sample) is not int
        or start_sample < 0
        or type(start_ms) is not int
        or type(duration_ms) is not int
    ):
        raise GoldenReferenceError("golden artifact metadata is invalid")
    if (
        float(expected_rate) != float(sample_rate)
        or float(runtime_sample_rate) != float(sample_rate)
        or frame_count != expected_frames
        or frame_count != round(float(analysis_duration) * sample_rate)
        or start_ms != round(start_sample * 1000 / sample_rate)
        or duration_ms != round(frame_count * 1000 / sample_rate)
    ):
        raise GoldenReferenceError(
            "golden artifact WAV facts do not match frozen configuration"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            decoded_rate, decoded = wavfile.read(io.BytesIO(raw))
    except Exception as exc:
        raise GoldenReferenceError("golden artifact PCM cannot be decoded") from exc
    decoded_array = np.asarray(decoded)
    expected_dtype = {
        1: np.dtype(np.uint8),
        2: np.dtype(np.int16),
        3: np.dtype(np.int32),
        4: np.dtype(np.int32),
    }[sample_width]
    if (
        isinstance(decoded_rate, (bool, np.bool_))
        or not isinstance(decoded_rate, (int, np.integer))
        or int(decoded_rate) != sample_rate
        or decoded_array.ndim != 1
        or decoded_array.dtype != expected_dtype
        or decoded_array.shape[0] != frame_count
    ):
        raise GoldenReferenceError(
            "golden artifact decoded facts do not match WAV header"
        )
    decoded_signal = np.asarray(decoded_array, dtype=np.float64)
    signal = np.frombuffer(decoded_signal.tobytes(), dtype=np.float64)
    frozen_bytes = bytes(raw)
    artifact = FrozenGoldenArtifact(
        config_id=int(config_id),
        channel_index=int(channel_index),
        canonical_source_path=canonical,
        sha256=digest,
        byte_size=len(frozen_bytes),
        sample_rate_hz=int(decoded_rate),
        sample_width_bytes=sample_width,
        channel_count=channel_count,
        frame_count=frame_count,
        file_bytes=frozen_bytes,
        signal=signal,
    )
    if artifact.rehash() != digest:
        raise GoldenReferenceError("frozen golden artifact digest changed")
    return artifact


@dataclass
class GoldenReferenceBundle:
    signal: np.ndarray
    sample_rate: float
    fft_by_key: dict[tuple[int, str], tuple[np.ndarray, np.ndarray]] = field(
        default_factory=dict
    )
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
        allow_runtime_resample: bool = False,
    ) -> GoldenReferenceBundle:
        cached = self._bundles.get(channel_index)
        if cached is not None:
            return cached

        frozen = channel_config.get("_frozen_golden_artifact")
        if frozen is not None:
            if type(frozen) is not FrozenGoldenArtifact:
                raise GoldenReferenceError("frozen golden artifact is invalid")
            configured_rate = _coerce_positive_float(
                configured_input_sample_rate
            )
            runtime_rate = _coerce_positive_float(runtime_sample_rate)
            if (
                frozen.channel_index != channel_index
                or frozen.rehash() != frozen.sha256
                or configured_rate != float(frozen.sample_rate_hz)
                or runtime_rate != float(frozen.sample_rate_hz)
                or allow_runtime_resample
            ):
                raise GoldenReferenceError(
                    "frozen golden artifact facts do not match execution"
                )
            calibrated = dsp.apply_mic_calibration(
                frozen.signal,
                mic_sensitivity,
            )
            bundle = GoldenReferenceBundle(
                signal=np.asarray(calibrated, dtype=np.float64).reshape(-1),
                sample_rate=float(frozen.sample_rate_hz),
            )
            self._bundles[channel_index] = bundle
            return bundle

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
        if stored_sample_rate != configured_sample_rate or (
            not allow_runtime_resample and stored_sample_rate != runtime_sample_rate
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

        bundle_sample_rate = stored_sample_rate
        if allow_runtime_resample:
            signal = _resample_signal(
                signal,
                source_rate=stored_sample_rate,
                target_rate=runtime_sample_rate,
            )
            bundle_sample_rate = runtime_sample_rate

        calibrated = dsp.apply_mic_calibration(signal, mic_sensitivity)
        bundle = GoldenReferenceBundle(
            signal=np.asarray(calibrated, dtype=np.float64).reshape(-1),
            sample_rate=bundle_sample_rate,
        )
        self._bundles[channel_index] = bundle
        return bundle

    def ensure_fft(
        self,
        bundle: GoldenReferenceBundle,
        *,
        nfft: int,
        weighting: str = "Z",
    ) -> tuple[np.ndarray, np.ndarray]:
        try:
            resolved_nfft = _validated_positive_nfft(nfft)
            resolved_weighting = _normalize_fft_weighting(weighting)
            cache_key = (resolved_nfft, resolved_weighting)
            cached = bundle.fft_by_key.get(cache_key)
            if cached is not None:
                return cached

            frequencies_hz, magnitudes_db = dsp.compute_fft_spectrum(
                bundle.signal,
                bundle.sample_rate,
                resolved_nfft,
                resolved_weighting,
            )
        except Exception as exc:
            message = str(exc) or "黄金样本 FFT 参考计算失败"
            raise GoldenReferenceError(message) from exc

        cached = (
            np.asarray(frequencies_hz, dtype=np.float64),
            _absolute_levels_db(magnitudes_db),
        )
        bundle.fft_by_key[cache_key] = cached
        return cached

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


def _normalize_fft_weighting(weighting: str) -> str:
    normalized = str(weighting or "Z").strip().upper()
    return normalized if normalized in {"A", "C", "Z"} else "Z"


def _resample_signal(
    signal: np.ndarray,
    source_rate: float,
    target_rate: float,
) -> np.ndarray:
    source = int(round(source_rate))
    target = int(round(target_rate))
    if source <= 0 or target <= 0:
        raise GoldenReferenceError("黄金样本采样率无效")
    samples = np.asarray(signal, dtype=np.float64).reshape(-1)
    if source == target:
        return samples
    divisor = math.gcd(source, target)
    return scipy_signal.resample_poly(samples, target // divisor, source // divisor)


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


def _validated_positive_nfft(value) -> int:
    numeric = _coerce_positive_int(value)
    if numeric is None:
        raise ValueError("nfft must be a positive integer")
    return numeric


__all__ = [
    "GoldenReferenceBundle",
    "GoldenReferenceCache",
    "GoldenReferenceError",
]
