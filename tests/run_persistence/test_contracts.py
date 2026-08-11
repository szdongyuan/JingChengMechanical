from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
from uuid import UUID

import pytest

import app.service.run_persistence.contracts as contracts_module
from app.service.run_persistence.analysis_envelope import AnalysisEnvelopeError
from app.service.run_persistence.contracts import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    ContractValidationError,
    CurrentTrace,
    FrozenRunInputs,
    IngressKind,
    Judgment,
    JudgmentSource,
    RunInsert,
    RunState,
    TransactionResult,
    TransactionState,
    TriggerSource,
    VerifiedAudioFact,
    _issue_verified_audio_fact,
    freeze_run_inputs,
    new_uuid4,
    validate_snapshot_object,
    validate_utc_timestamp,
    validate_uuid4,
)
from tests.run_persistence.conftest import make_analysis_envelope


def _frozen_inputs(
    *,
    config: dict[str, object] | None = None,
    hardware: dict[str, object] | None = None,
    calibration: dict[str, object] | None = None,
) -> FrozenRunInputs:
    return freeze_run_inputs(
        make_analysis_envelope(
            config={"config_id": 1} if config is None else config
        ),
        hardware,
        calibration,
    )


def test_new_uuid4_is_lowercase_canonical_text() -> None:
    value = new_uuid4()

    assert value == str(UUID(value))
    assert UUID(value).version == 4
    assert value == value.lower()
    assert len(value) == 36


def test_current_trace_is_an_exact_frozen_typed_nonempty_value() -> None:
    trace = CurrentTrace(samples=((0.0, 1.25), (0.02, 1.5)))

    assert trace.samples == ((0.0, 1.25), (0.02, 1.5))
    with pytest.raises(FrozenInstanceError):
        trace.samples = ((0.0, 2.0),)


@pytest.mark.parametrize(
    "samples",
    [
        (),
        [],
        ((0.0,),),
        ([0.0, 1.0],),
        ((True, 1.0),),
        ((0.0, False),),
        ((math.nan, 1.0),),
        ((0.0, math.inf),),
        ((-0.01, 1.0),),
        ((0.0, 1.0), (0.0, 2.0)),
        ((0.02, 1.0), (0.01, 2.0)),
    ],
)
def test_current_trace_rejects_empty_malformed_nonfinite_or_unordered_samples(
    samples,
) -> None:
    with pytest.raises(ContractValidationError, match="current trace"):
        CurrentTrace(samples=samples)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "run/config/1",
        "550E8400-E29B-41D4-A716-446655440000",
        "550e8400e29b41d4a716446655440000",
        "550e8400-e29b-11d4-a716-446655440000",
    ],
)
def test_validate_uuid4_rejects_invalid_non_v4_uppercase_and_path_like_ids(
    value: str,
) -> None:
    with pytest.raises(ContractValidationError, match="UUIDv4"):
        validate_uuid4(value)


def test_validate_uuid4_rejects_before_any_database_access() -> None:
    with pytest.raises(ContractValidationError, match="UUIDv4"):
        RunInsert(
            run_id="run/config/1",
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=1,
            hardware_id=2,
            snapshots=_frozen_inputs(),
            accepted_at="2026-07-27T12:34:56.123456Z",
        )


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("ingress_kind", "CAPTURED"),
        ("ingress_kind", "INVALID"),
        ("trigger_source", "MANUAL"),
        ("trigger_source", "INVALID"),
    ],
)
def test_run_insert_requires_declared_enum_instances(
    now: str, field: str, raw_value: str
) -> None:
    values = {
        "run_id": "550e8400-e29b-41d4-a716-446655440000",
        "ingress_kind": IngressKind.CAPTURED,
        "trigger_source": TriggerSource.MANUAL,
        "config_id": 1,
        "hardware_id": 2,
        "snapshots": _frozen_inputs(hardware={"hardware": 2}),
        "accepted_at": now,
    }
    values[field] = raw_value

    with pytest.raises(ContractValidationError, match=field):
        RunInsert(**values)


@pytest.mark.parametrize(
    ("ingress_kind", "trigger_source"),
    [
        (IngressKind.CAPTURED, TriggerSource.IMPORT),
        (IngressKind.IMPORTED, TriggerSource.MANUAL),
    ],
)
def test_run_insert_rejects_invalid_ingress_trigger_pairing(
    now: str,
    ingress_kind: IngressKind,
    trigger_source: TriggerSource,
) -> None:
    with pytest.raises(ContractValidationError, match="trigger"):
        RunInsert(
            run_id="550e8400-e29b-41d4-a716-446655440000",
            ingress_kind=ingress_kind,
            trigger_source=trigger_source,
            config_id=1,
            hardware_id=2,
            snapshots=_frozen_inputs(hardware={"hardware": 2}),
            accepted_at=now,
        )


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("config_id", True),
        ("config_id", 1.0),
        ("config_id", "1"),
        ("config_id", None),
        ("hardware_id", True),
        ("hardware_id", 2.0),
        ("hardware_id", "2"),
    ],
)
def test_run_insert_requires_non_bool_integer_ids(
    now: str, field: str, raw_value: object
) -> None:
    values = {
        "run_id": "550e8400-e29b-41d4-a716-446655440000",
        "ingress_kind": IngressKind.CAPTURED,
        "trigger_source": TriggerSource.MANUAL,
        "config_id": 1,
        "hardware_id": 2,
        "snapshots": _frozen_inputs(hardware={"hardware": 2}),
        "accepted_at": now,
    }
    values[field] = raw_value

    with pytest.raises(ContractValidationError, match=field):
        RunInsert(**values)


@pytest.mark.parametrize("raw_value", [{}, "{}", None])
def test_run_insert_requires_frozen_run_inputs(
    now: str, raw_value: object
) -> None:
    with pytest.raises(ContractValidationError, match="snapshots"):
        RunInsert(
            run_id="550e8400-e29b-41d4-a716-446655440000",
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=1,
            hardware_id=2,
            snapshots=raw_value,
            accepted_at=now,
        )


@pytest.mark.parametrize("raw_value", ["INITIAL", "INVALID"])
def test_attempt_insert_requires_declared_attempt_kind_enum(
    now: str, raw_value: str
) -> None:
    with pytest.raises(ContractValidationError, match="attempt_kind"):
        AttemptInsert(
            attempt_id="6ba7b815-9dad-41d1-80b4-00c04fd430c8",
            run_id="550e8400-e29b-41d4-a716-446655440000",
            audio_id="6ba7b814-9dad-41d1-80b4-00c04fd430c8",
            attempt_kind=raw_value,
            queued_at=now,
        )


@pytest.mark.parametrize(
    "raw_value",
    ["QUEUED", AttemptState.RUNNING, RunState.ACTIVE],
)
def test_attempt_insert_requires_exact_queued_attempt_state(
    now: str, raw_value: object
) -> None:
    with pytest.raises(ContractValidationError, match="state"):
        AttemptInsert(
            attempt_id="6ba7b815-9dad-41d1-80b4-00c04fd430c8",
            run_id="550e8400-e29b-41d4-a716-446655440000",
            audio_id="6ba7b814-9dad-41d1-80b4-00c04fd430c8",
            attempt_kind=AttemptKind.INITIAL,
            queued_at=now,
            state=raw_value,
        )


def _verified_audio_fact(now: str, **overrides: object) -> VerifiedAudioFact:
    values = {
        "audio_id": "6ba7b814-9dad-41d1-80b4-00c04fd430c8",
        "save_job_id": "6ba7b816-9dad-41d1-80b4-00c04fd430c8",
        "run_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_kind": "CAPTURED_AUDIO",
        "source_identity": "capture:contract",
        "content_revision": 1,
        "final_path": "C:/managed/audio.wav",
        "container_format": "WAV_PCM",
        "sample_rate_hz": 48_000,
        "channel_count": 1,
        "frame_count": 48_000,
        "duration_ms": 1_000,
        "byte_size": 96_044,
        "closed_verified_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return _issue_verified_audio_fact(**values)


def test_verified_audio_fact_blocks_direct_public_construction(now: str) -> None:
    with pytest.raises(
        ContractValidationError, match="only be issued after audio publication"
    ):
        VerifiedAudioFact(
            audio_id="6ba7b814-9dad-41d1-80b4-00c04fd430c8",
            run_id="550e8400-e29b-41d4-a716-446655440000",
            final_path="C:/managed/audio.wav",
            container_format="WAV_PCM",
            sample_rate_hz=48_000,
            channel_count=1,
            frame_count=48_000,
            duration_ms=1_000,
            byte_size=96_044,
            closed_verified_at=now,
            created_at=now,
        )


@pytest.mark.parametrize("raw_value", ["", None, 1])
def test_verified_audio_fact_requires_non_empty_string_path(
    now: str, raw_value: object
) -> None:
    with pytest.raises(ContractValidationError, match="final_path"):
        _verified_audio_fact(now, final_path=raw_value)


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("sample_rate_hz", True),
        ("channel_count", 1.0),
        ("frame_count", "48000"),
        ("duration_ms", None),
        ("byte_size", 0),
    ],
)
def test_verified_audio_fact_requires_positive_non_bool_integer_measurements(
    now: str, field: str, raw_value: object
) -> None:
    with pytest.raises(ContractValidationError, match=field):
        _verified_audio_fact(now, **{field: raw_value})


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("state", "COMMITTED"),
        ("state", AttemptState.SUCCEEDED),
        ("reason", 1),
        ("reason", False),
        ("restart_required", 0),
        ("restart_required", "false"),
    ],
)
def test_transaction_result_requires_exact_enum_and_scalar_types(
    field: str, raw_value: object
) -> None:
    values = {
        "state": TransactionState.COMMITTED,
        "value": object(),
        "reason": None,
        "restart_required": False,
    }
    values[field] = raw_value

    with pytest.raises(ContractValidationError, match=field):
        TransactionResult(**values)


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-27T12:34:56Z",
        "2026-07-27T12:34:56.12345Z",
        "2026-07-27T12:34:56.123456+00:00",
        "2026-07-27 12:34:56.123456Z",
        "2026-02-30T12:34:56.123456Z",
        "2026-07-27T12:34:56.123456z",
    ],
)
def test_utc_timestamps_require_exact_six_microsecond_z_format(value: str) -> None:
    with pytest.raises(ContractValidationError, match="UTC timestamp"):
        validate_utc_timestamp(value)


def test_utc_timestamp_accepts_exact_required_format(now: str) -> None:
    assert validate_utc_timestamp(now) == now


@pytest.mark.parametrize(
    "value",
    [None, [], ["object"], "{}", 1, True],
)
def test_snapshot_validation_accepts_json_objects_only(value: object) -> None:
    with pytest.raises(ContractValidationError, match="JSON object"):
        validate_snapshot_object(value, "config_snapshot")


def test_snapshot_validation_rejects_non_json_serializable_object() -> None:
    with pytest.raises(ContractValidationError, match="JSON object"):
        validate_snapshot_object({"bad": object()}, "config_snapshot")


def test_frozen_snapshot_json_rejects_non_standard_json_constants() -> None:
    with pytest.raises(ContractValidationError, match="JSON object"):
        FrozenRunInputs(
            _frozen_inputs().config_snapshot_json,
            '{"value":NaN}',
            "{}",
        )


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("config_snapshot_json", b"{}"),
        ("config_snapshot_json", bytearray(b"{}")),
        ("hardware_snapshot_json", b"{}"),
        ("hardware_snapshot_json", bytearray(b"{}")),
        ("calibration_snapshot_json", b"{}"),
        ("calibration_snapshot_json", bytearray(b"{}")),
    ],
)
def test_frozen_snapshot_json_fields_require_exact_strings_before_decoding(
    monkeypatch, field: str, raw_value: object
) -> None:
    original_loads = contracts_module.json.loads

    def reject_non_string_decode(value, *args, **kwargs):
        if type(value) is not str:
            raise AssertionError("non-string reached json.loads")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(contracts_module.json, "loads", reject_non_string_decode)
    values = {
        "config_snapshot_json": _frozen_inputs().config_snapshot_json,
        "hardware_snapshot_json": "{}",
        "calibration_snapshot_json": "{}",
    }
    values[field] = raw_value

    expected_error = (
        AnalysisEnvelopeError
        if field == "config_snapshot_json"
        else ContractValidationError
    )
    with pytest.raises(expected_error):
        FrozenRunInputs(**values)


def test_missing_hardware_and_calibration_use_explicit_none_objects() -> None:
    frozen = freeze_run_inputs(
        config_snapshot=make_analysis_envelope(config={"config_id": 7})
    )

    assert json.loads(frozen.config_snapshot_json)["config"] == {"config_id": 7}
    assert json.loads(frozen.hardware_snapshot_json) == {"kind": "NONE"}
    assert json.loads(frozen.calibration_snapshot_json) == {"kind": "NONE"}
    assert frozen.hardware_snapshot_json is not None
    assert frozen.calibration_snapshot_json is not None


def test_freezing_deep_copies_inputs_and_stores_serialized_values_once() -> None:
    config = {"channels": [{"index": 1, "enabled": True}]}
    hardware = {"device": {"serial": "abc"}}
    calibration = {"points": [1, 2]}

    envelope = make_analysis_envelope(config=config)
    frozen = freeze_run_inputs(envelope, hardware, calibration)
    serialized_values = (
        frozen.config_snapshot_json,
        frozen.hardware_snapshot_json,
        frozen.calibration_snapshot_json,
    )
    config["channels"][0]["enabled"] = False
    hardware["device"]["serial"] = "changed"
    calibration["points"].append(3)

    assert serialized_values == (
        frozen.config_snapshot_json,
        frozen.hardware_snapshot_json,
        frozen.calibration_snapshot_json,
    )
    assert (
        json.loads(frozen.config_snapshot_json)["config"]["channels"][0]["enabled"]
        is True
    )
    assert json.loads(frozen.hardware_snapshot_json)["device"]["serial"] == "abc"
    assert json.loads(frozen.calibration_snapshot_json)["points"] == [1, 2]


def test_freezing_serializes_each_snapshot_exactly_once(monkeypatch) -> None:
    calls = 0
    original_dumps = contracts_module.json.dumps

    def counting_dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(contracts_module.json, "dumps", counting_dumps)

    freeze_run_inputs(
        make_analysis_envelope(config={"config": 1}),
        {"hardware": 2},
        {"calibration": 3},
    )

    assert calls == 3


def test_contract_enums_have_exact_spec_values() -> None:
    assert {item.value for item in IngressKind} == {"CAPTURED", "IMPORTED"}
    assert {item.value for item in TriggerSource} == {
        "PLC",
        "MANUAL",
        "SCANNER",
        "IMPORT",
    }
    assert {item.value for item in RunState} == {
        "ACTIVE",
        "SUCCEEDED",
        "NO_RESULT",
        "FAILED",
        "CANCELED",
        "INTERRUPTED",
    }
    assert {item.value for item in Judgment} == {"PASS", "FAIL"}
    assert {item.value for item in JudgmentSource} == {
        "NONE",
        "AUTOMATIC",
        "MANUAL",
    }
    assert {item.value for item in AttemptKind} == {"INITIAL", "REANALYSIS"}
    assert {item.value for item in AttemptState} == {
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "NO_RESULT",
        "FAILED",
        "CANCELED",
        "INTERRUPTED",
    }
    assert {item.value for item in TransactionState} == {
        "COMMITTED",
        "ROLLED_BACK",
        "UNKNOWN",
    }


def test_contract_dataclasses_are_immutable(now: str) -> None:
    run_id = "550e8400-e29b-41d4-a716-446655440000"
    audio_id = "6ba7b814-9dad-41d1-80b4-00c04fd430c8"
    attempt_id = "6ba7b815-9dad-41d1-80b4-00c04fd430c8"
    snapshots = _frozen_inputs(hardware={"hardware_id": 2})
    values = [
        snapshots,
        RunInsert(
            run_id=run_id,
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=1,
            hardware_id=2,
            snapshots=snapshots,
            accepted_at=now,
        ),
            _issue_verified_audio_fact(
                audio_id=audio_id,
                save_job_id="6ba7b816-9dad-41d1-80b4-00c04fd430c8",
                run_id=run_id,
                source_kind="CAPTURED_AUDIO",
                source_identity="capture:contract",
                content_revision=1,
            final_path="C:/managed/audio.wav",
            container_format="WAV_PCM",
            sample_rate_hz=48_000,
            channel_count=1,
            frame_count=48_000,
            duration_ms=1_000,
            byte_size=96_044,
            closed_verified_at=now,
            created_at=now,
        ),
        AttemptInsert(
            attempt_id=attempt_id,
            run_id=run_id,
            audio_id=audio_id,
            attempt_kind=AttemptKind.INITIAL,
            queued_at=now,
        ),
        TransactionResult(
            state=TransactionState.COMMITTED,
            value=1,
            reason=None,
            restart_required=False,
        ),
    ]

    for value in values:
        with pytest.raises(FrozenInstanceError):
            value.__setattr__(next(iter(value.__dataclass_fields__)), "changed")
