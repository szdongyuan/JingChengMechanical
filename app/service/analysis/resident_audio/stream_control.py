"""Pool-facing, acknowledgement-based control API for a resident stream."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .backend import production_block_frames
from .contracts import StreamReadyToken, StreamState
from .owner_types import OwnerControlEvent
from .owner_finality import FinalityDurableProgress
from .owner_identity import (
    FinalityAckReceipt,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
)
from .stream_types import (
    OwnerRemovalReceipt,
    ResidentStreamSnapshot,
)


@dataclass(slots=True)
class StreamCommand:
    kind: str
    owner: object | None = None
    identity: OwnerInstanceId | None = None
    value: object | None = None
    ack: threading.Event | None = None
    result: object = False
    _state: str = field(default="QUEUED", init=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def claim(self) -> bool:
        with self._lifecycle_lock:
            if self._state != "QUEUED":
                return False
            self._state = "CLAIMED"
            return True

    def cancel_if_queued(self) -> bool:
        with self._lifecycle_lock:
            if self._state != "QUEUED":
                return False
            self._state = "CANCELED"
            self.result = False
            ack = self.ack
        if ack is not None:
            ack.set()
        return True

    def complete(self, result: object) -> None:
        with self._lifecycle_lock:
            if self._state != "CLAIMED":
                return
            self.result = result
            self._state = "COMPLETED"
            ack = self.ack
        if ack is not None:
            ack.set()


class StreamCommandIndeterminate(RuntimeError):
    """A claimed command exceeded its acknowledgement deadline."""

    def __init__(self, command: StreamCommand) -> None:
        super().__init__(
            f"{command.kind} began but did not acknowledge completion; "
            "identity rollback is required"
        )
        self.command_kind = command.kind
        self.identity = command.identity
        self.owner = command.owner
        self.rollback_required = True


class ResidentStreamControlMixin:
    """Identity-guarded commands serialized by the ingest worker."""

    def snapshot(self) -> ResidentStreamSnapshot:
        with self._state_lock:
            state = self._state
            failure_code = self._failure_code
        timeline = self._timeline
        ring = self._ring
        with self._handle_lock:
            handle_open = (
                (self._handle is not None and not self._handle_closed)
                or self._tracked_cleanup_handle is not None
            )
            handle_close_attempted = self._handle_close_attempted
            handle_abort_succeeded = self._handle_abort_succeeded
            handle_stop_succeeded = self._handle_stop_succeeded
        worker = self._worker
        finality = self._owner_finality.snapshot()
        return ResidentStreamSnapshot(
            hardware_id=self._resolved.hardware_id,
            pool_epoch=self._pool_epoch,
            stream_generation=self._generation_number,
            state=state,
            cursor=0 if timeline is None else timeline.cursor,
            ring_blocks=0 if ring is None else ring.block_count,
            owner_count=len(self._registrations),
            ingress_depth=self._ingress.size,
            generation_alive=self._generation.alive,
            handle_open=handle_open,
            handle_close_attempted=handle_close_attempted,
            handle_abort_succeeded=handle_abort_succeeded,
            handle_stop_succeeded=handle_stop_succeeded,
            failure_code=failure_code,
            ingest_thread_name=None if worker is None else worker.name,
            ingest_thread_alive=bool(worker and worker.is_alive()),
            sample_rate=self._resolved.sample_rate,
            channels=self._resolved.channels,
            callback_frames=production_block_frames(
                self._resolved.sample_rate
            ),
            ring_earliest_sample=(
                None if ring is None else ring.earliest_cursor
            ),
            ring_latest_sample=None if ring is None else ring.latest_cursor,
            ring_earliest_monotonic=(
                None if ring is None else ring.earliest_monotonic
            ),
            ring_latest_monotonic=(
                None if ring is None else ring.latest_monotonic
            ),
            command_depth=self._commands.qsize(),
            old_generation_drops=self._generation.dropped_callbacks,
            overflow_count=self._overflow_count,
            recovery_count=self._recovery_count,
            backlog_count=self._backlog_count,
            finality_entry_count=len(finality.entries),
        )

    def token_matches(self, token: StreamReadyToken | None) -> bool:
        return bool(
            token is not None
            and token.hardware_id == self._resolved.hardware_id
            and token.pool_epoch == self._pool_epoch
            and token.stream_generation == self._generation_number
            and token.sample_rate == self._resolved.sample_rate
            and token.channels == self._resolved.channels
        )

    def capture_ring(self, token: StreamReadyToken):
        """Return this generation's ring only for an admissible capture token."""

        if not self.token_matches(token):
            return None
        with self._state_lock:
            if self._state not in (StreamState.READY, StreamState.TIMING_RECOVERY):
                return None
        return self._ring

    def prepare_owner(
        self,
        owner: object,
        identity: OwnerInstanceId,
        *,
        timeout: float | None = None,
    ) -> bool | OwnerNonAdmissionProof:
        with self._registration_guard:
            reservation_prepared = identity in self._pool_reserved_prepares
            self._pool_reserved_prepares.discard(identity)
        if not reservation_prepared:
            try:
                admission = self._owner_finality.reserve_prepare(identity)
            except ValueError:
                return False
            if isinstance(admission, OwnerNonAdmissionProof):
                return admission
        command = StreamCommand("prepare", owner=owner, identity=identity)
        command.ack = threading.Event()
        self._owner_finality.mark_prepare_enqueued(identity)
        if not self._command_admission.submit(
            command, self._commands, self._wake
        ):
            try:
                self._owner_finality.claim_prepare(identity)
            finally:
                self._owner_finality.resolve_prepare(
                    identity, activated=False
                )
            return False
        if not command.ack.wait(timeout):
            if command.cancel_if_queued():
                self._owner_finality.resolve_queued_prepare_cancellation(
                    identity, cancelled=True
                )
                return False
            if not command.ack.wait(timeout):
                raise StreamCommandIndeterminate(command)
        return bool(command.result)

    def reserve_owner_prepare(
        self, identity: OwnerInstanceId
    ):
        admission = self._owner_finality.reserve_prepare(identity)
        if not isinstance(admission, OwnerNonAdmissionProof):
            with self._registration_guard:
                self._pool_reserved_prepares.add(identity)
        return admission

    def abort_owner_prepare_reservation(
        self, identity: OwnerInstanceId
    ):
        with self._registration_guard:
            self._pool_reserved_prepares.discard(identity)
        return self._owner_finality.abort_prepare_reservation(
            identity, command_not_enqueued=True
        )

    def activate_owner(
        self,
        owner: object,
        identity: OwnerInstanceId,
        *,
        commit_monotonic: float,
        timeout: float | None = None,
    ) -> bool:
        return bool(
            self._request(
                StreamCommand(
                    "activate",
                    owner=owner,
                    identity=identity,
                    value=float(commit_monotonic),
                ),
                timeout,
            )
        )

    def remove_owner(
        self,
        instance_id: OwnerInstanceId,
        *,
        timeout: float | None = None,
    ) -> OwnerRemovalReceipt:
        if not isinstance(instance_id, OwnerInstanceId):
            raise TypeError("remove_owner requires OwnerInstanceId")
        finalized = self._begin_owner_removal(instance_id)
        if finalized is not None:
            return finalized
        try:
            result = self._request(
                StreamCommand("remove", identity=instance_id),
                timeout,
            )
        except StreamCommandIndeterminate:
            return self._indeterminate_removal_receipt(instance_id)
        return (
            result
            if isinstance(result, OwnerRemovalReceipt)
            else self._indeterminate_removal_receipt(instance_id)
        )

    def acknowledge_finality(
        self,
        instance_id: OwnerInstanceId,
        receipt: OwnerRemovalReceipt,
        durable_progress: FinalityDurableProgress,
        *,
        timeout: float | None = None,
    ) -> FinalityAckReceipt | None:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return self._dispatch_acknowledge_finality(
                instance_id, receipt, durable_progress
            )
        result = self._request(
            StreamCommand(
                "ack-finality",
                identity=instance_id,
                value=(receipt, durable_progress),
            ),
            timeout,
        )
        return result if isinstance(result, FinalityAckReceipt) else None

    def prune_finality(
        self,
        instance_id: OwnerInstanceId,
        ack_receipt: FinalityAckReceipt,
        *,
        timeout: float | None = None,
    ) -> FinalityPruneReceipt:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return self._dispatch_prune_finality(
                instance_id, ack_receipt
            )
        result = self._request(
            StreamCommand(
                "prune-finality",
                identity=instance_id,
                value=ack_receipt,
            ),
            timeout,
        )
        return result

    def finality_snapshot(self):
        return self._owner_finality.snapshot()

    def has_active_registrations_for_reconcile(self) -> bool:
        """Report physical-close eligibility, never exact owner finality."""

        with self._registration_guard:
            return bool(self._registrations)

    @property
    def stream_instance_id(self):
        return self._stream_instance_id

    def switch_owner_live(
        self,
        owner: object,
        identity: OwnerInstanceId,
        *,
        timeout: float | None = None,
    ) -> bool:
        return bool(
            self._request(
                StreamCommand("switch", owner=owner, identity=identity),
                timeout,
            )
        )

    def submit_owner_control(self, event: OwnerControlEvent) -> bool:
        if not isinstance(event, OwnerControlEvent):
            raise TypeError("event must be an OwnerControlEvent")
        command = (
            StreamCommand(
                "owner-control", owner=event.owner, value=event
            )
        )
        return self._command_admission.submit(
            command, self._commands, self._wake
        )

    def report_backend_failure(
        self,
        error: Exception,
        *,
        disconnected: bool = False,
        timeout: float | None = None,
    ) -> bool:
        return bool(
            self._request(
                StreamCommand(
                    "backend-failure", value=(error, disconnected)
                ),
                timeout,
            )
        )

    def check_deadlines(
        self, now_monotonic: float, *, timeout: float | None = None
    ) -> bool:
        return bool(
            self._request(
                StreamCommand("deadline", value=float(now_monotonic)),
                timeout,
            )
        )

    def synchronize(self, timeout: float | None = None) -> bool:
        return bool(self._request(StreamCommand("barrier"), timeout))

    def pause_ingest(self) -> None:
        self._request(StreamCommand("pause"), 1.0)

    def resume_ingest(self) -> None:
        self._request(StreamCommand("resume"), 1.0)

    def _request(
        self, command: StreamCommand, timeout: float | None
    ) -> object:
        command.ack = threading.Event()
        if not self._command_admission.submit(
            command, self._commands, self._wake
        ):
            return False
        if not command.ack.wait(timeout):
            if command.cancel_if_queued():
                return False
            if not command.ack.wait(timeout):
                raise StreamCommandIndeterminate(command)
        return command.result
__all__ = [
    "ResidentStreamControlMixin",
    "StreamCommand",
    "StreamCommandIndeterminate",
]
