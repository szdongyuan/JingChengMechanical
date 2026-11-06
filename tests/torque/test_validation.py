"""Shared torque contracts and pure input validation."""
from dataclasses import FrozenInstanceError, replace
import math
import struct

import pytest

from app.service.torque.types import (
    RuleSnapshot, TorqueConfig, TorqueConfigApplied, TorqueCycle, TorqueStatus,
)
from app.service.torque.validation import (
    normalize_key, parse_offset, resolve_addresses, validate_db_number,
    validate_match_type, validate_torque,
)


@pytest.mark.parametrize("value, expected", [
    (" 000123 ", "000123"), (" AbC x ", "AbC x"), ("\tM100\n", "M100"),
])
def test_match_key_preserves_exact_text(value, expected):
    assert normalize_key(value) == expected


@pytest.mark.parametrize("value", [None, 123, True, "", " \t\n"])
def test_match_key_requires_nonempty_text(value):
    with pytest.raises(ValueError):
        normalize_key(value)


@pytest.mark.parametrize("value", ["model", "sn"])
def test_match_type_accepts_internal_types(value):
    assert validate_match_type(value) == value


@pytest.mark.parametrize("value", [None, True, [], "", "SN", "Model", "型号", " model "])
def test_match_type_rejects_other_values(value):
    with pytest.raises(ValueError):
        validate_match_type(value)


@pytest.mark.parametrize("value", [2.5, -2.5, 0, -0.0, " 2.5 ", "-2.5", "0", "-0", 1e-40])
def test_real_returns_encodable_float32(value):
    expected = struct.unpack(">f", struct.pack(">f", float(value)))[0]
    actual = validate_torque(value)
    assert actual == expected
    assert math.copysign(1, actual) == math.copysign(1, expected)


def test_real_rounds_to_the_value_actually_written_to_plc():
    assert validate_torque("0.1") == 0.10000000149011612


@pytest.mark.parametrize("value", [
    True, False, None, [], "", "abc", float("nan"), float("inf"),
    -float("inf"), "NaN", "Infinity", "-Infinity", 1e39, -1e39,
    "1e1000", "-1e1000", 1e-100, -1e-100, "1e-1000", "-1e-1000",
])
def test_real_rejects_invalid_overflow_and_nonzero_underflow(value):
    with pytest.raises(ValueError):
        validate_torque(value)


@pytest.mark.parametrize("value, expected", [
    (0, 0), (20, 20), ("20", 20), ("20.0", 20), (" 020.0 ", 20), (20.0, 20),
])
def test_offset_accepts_whole_byte_addresses(value, expected):
    assert parse_offset(value) == expected


@pytest.mark.parametrize("value", [
    True, False, None, -1, "-1", "-1.0", 1.1, "20.1", "1e2", "", "abc", "20.00",
])
def test_offset_rejects_invalid_or_bit_addresses(value):
    with pytest.raises(ValueError):
        parse_offset(value)


@pytest.mark.parametrize("value, expected", [(None, None), (1, 1), (" 12 ", 12)])
def test_db_number_accepts_positive_integer_or_inheritance(value, expected):
    assert validate_db_number(value) == expected


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, 1.0, "1.0", "", "0", "-1"])
def test_db_number_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_db_number(value)


def test_addresses_are_validated_after_db_inheritance():
    config = TorqueConfig(7, True, "model", None, 20, 1, 22)
    with pytest.raises(ValueError, match="重叠"):
        resolve_addresses(config, inherited_db=1)
    assert resolve_addresses(config, inherited_db=2) == ((2, 20), (1, 22))


@pytest.mark.parametrize("write_offset", [17, 18, 19, 20, 21, 22, 23])
def test_real_addresses_overlap_in_either_direction(write_offset):
    config = TorqueConfig(7, True, "model", 1, 20, 1, write_offset)
    with pytest.raises(ValueError, match="重叠"):
        resolve_addresses(config, inherited_db=2)


@pytest.mark.parametrize("write_offset", [16, 24])
def test_adjacent_real_addresses_do_not_overlap(write_offset):
    config = TorqueConfig(7, True, "sn", None, 20, None, write_offset)
    assert resolve_addresses(config, inherited_db=2) == ((2, 20), (2, write_offset))


@pytest.mark.parametrize("read_offset, write_offset", [(None, None), (20, None), (None, 24)])
def test_enabled_config_requires_both_offsets(read_offset, write_offset):
    config = TorqueConfig(7, True, read_byte_offset=read_offset, write_byte_offset=write_offset)
    with pytest.raises(ValueError):
        resolve_addresses(config, inherited_db=1)


def test_disabled_config_allows_incomplete_addresses():
    assert resolve_addresses(TorqueConfig(7), inherited_db=1) == ((1, None), (1, None))


@pytest.mark.parametrize("changes", [
    {"read_byte_offset": "1.1"}, {"write_byte_offset": -1},
    {"read_db_number": 0}, {"write_db_number": False}, {"match_type": "other"},
])
def test_disabled_config_still_validates_supplied_fields(changes):
    with pytest.raises(ValueError):
        resolve_addresses(replace(TorqueConfig(7), **changes), inherited_db=1)


@pytest.mark.parametrize("inherited_db", [None, True, 0, -1, "bad"])
def test_inherited_db_must_resolve_to_positive_number(inherited_db):
    config = TorqueConfig(7, True, read_byte_offset=20, write_byte_offset=24)
    with pytest.raises(ValueError):
        resolve_addresses(config, inherited_db=inherited_db)


def test_rule_snapshot_copies_values_and_prevents_mutation():
    values = {("model", "M100"): 2.5}
    snapshot = RuleSnapshot(3, values)
    values[("model", "M100")] = 9.0
    assert snapshot.values[("model", "M100")] == 2.5
    with pytest.raises(TypeError):
        snapshot.values[("sn", "0001")] = 3.0
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 4


def test_cycle_holds_immutable_config_and_rules_for_one_start():
    config = TorqueConfig(7)
    rules = RuleSnapshot(3, {})
    cycle = TorqueCycle(7, 100, "0001", "M100", config, rules, 1)
    assert cycle.config.enabled is False
    assert cycle.config.match_type == "model"
    assert cycle.config.revision == 0
    with pytest.raises(FrozenInstanceError):
        cycle.trigger_ns = 200
    with pytest.raises(FrozenInstanceError):
        cycle.config.enabled = True


def test_test_status_and_config_receipt_have_separate_identities():
    status = TorqueStatus(7, 100, "reading", None, None, "model", "M100", 3)
    receipt = TorqueConfigApplied(7, "generation-1", 2, True)
    assert (status.read_value, status.target_db, status.target_byte_offset) == (None, None, None)
    assert receipt.service_generation == "generation-1"
    assert receipt.config_revision == 2
    assert receipt.success is True
    with pytest.raises(FrozenInstanceError):
        status.state = "plc"
    with pytest.raises(FrozenInstanceError):
        receipt.success = False
