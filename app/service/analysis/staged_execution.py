"""Closed contracts shared by Decision and Enrichment analysis stages."""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from enum import Enum

from .performance_diagnostics import AnalysisStageTimings
from .staged_result_payload import (
    decode_and_validate_canonical_result,
    decode_json_object,
    json_values_equal_exact,
)


_BUNDLE_FIXED_ALLOWANCE_BYTES = 32 * 1024 * 1024
_MAX_CANONICAL_RESULT_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSED_RESULT_BYTES = _MAX_CANONICAL_RESULT_BYTES + 1024 * 1024
REQUIRED_ENRICHMENT_CHART_METRICS = (
    "fft",
    "loudness",
    "octave",
    "spl",
    "stft",
    "stft_score",
)


class AnalysisPhase(str, Enum):
    DECISION = "decision"
    ENRICHMENT = "enrichment"


def _require_non_empty_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_exact_float(value: object, name: str) -> None:
    if type(value) is not float:
        raise TypeError(f"{name} must be an exact float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")


def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_judgment(value: object) -> None:
    if type(value) is not str:
        raise TypeError("judgment must be an exact str")
    if value not in {"PASS", "FAIL"}:
        raise ValueError("judgment must be PASS or FAIL")


def _require_timing_identity(
    identity: AnalysisBundleIdentity,
    timings: AnalysisStageTimings,
) -> None:
    for event in timings.events:
        if event.run_id != identity.run_id:
            raise ValueError("timings run_id must match envelope identity")
        if event.analysis_attempt_id != identity.analysis_attempt_id:
            raise ValueError(
                "timings analysis_attempt_id must match envelope identity"
            )


def _require_exact_result_payload_zlib(
    canonical_result_json: str,
    compressed: object,
) -> None:
    if type(canonical_result_json) is not str:
        raise TypeError("canonical_result_json must be an exact str")
    if type(compressed) is not bytes:
        raise TypeError("result_payload_zlib must be exact bytes")
    if not compressed:
        raise ValueError("result_payload_zlib must not be empty")
    if len(compressed) > _MAX_COMPRESSED_RESULT_BYTES:
        raise ValueError("result_payload_zlib exceeds the compressed size limit")
    try:
        expected = canonical_result_json.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("canonical result must be valid UTF-8") from exc
    if not expected or len(expected) > _MAX_CANONICAL_RESULT_BYTES:
        raise ValueError("canonical result exceeds the canonical size limit")

    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(compressed, len(expected) + 1)
        if len(decoded) > len(expected) or decompressor.unconsumed_tail:
            raise ValueError("compressed result expands beyond the expected size")
        flushed = decompressor.flush()
    except zlib.error as exc:
        raise ValueError("result_payload_zlib decompression failed") from exc
    if (
        not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or flushed
    ):
        raise ValueError("result_payload_zlib has an invalid or trailing stream")
    if decoded != expected:
        raise ValueError("result_payload_zlib does not match canonical result")


def _require_enrichment_json_boundary(
    canonical_result_json: str,
    judgment: str,
) -> None:
    prefix = f'{{"judgment":"{judgment}","result":'
    if (
        not canonical_result_json.startswith(prefix)
        or not canonical_result_json.endswith("}")
    ):
        raise ValueError(
            "canonical enrichment result must have the judgment/result boundary"
        )


def _require_chart_manifest(
    manifest: object,
) -> None:
    if type(manifest) is not tuple:
        raise TypeError("chart_manifest must be an exact tuple")
    previous_channel = -1
    for entry in manifest:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("chart_manifest entries must be exact pairs")
        channel_index, metric_names = entry
        if type(channel_index) is not int:
            raise TypeError("chart_manifest channel index must be an exact int")
        if channel_index < 0:
            raise ValueError("chart_manifest channel index must be non-negative")
        if channel_index <= previous_channel:
            raise ValueError("chart_manifest channels must be sorted and unique")
        if type(metric_names) is not tuple:
            raise TypeError("chart_manifest metric names must be an exact tuple")
        if not metric_names:
            raise ValueError("chart_manifest metric names must not be empty")
        if any(type(metric) is not str or not metric for metric in metric_names):
            raise TypeError(
                "chart_manifest metric names must be nonempty exact strings"
            )
        if metric_names != tuple(sorted(set(metric_names))):
            raise ValueError(
                "chart_manifest metric names must be sorted and unique"
            )
        previous_channel = channel_index


@dataclass(frozen=True, slots=True)
class AnalysisBundleIdentity:
    run_id: str
    analysis_attempt_id: str
    content_revision: str
    algorithm_revision: str
    config_identity: str
    calibration_identity: str
    channel_mapping_identity: str
    window_start_seconds: float
    window_end_seconds: float

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "analysis_attempt_id",
            "content_revision",
            "algorithm_revision",
            "config_identity",
            "calibration_identity",
            "channel_mapping_identity",
        ):
            _require_non_empty_str(getattr(self, field_name), field_name)
        _require_exact_float(self.window_start_seconds, "window_start_seconds")
        _require_exact_float(self.window_end_seconds, "window_end_seconds")
        if self.window_end_seconds <= self.window_start_seconds:
            raise ValueError("window_end_seconds must be after window_start_seconds")


@dataclass(frozen=True, slots=True)
class AnalysisBundleToken:
    slot_id: int
    bundle_id: str
    identity: AnalysisBundleIdentity

    def __post_init__(self) -> None:
        if type(self.slot_id) is not int:
            raise TypeError("slot_id must be an exact int")
        if self.slot_id < 0:
            raise ValueError("slot_id must be non-negative")
        _require_non_empty_str(self.bundle_id, "bundle_id")
        if type(self.identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")

    def require_identity(self, expected: AnalysisBundleIdentity) -> None:
        if type(expected) is not AnalysisBundleIdentity:
            raise TypeError("expected identity must be an exact AnalysisBundleIdentity")
        labels = {
            "run_id": "run id",
            "analysis_attempt_id": "analysis attempt id",
            "content_revision": "content revision",
            "algorithm_revision": "algorithm revision",
            "config_identity": "config identity",
            "calibration_identity": "calibration identity",
            "channel_mapping_identity": "channel mapping identity",
            "window_start_seconds": "window start seconds",
            "window_end_seconds": "window end seconds",
        }
        for field_name, label in labels.items():
            if getattr(self.identity, field_name) != getattr(expected, field_name):
                raise ValueError(f"bundle token {label} mismatch")


@dataclass(frozen=True, slots=True)
class AnalysisBundleBudget:
    frame_count: int
    channel_count: int
    dtype_size: int

    def __post_init__(self) -> None:
        _require_positive_int(self.frame_count, "frame_count")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.dtype_size, "dtype_size")

    @property
    def audio_bytes(self) -> int:
        return self.frame_count * self.channel_count * self.dtype_size

    @property
    def max_bytes(self) -> int:
        return 4 * self.audio_bytes + _BUNDLE_FIXED_ALLOWANCE_BYTES

    def require_within(self, actual_bytes: int) -> None:
        if type(actual_bytes) is not int:
            raise TypeError("actual_bytes must be an exact int")
        if actual_bytes < 0:
            raise ValueError("actual_bytes must be non-negative")
        if actual_bytes > self.max_bytes:
            raise ValueError("analysis bundle exceeds its hard byte budget")


@dataclass(frozen=True, slots=True)
class AnalysisDecisionEnvelope:
    identity: AnalysisBundleIdentity
    judgment: str
    canonical_result_json: str
    summary_payload_json: str
    bundle_token: AnalysisBundleToken
    timings: AnalysisStageTimings

    def __post_init__(self) -> None:
        if type(self.identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")
        _require_judgment(self.judgment)
        root = decode_and_validate_canonical_result(
            self.canonical_result_json,
            "canonical_result_json",
            require_empty_chart_payload=True,
        )
        if root.get("judgment") != self.judgment:
            raise ValueError("canonical result judgment mismatch")
        summary = decode_json_object(
            self.summary_payload_json,
            "summary_payload_json",
        )
        if not json_values_equal_exact(
            summary,
            root["result"]["summary_payload"],
        ):
            raise ValueError(
                "summary_payload_json must equal result.summary_payload"
            )
        if type(self.bundle_token) is not AnalysisBundleToken:
            raise TypeError("bundle_token must be an exact AnalysisBundleToken")
        self.bundle_token.require_identity(self.identity)
        if type(self.timings) is not AnalysisStageTimings:
            raise TypeError("timings must be exact AnalysisStageTimings")
        _require_timing_identity(self.identity, self.timings)


@dataclass(frozen=True, slots=True)
class AnalysisEnrichmentReport:
    identity: AnalysisBundleIdentity
    judgment: str
    canonical_result_json: str
    result_payload_zlib: bytes
    timings: AnalysisStageTimings
    chart_manifest: tuple[tuple[int, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if type(self.identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")
        _require_judgment(self.judgment)
        _require_exact_result_payload_zlib(
            self.canonical_result_json,
            self.result_payload_zlib,
        )
        _require_enrichment_json_boundary(
            self.canonical_result_json,
            self.judgment,
        )
        _require_chart_manifest(self.chart_manifest)
        if type(self.timings) is not AnalysisStageTimings:
            raise TypeError("timings must be exact AnalysisStageTimings")
        _require_timing_identity(self.identity, self.timings)


__all__ = (
    "AnalysisBundleBudget",
    "AnalysisBundleIdentity",
    "AnalysisBundleToken",
    "AnalysisDecisionEnvelope",
    "AnalysisEnrichmentReport",
    "AnalysisPhase",
    "REQUIRED_ENRICHMENT_CHART_METRICS",
)
