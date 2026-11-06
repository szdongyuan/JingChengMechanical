"""Canonical lightweight Decision payload and Enrichment compatibility checks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .staged_execution import AnalysisBundleIdentity


REQUIRED_RESULT_FIELDS = (
    "legacy_overall",
    "limit_pass",
    "ai_pass",
    "ai_score",
    "analysis_window",
    "channel_results",
    "review",
    "displayed_config_id",
    "config_payload",
    "captured_duration_sec",
    "default_window",
    "fallback_window",
    "manual_reanalysis_allowed",
    "judgment_payload",
    "summary_payload",
    "chart_payload",
)
_REQUIRED_RESULT_FIELD_SET = frozenset(REQUIRED_RESULT_FIELDS)


def _require_judgment(value: object) -> None:
    if type(value) is not str:
        raise TypeError("judgment must be an exact str")
    if value not in {"PASS", "FAIL"}:
        raise ValueError("judgment must be PASS or FAIL")


def _require_result_fields(result: object) -> dict[str, object]:
    if type(result) is not dict:
        raise TypeError("result must be an exact dict")
    if frozenset(result) != _REQUIRED_RESULT_FIELD_SET:
        missing = sorted(_REQUIRED_RESULT_FIELD_SET - frozenset(result))
        unknown = sorted(frozenset(result) - _REQUIRED_RESULT_FIELD_SET)
        raise ValueError(
            f"result fields mismatch; missing={missing!r}, unknown={unknown!r}"
        )
    if type(result["summary_payload"]) is not dict:
        raise TypeError("summary_payload must be an exact dict")
    if type(result["channel_results"]) is not list:
        raise TypeError("channel_results must be an exact list")
    if type(result["judgment_payload"]) is not dict:
        raise TypeError("judgment_payload must be an exact dict")
    if type(result["chart_payload"]) is not dict:
        raise TypeError("chart_payload must be an exact dict")
    return result


def _canonical_json(value: object) -> str:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical.encode("utf-8")
        return canonical
    except UnicodeError as exc:
        raise ValueError("result payload must be valid UTF-8 JSON") from exc
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("result payload must contain finite JSON values") from exc


def json_values_equal_exact(left: object, right: object) -> bool:
    """Compare JSON values without Python's bool/int numeric coercion."""

    return _canonical_json(left) == _canonical_json(right)


def build_lightweight_canonical_result(
    *,
    judgment: str,
    result: dict[str, object],
) -> str:
    """Return the compatible Decision payload with charts deliberately empty."""

    _require_judgment(judgment)
    source = _require_result_fields(result)
    lightweight = {
        field_name: source[field_name]
        for field_name in REQUIRED_RESULT_FIELDS
        if field_name != "chart_payload"
    }
    lightweight["chart_payload"] = {}
    return _canonical_json({"judgment": judgment, "result": lightweight})


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def decode_json_object(value: object, name: str) -> dict[str, object]:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    if not value:
        raise ValueError(f"{name} must not be empty")
    try:
        root = json.loads(
            value,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except (ValueError, RecursionError, OverflowError) as exc:
        raise ValueError(f"{name} must be strict JSON: {exc}") from exc
    if type(root) is not dict:
        raise ValueError(f"{name} must encode a JSON object")
    canonical = _canonical_json(root)
    if value != canonical:
        raise ValueError(f"{name} must use canonical JSON serialization")
    return root


def decode_and_validate_canonical_result(
    value: object,
    name: str,
    *,
    require_empty_chart_payload: bool = False,
) -> dict[str, object]:
    root = decode_json_object(value, name)
    if set(root) != {"judgment", "result"}:
        raise ValueError(f"{name} must have the exact judgment/result root")
    _require_judgment(root["judgment"])
    result = _require_result_fields(root["result"])
    if require_empty_chart_payload and result["chart_payload"] != {}:
        raise ValueError(f"{name} chart_payload must be empty")
    return root


def validate_enrichment_compatibility(
    *,
    decision_identity: AnalysisBundleIdentity,
    enrichment_identity: AnalysisBundleIdentity,
    decision_canonical_result_json: str,
    enrichment_canonical_result_json: str,
) -> None:
    """Reject any Enrichment that would revise the committed Decision fact."""

    from .staged_execution import AnalysisBundleIdentity

    if type(decision_identity) is not AnalysisBundleIdentity:
        raise TypeError("decision_identity must be an exact AnalysisBundleIdentity")
    if type(enrichment_identity) is not AnalysisBundleIdentity:
        raise TypeError("enrichment_identity must be an exact AnalysisBundleIdentity")
    identity_labels = {
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
    for field_name, label in identity_labels.items():
        if getattr(decision_identity, field_name) != getattr(
            enrichment_identity, field_name
        ):
            raise ValueError(f"enrichment {label} mismatch")

    decision = decode_and_validate_canonical_result(
        decision_canonical_result_json,
        "decision_canonical_result_json",
        require_empty_chart_payload=True,
    )
    enrichment = decode_and_validate_canonical_result(
        enrichment_canonical_result_json,
        "enrichment_canonical_result_json",
    )
    if decision["judgment"] != enrichment["judgment"]:
        raise ValueError("enrichment cannot change judgment")

    decision_result = decision["result"]
    enrichment_result = enrichment["result"]
    enrichable_fields = {
        "channel_results",
        "judgment_payload",
        "summary_payload",
    }
    for field_name in REQUIRED_RESULT_FIELDS:
        if field_name == "chart_payload" or field_name in enrichable_fields:
            continue
        if not json_values_equal_exact(
            decision_result[field_name],
            enrichment_result[field_name],
        ):
            raise ValueError(
                f"enrichment cannot change non-chart result field {field_name}"
            )
    _validate_authoritative_enrichment_fields(
        decision_result,
        enrichment_result,
    )


def _decision_facts(value: object, path: str = "") -> dict[str, object]:
    facts: dict[str, object] = {}
    if type(value) is dict:
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if (
                key == "channel_index"
                or key == "passed"
                or key.endswith("_pass")
                or key in {"overall", "limit_pass", "ai_pass", "ai_score"}
            ):
                facts[child_path] = child
            facts.update(_decision_facts(child, child_path))
    elif type(value) is list:
        for index, child in enumerate(value):
            facts.update(_decision_facts(child, f"{path}[{index}]"))
    return facts


def _availability_true_paths(value: object, path: str = "") -> set[str]:
    paths: set[str] = set()
    if type(value) is dict:
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key.endswith("_available") and child is True:
                paths.add(child_path)
            paths.update(_availability_true_paths(child, child_path))
    elif type(value) is list:
        for index, child in enumerate(value):
            paths.update(_availability_true_paths(child, f"{path}[{index}]"))
    return paths


def _value_at_path(value: object, path: str) -> object:
    current = value
    for part in path.replace("]", "").replace("[", ".").split("."):
        if not part:
            continue
        current = current[int(part)] if part.isdigit() else current[part]
    return current


def _summary_rows(value: object) -> dict[str, dict]:
    if type(value) is not dict or type(value.get("rows")) is not list:
        raise ValueError("enrichment summary payload is invalid")
    rows = {}
    for row in value["rows"]:
        if type(row) is not dict or type(row.get("item_key")) is not str:
            raise ValueError("enrichment summary row is invalid")
        rows[row["item_key"]] = row
    return rows


def _validate_authoritative_enrichment_fields(
    decision_result: dict[str, object],
    enrichment_result: dict[str, object],
) -> None:
    for field_name in ("channel_results", "judgment_payload"):
        decision_value = decision_result[field_name]
        enrichment_value = enrichment_result[field_name]
        if not json_values_equal_exact(
            _decision_facts(decision_value),
            _decision_facts(enrichment_value),
        ):
            raise ValueError(
                "enrichment cannot change non-chart authoritative "
                f"{field_name} conclusions"
            )
        for path in _availability_true_paths(decision_value):
            if _value_at_path(enrichment_value, path) is not True:
                raise ValueError(
                    f"enrichment cannot revoke required availability at {path}"
                )

    decision_summary = decision_result["summary_payload"]
    enrichment_summary = enrichment_result["summary_payload"]
    for key in ("overall_status", "overall_label"):
        if not json_values_equal_exact(
            decision_summary.get(key), enrichment_summary.get(key)
        ):
            raise ValueError(
                f"enrichment cannot change non-chart summary {key}"
            )
    decision_rows = _summary_rows(decision_summary)
    enrichment_rows = _summary_rows(enrichment_summary)
    for item_key, decision_row in decision_rows.items():
        if decision_row.get("constrained") is True or item_key == "ai":
            if not json_values_equal_exact(
                decision_row,
                enrichment_rows.get(item_key),
            ):
                raise ValueError(
                    "enrichment cannot change non-chart authoritative "
                    f"summary row {item_key}"
                )


__all__ = (
    "REQUIRED_RESULT_FIELDS",
    "build_lightweight_canonical_result",
    "decode_and_validate_canonical_result",
    "decode_json_object",
    "json_values_equal_exact",
    "validate_enrichment_compatibility",
)
