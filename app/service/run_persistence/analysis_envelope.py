"""Pure validation and canonicalization for durable analysis inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from types import MappingProxyType


class AnalysisEnvelopeError(ValueError):
    """The analysis input is not the exact supported envelope."""


@dataclass(frozen=True, slots=True)
class DecodedToolchainEnvelope:
    toolchain_id: str
    execution_revision: str
    execution_mode: str
    entrypoint: str
    package_dir: str
    params: Mapping[str, object]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DecodedAnalysisEnvelope:
    config: Mapping[str, object]
    workorder: Mapping[str, object]
    channel_mapping: Mapping[str, object]
    window_start_seconds: float
    window_end_seconds: float
    algorithm_revision: str
    execution_timeout_seconds: float
    queue_timeout_seconds: float | None
    toolchain: DecodedToolchainEnvelope | None


_ROOT_KEYS = frozenset(
    {"schema", "config", "workorder", "channel_mapping", "analysis"}
)
_ANALYSIS_KEYS = frozenset(
    {
        "window_start_seconds",
        "window_end_seconds",
        "algorithm_revision",
        "execution_timeout_seconds",
        "queue_timeout_seconds",
        "toolchain",
    }
)
_TOOLCHAIN_KEYS = frozenset(
    {
        "toolchain_id",
        "execution_revision",
        "execution_mode",
        "entrypoint",
        "package_dir",
        "params",
        "timeout_seconds",
    }
)
_MAX_JSON_NESTING_DEPTH = 64


def _require_utf8_string(value: str, name: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise AnalysisEnvelopeError(
            f"{name} must contain only Unicode scalar values"
        ) from None
    return value


def _normalize_json_value(
    value: object,
    name: str,
    *,
    depth: int,
) -> object:
    if isinstance(value, Mapping):
        if depth > _MAX_JSON_NESTING_DEPTH:
            raise AnalysisEnvelopeError(
                f"{name} exceeds maximum JSON nesting depth"
            )
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise AnalysisEnvelopeError(
                    f"{name} must be a valid JSON object"
                )
            _require_utf8_string(key, f"{name} object key")
            normalized[key] = _normalize_json_value(
                item,
                f"{name}.{key}",
                depth=depth + 1,
            )
        return normalized
    if type(value) in (list, tuple):
        if depth > _MAX_JSON_NESTING_DEPTH:
            raise AnalysisEnvelopeError(
                f"{name} exceeds maximum JSON nesting depth"
            )
        return [
            _normalize_json_value(
                item,
                f"{name}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    if type(value) is str:
        return _require_utf8_string(value, name)
    if value is None or type(value) in (int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise AnalysisEnvelopeError(f"{name} must contain only valid JSON values")


def _copy_json_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AnalysisEnvelopeError(f"{name} must be a valid JSON object")
    try:
        normalized = _normalize_json_value(value, name, depth=1)
    except AnalysisEnvelopeError:
        raise
    except Exception:
        raise AnalysisEnvelopeError(f"{name} must be a valid JSON object") from None
    if type(normalized) is not dict:
        raise AnalysisEnvelopeError(f"{name} must be a valid JSON object")
    return normalized


def _canonical_dumps(value: dict[str, object], name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        raise AnalysisEnvelopeError(f"{name} must be valid JSON") from None


def _require_exact_keys(
    value: object, expected: frozenset[str], name: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise AnalysisEnvelopeError(f"{name} must be a valid JSON object")
    if set(value) != expected:
        raise AnalysisEnvelopeError(f"{name} keys must match the closed contract")
    return value


def _require_json_object(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise AnalysisEnvelopeError(f"{name} must be a valid JSON object")
    return value


def _require_nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or value == "":
        raise AnalysisEnvelopeError(f"{name} must be a non-empty string")
    return value


def _require_float(
    value: object,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise AnalysisEnvelopeError(f"{name} must be an exact finite float")
    if positive and value <= 0.0:
        raise AnalysisEnvelopeError(f"{name} must be positive")
    if nonnegative and value < 0.0:
        raise AnalysisEnvelopeError(f"{name} must be non-negative")
    return value


def _validate_envelope(envelope: dict[str, object]) -> None:
    root = _require_exact_keys(envelope, _ROOT_KEYS, "root")
    if type(root["schema"]) is not str or root["schema"] != "analysis-input/v1":
        raise AnalysisEnvelopeError("schema must be exactly analysis-input/v1")
    _require_json_object(root["config"], "config")
    _require_json_object(root["workorder"], "workorder")
    _require_json_object(root["channel_mapping"], "channel_mapping")

    analysis = _require_exact_keys(root["analysis"], _ANALYSIS_KEYS, "analysis")
    start = _require_float(
        analysis["window_start_seconds"],
        "window_start_seconds",
        nonnegative=True,
    )
    end = _require_float(analysis["window_end_seconds"], "window_end_seconds")
    if end <= start:
        raise AnalysisEnvelopeError(
            "window_end_seconds must be strictly greater than window_start_seconds"
        )
    _require_nonempty_string(
        analysis["algorithm_revision"], "algorithm_revision"
    )
    _require_float(
        analysis["execution_timeout_seconds"],
        "execution_timeout_seconds",
        positive=True,
    )
    queue_timeout = analysis["queue_timeout_seconds"]
    if queue_timeout is not None:
        _require_float(
            queue_timeout,
            "queue_timeout_seconds",
            positive=True,
        )

    toolchain_value = analysis["toolchain"]
    if toolchain_value is None:
        return
    toolchain = _require_exact_keys(
        toolchain_value, _TOOLCHAIN_KEYS, "toolchain"
    )
    for field in (
        "toolchain_id",
        "execution_revision",
        "execution_mode",
        "entrypoint",
        "package_dir",
    ):
        _require_nonempty_string(toolchain[field], field)
    _require_json_object(toolchain["params"], "params")
    _require_float(
        toolchain["timeout_seconds"],
        "timeout_seconds",
        positive=True,
    )


def freeze_analysis_envelope(value: object) -> str:
    """Deep-copy, validate, and canonically serialize one closed envelope."""

    try:
        copied = _copy_json_object(value, "analysis envelope")
        _validate_envelope(copied)
        return _canonical_dumps(copied, "analysis envelope")
    except AnalysisEnvelopeError:
        raise
    except RecursionError:
        raise AnalysisEnvelopeError(
            "analysis envelope exceeds maximum JSON nesting depth"
        ) from None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _immutable_json_value(
    value: object,
    name: str,
    *,
    depth: int,
) -> object:
    if type(value) is dict:
        if depth > _MAX_JSON_NESTING_DEPTH:
            raise AnalysisEnvelopeError(
                f"{name} exceeds maximum JSON nesting depth"
            )
        return MappingProxyType(
            {
                key: _immutable_json_value(
                    item,
                    f"{name}.{key}",
                    depth=depth + 1,
                )
                for key, item in value.items()
            }
        )
    if type(value) is list:
        if depth > _MAX_JSON_NESTING_DEPTH:
            raise AnalysisEnvelopeError(
                f"{name} exceeds maximum JSON nesting depth"
            )
        return tuple(
            _immutable_json_value(
                item,
                f"{name}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        )
    return value


def _decode_analysis_envelope(value: str) -> DecodedAnalysisEnvelope:
    if type(value) is not str:
        raise AnalysisEnvelopeError("analysis envelope JSON must be a string")
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AnalysisEnvelopeError(
            "analysis envelope must be valid JSON"
        ) from None
    normalized = _normalize_json_value(
        decoded,
        "analysis envelope",
        depth=1,
    )
    if type(normalized) is not dict:
        raise AnalysisEnvelopeError(
            "analysis envelope must be a valid JSON object"
        )
    _validate_envelope(normalized)

    analysis = normalized["analysis"]
    toolchain_value = analysis["toolchain"]
    toolchain = None
    if type(toolchain_value) is dict:
        toolchain = DecodedToolchainEnvelope(
            toolchain_id=toolchain_value["toolchain_id"],
            execution_revision=toolchain_value["execution_revision"],
            execution_mode=toolchain_value["execution_mode"],
            entrypoint=toolchain_value["entrypoint"],
            package_dir=toolchain_value["package_dir"],
            params=_immutable_json_value(
                toolchain_value["params"],
                "analysis envelope.analysis.toolchain.params",
                depth=4,
            ),
            timeout_seconds=toolchain_value["timeout_seconds"],
        )
    return DecodedAnalysisEnvelope(
        config=_immutable_json_value(
            normalized["config"],
            "analysis envelope.config",
            depth=2,
        ),
        workorder=_immutable_json_value(
            normalized["workorder"],
            "analysis envelope.workorder",
            depth=2,
        ),
        channel_mapping=_immutable_json_value(
            normalized["channel_mapping"],
            "analysis envelope.channel_mapping",
            depth=2,
        ),
        window_start_seconds=analysis["window_start_seconds"],
        window_end_seconds=analysis["window_end_seconds"],
        algorithm_revision=analysis["algorithm_revision"],
        execution_timeout_seconds=analysis["execution_timeout_seconds"],
        queue_timeout_seconds=analysis["queue_timeout_seconds"],
        toolchain=toolchain,
    )


def decode_analysis_envelope(value: str) -> DecodedAnalysisEnvelope:
    """Decode validated durable JSON into recursively immutable values."""

    try:
        return _decode_analysis_envelope(value)
    except AnalysisEnvelopeError:
        raise
    except RecursionError:
        raise AnalysisEnvelopeError(
            "analysis envelope exceeds maximum JSON nesting depth"
        ) from None


def canonicalize_json_object(value: object, name: str) -> str:
    """Deep-copy and canonically serialize one ordinary JSON object."""

    if type(name) is not str or name == "":
        raise AnalysisEnvelopeError("name must be a non-empty string")
    copied = _copy_json_object(value, name)
    return _canonical_dumps(copied, name)
