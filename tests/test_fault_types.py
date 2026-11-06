from __future__ import annotations

import hashlib
import json

import pytest

from app.service.fault_types import (
    MAX_FAULT_TYPES_EDITOR_CODE_POINTS,
    decode_fault_types_json,
    decode_history_labels_payload,
    decode_run_config_payload,
    encode_fault_types_json,
    encode_history_labels_payload,
    encode_run_config_payload,
    normalize_fault_type_name,
    normalize_fault_types,
    parse_fault_types_editor,
    render_fault_types_editor,
)


def _editor_text_with_length(length: int, *, non_bmp: bool = False) -> str:
    names = [f"n{index}" for index in range(32)]
    if non_bmp:
        names[-1] = "😀"
    base = ",".join(names)
    assert len(base) <= length
    return names[0] + (" " * (length - len(base))) + "," + ",".join(names[1:])


def _canonical_identity(value: object) -> tuple[str, bytes]:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest(), payload


def test_editor_empty_trim_order_and_rendering() -> None:
    assert parse_fault_types_editor("") == []
    assert parse_fault_types_editor(" \t ") == []
    assert parse_fault_types_editor(" 异响 , 漏装,划伤 ") == [
        "异响",
        "漏装",
        "划伤",
    ]
    assert render_fault_types_editor([" 异响 ", "漏装"]) == "异响, 漏装"


@pytest.mark.parametrize("value", [",异响", "异响,", "异响,,漏装"])
def test_editor_rejects_empty_delimited_items(value: str) -> None:
    with pytest.raises(ValueError):
        parse_fault_types_editor(value)


@pytest.mark.parametrize(
    "value",
    [
        "\t异响",
        "异响\t",
        "\n异响",
        "异响\n",
        "\u2028异响",
        "异响\u2028",
        "\u2029异响",
        "异响\u2029",
    ],
)
def test_editor_rejects_forbidden_outer_characters_before_trimming(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        parse_fault_types_editor(value)


def test_full_width_comma_is_name_text_but_ascii_comma_is_not() -> None:
    assert normalize_fault_type_name("异响，漏装") == "异响，漏装"
    with pytest.raises(ValueError):
        normalize_fault_type_name("异响,漏装")


def test_duplicates_are_exact_case_sensitive_after_trim() -> None:
    with pytest.raises(ValueError):
        normalize_fault_types(["异响", " 异响 "])
    assert normalize_fault_types(["Fault", "fault"]) == ["Fault", "fault"]


def test_count_and_item_utf8_boundaries() -> None:
    assert len(normalize_fault_types([f"type-{index}" for index in range(32)])) == 32
    with pytest.raises(ValueError):
        normalize_fault_types([f"type-{index}" for index in range(33)])

    assert normalize_fault_type_name("a" * 128) == "a" * 128
    with pytest.raises(ValueError):
        normalize_fault_type_name("a" * 129)


def test_canonical_list_json_exact_size_boundary() -> None:
    names = [f"{index:02d}" + ("a" * 126) for index in range(31)]
    names.append("z" * 31)
    encoded = encode_fault_types_json(names)
    assert len(encoded.encode("utf-8")) == 4096

    names[-1] += "z"
    with pytest.raises(ValueError):
        encode_fault_types_json(names)


def test_editor_uses_python_code_point_boundary_without_truncation() -> None:
    assert len(_editor_text_with_length(4095)) == 4095
    assert len(parse_fault_types_editor(_editor_text_with_length(4095))) == 32
    assert len(parse_fault_types_editor(_editor_text_with_length(4096))) == 32
    with pytest.raises(ValueError):
        parse_fault_types_editor(_editor_text_with_length(4097))

    non_bmp = _editor_text_with_length(MAX_FAULT_TYPES_EDITOR_CODE_POINTS, non_bmp=True)
    assert len(non_bmp) == MAX_FAULT_TYPES_EDITOR_CODE_POINTS
    assert parse_fault_types_editor(non_bmp)[-1] == "😀"


@pytest.mark.parametrize(
    "value",
    [
        "bad\x00name",
        "bad\nname",
        "bad\u0001name",
        "bad\ud800name",
        "bad\u2028name",
        "bad\u2029name",
    ],
)
def test_forbidden_unicode_categories_are_rejected_before_trim(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_fault_type_name(value)


def test_format_private_use_and_other_unicode_scalars_are_allowed() -> None:
    assert normalize_fault_type_name("a\u200db") == "a\u200db"
    assert normalize_fault_type_name("a\ue000b") == "a\ue000b"
    assert normalize_fault_type_name("😀") == "😀"


def test_fault_types_json_is_strict_canonical_utf8_json() -> None:
    encoded = encode_fault_types_json([" 异响 ", "漏装"])
    assert encoded == '["异响","漏装"]'
    assert decode_fault_types_json(encoded) == ["异响", "漏装"]

    for invalid in (
        '["异响", "漏装"]',
        '[" 异响 "]',
        '"异响"',
        "[NaN]",
        b'["\xff"]',
    ):
        with pytest.raises(ValueError):
            decode_fault_types_json(invalid)


def test_run_config_codec_preserves_complete_config_and_exact_shape() -> None:
    value_id, payload = encode_run_config_payload(
        {
            "config_id": 7,
            "config_name": "配置 A",
            "fault_types": [" 异响 ", "漏装"],
            "record_duration": 5.0,
            "nested": {"enabled": True},
        }
    )
    decoded = decode_run_config_payload(
        value_id=value_id,
        canonical_bytes=payload,
    )
    assert set(decoded) == {"schema", "config"}
    assert decoded["schema"] == "jingcheng-run-config/v1"
    assert decoded["config"] == {
        "config_id": 7,
        "config_name": "配置 A",
        "fault_types": ["异响", "漏装"],
        "record_duration": 5.0,
        "nested": {"enabled": True},
    }


def test_run_config_decoder_rejects_malformed_noncanonical_and_identity_errors() -> None:
    valid_id, valid_bytes = encode_run_config_payload(
        {"config_id": 1, "config_name": "Cfg", "fault_types": []}
    )
    bad_values = [
        (valid_id, b"\xff"),
        (valid_id, valid_bytes + b" "),
        ("sha256:" + ("0" * 64), valid_bytes),
        _canonical_identity(
            {
                "schema": "jingcheng-run-config/v1",
                "config": {
                    "config_id": 1,
                    "config_name": "Cfg",
                    "fault_types": [],
                },
                "extra": True,
            }
        ),
    ]
    duplicate = (
        b'{"config":{"config_id":1,"config_name":"Cfg","fault_types":[]},'
        b'"schema":"jingcheng-run-config/v1","schema":"jingcheng-run-config/v1"}'
    )
    bad_values.append(
        ("sha256:" + hashlib.sha256(duplicate).hexdigest(), duplicate)
    )
    nan_payload = (
        b'{"config":{"config_id":1,"config_name":"Cfg","fault_types":[],'
        b'"value":NaN},"schema":"jingcheng-run-config/v1"}'
    )
    bad_values.append(
        ("sha256:" + hashlib.sha256(nan_payload).hexdigest(), nan_payload)
    )

    for value_id, payload in bad_values:
        with pytest.raises(ValueError):
            decode_run_config_payload(
                value_id=value_id,
                canonical_bytes=payload,
            )


def test_history_labels_codec_exact_shape_trigger_mapping_and_unresolved_form() -> None:
    value_id, payload = encode_history_labels_payload(
        config_id=7,
        name="配置 A",
        fault_types=[" 异响 ", "漏装"],
        manual_judgment_buttons_visible=True,
        details=[{"label": "录音时长", "value": "10 s"}],
        sn="SN-001",
        trigger_source="manual",
    )
    decoded = decode_history_labels_payload(
        value_id=value_id,
        canonical_bytes=payload,
    )
    assert set(decoded) == {"schema", "config", "sn", "trigger_source"}
    assert decoded["schema"] == "jingcheng-history-labels/v2"
    assert set(decoded["config"]) == {
        "config_id",
        "name",
        "fault_types",
        "manual_judgment_buttons_visible",
        "details",
    }
    assert decoded["config"]["fault_types"] == ["异响", "漏装"]
    assert decoded["config"]["manual_judgment_buttons_visible"] is True

    for trigger in ("plc", "manual", "scan", "import_audio"):
        for visible in (False, True):
            config_id = 7 if visible else None
            identity, canonical = encode_history_labels_payload(
                config_id=config_id,
                name="配置 A" if config_id is not None else "未解析配置",
                fault_types=[] if config_id is None else ["异响"],
                manual_judgment_buttons_visible=visible,
                details=[],
                sn="",
                trigger_source=trigger,
            )
            round_tripped = decode_history_labels_payload(
                value_id=identity,
                canonical_bytes=canonical,
            )
            assert round_tripped["trigger_source"] == trigger
            assert (
                round_tripped["config"]["manual_judgment_buttons_visible"]
                is visible
            )


def test_history_labels_v1_canonical_bytes_and_hash_remain_unchanged() -> None:
    legacy_bytes = (
        b'{"config":{"config_id":7,"details":[{"label":"\xe5\xbd\x95\xe9\x9f\xb3\xe6\x97\xb6\xe9\x95\xbf",'
        b'"value":"10 s"}],"fault_types":["\xe5\xbc\x82\xe5\x93\x8d","\xe6\xbc\x8f\xe8\xa3\x85"],'
        b'"name":"\xe9\x85\x8d\xe7\xbd\xae A"},"schema":"jingcheng-history-labels/v1",'
        b'"sn":"SN-001","trigger_source":"manual"}'
    )
    legacy_id = "sha256:44c601ddd4b38820f41c425ef468cc955b7361ba02152862aa6b58821b52602f"

    decoded = decode_history_labels_payload(
        value_id=legacy_id,
        canonical_bytes=legacy_bytes,
    )

    assert decoded["schema"] == "jingcheng-history-labels/v1"
    assert set(decoded["config"]) == {
        "config_id",
        "name",
        "fault_types",
        "details",
    }
    assert "manual_judgment_buttons_visible" not in decoded["config"]
    assert "sha256:" + hashlib.sha256(legacy_bytes).hexdigest() == legacy_id


@pytest.mark.parametrize(
    "updates",
    [
        {"trigger_source": "PLC"},
        {"details": [{"label": "x", "value": 1}]},
        {"fault_types": ["异响"]},
        {"manual_judgment_buttons_visible": True},
    ],
)
def test_history_labels_rejects_invalid_stable_or_unresolved_values(updates: dict) -> None:
    values = {
        "config_id": None,
        "name": "未解析配置",
        "fault_types": [],
        "manual_judgment_buttons_visible": False,
        "details": [],
        "sn": "",
        "trigger_source": "plc",
    }
    values.update(updates)
    with pytest.raises(ValueError):
        encode_history_labels_payload(**values)


def test_history_labels_decoder_rejects_wrong_shape_noncanonical_and_identity() -> None:
    value_id, payload = encode_history_labels_payload(
        config_id=1,
        name="Cfg",
        fault_types=[],
        manual_judgment_buttons_visible=False,
        details=[],
        sn="SN",
        trigger_source="scan",
    )
    wrong_shape_id, wrong_shape = _canonical_identity(
        {
            "schema": "jingcheng-history-labels/v1",
            "config": {
                "config_id": 1,
                "name": "Cfg",
                "fault_types": [],
                "details": [],
                "extra": True,
            },
            "sn": "SN",
            "trigger_source": "scan",
        }
    )
    for identity, canonical in (
        (value_id, payload + b"\n"),
        ("sha256:" + ("0" * 64), payload),
        (wrong_shape_id, wrong_shape),
    ):
        with pytest.raises(ValueError):
            decode_history_labels_payload(
                value_id=identity,
                canonical_bytes=canonical,
            )


@pytest.mark.parametrize("visible", [0, 1, "true", None])
def test_history_labels_v2_requires_exact_boolean_on_encode(visible: object) -> None:
    with pytest.raises(ValueError, match="manual_judgment_buttons_visible"):
        encode_history_labels_payload(
            config_id=1,
            name="Cfg",
            fault_types=[],
            manual_judgment_buttons_visible=visible,
            details=[],
            sn="SN",
            trigger_source="manual",
        )


@pytest.mark.parametrize("visible", [pytest.param("missing"), 0, 1, "false", None])
def test_history_labels_v2_decoder_rejects_missing_or_non_boolean_visible(
    visible: object,
) -> None:
    config = {
        "config_id": 1,
        "name": "Cfg",
        "fault_types": [],
        "details": [],
    }
    if visible != "missing":
        config["manual_judgment_buttons_visible"] = visible
    identity, canonical = _canonical_identity(
        {
            "schema": "jingcheng-history-labels/v2",
            "config": config,
            "sn": "SN",
            "trigger_source": "manual",
        }
    )

    with pytest.raises(ValueError, match="shape|manual_judgment_buttons_visible"):
        decode_history_labels_payload(
            value_id=identity,
            canonical_bytes=canonical,
        )
