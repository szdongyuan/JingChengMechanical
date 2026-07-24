from __future__ import annotations

import pytest

from app.service.plc_protocol import (
    PLC_CONFIG_RETURN_FIELDS,
    PLC_FIELD_BY_KEY,
    PLC_FIELD_SPECS,
    PLC_RESULT_FIELDS,
    PLC_WORKORDER_FIELDS,
    PlcDirection,
    PlcValueType,
    parse_plc_offset,
)


EXPECTED_FIELDS = {
    "start_signal_offset": (PlcValueType.BOOL, PlcDirection.READ_WRITE),
    "current_value_offset": (PlcValueType.REAL, PlcDirection.READ),
    "rotation_speed_offset": (PlcValueType.INT, PlcDirection.READ),
    "res_testing_signal_offset": (PlcValueType.BOOL, PlcDirection.WRITE),
    "res_finish_signal_offset": (PlcValueType.BOOL, PlcDirection.WRITE),
    "res_error_code_offset": (PlcValueType.INT, PlcDirection.WRITE),
    "res_result_offset": (PlcValueType.BOOL, PlcDirection.WRITE),
    "res_score_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_spl_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_loudness_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_vibrate_data_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_ai_score_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_spl_limit_up_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_spl_limit_down_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_loudness_limit_up_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_loudness_limit_down_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_vibrate_limit_up_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_vibrate_limit_down_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "res_ai_score_limit_down_offset": (PlcValueType.REAL, PlcDirection.WRITE),
    "sn_code_offset": (PlcValueType.STRING, PlcDirection.READ),
    "product_model_offset": (PlcValueType.STRING, PlcDirection.READ),
    "res_sn_code_offset": (PlcValueType.STRING, PlcDirection.WRITE),
    "res_product_model_offset": (PlcValueType.STRING, PlcDirection.WRITE),
}


def test_start_is_the_only_required_plc_field():
    required = [spec.key for spec in PLC_FIELD_SPECS if spec.required]

    assert required == ["start_signal_offset"]


def test_authoritative_field_types_directions_and_unique_keys():
    assert {
        spec.key: (spec.value_type, spec.direction)
        for spec in PLC_FIELD_SPECS
    } == EXPECTED_FIELDS
    assert len(PLC_FIELD_BY_KEY) == len(PLC_FIELD_SPECS)
    assert PLC_FIELD_BY_KEY["res_finish_signal_offset"].result_role == "finish"


def test_exact_workorder_field_mapping():
    assert tuple(
        (spec.key, spec.workorder_key)
        for spec in PLC_WORKORDER_FIELDS
    ) == (
        ("rotation_speed_offset", "rotation_speed"),
        ("sn_code_offset", "sn"),
        ("product_model_offset", "model"),
    )


def test_exact_ordered_result_payload_mapping():
    assert tuple(
        (spec.key, spec.result_payload_key, spec.result_order)
        for spec in PLC_RESULT_FIELDS
    ) == (
        ("res_error_code_offset", "error_code", 10),
        ("res_sn_code_offset", "sn_code", 20),
        ("res_result_offset", "test_result", 30),
        ("res_score_offset", "score", 40),
        ("res_spl_offset", "spl", 50),
        ("res_loudness_offset", "loudness", 60),
        ("res_vibrate_data_offset", "vibrate", 70),
        ("res_ai_score_offset", "ai_score", 80),
    )


def test_exact_configuration_return_group():
    assert tuple(spec.key for spec in PLC_CONFIG_RETURN_FIELDS) == (
        "res_spl_limit_up_offset",
        "res_spl_limit_down_offset",
        "res_loudness_limit_up_offset",
        "res_loudness_limit_down_offset",
        "res_vibrate_limit_up_offset",
        "res_vibrate_limit_down_offset",
        "res_ai_score_limit_down_offset",
        "res_product_model_offset",
    )


@pytest.mark.parametrize(
    ("text", "value_type", "expected"),
    [
        ("8.1", PlcValueType.BOOL, (8, 1)),
        ("62.0", PlcValueType.STRING, (62, 0)),
        ("10.0", PlcValueType.INT, (10, 0)),
        ("2.0", PlcValueType.REAL, (2, 0)),
    ],
)
def test_parse_plc_offset_accepts_type_appropriate_offsets(
    text,
    value_type,
    expected,
):
    assert parse_plc_offset(text, value_type) == expected


@pytest.mark.parametrize(
    ("text", "value_type"),
    [
        ("8.8", PlcValueType.BOOL),
        ("10.1", PlcValueType.INT),
        ("10.1", PlcValueType.REAL),
        ("10.1", PlcValueType.STRING),
        ("-1.0", PlcValueType.REAL),
        ("10", PlcValueType.INT),
        ("1.0.0", PlcValueType.BOOL),
    ],
)
def test_parse_plc_offset_rejects_invalid_or_misaligned_offsets(
    text,
    value_type,
):
    with pytest.raises(ValueError):
        parse_plc_offset(text, value_type)
