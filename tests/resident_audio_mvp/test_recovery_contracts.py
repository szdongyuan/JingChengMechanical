from __future__ import annotations

import dataclasses
import threading

import pytest

from app.service.analysis.resident_audio_mvp import (
    MAX_BOUNDED_TEXT_CHARS,
    DiagnosticReason,
    DiagnosticStage,
    LifecycleOutcome,
    LifecycleStatus,
    ReadinessTicket,
    ResidentDiagnostic,
    ResidentErrorCode,
    ResidentRequestError,
)
from app.service.analysis.resident_audio_mvp.contracts import (
    _issue_readiness_ticket,
    _new_lifecycle_future,
    _validate_readiness_ticket,
)


def diagnostic(
    stage: DiagnosticStage,
    reason: DiagnosticReason,
    detail: str = "bounded recovery evidence",
) -> ResidentDiagnostic:
    return ResidentDiagnostic(stage=stage, reason_code=reason, detail=detail)


def test_lifecycle_status_values_are_stable_and_complete() -> None:
    assert [status.value for status in LifecycleStatus] == [
        "GENERATION_CONTINUOUS",
        "GENERATION_REPLACED",
        "RECOVERY_PENDING",
        "RECOVERY_FAILED",
        "LIFECYCLE_CANCELLED",
    ]


@pytest.mark.parametrize(
    ("stage", "reason"),
    [
        (DiagnosticStage.LIVENESS, DiagnosticReason.HEARTBEAT_STALE),
        (DiagnosticStage.LIVENESS, DiagnosticReason.INGRESS_OVERFLOW),
        (
            DiagnosticStage.LIVENESS,
            DiagnosticReason.PORTAUDIO_INPUT_OVERFLOW,
        ),
        (
            DiagnosticStage.LIVENESS,
            DiagnosticReason.CALLBACK_CONTINUITY_LOST,
        ),
        (DiagnosticStage.LIVENESS, DiagnosticReason.BACKEND_FAULT),
        (
            DiagnosticStage.RESOLVE_ENDPOINT,
            DiagnosticReason.ENDPOINT_SCAN_FAILED,
        ),
        (DiagnosticStage.RESOLVE_ENDPOINT, DiagnosticReason.ENDPOINT_MISSING),
        (
            DiagnosticStage.RESOLVE_ENDPOINT,
            DiagnosticReason.ENDPOINT_AMBIGUOUS,
        ),
        (
            DiagnosticStage.RESOLVE_ENDPOINT,
            DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
        ),
        (DiagnosticStage.OPEN_CANDIDATE, DiagnosticReason.OPEN_FAILED),
        (DiagnosticStage.OPEN_CANDIDATE, DiagnosticReason.OPEN_TIMEOUT),
        (DiagnosticStage.START_CANDIDATE, DiagnosticReason.START_FAILED),
        (DiagnosticStage.START_CANDIDATE, DiagnosticReason.START_TIMEOUT),
        (
            DiagnosticStage.AWAIT_FIRST_SAMPLE,
            DiagnosticReason.FIRST_SAMPLE_TIMEOUT,
        ),
        (
            DiagnosticStage.PUBLISH_CANDIDATE,
            DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER,
        ),
        (
            DiagnosticStage.BACKGROUND_RETRY,
            DiagnosticReason.ORPHAN_LIMIT_REACHED,
        ),
        (DiagnosticStage.INTERNAL, DiagnosticReason.INVARIANT_VIOLATION),
        (DiagnosticStage.LIFECYCLE, DiagnosticReason.CLOSING),
    ],
)
def test_diagnostics_cover_only_concrete_recovery_reasons(
    stage: DiagnosticStage,
    reason: DiagnosticReason,
) -> None:
    value = diagnostic(stage, reason)

    assert value.stage is stage
    assert value.reason_code is reason


def test_diagnostic_is_immutable_and_rejects_unbounded_or_mismatched_values(
) -> None:
    value = diagnostic(
        DiagnosticStage.LIVENESS,
        DiagnosticReason.HEARTBEAT_STALE,
        "x" * MAX_BOUNDED_TEXT_CHARS,
    )
    assert len(value.detail) == MAX_BOUNDED_TEXT_CHARS
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.detail = "changed"  # type: ignore[misc]
    with pytest.raises(ResidentRequestError) as unbounded:
        diagnostic(
            DiagnosticStage.LIVENESS,
            DiagnosticReason.HEARTBEAT_STALE,
            "x" * (MAX_BOUNDED_TEXT_CHARS + 1),
        )
    assert unbounded.value.error.code is ResidentErrorCode.INVALID
    with pytest.raises(ResidentRequestError):
        ResidentDiagnostic(  # type: ignore[arg-type]
            stage="liveness",
            reason_code=DiagnosticReason.HEARTBEAT_STALE,
            detail="invalid stage type",
        )
    with pytest.raises(ResidentRequestError):
        diagnostic(
            DiagnosticStage.OPEN_CANDIDATE,
            DiagnosticReason.HEARTBEAT_STALE,
        )


def test_lifecycle_outcome_requires_generation_or_diagnostic_by_status() -> None:
    continuous = LifecycleOutcome(
        status=LifecycleStatus.GENERATION_CONTINUOUS,
        generation_id="generation-1",
    )
    replaced = LifecycleOutcome(
        status=LifecycleStatus.GENERATION_REPLACED,
        generation_id="generation-2",
        diagnostic=diagnostic(
            DiagnosticStage.PUBLISH_CANDIDATE,
            DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER,
        ),
    )
    pending = LifecycleOutcome(
        status=LifecycleStatus.RECOVERY_PENDING,
        diagnostic=diagnostic(
            DiagnosticStage.AWAIT_FIRST_SAMPLE,
            DiagnosticReason.FIRST_SAMPLE_TIMEOUT,
        ),
    )
    failed = LifecycleOutcome(
        status=LifecycleStatus.RECOVERY_FAILED,
        diagnostic=diagnostic(
            DiagnosticStage.START_CANDIDATE,
            DiagnosticReason.START_FAILED,
        ),
    )
    cancelled = LifecycleOutcome(
        status=LifecycleStatus.LIFECYCLE_CANCELLED,
        diagnostic=diagnostic(
            DiagnosticStage.LIFECYCLE,
            DiagnosticReason.CLOSING,
        ),
    )

    assert continuous.diagnostic is None
    assert replaced.generation_id == "generation-2"
    assert pending.generation_id is None
    assert failed.generation_id is None
    assert cancelled.generation_id is None
    with pytest.raises(ResidentRequestError):
        LifecycleOutcome(status=LifecycleStatus.GENERATION_CONTINUOUS)
    with pytest.raises(ResidentRequestError):
        LifecycleOutcome(status=LifecycleStatus.RECOVERY_FAILED)


def test_lifecycle_future_settles_exactly_once_under_concurrent_publishers(
) -> None:
    future, publisher = _new_lifecycle_future()
    first = LifecycleOutcome(
        status=LifecycleStatus.GENERATION_CONTINUOUS,
        generation_id="generation-1",
    )
    second = LifecycleOutcome(
        status=LifecycleStatus.RECOVERY_FAILED,
        diagnostic=diagnostic(
            DiagnosticStage.OPEN_CANDIDATE,
            DiagnosticReason.OPEN_FAILED,
        ),
    )
    barrier = threading.Barrier(3)
    results: list[tuple[bool, LifecycleOutcome]] = []

    def settle(outcome: LifecycleOutcome) -> None:
        barrier.wait()
        results.append((publisher.settle(outcome), outcome))

    threads = [
        threading.Thread(target=settle, args=(outcome,))
        for outcome in (first, second)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()

    winners = [outcome for won, outcome in results if won]
    assert len(winners) == 1
    assert future.wait(0.1) is winners[0]
    assert future.result_if_ready() is winners[0]
    assert publisher.settle(first) is False


def test_readiness_ticket_is_immutable_and_authenticated_to_its_owner() -> None:
    future, _publisher = _new_lifecycle_future()
    owner = object()
    ticket = _issue_readiness_ticket(
        ticket_id="readiness-1",
        hardware_id="mic-1",
        future=future,
        owner_nonce=owner,
    )

    assert ticket.future is future
    assert _validate_readiness_ticket(ticket, owner_nonce=owner) is ticket
    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.hardware_id = "mic-2"  # type: ignore[misc]
    with pytest.raises(ResidentRequestError):
        _validate_readiness_ticket(ticket, owner_nonce=object())
    with pytest.raises(TypeError):
        ReadinessTicket()
