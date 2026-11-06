from __future__ import annotations

import pytest
from peewee import IntegrityError

from app.service.managed_input_contracts import (
    MAX_CALIBRATABLE_INPUT_CHANNELS,
    MAX_MANAGED_AUDIO_INPUTS,
    parse_calibration_json,
    serialize_calibration_json,
)
from model.managed_audio_input_table import ManagedAudioInputTable


@pytest.fixture
def managed_input_table(setup_test_db):
    setup_test_db.bind([ManagedAudioInputTable])
    setup_test_db.create_tables([ManagedAudioInputTable])
    try:
        yield setup_test_db
    finally:
        setup_test_db.drop_tables([ManagedAudioInputTable])


def calibration_json(channels: int = 2) -> str:
    return serialize_calibration_json(
        tuple((index, False, 0, None) for index in range(channels)),
        expected_channels=channels,
    )


def make_row(alias: str, uid: str | None = None) -> ManagedAudioInputTable:
    return ManagedAudioInputTable.create(
        alias=alias,
        stable_backend_uid=uid,
        descriptor_device_name="Device",
        descriptor_hostapi_name="Host API",
        descriptor_sample_rate_hz=48_000.0,
        descriptor_input_channels=2,
        calibration_json=calibration_json(),
    )


def test_exact_table_and_column_names(managed_input_table):
    assert ManagedAudioInputTable._meta.table_name == "managed_audio_input_table"
    assert tuple(ManagedAudioInputTable._meta.fields) == (
        "hardware_id",
        "alias",
        "hardware_revision",
        "stable_backend_uid",
        "descriptor_device_name",
        "descriptor_hostapi_name",
        "descriptor_sample_rate_hz",
        "descriptor_input_channels",
        "calibration_revision",
        "calibration_json",
        "created_time",
        "updated_time",
    )


def test_database_issues_positive_hardware_ids_and_defaults(managed_input_table):
    row = make_row("one")
    assert row.hardware_id == 1
    assert row.hardware_revision == 1
    assert row.calibration_revision == 0
    assert isinstance(row.created_time, str) and row.created_time
    assert isinstance(row.updated_time, str) and row.updated_time


def test_alias_and_non_null_uid_are_exactly_unique(managed_input_table):
    make_row("one", "uid-1")
    with pytest.raises(IntegrityError):
        make_row("one", "uid-2")
    with pytest.raises(IntegrityError):
        make_row("two", "uid-1")


def test_multiple_null_uids_and_equal_descriptors_are_permitted(
    managed_input_table,
):
    first = make_row("one")
    second = make_row("two")
    assert first.stable_backend_uid is None
    assert second.stable_backend_uid is None
    assert ManagedAudioInputTable.select().count() == 2


def test_durable_channel_count_is_bounded_by_calibration_capacity(
    managed_input_table,
):
    supported = ManagedAudioInputTable.create(
        alias="supported",
        stable_backend_uid=None,
        descriptor_device_name="Device",
        descriptor_hostapi_name="Host API",
        descriptor_sample_rate_hz=48_000.0,
        descriptor_input_channels=MAX_CALIBRATABLE_INPUT_CHANNELS,
        calibration_json=calibration_json(
            MAX_CALIBRATABLE_INPUT_CHANNELS
        ),
    )
    assert (
        supported.descriptor_input_channels
        == MAX_CALIBRATABLE_INPUT_CHANNELS
    )

    for unsupported in (
        MAX_CALIBRATABLE_INPUT_CHANNELS + 1,
        2**31 - 1,
    ):
        with pytest.raises(IntegrityError):
            ManagedAudioInputTable.create(
                alias=f"unsupported-{unsupported}",
                stable_backend_uid=None,
                descriptor_device_name="Device",
                descriptor_hostapi_name="Host API",
                descriptor_sample_rate_hz=48_000.0,
                descriptor_input_channels=unsupported,
                calibration_json="{}",
            )


def test_calibration_json_round_trips_through_canonical_helper(
    managed_input_table,
):
    row = make_row("one")
    assert parse_calibration_json(
        row.calibration_json,
        expected_channels=row.descriptor_input_channels,
    ) == ((0, False, 0, None), (1, False, 0, None))


def test_sqlite_schema_uses_non_reusing_autoincrement_primary_key(
    managed_input_table,
):
    sql = managed_input_table.execute_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (ManagedAudioInputTable._meta.table_name,),
    ).fetchone()[0]
    assert "INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT" in sql


def test_six_rows_and_atomic_seventh_capacity_check_leaves_six(
    managed_input_table,
):
    for index in range(MAX_MANAGED_AUDIO_INPUTS):
        make_row(f"alias-{index}")

    rejected = False
    with ManagedAudioInputTable._meta.database.atomic():
        if (
            ManagedAudioInputTable.select().count()
            >= MAX_MANAGED_AUDIO_INPUTS
        ):
            rejected = True
        else:
            make_row("seventh")

    assert rejected is True
    assert ManagedAudioInputTable.select().count() == 6


def test_deleted_highest_hardware_id_is_never_reused(managed_input_table):
    first = make_row("first")
    deleted = make_row("deleted")
    deleted.delete_instance()
    replacement = make_row("replacement")
    assert replacement.hardware_id > deleted.hardware_id > first.hardware_id
