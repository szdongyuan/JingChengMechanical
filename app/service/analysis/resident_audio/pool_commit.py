"""Sole six-lease table and identity-guarded two-phase capture commit."""

from __future__ import annotations

import threading
import uuid

from .contracts import (
    CaptureErrorCode,
    CaptureLayerError,
    CaptureOwnerState,
    LeaseState,
    StreamState,
    checked_capture_request,
)
from .owner import CaptureOwner
from .owner_types import CommitToken, OwnerTerminalEvent
from .owner_identity import OwnerInstanceId, OwnerNonAdmissionProof
from .owner_settlement_types import SettlementIntent
from .pool_types import (
    MAX_CAPTURE_LEASES,
    CaptureCompletion,
    LeaseOutcome,
    PoolState,
    _ACTIVE_LEASE_STATES,
    _LeaseRecord,
    _TERMINAL_LEASE_STATES,
)
from .stream_control import StreamCommandIndeterminate


class _SettlementOwnedWriterView:
    """Let owners write/freeze while the coordinator owns disposition."""

    __slots__ = ("_writer",)

    def __init__(self, writer) -> None:
        self._writer = writer

    @property
    def artifact_id(self):
        return self._writer.artifact_id

    def write(self, block):
        return self._writer.write(block)

    def bind_capture_start(self, *args, **kwargs):
        return self._writer.bind_capture_start(*args, **kwargs)

    def freeze(self):
        return self._writer.freeze()

    @staticmethod
    def discard() -> bool:
        # Owner drain is complete, but exact artifact disposition remains
        # downstream of removal, join, acknowledgement, and prune.
        return True


class ResidentPoolCommitMixin:
    """Lease transitions and terminal arbitration under the pool lock."""

    @staticmethod
    def _validated_run_id(run_id):
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if len(run_id) > 256:
            raise ValueError("run_id must contain at most 256 characters")
        return run_id

    def reserve(self, run_id: str) -> LeaseOutcome:
        run_id = self._validated_run_id(run_id)
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is not None:
                return existing.outcome
            if not self._pool_is_open_locked():
                return self._create_terminal_lease_locked(
                    run_id,
                    LeaseState.FAILED,
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "resident audio pool is shutting down",
                ).outcome
            if self._active_lease_count_locked() >= MAX_CAPTURE_LEASES:
                return self._create_terminal_lease_locked(
                    run_id,
                    LeaseState.FAILED,
                    CaptureErrorCode.CAPTURE_CAPACITY_FULL,
                    "resident capture capacity is full",
                ).outcome
            outcome = LeaseOutcome(run_id, LeaseState.RESERVED)
            self._leases[run_id] = _LeaseRecord(
                run_id, LeaseState.RESERVED, outcome
            )
            return outcome

    def commit(self, run_id, stream_token, request) -> LeaseOutcome:
        run_id = self._validated_run_id(run_id)
        with self._lock:
            record = self._leases.get(run_id)
            if record is None:
                return self._unknown_run_outcome(run_id)
            if record.state is not LeaseState.RESERVED:
                return record.outcome

        try:
            if request.run_id != run_id:
                raise CaptureLayerError(
                    CaptureErrorCode.INVALID_CAPTURE_REQUEST,
                    "capture request run_id does not match its lease",
                )
            if request.stream_token != stream_token:
                raise CaptureLayerError(
                    CaptureErrorCode.INVALID_CAPTURE_REQUEST,
                    "capture request token does not match commit token",
                )
            size = checked_capture_request(
                request, commit_monotonic=float(self._clock())
            )
        except Exception as error:
            code = (
                error.code
                if isinstance(error, CaptureLayerError)
                else CaptureErrorCode.INVALID_CAPTURE_REQUEST
            )
            with self._lock:
                record = self._leases.get(run_id)
                if record is not None and record.state is LeaseState.RESERVED:
                    return self._ownerless_reserved_terminal_locked(
                        record,
                        LeaseState.FAILED,
                        code,
                        str(error),
                    )
                return record.outcome

        with self._lock:
            record = self._leases.get(run_id)
            if record is None or record.state is not LeaseState.RESERVED:
                return record.outcome if record is not None else self._unknown_run_outcome(run_id)
            validation = self._validate_stream_for_commit_locked(stream_token)
            if validation[0] is None:
                return self._ownerless_reserved_terminal_locked(
                    record,
                    LeaseState.FAILED,
                    validation[1],
                    validation[2],
                )
            stream = validation[0]
            commit_attempt_id = uuid.uuid4().hex
            owner_capability = uuid.uuid4().hex
            commit_token = CommitToken()
            identity = OwnerInstanceId(
                pool_epoch=self._pool_epoch,
                hardware_id=stream_token.hardware_id,
                stream_generation=stream_token.stream_generation,
                run_id=run_id,
                commit_attempt_id=commit_attempt_id,
                owner_capability=owner_capability,
                instance_nonce=uuid.uuid4().hex,
            )
            record.previous_state = record.state
            record.state = LeaseState.COMMITTING
            record.commit_attempt_id = commit_attempt_id
            record.owner_capability = owner_capability
            record.stream = stream
            record.stream_generation = stream_token.stream_generation
            record.identity = identity
            record.owner_instance_id = identity
            record.commit_token = commit_token
            record.settled = False
            record.commit_settled = self._new_event()
            record.commit_worker = threading.current_thread()
            record.pre_teardown_settled = self._new_event()
            record.commit_resources_published = False
            record.outcome = LeaseOutcome(
                run_id,
                LeaseState.COMMITTING,
            )

        writer = None
        owner = None
        prepared = False
        preparation_reserved = False
        committed = False
        failure_code = CaptureErrorCode.STREAM_NOT_READY
        failure_message = "capture commit failed"
        try:
            preparation_admission = stream.reserve_owner_prepare(identity)
            if isinstance(preparation_admission, OwnerNonAdmissionProof):
                record.non_admission_proof = preparation_admission
                record.diagnostic_context = (
                    self._ledger_backpressure_diagnostic(stream)
                )
                failure_code = CaptureErrorCode.CAPTURE_CAPACITY_FULL
                failure_message = (
                    "owner finality ledger capacity is full "
                    "(capacity_domain=owner_finality_ledger)"
                )
                raise _CommitFailed()
            preparation_reserved = True
            ring = stream.capture_ring(stream_token)
            if ring is None:
                failure_code = CaptureErrorCode.STREAM_NOT_READY
                failure_message = "stream is not ready"
                raise _CommitFailed()
            writer = self._artifact_store.allocate_writer(
                run_id=run_id,
                target_frames=size.target_frames,
                channels=stream_token.channels,
                sample_rate=stream_token.sample_rate,
                pool_epoch=stream_token.pool_epoch,
                stream_generation=stream_token.stream_generation,
                trigger_monotonic=request.trigger_monotonic,
            )
            holder = {}

            def terminal_sink(event):
                self._owner_terminal(holder.get("owner"), event)

            owner = CaptureOwner(
                request=request,
                target_frames=size.target_frames,
                commit_attempt_id=commit_attempt_id,
                owner_capability=owner_capability,
                ring=ring,
                writer=_SettlementOwnedWriterView(writer),
                terminal_sink=terminal_sink,
                control_sink=stream.submit_owner_control,
                monotonic_clock=self._clock,
                commit_token=commit_token,
            )
            holder["owner"] = owner
            with self._lock:
                current = self._leases.get(run_id)
                if not self._commit_identity_matches_locked(
                    current, record, owner=None
                ):
                    raise _CommitAborted()
                if current.settlement_claim is not None:
                    self._settlement_adapter.publish_commit_resources(
                        current, owner, writer, stream
                    )
                    raise _CommitAborted()
                current.owner = owner
                current.writer = writer
                current.commit_resources_published = True
            try:
                preparation_acknowledged = stream.prepare_owner(
                    owner,
                    identity,
                    timeout=1.0,
                )
            except StreamCommandIndeterminate:
                prepared = True
                raise
            if isinstance(
                preparation_acknowledged, OwnerNonAdmissionProof
            ):
                record.non_admission_proof = preparation_acknowledged
                record.diagnostic_context = (
                    self._ledger_backpressure_diagnostic(stream)
                )
                failure_code = CaptureErrorCode.CAPTURE_CAPACITY_FULL
                failure_message = (
                    "owner finality ledger capacity is full "
                    "(capacity_domain=owner_finality_ledger)"
                )
                raise _CommitFailed()
            if not preparation_acknowledged:
                failure_message = "stream rejected dormant capture owner"
                raise _CommitFailed()
            prepared = True

            owner_is_prepared = (
                owner.snapshot().state is CaptureOwnerState.PREPARED
            )
            with self._lock:
                current = self._leases.get(run_id)
                if (
                    not self._commit_identity_matches_locked(current, record, owner)
                    or current.cancel_requested
                    or not self._pool_is_open_locked()
                    or not owner_is_prepared
                ):
                    raise _CommitAborted()
                if not commit_token.open():
                    raise _CommitAborted()
                current.previous_state = current.state
                current.state = LeaseState.COMMITTED
                current.outcome = LeaseOutcome(
                    run_id,
                    LeaseState.COMMITTED,
                    artifact_id=writer.artifact_id,
                )
                current.artifact_id = writer.artifact_id

            if not stream.activate_owner(
                owner,
                identity,
                commit_monotonic=float(self._clock()),
                timeout=1.0,
            ):
                snapshot = stream.snapshot()
                failure_code = (
                    CaptureErrorCode.STREAM_GENERATION_CHANGED
                    if snapshot.stream_generation != stream_token.stream_generation
                    or snapshot.state is StreamState.FAILED
                    else CaptureErrorCode.STREAM_NOT_READY
                )
                failure_message = "stream rejected capture owner activation"
                raise _CommitFailed()
            with self._lock:
                current = self._leases.get(run_id)
                if (
                    current is record
                    and current.state in (
                        LeaseState.RELEASED,
                        LeaseState.FAILED,
                        LeaseState.CANCELED,
                    )
                ):
                    current.commit_settled.set()
                    committed = True
                    return current.outcome
                if (
                    not self._committed_identity_matches_locked(current, record, owner)
                    or current.cancel_requested
                    or not self._pool_is_open_locked()
                ):
                    raise _CommitAborted()
                current.commit_settled.set()
                committed = True
                return current.outcome
        except CaptureLayerError as error:
            failure_code = error.code
            failure_message = error.message
        except _CommitAborted:
            failure_code = CaptureErrorCode.CAPTURE_CANCELED
            failure_message = "capture commit was canceled"
        except _CommitFailed:
            pass
        except Exception:
            failure_code = CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
            failure_message = "capture artifact operation failed"
        finally:
            if not committed:
                if preparation_reserved and not prepared:
                    stream.abort_owner_prepare_reservation(identity)
                self._rollback_commit(
                    record,
                    owner,
                    writer,
                    stream,
                    identity,
                    prepared,
                    failure_code,
                    failure_message,
                    intent=SettlementIntent.PREPARE_ROLLBACK,
                )
        with self._lock:
            if (
                self._state is PoolState.SHUTTING_DOWN
                and record.state is LeaseState.COMMITTING
                and record.settlement_claim is not None
            ):
                return LeaseOutcome(
                    record.run_id,
                    LeaseState.CANCELED,
                    CaptureErrorCode.CAPTURE_CANCELED,
                    "capture commit was canceled by pool shutdown",
                    artifact_id=record.artifact_id,
                )
            return record.outcome

    def cancel(self, run_id: str, reason: str) -> LeaseOutcome:
        run_id = self._validated_run_id(run_id)
        cleanup = None
        retry_cleanup = None
        with self._lock:
            record = self._leases.get(run_id)
            if record is None:
                return self._unknown_run_outcome(run_id)
            if record.state in (
                LeaseState.RELEASED,
                LeaseState.FAILED,
                LeaseState.CANCELED,
            ):
                return record.outcome
            if record.state is LeaseState.RESERVED:
                return self._ownerless_reserved_terminal_locked(
                    record,
                    LeaseState.CANCELED,
                    CaptureErrorCode.CAPTURE_CANCELED,
                    reason,
                )
            record.cancel_requested = True
            record.cancel_reason = reason
            if record.settlement_claim is not None:
                record.settled = False
                retry_cleanup = record
            elif record.state is LeaseState.COMMITTED:
                record.settled = False
                cleanup = (
                    record,
                    record.owner,
                    record.stream,
                    record.identity,
                )
            else:
                return record.outcome
        if retry_cleanup is not None:
            self._retry_lease_cleanup(retry_cleanup)
        else:
            self._cleanup_committed(
                *cleanup,
                shutdown=False,
                intent=SettlementIntent.CLIENT_CANCEL,
            )
        with self._lock:
            return record.outcome

    def release(self, run_id, owner_capability, artifact_id) -> LeaseOutcome:
        run_id = self._validated_run_id(run_id)
        with self._lock:
            record = self._leases.get(run_id)
            if record is None:
                return self._unknown_run_outcome(run_id)
            if record.state is LeaseState.RELEASED:
                return record.outcome
            if (
                record.state is not LeaseState.COMMITTED
                or record.owner_capability != owner_capability
                or record.artifact_id != artifact_id
            ):
                return record.outcome
            # A public caller cannot manufacture the owner's frozen artifact.
            return record.outcome

    def lease_outcome(self, run_id: str) -> LeaseOutcome:
        run_id = self._validated_run_id(run_id)
        with self._lock:
            record = self._leases.get(run_id)
            return (
                self._unknown_run_outcome(run_id)
                if record is None
                else record.outcome
            )

    def _owner_terminal(self, owner, event: OwnerTerminalEvent) -> None:
        execution = self._new_event()

        def settle_on_control_path():
            try:
                self._execute_owner_terminal(owner, event)
            finally:
                with self._lock:
                    execution.set()

        worker = threading.Thread(
            target=settle_on_control_path,
            name="resident-audio-owner-settlement",
            daemon=True,
        )
        with self._lock:
            worker.start()
            self._terminal_executions.add(execution)
            self._register_execution_locked(
                execution,
                "terminal",
                worker,
                self._execution_identities_for_run_ids_locked(
                    (event.run_id,)
                ),
            )

    def _execute_owner_terminal(self, owner, event: OwnerTerminalEvent) -> None:
        decline = False
        retry = False
        with self._lock:
            record = self._leases.get(event.run_id)
            if not self._terminal_identity_matches_locked(record, owner, event):
                decline = True
            elif record.settlement_claim is not None:
                retry = True
                decline = True
            elif record.cancel_requested or record.shutdown_requested:
                decline = True
                retry = True
            elif (
                event.state is CaptureOwnerState.COMPLETED
                and event.artifact is not None
                and record.artifact_id == event.artifact_id
                and event.publication_token
                == event.artifact.publication_token
            ):
                record.terminal_attempt_id = event.terminal_attempt_id
                record.publication_token = event.publication_token
                record.artifact = event.artifact
                intent = SettlementIntent.OWNER_SUCCESS
                terminal_state = LeaseState.RELEASED
                error_code = None
                failure_reason = None
            else:
                intent = (
                    SettlementIntent.STREAM_FAILURE
                    if event.error_code
                    in (
                        CaptureErrorCode.STREAM_GENERATION_CHANGED,
                        CaptureErrorCode.STREAM_OPEN_FAILED,
                        CaptureErrorCode.INPUT_DATA_TIMEOUT,
                        CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                    )
                    else SettlementIntent.OWNER_FAILURE
                )
                terminal_state = (
                    LeaseState.CANCELED
                    if event.state is CaptureOwnerState.CANCELED
                    else LeaseState.FAILED
                )
                error_code = event.error_code
                failure_reason = event.message
        if retry:
            return
        if decline:
            return
        self._submit_settlement(
            record,
            intent,
            terminal_state,
            error_code,
            failure_reason,
            artifact=event.artifact,
        )

    def _validate_stream_for_commit_locked(self, token):
        if not self._pool_is_open_locked():
            return None, CaptureErrorCode.POOL_SHUTTING_DOWN, "pool is shutting down"
        if token.pool_epoch != self._pool_epoch:
            return None, CaptureErrorCode.STREAM_GENERATION_CHANGED, "pool epoch changed"
        record = self._streams.get(token.hardware_id)
        desired = self._desired.get(token.hardware_id)
        if (
            record is None
            or record.stream_generation != token.stream_generation
            or desired is None
            or desired.sample_rate != token.sample_rate
            or desired.channels != token.channels
        ):
            return None, CaptureErrorCode.STREAM_GENERATION_CHANGED, "stream generation changed"
        diagnostic = self._stream_diagnostics.get(token.hardware_id)
        if (
            diagnostic is None
            or diagnostic.state
            not in (StreamState.READY, StreamState.TIMING_RECOVERY)
        ):
            return None, CaptureErrorCode.STREAM_NOT_READY, "stream is not ready"
        return record.stream, None, None

    @staticmethod
    def _commit_identity_matches_locked(current, expected, owner):
        return bool(
            current is expected
            and current.state is LeaseState.COMMITTING
            and current.commit_attempt_id == expected.commit_attempt_id
            and (owner is None or current.owner is owner)
        )

    @staticmethod
    def _committed_identity_matches_locked(current, expected, owner):
        return bool(
            current is expected
            and current.state is LeaseState.COMMITTED
            and current.commit_attempt_id == expected.commit_attempt_id
            and current.owner is owner
        )

    def _terminal_identity_matches_locked(self, record, owner, event):
        return bool(
            record is not None
            and record.state is LeaseState.COMMITTED
            and record.owner is owner
            and record.commit_attempt_id == event.commit_attempt_id
            and record.owner_capability == event.owner_capability
            and event.pool_epoch == self._pool_epoch
            and record.stream_generation == event.stream_generation
        )


class _CommitAborted(RuntimeError):
    pass


class _CommitFailed(RuntimeError):
    pass


__all__ = ["ResidentPoolCommitMixin"]
