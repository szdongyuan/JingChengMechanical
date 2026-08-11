from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from types import MappingProxyType

import pytest

from app.service.run_persistence import analysis_envelope
from app.service.run_persistence.analysis_envelope import (
    AnalysisEnvelopeError,
    DecodedAnalysisEnvelope,
    DecodedToolchainEnvelope,
    canonicalize_json_object,
    decode_analysis_envelope,
    freeze_analysis_envelope,
)
from tests.run_persistence.conftest import make_analysis_envelope


def _toolchain() -> dict[str, object]:
    return {
        "toolchain_id": "toolchain",
        "execution_revision": "revision",
        "execution_mode": "subprocess",
        "entrypoint": "analyze",
        "package_dir": "C:/toolchain",
        "params": {"阈值": 0.5, "channels": [0, 1]},
        "timeout_seconds": 30.0,
    }


def _nested_lists(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def _exercise_envelope_public_path(path: str, payload: object) -> object:
    if path == "freeze":
        return freeze_analysis_envelope(
            make_analysis_envelope(config={"nested": [payload]})
        )
    if path == "decode":
        serialized = json.dumps(
            make_analysis_envelope(config={"nested": [payload]}),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return decode_analysis_envelope(serialized)
    if path == "canonicalize":
        return canonicalize_json_object({"nested": [payload]}, "params")
    raise AssertionError(f"unexpected public path: {path}")


def test_freeze_returns_canonical_utf8_compact_closed_envelope() -> None:
    envelope = make_analysis_envelope(
        config={"z": 1, "显示": "正常", "a": 2},
        analysis={"toolchain": _toolchain()},
    )

    frozen = freeze_analysis_envelope(envelope)

    assert frozen == json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert "显示" in frozen
    assert "\\u663e" not in frozen
    assert ": " not in frozen
    assert ", " not in frozen


@pytest.mark.parametrize("path", ["freeze", "decode", "canonicalize"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("location", ["value", "key"])
def test_public_paths_reject_isolated_unicode_surrogates_in_nested_json(
    path: str,
    surrogate: str,
    location: str,
) -> None:
    payload: object
    if location == "value":
        payload = {"valid-key": surrogate}
    else:
        payload = {surrogate: "valid-value"}

    with pytest.raises(AnalysisEnvelopeError, match="Unicode"):
        _exercise_envelope_public_path(path, payload)


def test_valid_unicode_scalars_round_trip_and_canonical_outputs_encode_utf8() -> None:
    unicode_values = {
        "普通键": "普通值",
        "\U0001f680": ["résumé", "\U0001f642"],
    }
    envelope = make_analysis_envelope(config=unicode_values)

    frozen = freeze_analysis_envelope(envelope)
    canonical = canonicalize_json_object(unicode_values, "config")
    decoded = decode_analysis_envelope(frozen)

    assert frozen.encode("utf-8")
    assert canonical.encode("utf-8")
    assert decoded.config["普通键"] == "普通值"
    assert decoded.config["\U0001f680"] == ("résumé", "\U0001f642")
    assert json.loads(canonical) == unicode_values


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.pop("config"), "root keys"),
        (lambda value: value.update(extra=True), "root keys"),
        (lambda value: value.update(schema="analysis-input/v2"), "schema"),
        (lambda value: value.update(config=[]), "config"),
        (lambda value: value.update(workorder=[]), "workorder"),
        (lambda value: value.update(channel_mapping=[]), "channel_mapping"),
        (lambda value: value.update(analysis=[]), "analysis"),
        (lambda value: value["analysis"].pop("toolchain"), "analysis keys"),
        (
            lambda value: value["analysis"].update(extra=True),
            "analysis keys",
        ),
    ],
)
def test_freeze_rejects_wrong_closed_shape(mutator, message: str) -> None:
    envelope = make_analysis_envelope()
    mutator(envelope)

    with pytest.raises(AnalysisEnvelopeError, match=message):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_start_seconds", 0),
        ("window_start_seconds", True),
        ("window_start_seconds", -0.1),
        ("window_start_seconds", float("nan")),
        ("window_end_seconds", 3),
        ("window_end_seconds", False),
        ("window_end_seconds", float("inf")),
        ("execution_timeout_seconds", 30),
        ("execution_timeout_seconds", True),
        ("execution_timeout_seconds", 0.0),
        ("execution_timeout_seconds", float("-inf")),
        ("queue_timeout_seconds", 30),
        ("queue_timeout_seconds", False),
        ("queue_timeout_seconds", 0.0),
        ("queue_timeout_seconds", float("nan")),
    ],
)
def test_scheduler_float_fields_require_exact_finite_float(
    field: str, value: object
) -> None:
    envelope = make_analysis_envelope(analysis={field: value})

    with pytest.raises(AnalysisEnvelopeError, match=field):
        freeze_analysis_envelope(envelope)


def test_window_end_must_be_strictly_after_start() -> None:
    envelope = make_analysis_envelope(
        analysis={"window_start_seconds": 3.0, "window_end_seconds": 3.0}
    )

    with pytest.raises(AnalysisEnvelopeError, match="window_end_seconds"):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize("value", ["", 7, True, None])
def test_algorithm_revision_requires_nonempty_exact_string(value: object) -> None:
    envelope = make_analysis_envelope(
        analysis={"algorithm_revision": value}
    )

    with pytest.raises(AnalysisEnvelopeError, match="algorithm_revision"):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.pop("entrypoint"),
        lambda value: value.update(extra=True),
    ],
)
def test_toolchain_requires_exact_keys(mutator) -> None:
    toolchain = _toolchain()
    mutator(toolchain)
    envelope = make_analysis_envelope(analysis={"toolchain": toolchain})

    with pytest.raises(AnalysisEnvelopeError, match="toolchain keys"):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("toolchain_id", ""),
        ("execution_revision", 1),
        ("execution_mode", True),
        ("entrypoint", None),
        ("package_dir", ""),
        ("params", []),
        ("timeout_seconds", 30),
        ("timeout_seconds", True),
        ("timeout_seconds", 0.0),
        ("timeout_seconds", float("inf")),
    ],
)
def test_toolchain_fields_are_exact_and_closed(field: str, value: object) -> None:
    toolchain = _toolchain()
    toolchain[field] = value
    envelope = make_analysis_envelope(analysis={"toolchain": toolchain})

    with pytest.raises(AnalysisEnvelopeError, match=field):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize(
    "value",
    [
        {"bad": object()},
        {"bad": float("nan")},
        {1: "non-string-key"},
    ],
)
def test_json_object_fields_reject_non_json_values(value: object) -> None:
    envelope = make_analysis_envelope(config=value)

    with pytest.raises(AnalysisEnvelopeError, match="config"):
        freeze_analysis_envelope(envelope)


@pytest.mark.parametrize(
    "serialized",
    [
        '{"schema":"analysis-input/v1","schema":"analysis-input/v1",'
        '"config":{},"workorder":{},"channel_mapping":{},'
        '"analysis":{"window_start_seconds":0.0,"window_end_seconds":3.0,'
        '"algorithm_revision":"revision","execution_timeout_seconds":30.0,'
        '"queue_timeout_seconds":null,"toolchain":null}}',
        '{"schema":"analysis-input/v1","config":{"x":1,"x":2},'
        '"workorder":{},"channel_mapping":{},'
        '"analysis":{"window_start_seconds":0.0,"window_end_seconds":3.0,'
        '"algorithm_revision":"revision","execution_timeout_seconds":30.0,'
        '"queue_timeout_seconds":null,"toolchain":null}}',
        '{"schema":"analysis-input/v1","config":{},"workorder":{},'
        '"channel_mapping":{},"analysis":{"window_start_seconds":NaN,'
        '"window_end_seconds":3.0,"algorithm_revision":"revision",'
        '"execution_timeout_seconds":30.0,"queue_timeout_seconds":null,'
        '"toolchain":null}}',
    ],
)
def test_decode_rejects_duplicate_keys_and_invalid_json_constants(
    serialized: str,
) -> None:
    with pytest.raises(AnalysisEnvelopeError):
        decode_analysis_envelope(serialized)


def test_freeze_and_decode_are_deep_copy_isolated_and_immutable() -> None:
    envelope = make_analysis_envelope(
        config={"nested": {"values": [1, 2]}},
        analysis={"toolchain": _toolchain()},
    )

    frozen = freeze_analysis_envelope(envelope)
    decoded = decode_analysis_envelope(frozen)
    envelope["config"]["nested"]["values"].append(3)
    envelope["analysis"]["toolchain"]["params"]["channels"].append(2)

    assert isinstance(decoded, DecodedAnalysisEnvelope)
    assert isinstance(decoded.config, MappingProxyType)
    assert decoded.config["nested"]["values"] == (1, 2)
    assert isinstance(decoded.toolchain, DecodedToolchainEnvelope)
    assert isinstance(decoded.toolchain.params, MappingProxyType)
    assert decoded.toolchain.params["channels"] == (0, 1)
    assert canonicalize_json_object(decoded.config, "config") == (
        '{"nested":{"values":[1,2]}}'
    )
    with pytest.raises(TypeError):
        decoded.config["new"] = "value"
    with pytest.raises(TypeError):
        decoded.config["nested"]["new"] = "value"
    with pytest.raises(FrozenInstanceError):
        decoded.window_start_seconds = 2.0


def test_freeze_and_decode_round_trip_at_maximum_json_nesting_depth() -> None:
    envelope = make_analysis_envelope(
        config={"nested": _nested_lists(62)}
    )

    frozen = freeze_analysis_envelope(envelope)
    decoded = decode_analysis_envelope(frozen)

    nested = decoded.config["nested"]
    for _ in range(62):
        assert type(nested) is tuple
        nested = nested[0]
    assert nested == "leaf"


def test_freeze_rejects_excessive_json_nesting_with_typed_error() -> None:
    envelope = make_analysis_envelope(
        config={"nested": _nested_lists(63)}
    )

    with pytest.raises(AnalysisEnvelopeError, match="nesting"):
        freeze_analysis_envelope(envelope)


def test_decode_rejects_excessive_json_nesting_with_typed_error() -> None:
    envelope = make_analysis_envelope(
        config={"nested": _nested_lists(63)}
    )
    serialized = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(AnalysisEnvelopeError, match="nesting"):
        decode_analysis_envelope(serialized)


def test_decode_contains_parser_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(*args: object, **kwargs: object) -> object:
        raise RecursionError

    monkeypatch.setattr(json, "loads", recurse)

    with pytest.raises(AnalysisEnvelopeError):
        decode_analysis_envelope("{}")


def test_freeze_contains_runtime_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(value: object, name: str) -> dict[str, object]:
        raise RecursionError

    monkeypatch.setattr(analysis_envelope, "_copy_json_object", recurse)

    with pytest.raises(AnalysisEnvelopeError):
        freeze_analysis_envelope(make_analysis_envelope())


def test_canonicalize_json_object_isolated_canonical_and_rejects_non_objects() -> None:
    value = {"z": [{"显示": True}], "a": 1}

    canonical = canonicalize_json_object(value, "params")
    value["z"][0]["显示"] = False

    assert canonical == '{"a":1,"z":[{"显示":true}]}'
    with pytest.raises(AnalysisEnvelopeError, match="params"):
        canonicalize_json_object([], "params")
    with pytest.raises(AnalysisEnvelopeError, match="params"):
        canonicalize_json_object({"bad": float("nan")}, "params")


@pytest.mark.parametrize("value", [b"{}", bytearray(b"{}"), None, 1])
def test_decode_requires_exact_string(value: object) -> None:
    with pytest.raises(AnalysisEnvelopeError):
        decode_analysis_envelope(value)
