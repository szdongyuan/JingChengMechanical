from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from copy import deepcopy


MAX_FAULT_TYPE_COUNT = 32
MAX_FAULT_TYPE_UTF8_BYTES = 128
MAX_FAULT_TYPES_JSON_UTF8_BYTES = 4096
MAX_FAULT_TYPES_EDITOR_CODE_POINTS = 4096

_RUN_CONFIG_SCHEMA = "jingcheng-run-config/v1"
_HISTORY_LABELS_LEGACY_SCHEMA = "jingcheng-history-labels/v1"
_HISTORY_LABELS_SCHEMA = "jingcheng-history-labels/v2"
_HISTORY_TRIGGER_SOURCES = frozenset({"plc", "manual", "scan", "import_audio"})
_FORBIDDEN_NAME_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})


def normalize_fault_type_name(value: object) -> str:
    if type(value) is not str:
        raise ValueError("fault type name must be a string")
    for character in value:
        if unicodedata.category(character) in _FORBIDDEN_NAME_CATEGORIES:
            raise ValueError("fault type name contains a forbidden character")
    normalized = value.strip()
    if not normalized:
        raise ValueError("fault type name must not be empty")
    if "," in normalized:
        raise ValueError("fault type name must not contain an ASCII comma")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("fault type name must contain only Unicode scalars") from exc
    if len(encoded) > MAX_FAULT_TYPE_UTF8_BYTES:
        raise ValueError(
            f"fault type name exceeds {MAX_FAULT_TYPE_UTF8_BYTES} UTF-8 bytes"
        )
    return normalized


def normalize_fault_types(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("fault_types must be a list")
    if len(value) > MAX_FAULT_TYPE_COUNT:
        raise ValueError(f"fault_types exceeds {MAX_FAULT_TYPE_COUNT} items")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = normalize_fault_type_name(item)
        if name in seen:
            raise ValueError(f"duplicate fault type: {name}")
        seen.add(name)
        normalized.append(name)
    _encode_normalized_fault_types(normalized)
    return normalized


def parse_fault_types_editor(value: object) -> list[str]:
    if type(value) is not str:
        raise ValueError("fault types editor value must be a string")
    stripped = value.strip()
    if len(stripped) > MAX_FAULT_TYPES_EDITOR_CODE_POINTS:
        raise ValueError(
            "fault types editor exceeds "
            f"{MAX_FAULT_TYPES_EDITOR_CODE_POINTS} Unicode code points"
        )
    if not stripped:
        return []
    return normalize_fault_types(value.split(","))


def render_fault_types_editor(value: object) -> str:
    return ", ".join(normalize_fault_types(value))


def encode_fault_types_json(value: object) -> str:
    return _encode_normalized_fault_types(normalize_fault_types(value))


def decode_fault_types_json(value: object) -> list[str]:
    if type(value) is not str:
        raise ValueError("fault_types_json must be a string")
    decoded = _load_json_text(value)
    if type(decoded) is not list:
        raise ValueError("fault_types_json must contain a JSON array")
    normalized = normalize_fault_types(decoded)
    if _encode_normalized_fault_types(normalized) != value:
        raise ValueError("fault_types_json must use canonical JSON encoding")
    return list(normalized)


def encode_run_config_payload(
    semantic_config: object,
) -> tuple[str, bytes]:
    config = _normalize_run_config(semantic_config)
    return _encode_identified_payload(
        {
            "schema": _RUN_CONFIG_SCHEMA,
            "config": config,
        }
    )


def decode_run_config_payload(
    *,
    value_id: object,
    canonical_bytes: object,
) -> dict[str, object]:
    decoded = _decode_identified_payload(
        value_id=value_id,
        canonical_bytes=canonical_bytes,
    )
    if type(decoded) is not dict or set(decoded) != {"schema", "config"}:
        raise ValueError("run config payload has an invalid root shape")
    if decoded["schema"] != _RUN_CONFIG_SCHEMA:
        raise ValueError("run config payload has an unsupported schema")
    normalized = {
        "schema": _RUN_CONFIG_SCHEMA,
        "config": _normalize_run_config(decoded["config"]),
    }
    if _canonical_json_bytes(normalized) != canonical_bytes:
        raise ValueError("run config payload must be canonical")
    return deepcopy(normalized)


def encode_history_labels_payload(
    *,
    config_id: object,
    name: object,
    fault_types: object,
    manual_judgment_buttons_visible: object,
    details: object,
    sn: object,
    trigger_source: object,
) -> tuple[str, bytes]:
    return _encode_identified_payload(
        _normalize_history_labels(
            {
                "schema": _HISTORY_LABELS_SCHEMA,
                "config": {
                    "config_id": config_id,
                    "name": name,
                    "fault_types": fault_types,
                    "manual_judgment_buttons_visible": (
                        manual_judgment_buttons_visible
                    ),
                    "details": details,
                },
                "sn": sn,
                "trigger_source": trigger_source,
            },
            allow_tuple_details=True,
        )
    )


def decode_history_labels_payload(
    *,
    value_id: object,
    canonical_bytes: object,
) -> dict[str, object]:
    decoded = _decode_identified_payload(
        value_id=value_id,
        canonical_bytes=canonical_bytes,
    )
    normalized = _normalize_history_labels(decoded, allow_tuple_details=False)
    if _canonical_json_bytes(normalized) != canonical_bytes:
        raise ValueError("history labels payload must be canonical")
    return deepcopy(normalized)


def _encode_normalized_fault_types(normalized: list[str]) -> str:
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:  # defensive; names already reject surrogates
        raise ValueError("fault_types must contain only Unicode scalars") from exc
    if size > MAX_FAULT_TYPES_JSON_UTF8_BYTES:
        raise ValueError(
            "fault_types canonical JSON exceeds "
            f"{MAX_FAULT_TYPES_JSON_UTF8_BYTES} UTF-8 bytes"
        )
    return encoded


def _load_json_text(text: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("invalid JSON") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _normalize_run_config(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("semantic config must be an object")
    config = deepcopy(value)
    _validate_json_value(config)
    config_id = config.get("config_id")
    if type(config_id) is not int or config_id <= 0:
        raise ValueError("config_id must be a positive integer")
    _validate_nonempty_scalar_string(config.get("config_name"), "config_name")
    if "fault_types" not in config:
        raise ValueError("semantic config is missing fault_types")
    config["fault_types"] = normalize_fault_types(config["fault_types"])
    return config


def _normalize_history_labels(
    value: object,
    *,
    allow_tuple_details: bool,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "config",
        "sn",
        "trigger_source",
    }:
        raise ValueError("history labels payload has an invalid root shape")
    schema = value["schema"]
    if schema not in {
        _HISTORY_LABELS_LEGACY_SCHEMA,
        _HISTORY_LABELS_SCHEMA,
    }:
        raise ValueError("history labels payload has an unsupported schema")
    config = value["config"]
    expected_config_fields = {
        "config_id",
        "name",
        "fault_types",
        "details",
    }
    if schema == _HISTORY_LABELS_SCHEMA:
        expected_config_fields.add("manual_judgment_buttons_visible")
    if type(config) is not dict or set(config) != expected_config_fields:
        raise ValueError("history labels config has an invalid shape")

    config_id = config["config_id"]
    if config_id is not None and (type(config_id) is not int or config_id <= 0):
        raise ValueError("history labels config_id must be positive or null")
    name = _validate_nonempty_scalar_string(config["name"], "name")
    fault_types = normalize_fault_types(config["fault_types"])
    if config_id is None and fault_types:
        raise ValueError("unresolved history labels must have no fault types")
    if schema == _HISTORY_LABELS_SCHEMA:
        manual_judgment_buttons_visible = config[
            "manual_judgment_buttons_visible"
        ]
        if type(manual_judgment_buttons_visible) is not bool:
            raise ValueError(
                "manual_judgment_buttons_visible must be a boolean"
            )
        if config_id is None and manual_judgment_buttons_visible:
            raise ValueError(
                "unresolved history labels must hide manual judgment buttons"
            )
    details = _normalize_details(
        config["details"],
        allow_tuple=allow_tuple_details,
    )
    sn = _validate_scalar_string(configured=value["sn"], field_name="sn")
    trigger_source = value["trigger_source"]
    if type(trigger_source) is not str or trigger_source not in _HISTORY_TRIGGER_SOURCES:
        raise ValueError("history labels trigger_source is invalid")

    normalized_config = {
        "config_id": config_id,
        "name": name,
        "fault_types": fault_types,
        "details": details,
    }
    if schema == _HISTORY_LABELS_SCHEMA:
        normalized_config["manual_judgment_buttons_visible"] = (
            manual_judgment_buttons_visible
        )
    return {
        "schema": schema,
        "config": normalized_config,
        "sn": sn,
        "trigger_source": trigger_source,
    }


def _normalize_details(value: object, *, allow_tuple: bool) -> list[dict[str, str]]:
    allowed_types = (list, tuple) if allow_tuple else (list,)
    if not isinstance(value, allowed_types):
        raise ValueError("history labels details must be a list")
    normalized: list[dict[str, str]] = []
    for item in value:
        if type(item) is not dict or set(item) != {"label", "value"}:
            raise ValueError("history labels detail has an invalid shape")
        normalized.append(
            {
                "label": _validate_scalar_string(
                    configured=item["label"],
                    field_name="detail label",
                ),
                "value": _validate_scalar_string(
                    configured=item["value"],
                    field_name="detail value",
                ),
            }
        )
    return normalized


def _validate_nonempty_scalar_string(value: object, field_name: str) -> str:
    normalized = _validate_scalar_string(
        configured=value,
        field_name=field_name,
    )
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _validate_scalar_string(*, configured: object, field_name: str) -> str:
    if type(configured) is not str:
        raise ValueError(f"{field_name} must be a string")
    try:
        configured.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain only Unicode scalars") from exc
    return configured


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if type(value) is str:
        _validate_scalar_string(configured=value, field_name="JSON string")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _validate_scalar_string(configured=key, field_name="JSON object key")
            _validate_json_value(item)
        return
    raise ValueError("semantic config contains a non-JSON value")


def _encode_identified_payload(value: object) -> tuple[str, bytes]:
    canonical = _canonical_json_bytes(value)
    return "sha256:" + hashlib.sha256(canonical).hexdigest(), canonical


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("value cannot be encoded as canonical JSON") from exc


def _decode_identified_payload(
    *,
    value_id: object,
    canonical_bytes: object,
) -> object:
    if type(value_id) is not str:
        raise ValueError("payload value_id must be a string")
    if type(canonical_bytes) is not bytes:
        raise ValueError("payload canonical_bytes must be bytes")
    expected_id = "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
    if value_id != expected_id:
        raise ValueError("payload identity mismatch")
    try:
        text = canonical_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("payload is not valid UTF-8") from exc
    return _load_json_text(text)
