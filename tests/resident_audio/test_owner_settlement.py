from __future__ import annotations

import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

from app.service.analysis.resident_audio.owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerNonAdmissionReason,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
    StreamInstanceId,
)
from app.service.analysis.resident_audio.owner_settlement import (
    OwnerSettlementCoordinator,
)
from app.service.analysis.resident_audio.owner_settlement_types import (
    ArtifactSettlementResult,
    CompletionInvocationResult,
    SettlementClaim,
    SettlementIntent,
    SettlementPhase,
    SettlementProgress,
)
from app.service.analysis.resident_audio.pool_settlement import (
    PoolSettlementAdapter,
)


def owner_id(*, nonce: str = "owner-nonce", capability: str = "owner-secret"):
    return OwnerInstanceId(
        pool_epoch=7,
        hardware_id="hw-1",
        stream_generation=3,
        run_id="run-9",
        commit_attempt_id=11,
        owner_capability=capability,
        instance_nonce=nonce,
    )


def stream_id(*, nonce: str = "stream-nonce"):
    return StreamInstanceId(
        pool_epoch=7,
        hardware_id="hw-1",
        stream_generation=3,
        instance_nonce=nonce,
    )


class FakePorts:
    def __init__(self) -> None:
        self.guard = threading.RLock()
        self.pool_lock_depth = 0
        self.claims: dict[str, SettlementClaim] = {}
        self.events: list[str] = []
        self.calls: dict[str, int] = {}
        self.removal_outcome = OwnerRemovalOutcome.REMOVED
        self.non_admission_proof: OwnerNonAdmissionProof | None = None
        self.join_complete = True
        self.ready = True
        self.late_resolved = True
        self.ack_matches = True
        self.prune_outcome = FinalityPruneOutcome.PRUNED
        self.artifact_result = ArtifactSettlementResult(True, True)
        self.quota_confirmed = True
        self.capacity = 1
        self.lease_state = "COMMITTED"
        self.settled = False
        self.terminal_cas_count = 0
        self.raise_remove: BaseException | None = None
        self.objects = tuple(object() for _ in range(7))
        self.injected: dict[str, list[object]] = {}
        self.remove_entered: threading.Event | None = None
        self.remove_release: threading.Event | None = None
        self.clear_failures = 0
        self.reenter_operation: str | None = None
        self.reenter_engine = None
        self.reentry_attempted = False
        self.reentry_observations: list[object] = []

    def _called(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.events.append(name)

    def _external(self, name: str) -> None:
        assert self.pool_lock_depth == 0
        self._called(name)
        if name == self.reenter_operation and not self.reentry_attempted:
            self.reentry_attempted = True
            assert self.reenter_engine is not None
            try:
                result = self.reenter_engine.advance("lease-1")
            except RuntimeError as error:
                self.reentry_observations.append(
                    ("rejected", str(error))
                )
            else:
                self.reentry_observations.append(result)

    def _outcome(self, name: str, default):
        self._external(name)
        queue = self.injected.get(name, [])
        outcome = queue.pop(0) if queue else default
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def claim_or_resume(self, candidate: SettlementClaim):
        with self.guard:
            self.pool_lock_depth += 1
            try:
                existing = self.claims.get(candidate.lease_id)
                if existing is not None:
                    return existing, False
                self.claims[candidate.lease_id] = candidate
                return candidate, True
            finally:
                self.pool_lock_depth -= 1

    def validate_and_load(self, lease_id: str):
        with self.guard:
            self.pool_lock_depth += 1
            try:
                self._called("validate")
                return self.claims.get(lease_id)
            finally:
                self.pool_lock_depth -= 1

    def compare_and_set_progress(
        self,
        lease_id: str,
        expected_revision: int,
        progress: SettlementProgress,
    ):
        with self.guard:
            self.pool_lock_depth += 1
            try:
                current = self.claims.get(lease_id)
                if current is None or current.revision != expected_revision:
                    return None
                updated = dataclasses.replace(
                    current,
                    progress=progress,
                    revision=current.revision + 1,
                )
                self.claims[lease_id] = updated
                if (
                    progress.remove_retry_disabled
                    and not current.progress.remove_retry_disabled
                ):
                    self.events.append("persist remove-disable")
                if (
                    progress.finality_acknowledged
                    and not current.progress.finality_acknowledged
                ):
                    self.events.append("persist acknowledgement")
                if (
                    progress.finality_pruned
                    and not current.progress.finality_pruned
                ):
                    self.events.append("persist prune")
                return updated
            finally:
                self.pool_lock_depth -= 1

    def diagnostic_claims(self):
        with self.guard:
            return tuple(self.claims.values())

    def remove_owner(self, claim: SettlementClaim):
        if self.remove_entered is not None:
            self.remove_entered.set()
            assert self.remove_release is not None
            assert self.remove_release.wait(timeout=5)
        if self.raise_remove is not None:
            self._external("remove")
            failure, self.raise_remove = self.raise_remove, None
            raise failure
        return self._outcome("remove", OwnerRemovalReceipt(
            claim.owner_instance_id, self.removal_outcome
        ))

    def signal_owner(self, claim: SettlementClaim):
        return self._outcome("signal", True)

    def join_owner(self, claim: SettlementClaim):
        return self._outcome("join", self.join_complete)

    def check_artifact_readiness(self, claim: SettlementClaim):
        return self._outcome("freeze/readiness", self.ready)

    def late_owner_commands_resolved(self, claim: SettlementClaim):
        return self._outcome(
            "late-command resolution", self.late_resolved
        )

    def acknowledge_finality(
        self, claim: SettlementClaim, progress: SettlementProgress
    ):
        injected = self.injected.get("acknowledge", [])
        if injected:
            return self._outcome("acknowledge", None)
        self._external("acknowledge")
        if not self.ack_matches:
            return None
        return FinalityAckReceipt(
            stream_instance_id=claim.stream_instance_id,
            owner_instance_id=claim.owner_instance_id,
            ledger_version=4,
            ack_token="ack-secret",
        )

    def prune_finality(
        self, claim: SettlementClaim, receipt: FinalityAckReceipt
    ):
        return self._outcome("prune", FinalityPruneReceipt(
            claim.owner_instance_id, self.prune_outcome
        ))

    def settle_artifact_and_publication(
        self, claim: SettlementClaim, *, invoke_handler=False
    ):
        del invoke_handler
        return self._outcome(
            "artifact/publication", self.artifact_result
        )

    def invoke_completion_handler(self, claim: SettlementClaim):
        del claim
        return self._outcome(
            "handler invocation",
            CompletionInvocationResult(
                handler_returned=True,
                resolution=None,
                exception_type=None,
            ),
        )

    def confirm_quota_outcome(self, claim: SettlementClaim):
        return self._outcome(
            "quota confirmation", self.quota_confirmed
        )

    def terminal_cas(
        self,
        claim: SettlementClaim,
        progress: SettlementProgress,
        summary,
    ):
        with self.guard:
            self.pool_lock_depth += 1
            try:
                self._called("terminal CAS")
                if self.settled:
                    return self.claims[claim.lease_id]
                current = self.claims[claim.lease_id]
                if current.revision != claim.revision:
                    return None
                self.terminal_cas_count += 1
                self.settled = True
                self.lease_state = (
                    progress.terminal_state_override
                    or claim.terminal_state
                )
                self.capacity = 0
                updated = dataclasses.replace(
                    current,
                    progress=progress,
                    revision=current.revision + 1,
                    terminal_summary=summary,
                )
                self.claims[claim.lease_id] = updated
                return updated
            finally:
                self.pool_lock_depth -= 1

    def clear_references(
        self,
        lease_id: str,
        expected_revision: int,
        progress: SettlementProgress,
        summary,
    ):
        with self.guard:
            self.pool_lock_depth += 1
            try:
                self._called("clear references")
                if self.clear_failures:
                    self.clear_failures -= 1
                    raise RuntimeError("clear failed")
                current = self.claims[lease_id]
                if current.revision != expected_revision:
                    return None
                updated = dataclasses.replace(
                    current,
                    progress=progress,
                    revision=current.revision + 1,
                    owner=None,
                    writer=None,
                    stream=None,
                    artifact=None,
                    ring=None,
                    callback=None,
                    completion_handler=None,
                    terminal_summary=summary,
                )
                self.claims[lease_id] = updated
                return updated
            finally:
                self.pool_lock_depth -= 1


def candidate(
    *,
    intent: SettlementIntent = SettlementIntent.OWNER_FAILURE,
    proof: OwnerNonAdmissionProof | None = None,
    owner_instance_id: OwnerInstanceId | None = None,
):
    instance = owner_instance_id or owner_id()
    terminal = {
        SettlementIntent.OWNER_SUCCESS: "RELEASED",
        SettlementIntent.CLIENT_CANCEL: "CANCELED",
    }.get(intent, "FAILED")
    refs = tuple(object() for _ in range(7))
    return SettlementClaim(
        lease_id="lease-1",
        intent=intent,
        owner_instance_id=instance,
        stream_instance_id=stream_id(),
        terminal_state=terminal,
        error_code="TEST_FAILURE" if terminal == "FAILED" else None,
        non_admission_proof=proof,
        owner=refs[0],
        writer=refs[1],
        stream=refs[2],
        artifact=refs[3],
        ring=refs[4],
        callback=refs[5],
        completion_handler=refs[6],
    )


def coordinator(ports: FakePorts):
    return OwnerSettlementCoordinator(
        claims=ports,
        finality=ports,
        owners=ports,
        artifacts=ports,
        terminal=ports,
    )


def run_to_pause(engine, lease_id: str = "lease-1", *, limit: int = 40):
    result = None
    for _ in range(limit):
        previous = result
        result = engine.advance(lease_id)
        if result.settled or not result.progressed:
            return result
        assert result != previous or result.progressed
    raise AssertionError("settlement did not reach a pause")


def test_first_valid_intent_claims_settlement_without_terminalizing():
    ports = FakePorts()
    engine = coordinator(ports)

    result = engine.claim_or_resume(candidate())

    assert result.claimed is True
    assert result.settled is False
    assert ports.lease_state == "COMMITTED"
    assert ports.capacity == 1
    assert ports.events == []


def test_later_intent_resumes_existing_claim_instead_of_replacing_it():
    ports = FakePorts()
    engine = coordinator(ports)
    first = candidate(intent=SettlementIntent.OWNER_FAILURE)
    later = dataclasses.replace(
        candidate(intent=SettlementIntent.CLIENT_CANCEL),
        error_code=None,
    )

    engine.claim_or_resume(first)
    result = engine.claim_or_resume(later)

    assert result.claimed is False
    assert result.intent is SettlementIntent.OWNER_FAILURE
    assert ports.claims["lease-1"].intent is SettlementIntent.OWNER_FAILURE


def test_external_steps_run_without_pool_lock():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)

    assert result.settled


def test_successful_pipeline_obeys_the_strict_settlement_prefix():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    assert run_to_pause(engine).settled

    assert [event for event in ports.events if event != "validate"] == [
        "remove",
        "signal",
        "join",
        "freeze/readiness",
        "late-command resolution",
        "persist remove-disable",
        "acknowledge",
        "persist acknowledgement",
        "prune",
        "persist prune",
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
        "clear references",
    ]


@pytest.mark.parametrize(
    ("outcome", "failure"),
    [
        (OwnerRemovalOutcome.MISMATCH, None),
        (OwnerRemovalOutcome.INDETERMINATE, None),
        (OwnerRemovalOutcome.REMOVED, TimeoutError()),
        (OwnerRemovalOutcome.REMOVED, RuntimeError("secret payload")),
    ],
)
def test_mismatch_indeterminate_timeout_and_exception_retain_capacity(
    outcome, failure
):
    ports = FakePorts()
    ports.removal_outcome = outcome
    ports.raise_remove = failure
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)

    assert not result.settled
    assert ports.capacity == 1
    assert ports.lease_state == "COMMITTED"
    assert ports.calls.get("signal", 0) == 0
    assert ports.calls.get("terminal CAS", 0) == 0


def test_progress_resumes_at_first_incomplete_step():
    ports = FakePorts()
    ports.join_complete = False
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    run_to_pause(engine)
    calls_before = dict(ports.calls)

    ports.join_complete = True
    result = run_to_pause(engine)

    assert result.settled
    assert ports.calls["remove"] == calls_before["remove"]
    assert ports.calls["signal"] == calls_before["signal"]
    assert ports.calls["join"] == calls_before["join"] + 1


def test_durable_remove_disable_prevents_every_later_remove_retry():
    ports = FakePorts()
    ports.ack_matches = False
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    run_to_pause(engine)
    progress = ports.claims["lease-1"].progress
    assert progress.remove_retry_disabled
    remove_calls = ports.calls["remove"]

    for _ in range(3):
        engine.advance("lease-1")

    assert ports.calls["remove"] == remove_calls


def test_non_admission_skips_only_stream_finality_steps():
    ports = FakePorts()
    proof = OwnerNonAdmissionProof(
        owner_id(),
        OwnerNonAdmissionReason.LEDGER_FULL,
        command_enqueued=False,
        command_claimed=False,
    )
    engine = coordinator(ports)
    engine.claim_or_resume(candidate(proof=proof))

    result = run_to_pause(engine)
    progress = ports.claims["lease-1"].progress

    assert result.settled
    assert progress.non_admission_confirmed
    assert progress.finality_not_applicable
    assert progress.late_owner_commands_not_applicable
    assert ports.calls.get("remove", 0) == 0
    assert ports.calls.get("late-command resolution", 0) == 0
    assert ports.calls.get("acknowledge", 0) == 0
    assert ports.calls.get("prune", 0) == 0
    for applicable in (
        "signal",
        "join",
        "freeze/readiness",
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
    ):
        assert ports.calls[applicable] == 1


def test_terminal_cas_is_last_and_clears_strong_references():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)
    retained = ports.claims["lease-1"]

    assert result.settled
    assert ports.events[-2:] == ["terminal CAS", "clear references"]
    assert retained.progress.terminalized
    assert all(
        getattr(retained, field) is None
        for field in (
            "owner",
            "writer",
            "stream",
            "artifact",
            "ring",
            "callback",
            "completion_handler",
        )
    )
    assert retained.terminal_summary is not None


def test_pool_diagnostics_report_claim_phases_unsettled_and_retry_counters():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    ports.removal_outcome = OwnerRemovalOutcome.MISMATCH
    run_to_pause(engine)
    ports.removal_outcome = OwnerRemovalOutcome.REMOVED
    ports.join_complete = False
    run_to_pause(engine)
    ports.join_complete = True
    ports.artifact_result = ArtifactSettlementResult(False, False)
    run_to_pause(engine)
    ports.artifact_result = ArtifactSettlementResult(True, True)
    assert run_to_pause(engine).settled
    proof = OwnerNonAdmissionProof(
        owner_id(nonce="backpressure"),
        OwnerNonAdmissionReason.LEDGER_FULL,
        False,
        False,
    )
    backpressure = dataclasses.replace(
        candidate(
            proof=proof,
            owner_instance_id=proof.owner_instance_id,
        ),
        lease_id="lease-2",
    )
    engine.claim_or_resume(backpressure)
    snapshot = engine.diagnostics()

    assert dict(snapshot.claims_by_phase).keys() == set(SettlementPhase)
    assert snapshot.unsettled_leases == 1
    assert snapshot.removal_retries == 1
    assert snapshot.owner_join_retries == 1
    assert snapshot.artifact_disposition_retries == 1
    assert snapshot.publication_retries == 1
    assert snapshot.settlement_backpressure_rejections == 1


def test_pool_diagnostics_keep_required_ids_and_counters_but_redact_secrets():
    ports = FakePorts()
    proof = OwnerNonAdmissionProof(
        owner_id(),
        OwnerNonAdmissionReason.LEDGER_FULL,
        False,
        False,
    )
    engine = coordinator(ports)
    engine.claim_or_resume(candidate(proof=proof))
    run_to_pause(engine)

    encoded = json.dumps(
        dataclasses.asdict(engine.diagnostics()),
        default=lambda value: value.value,
    )

    assert "hw-1" in encoded
    assert "run-9" in encoded
    assert "11" in encoded
    assert "FAILED" in encoded
    for secret in (
        "owner-secret",
        "ack-secret",
        "publication-token",
        "secret payload",
    ):
        assert secret not in encoded


@pytest.mark.parametrize("intent", list(SettlementIntent))
@pytest.mark.parametrize(
    "outcome",
    [
        OwnerRemovalOutcome.REMOVED,
        OwnerRemovalOutcome.TOMBSTONED_ABSENT,
        OwnerRemovalOutcome.MISMATCH,
        OwnerRemovalOutcome.INDETERMINATE,
        "exception",
        "timeout",
    ],
)
def test_each_owner_admitted_intent_obeys_every_removal_outcome(intent, outcome):
    ports = FakePorts()
    if outcome == "exception":
        ports.raise_remove = RuntimeError("remove failed")
    elif outcome == "timeout":
        ports.raise_remove = TimeoutError()
    else:
        ports.removal_outcome = outcome
    engine = coordinator(ports)
    original = candidate(intent=intent)
    engine.claim_or_resume(original)

    result = run_to_pause(engine)
    positive = outcome in (
        OwnerRemovalOutcome.REMOVED,
        OwnerRemovalOutcome.TOMBSTONED_ABSENT,
    )

    if positive:
        assert ports.claims["lease-1"].progress.removal_receipt.confirms(
            original.owner_instance_id
        )
        assert ports.calls["signal"] == 1
    else:
        assert not result.settled
        assert ports.claims["lease-1"].intent is intent
        assert ports.capacity == 1
        for blocked in (
            "signal",
            "join",
            "artifact/publication",
            "acknowledge",
            "prune",
            "quota confirmation",
            "terminal CAS",
        ):
            assert ports.calls.get(blocked, 0) == 0


def test_coordinator_rejects_wrong_owner_non_admission_proof():
    expected = owner_id()
    proof = OwnerNonAdmissionProof(
        owner_id(nonce="wrong"),
        OwnerNonAdmissionReason.LEDGER_FULL,
        False,
        False,
    )
    ports = FakePorts()
    engine = coordinator(ports)

    result = engine.claim_or_resume(
        candidate(proof=proof, owner_instance_id=expected)
    )

    assert result.claimed is False
    assert "lease-1" not in ports.claims
    assert ports.capacity == 1
    assert ports.calls == {}


@pytest.mark.parametrize(
    ("enqueued", "claimed"),
    [(True, False), (False, True)],
)
def test_coordinator_rejects_forged_enqueued_or_claimed_non_admission_proof(
    enqueued, claimed
):
    proof = OwnerNonAdmissionProof(
        owner_id(),
        OwnerNonAdmissionReason.LEDGER_FULL,
        enqueued,
        claimed,
    )
    ports = FakePorts()
    engine = coordinator(ports)

    result = engine.claim_or_resume(candidate(proof=proof))

    assert result.claimed is False
    assert "lease-1" not in ports.claims
    assert ports.capacity == 1
    assert ports.calls == {}


@pytest.mark.parametrize(
    "operation",
    [
        "remove",
        "signal",
        "join",
        "freeze/readiness",
        "late-command resolution",
        "acknowledge",
        "prune",
        "artifact/publication",
        "quota confirmation",
    ],
)
@pytest.mark.parametrize("failure", [RuntimeError("boom"), TimeoutError()])
def test_external_failure_then_success_resumes_without_repeating_prefix(
    operation, failure
):
    ports = FakePorts()
    ports.injected[operation] = [failure]
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    paused = run_to_pause(engine)
    prefix_calls = dict(ports.calls)
    assert not paused.settled
    result = run_to_pause(engine)

    assert result.settled
    assert ports.calls[operation] == 2
    operation_index = [
        "remove",
        "signal",
        "join",
        "freeze/readiness",
        "late-command resolution",
        "acknowledge",
        "prune",
        "artifact/publication",
        "quota confirmation",
    ].index(operation)
    for earlier in [
        "remove",
        "signal",
        "join",
        "freeze/readiness",
        "late-command resolution",
        "acknowledge",
        "prune",
        "artifact/publication",
        "quota confirmation",
    ][:operation_index]:
        assert ports.calls[earlier] == prefix_calls[earlier]


def test_acknowledgement_mismatch_blocks_every_downstream_gate():
    ports = FakePorts()
    ports.ack_matches = False
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    paused = run_to_pause(engine)
    progress = ports.claims["lease-1"].progress

    assert not paused.settled
    assert not progress.finality_acknowledged
    assert progress.remove_retry_disabled
    assert ports.capacity == 1
    assert ports.lease_state == "COMMITTED"
    for blocked in (
        "prune",
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
    ):
        assert ports.calls.get(blocked, 0) == 0
    remove_calls = ports.calls["remove"]
    ports.ack_matches = True
    assert run_to_pause(engine).settled
    assert ports.calls["remove"] == remove_calls


def test_prune_mismatch_blocks_every_downstream_gate():
    ports = FakePorts()
    ports.prune_outcome = FinalityPruneOutcome.MISMATCH
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    paused = run_to_pause(engine)
    progress = ports.claims["lease-1"].progress

    assert not paused.settled
    assert progress.finality_acknowledged
    assert progress.finality_ack_receipt is not None
    assert not progress.finality_pruned
    assert ports.capacity == 1
    for blocked in (
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
    ):
        assert ports.calls.get(blocked, 0) == 0
    remove_calls = ports.calls["remove"]
    ack_calls = ports.calls["acknowledge"]
    ports.prune_outcome = FinalityPruneOutcome.ALREADY_PRUNED
    assert run_to_pause(engine).settled
    assert ports.calls["remove"] == remove_calls
    assert ports.calls["acknowledge"] == ack_calls


def test_duplicate_advance_after_success_is_idempotent():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    assert run_to_pause(engine).settled
    events = list(ports.events)

    result = engine.advance("lease-1")

    assert result.settled
    assert not result.progressed
    assert ports.terminal_cas_count == 1
    assert ports.events == events + ["validate"]


def test_two_concurrent_callers_share_one_in_progress_action_and_terminal_cas():
    ports = FakePorts()
    ports.remove_entered = threading.Event()
    ports.remove_release = threading.Event()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    outcomes = []

    def worker():
        for _ in range(40):
            result = engine.advance("lease-1")
            if result.settled:
                outcomes.append(result)
                return

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert ports.remove_entered.wait(timeout=5)
    second.start()
    ports.remove_release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes
    assert ports.calls["remove"] == 1
    assert ports.terminal_cas_count == 1


def test_unknown_and_stale_lease_ids_do_not_retain_per_id_advance_locks():
    ports = FakePorts()
    engine = coordinator(ports)

    for index in range(1_000):
        with pytest.raises(KeyError):
            engine.advance(f"missing-{index}")

    engine.claim_or_resume(candidate())
    del ports.claims["lease-1"]
    for index in range(1_000):
        with pytest.raises(KeyError):
            engine.advance(f"stale-{index}")

    barrier = threading.Barrier(16)
    unexpected: list[BaseException | str] = []

    def probe_shared_missing_id():
        barrier.wait()
        for _ in range(100):
            try:
                engine.advance("missing-shared")
            except KeyError:
                continue
            except BaseException as error:
                unexpected.append(error)
            else:
                unexpected.append("missing lease unexpectedly advanced")

    threads = [
        threading.Thread(target=probe_shared_missing_id)
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not unexpected
    assert all(not thread.is_alive() for thread in threads)
    assert len(engine._advance_locks) == 0


def test_retry_diagnostics_survive_bounded_claim_record_eviction():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    ports.removal_outcome = OwnerRemovalOutcome.MISMATCH
    run_to_pause(engine)
    ports.removal_outcome = OwnerRemovalOutcome.REMOVED
    ports.join_complete = False
    run_to_pause(engine)
    ports.join_complete = True
    ports.artifact_result = ArtifactSettlementResult(False, False)
    run_to_pause(engine)
    ports.artifact_result = ArtifactSettlementResult(True, True)
    assert run_to_pause(engine).settled
    before = engine.diagnostics()
    assert (
        before.removal_retries,
        before.owner_join_retries,
        before.artifact_disposition_retries,
        before.publication_retries,
    ) == (1, 1, 1, 1)

    ports.claims.clear()
    after = engine.diagnostics()

    assert (
        after.removal_retries,
        after.owner_join_retries,
        after.artifact_disposition_retries,
        after.publication_retries,
    ) == (1, 1, 1, 1)


def test_reference_clear_failure_stays_incomplete_and_retries_to_recovery():
    ports = FakePorts()
    ports.clear_failures = 2
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())
    while (
        ports.claims["lease-1"].progress.phase
        is not SettlementPhase.TERMINAL
    ):
        assert engine.advance("lease-1").progressed

    first = engine.advance("lease-1")
    retained = ports.claims["lease-1"]
    assert not first.settled
    assert retained.progress.terminalized
    assert retained.owner is not None
    assert ports.terminal_cas_count == 1

    second = engine.advance("lease-1")
    assert not second.settled
    assert ports.claims["lease-1"].owner is not None
    assert ports.terminal_cas_count == 1

    recovered = engine.advance("lease-1")
    retained = ports.claims["lease-1"]
    assert recovered.settled
    assert retained.progress.references_cleared
    assert all(
        getattr(retained, field) is None
        for field in (
            "owner",
            "writer",
            "stream",
            "artifact",
            "ring",
            "callback",
            "completion_handler",
        )
    )
    assert ports.terminal_cas_count == 1
    assert ports.calls["clear references"] == 3


@pytest.mark.parametrize(
    "operation",
    [
        "remove",
        "signal",
        "join",
        "freeze/readiness",
        "late-command resolution",
        "acknowledge",
        "prune",
        "artifact/publication",
        "quota confirmation",
    ],
)
def test_same_thread_same_lease_reentrant_advance_is_rejected(operation):
    ports = FakePorts()
    engine = coordinator(ports)
    ports.reenter_operation = operation
    ports.reenter_engine = engine
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)
    retained = ports.claims["lease-1"]

    assert result.settled
    assert ports.reentry_observations == [
        ("rejected", "same-lease settlement advance is reentrant")
    ]
    assert ports.calls[operation] == 1
    assert retained.progress.removal_attempts == 1
    assert retained.progress.removal_receipt is not None
    assert retained.progress.removal_receipt.confirms(
        retained.owner_instance_id
    )
    assert engine.diagnostics().removal_retries == 0
    assert ports.terminal_cas_count == 1


class _ArtifactStoreProbe:
    def __init__(
        self,
        state,
        *,
        pending=True,
        publication_token="publication-secret",
    ):
        self.current_state = state
        self.pending = pending
        self.publication_token = publication_token
        self.decline_tokens = []
        self.claim_tokens = []
        self.discard_calls = 0
        self.delete_pending = False
        self.inspection_failures = 0
        self.claim_raises_after_mutation = 0
        self.decline_raises_after_mutation = 0

    def state(self, artifact_id):
        del artifact_id
        if self.inspection_failures:
            self.inspection_failures -= 1
            raise OSError("secret inspection payload")
        return self.current_state

    def publication_pending(self, artifact_id):
        del artifact_id
        if self.inspection_failures:
            self.inspection_failures -= 1
            raise OSError("secret inspection payload")
        return self.pending

    def claim_ready(self, artifact_id, publication_token, consumer_id):
        del artifact_id, consumer_id
        self.claim_tokens.append(publication_token)
        if (
            publication_token != self.publication_token
            or not self.pending
        ):
            return None
        from app.service.analysis.resident_audio.contracts import ArtifactState

        self.pending = False
        self.current_state = ArtifactState.CLAIMED
        if self.claim_raises_after_mutation:
            self.claim_raises_after_mutation -= 1
            raise OSError("secret lost claim response")
        return "claim-token"

    def decline_publication(self, artifact_id, publication_token, reason):
        del artifact_id, reason
        self.decline_tokens.append(publication_token)
        if publication_token != self.publication_token:
            return False
        from app.service.analysis.resident_audio.contracts import ArtifactState

        self.pending = False
        self.current_state = (
            ArtifactState.DELETE_PENDING
            if self.delete_pending
            else ArtifactState.DELETED
        )
        if self.decline_raises_after_mutation:
            self.decline_raises_after_mutation -= 1
            raise OSError("secret lost decline response")
        return True

    def discard_partial(self, artifact_id):
        del artifact_id
        self.discard_calls += 1
        from app.service.analysis.resident_audio.contracts import ArtifactState

        self.current_state = (
            ArtifactState.DELETE_PENDING
            if self.delete_pending
            else ArtifactState.DELETED
        )
        return True

    def retry_delete_pending(self, *, limit):
        del limit
        from app.service.analysis.resident_audio.contracts import ArtifactState

        if (
            self.current_state is ArtifactState.DELETE_PENDING
            and not self.delete_pending
        ):
            self.current_state = ArtifactState.DELETED

    def retry_delete_pending_artifact(self, artifact_id):
        del artifact_id
        self.retry_delete_pending(limit=1)


def _artifact_receipt(store):
    from app.service.analysis.resident_audio.artifact_settlement import (
        ArtifactSettlementFacade,
    )

    return ArtifactSettlementFacade(store).inspect("artifact-1")


def test_success_ready_unclaimed_is_not_terminal_disposition():
    from app.service.analysis.resident_audio.contracts import ArtifactState
    from app.service.analysis.resident_audio.artifact_settlement import (
        ArtifactDisposition,
        ArtifactQuotaOutcome,
    )

    receipt = _artifact_receipt(
        _ArtifactStoreProbe(ArtifactState.READY_UNCLAIMED)
    )

    assert receipt.publication_resolved is False
    assert receipt.disposition is ArtifactDisposition.INCOMPLETE
    assert receipt.quota_outcome is ArtifactQuotaOutcome.UNKNOWN
    assert receipt.complete is False


def test_cancel_failure_shutdown_discard_only_after_finality_pruned():
    for intent in (
        SettlementIntent.CLIENT_CANCEL,
        SettlementIntent.OWNER_FAILURE,
        SettlementIntent.POOL_SHUTDOWN,
    ):
        ports = FakePorts()
        engine = coordinator(ports)
        engine.claim_or_resume(candidate(intent=intent))

        run_to_pause(engine)

        assert ports.events.index("persist prune") < ports.events.index(
            "artifact/publication"
        )


def test_delete_pending_keeps_quota_and_lease_nonterminal():
    from app.service.analysis.resident_audio.contracts import ArtifactState
    from app.service.analysis.resident_audio.artifact_settlement import (
        ArtifactDisposition,
        ArtifactQuotaOutcome,
    )

    receipt = _artifact_receipt(
        _ArtifactStoreProbe(
            ArtifactState.DELETE_PENDING,
            pending=False,
        )
    )

    assert receipt.disposition is ArtifactDisposition.INCOMPLETE
    assert receipt.quota_outcome is ArtifactQuotaOutcome.UNKNOWN
    assert receipt.complete is False


@pytest.mark.parametrize(
    "state_name, expected_disposition",
    [
        ("CLAIMED", "CLAIMED"),
        ("TRANSFERRED", "TRANSFERRED"),
    ],
)
def test_claimed_or_transferred_artifact_is_authoritative_retained_disposition(
    state_name,
    expected_disposition,
):
    from app.service.analysis.resident_audio.contracts import ArtifactState
    from app.service.analysis.resident_audio.artifact_settlement import (
        ArtifactQuotaOutcome,
    )

    receipt = _artifact_receipt(
        _ArtifactStoreProbe(
            ArtifactState[state_name],
            pending=False,
        )
    )

    assert receipt.publication_resolved is True
    assert receipt.disposition.value == expected_disposition
    assert receipt.quota_outcome is ArtifactQuotaOutcome.RETAINED
    assert receipt.complete is True


@pytest.mark.parametrize(
    "blocked_step",
    [
        "remove",
        "join",
        "late-command resolution",
        "acknowledge",
        "prune",
        "artifact/publication",
    ],
)
def test_quota_release_never_precedes_settlement_prerequisites(blocked_step):
    ports = FakePorts()
    if blocked_step == "artifact/publication":
        ports.artifact_result = ArtifactSettlementResult(True, False)
    else:
        ports.injected[blocked_step] = [False]
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)

    assert result.settled is False
    assert ports.calls.get("quota confirmation", 0) == 0
    assert ports.capacity == 1


def test_terminal_cas_releases_capacity_once_after_artifact_outcome():
    ports = FakePorts()
    engine = coordinator(ports)
    engine.claim_or_resume(candidate())

    result = run_to_pause(engine)
    repeated = engine.advance("lease-1")

    assert result.settled is True
    assert ports.capacity == 0
    assert ports.terminal_cas_count == 1
    assert repeated.settled is True
    assert ports.terminal_cas_count == 1


def test_exact_artifact_retry_releases_target_quota_and_lease_behind_old_failures(
    tmp_path,
):
    from app.service.analysis.resident_audio import (
        ArtifactState,
        CaptureArtifactStore,
    )

    old_paths = set()
    target_path = None
    target_attempts = 0

    def remove(path):
        nonlocal target_attempts
        if path in old_paths:
            raise PermissionError("permanently busy")
        if path == target_path:
            target_attempts += 1
            if target_attempts == 1:
                raise PermissionError("transiently busy")
        path.unlink()

    artifact_bytes = 4
    store = CaptureArtifactStore(
        tmp_path / "staging",
        quota_bytes=65 * artifact_bytes,
        free_space_bytes=lambda _: 65 * artifact_bytes,
        minimum_free_bytes=0,
        remove_file=remove,
    )

    def allocate(run_id):
        return store.allocate_writer(
            run_id=run_id,
            target_frames=1,
            channels=1,
            sample_rate=10.0,
            sample_start=100,
            pool_epoch="epoch-1",
            stream_generation=3,
            trigger_monotonic=10.0,
            capture_start_monotonic=10.1,
            time_quality="CONFIRMED",
        )

    old_ids = []
    for index in range(64):
        writer = allocate(f"old-{index}")
        old_paths.add(writer.partial_path)
        old_ids.append(writer.artifact_id)
        writer.discard()

    target = allocate("target")
    target_path = target.partial_path
    target.discard()
    assert store.state(target.artifact_id) is ArtifactState.DELETE_PENDING

    adapter = PoolSettlementAdapter(SimpleNamespace(_artifact_store=store))
    ports = FakePorts()
    ports.settle_artifact_and_publication = (
        adapter.settle_artifact_and_publication
    )
    ports.confirm_quota_outcome = adapter.confirm_quota_outcome
    claim = dataclasses.replace(
        candidate(intent=SettlementIntent.OWNER_FAILURE),
        writer=target,
        artifact=None,
    )
    engine = coordinator(ports)
    engine.claim_or_resume(claim)

    result = run_to_pause(engine)

    assert result.settled is True
    assert ports.capacity == 0
    assert store.state(target.artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 64 * artifact_bytes
    assert all(
        store.state(artifact_id) is ArtifactState.DELETE_PENDING
        for artifact_id in old_ids
    )
    assert target_attempts == 2


def _completion_harness(handler, *, delete_pending=False):
    from app.service.analysis.resident_audio.contracts import ArtifactState

    store = _ArtifactStoreProbe(ArtifactState.READY_UNCLAIMED)
    store.delete_pending = delete_pending
    artifact = SimpleNamespace(
        artifact_id="artifact-1",
        publication_token=store.publication_token,
    )
    pool = SimpleNamespace(
        _artifact_store=store,
        _completion_handler=handler,
    )
    adapter = PoolSettlementAdapter(pool)
    ports = FakePorts()
    ports.invoke_completion_handler = adapter.invoke_completion_handler
    ports.settle_artifact_and_publication = (
        adapter.settle_artifact_and_publication
    )
    ports.confirm_quota_outcome = adapter.confirm_quota_outcome
    claim = dataclasses.replace(
        candidate(intent=SettlementIntent.OWNER_SUCCESS),
        artifact=artifact,
        completion_handler=handler,
    )
    engine = coordinator(ports)
    engine.claim_or_resume(claim)
    return engine, ports, store


def _run_completion_handler(handler, *, delete_pending=False):
    engine, ports, store = _completion_harness(
        handler, delete_pending=delete_pending
    )
    result = run_to_pause(engine)
    return result, ports, store


def _advance_through_handler(engine, calls):
    for _ in range(40):
        result = engine.advance("lease-1")
        if calls:
            return result
        assert result.progressed
    raise AssertionError("completion handler was not invoked")


def test_completion_handler_is_invoked_at_most_once_across_retry():
    calls = []

    def handler(completion):
        calls.append(completion)

    result, ports, store = _run_completion_handler(
        handler, delete_pending=True
    )
    assert result.settled is False
    store.delete_pending = False
    engine = coordinator(ports)
    recovered = run_to_pause(engine)

    assert recovered.settled is True
    assert len(calls) == 1
    assert store.decline_tokens == [store.publication_token]


@pytest.mark.parametrize("resolution", ["claim", "decline"])
def test_completion_handler_resolved_then_raised_is_released_with_diagnostic(
    resolution,
):
    def handler(completion):
        if resolution == "claim":
            assert completion.claim("consumer") == "claim-token"
        else:
            assert completion.decline("declined")
        raise RuntimeError("secret arbitrary payload")

    result, ports, _store = _run_completion_handler(handler)
    retained = ports.claims["lease-1"]

    assert result.settled is True
    assert ports.lease_state == "RELEASED"
    assert retained.terminal_summary.failure_reason == (
        "completion_handler_raised_after_resolution"
    )
    assert retained.terminal_summary.exception_type == "RuntimeError"


def test_completion_handler_returned_pending_auto_declines_and_fails():
    result, ports, store = _run_completion_handler(lambda _completion: None)
    summary = ports.claims["lease-1"].terminal_summary

    assert result.settled is True
    assert store.decline_tokens == [store.publication_token]
    assert ports.lease_state == "FAILED"
    assert summary.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert summary.failure_reason == (
        "completion_handler_left_publication_pending"
    )


def test_completion_handler_raised_pending_auto_declines_and_fails():
    def handler(_completion):
        raise LookupError("secret arbitrary payload")

    result, ports, store = _run_completion_handler(handler)
    summary = ports.claims["lease-1"].terminal_summary

    assert result.settled is True
    assert store.decline_tokens == [store.publication_token]
    assert ports.lease_state == "FAILED"
    assert summary.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert summary.failure_reason == "completion_handler_raised"
    assert summary.exception_type == "LookupError"


def test_auto_decline_uses_exact_publication_token():
    _result, _ports, store = _run_completion_handler(
        lambda _completion: None
    )

    assert store.decline_tokens == ["publication-secret"]


def test_handler_exception_diagnostic_does_not_retain_exception_or_handler():
    class Handler:
        def __call__(self, _completion):
            raise RuntimeError("secret arbitrary payload")

    handler = Handler()
    _result, ports, _store = _run_completion_handler(handler)
    summary = ports.claims["lease-1"].terminal_summary
    encoded = json.dumps(dataclasses.asdict(summary), default=str)

    assert summary.exception_type == "RuntimeError"
    assert "artifact-1" in encoded
    assert "FAILED" in encoded
    assert "CAPTURE_ARTIFACT_IO_FAILED" in encoded
    assert "owner-secret" not in encoded
    assert "publication-secret" not in encoded
    assert "ack-secret" not in encoded
    assert "secret arbitrary payload" not in encoded
    assert handler is not summary


def test_lost_auto_decline_response_preserves_failed_mapping_across_retry():
    calls = []

    def handler(completion):
        calls.append(completion)

    engine, ports, store = _completion_harness(handler)
    store.decline_raises_after_mutation = 1

    paused = _advance_through_handler(engine, calls)
    invocation = ports.claims["lease-1"].progress

    assert paused.settled is False
    assert invocation.handler_invoked is True
    assert invocation.handler_completed is True
    assert invocation.terminal_state_override is None
    assert invocation.handler_returned is True
    assert invocation.handler_exception_type is None
    assert store.decline_tokens == []

    mapped = engine.advance("lease-1")
    durable = ports.claims["lease-1"].progress

    assert mapped.settled is False
    assert durable.terminal_state_override == "FAILED"
    assert durable.error_code_override == "CAPTURE_ARTIFACT_IO_FAILED"
    assert durable.failure_reason_override == (
        "completion_handler_left_publication_pending"
    )
    diagnostic = engine.diagnostics().claims[0]
    assert diagnostic.terminal_state == "FAILED"
    assert diagnostic.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert diagnostic.phase is SettlementPhase.ARTIFACT_SETTLEMENT
    assert diagnostic.settled is False
    assert ports.terminal_cas_count == 0
    assert ports.capacity == 1
    assert store.decline_tokens == []

    lost_response = engine.advance("lease-1")

    assert lost_response.settled is False
    assert store.decline_tokens == [store.publication_token]
    assert (
        ports.claims["lease-1"].progress.failure_reason_override
        == durable.failure_reason_override
    )
    retry_diagnostic = engine.diagnostics().claims[0]
    assert retry_diagnostic.terminal_state == "FAILED"
    assert retry_diagnostic.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert retry_diagnostic.settled is False

    recovered = run_to_pause(engine)
    summary = ports.claims["lease-1"].terminal_summary

    assert recovered.settled is True
    assert ports.lease_state == "FAILED"
    assert summary.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert summary.failure_reason == (
        "completion_handler_left_publication_pending"
    )
    assert len(calls) == 1
    assert store.decline_tokens == [store.publication_token]


@pytest.mark.parametrize("resolution", ["claim", "decline"])
def test_post_handler_inspection_exception_preserves_resolved_raise_diagnostic(
    resolution,
):
    calls = []
    store_holder = {}

    def handler(completion):
        calls.append(completion)
        if resolution == "claim":
            assert completion.claim("consumer") == "claim-token"
        else:
            assert completion.decline("declined")
        store_holder["store"].inspection_failures = 1
        raise LookupError("secret arbitrary handler payload")

    engine, ports, store = _completion_harness(handler)
    store_holder["store"] = store

    paused = _advance_through_handler(engine, calls)
    invocation = ports.claims["lease-1"].progress

    assert paused.settled is False
    assert invocation.handler_completed is True
    assert invocation.terminal_state_override is None
    assert invocation.handler_returned is False
    assert invocation.handler_resolution == {
        "claim": "CLAIMED",
        "decline": "DECLINED",
    }[resolution]
    assert invocation.handler_exception_type == "LookupError"
    assert ports.terminal_cas_count == 0
    assert ports.capacity == 1

    inspection_pause = engine.advance("lease-1")

    assert inspection_pause.settled is False
    assert ports.claims["lease-1"].progress.terminal_state_override is None
    assert ports.terminal_cas_count == 0
    assert ports.capacity == 1

    mapped = engine.advance("lease-1")
    assert mapped.settled is False
    assert ports.claims["lease-1"].progress.terminal_state_override == (
        "RELEASED"
    )

    recovered = run_to_pause(engine)
    summary = ports.claims["lease-1"].terminal_summary
    encoded = json.dumps(dataclasses.asdict(summary), default=str)

    assert recovered.settled is True
    assert ports.lease_state == "RELEASED"
    assert summary.failure_reason == (
        "completion_handler_raised_after_resolution"
    )
    assert summary.exception_type == "LookupError"
    assert len(calls) == 1
    assert len(store.claim_tokens) + len(store.decline_tokens) == 1
    assert "secret arbitrary handler payload" not in encoded
    assert "secret inspection payload" not in encoded


def test_pending_unverifiable_publication_retains_fixed_mapping_and_capacity():
    store_holder = {}
    calls = []

    def handler(completion):
        calls.append(completion)
        store_holder["store"].inspection_failures = 3

    engine, ports, store = _completion_harness(handler)
    store_holder["store"] = store

    first = _advance_through_handler(engine, calls)
    invocation = ports.claims["lease-1"].progress

    assert first.settled is False
    assert invocation.terminal_state_override is None
    assert invocation.handler_returned is True

    second = engine.advance("lease-1")
    third = engine.advance("lease-1")
    fourth = engine.advance("lease-1")

    assert second.settled is False
    assert third.settled is False
    assert fourth.settled is False
    assert ports.claims["lease-1"].progress.terminal_state_override is None
    assert ports.terminal_cas_count == 0
    assert ports.capacity == 1
    assert len(calls) == 1

    mapped = engine.advance("lease-1")
    durable = ports.claims["lease-1"].progress

    assert mapped.settled is False
    assert durable.terminal_state_override == "FAILED"
    assert durable.error_code_override == "CAPTURE_ARTIFACT_IO_FAILED"
    assert durable.failure_reason_override == (
        "completion_handler_left_publication_pending"
    )
    assert durable.publication_resolved is False
    assert durable.artifact_disposition_confirmed is False
    assert durable.quota_outcome_confirmed is False
    assert store.decline_tokens == []


def test_pending_handler_mapping_is_reported_before_auto_decline():
    calls = []
    engine, ports, store = _completion_harness(calls.append)

    _advance_through_handler(engine, calls)
    mapped = engine.advance("lease-1")
    diagnostic = engine.diagnostics().claims[0]
    encoded = json.dumps(dataclasses.asdict(diagnostic), default=str)

    assert mapped.settled is False
    assert ports.claims["lease-1"].progress.publication_resolved is False
    assert (
        ports.claims[
            "lease-1"
        ].progress.artifact_disposition_confirmed
        is False
    )
    assert diagnostic.terminal_state == "FAILED"
    assert diagnostic.error_code == "CAPTURE_ARTIFACT_IO_FAILED"
    assert diagnostic.phase is SettlementPhase.ARTIFACT_SETTLEMENT
    assert diagnostic.settled is False
    assert store.decline_tokens == []
    for secret in (
        "owner-secret",
        "publication-secret",
        "ack-secret",
    ):
        assert secret not in encoded


@pytest.mark.parametrize("operation", ["claim", "decline"])
def test_handler_resolution_mutation_then_lost_response_uses_artifact_state(
    operation,
):
    calls = []

    def handler(completion):
        calls.append(completion)
        if operation == "claim":
            completion.claim("consumer")
        else:
            completion.decline("declined")

    engine, ports, store = _completion_harness(handler)
    if operation == "claim":
        store.claim_raises_after_mutation = 1
    else:
        store.decline_raises_after_mutation = 1

    invoked = _advance_through_handler(engine, calls)
    facts = ports.claims["lease-1"].progress

    assert invoked.settled is False
    assert facts.handler_completed is True
    assert facts.terminal_state_override is None
    assert facts.handler_returned is False
    assert facts.handler_resolution is None
    assert facts.handler_exception_type == "OSError"

    mapped = engine.advance("lease-1")
    durable = ports.claims["lease-1"].progress

    assert mapped.settled is False
    assert durable.terminal_state_override == "RELEASED"
    assert durable.error_code_override is None
    assert durable.failure_reason_override == (
        "completion_handler_raised_after_resolution"
    )
    assert durable.handler_exception_type == "OSError"
    diagnostic = engine.diagnostics().claims[0]
    assert diagnostic.terminal_state == "RELEASED"
    assert diagnostic.error_code is None
    assert diagnostic.phase is SettlementPhase.QUOTA
    assert diagnostic.settled is False

    recovered = run_to_pause(engine)
    summary = ports.claims["lease-1"].terminal_summary

    assert recovered.settled is True
    assert ports.lease_state == "RELEASED"
    assert summary.error_code is None
    assert summary.failure_reason == (
        "completion_handler_raised_after_resolution"
    )
    assert summary.exception_type == "OSError"
    assert len(calls) == 1
    assert store.claim_tokens == (
        [store.publication_token] if operation == "claim" else []
    )
    assert store.decline_tokens == (
        [store.publication_token] if operation == "decline" else []
    )
    if operation == "claim":
        from app.service.analysis.resident_audio.contracts import ArtifactState

        assert store.current_state is ArtifactState.CLAIMED
