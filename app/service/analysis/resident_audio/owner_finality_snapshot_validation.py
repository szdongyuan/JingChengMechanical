"""Preflight-first validation for owner-finality diagnostic snapshots."""

from __future__ import annotations

from .owner_finality_types import (
    MAX_OWNER_FINALITY_LIMIT,
    OwnerFinalitySnapshot,
    OwnerFinalityState,
)
from .owner_finality_validation import (
    require_bounded_integer,
    require_enum_member,
    require_exact,
    require_field,
    require_text,
    validate_owner_finality_entry_snapshot,
    validate_stream_instance_id,
)


def _require_count(value: object, owner: str, field: str) -> None:
    require_bounded_integer(value, owner, field, 0)


def require_owner_finality_state(value: object) -> None:
    require_enum_member(value, OwnerFinalityState, "snapshot state")


def validate_owner_finality_snapshot(value: object) -> None:
    """Validate bounds before nested traversal, hashing, or aggregation."""

    owner = OwnerFinalitySnapshot.__name__
    names = (
        "stream_instance_id",
        "hardware_id",
        "entries",
        "state_counts",
        "ledger_limit",
        "pending_late_commands",
        "final_unacknowledged_entries",
        "acknowledged_awaiting_prune",
        "pruned_entry_count",
        "backpressure_rejection_count",
    )
    require_exact(value, OwnerFinalitySnapshot, owner)
    captured = {
        name: require_field(value, name, owner) for name in names
    }

    limit = captured["ledger_limit"]
    if type(limit) is not int:
        raise TypeError("snapshot ledger_limit must be an integer")
    if not 1 <= limit <= MAX_OWNER_FINALITY_LIMIT:
        raise ValueError("snapshot ledger_limit must be in 1..64")

    entries = captured["entries"]
    if type(entries) is not tuple:
        raise TypeError("snapshot entries must be an exact tuple")
    if len(entries) > limit:
        raise ValueError("snapshot entry count exceeds ledger limit")

    counts = captured["state_counts"]
    if type(counts) is not tuple:
        raise TypeError("snapshot state_counts must be an exact tuple")
    if len(counts) != len(OwnerFinalityState):
        raise ValueError("snapshot state_counts cardinality is invalid")

    stream = captured["stream_instance_id"]
    validate_stream_instance_id(stream)
    hardware = captured["hardware_id"]
    require_text(hardware, owner, "hardware_id")
    for field in names[5:]:
        _require_count(captured[field], owner, field)

    for entry in entries:
        validate_owner_finality_entry_snapshot(entry)

    validated_counts: list[tuple[OwnerFinalityState, int]] = []
    for item in counts:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("snapshot state count must be an exact pair")
        state, count = item
        require_owner_finality_state(state)
        _require_count(count, owner, "state count")
        validated_counts.append((state, count))

    if hardware != stream.hardware_id:
        raise ValueError("snapshot hardware does not match stream")
    for entry in entries:
        if (
            entry.pool_epoch != stream.pool_epoch
            or entry.hardware_id != stream.hardware_id
            or entry.stream_generation != stream.stream_generation
        ):
            raise ValueError("snapshot entry does not match stream")

    for state in OwnerFinalityState:
        matching = tuple(
            count
            for candidate, count in validated_counts
            if candidate is state
        )
        if len(matching) != 1:
            raise ValueError("snapshot states must be exact and unique")
        expected = sum(entry.state is state for entry in entries)
        if matching[0] != expected:
            raise ValueError("snapshot state counts do not match entries")

    pending = sum(entry.prepare_pending for entry in entries)
    final = sum(
        entry.state is OwnerFinalityState.FINAL_REMOVED
        or entry.state is OwnerFinalityState.FINAL_ABSENT
        for entry in entries
    )
    acknowledged = sum(
        entry.state is OwnerFinalityState.ACKNOWLEDGED for entry in entries
    )
    if captured["pending_late_commands"] != pending:
        raise ValueError("snapshot pending count does not match entries")
    if captured["final_unacknowledged_entries"] != final:
        raise ValueError("snapshot final count does not match entries")
    if captured["acknowledged_awaiting_prune"] != acknowledged:
        raise ValueError("snapshot acknowledged count does not match entries")
