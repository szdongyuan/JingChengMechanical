"""Exact value-identity guards and finality dispatch for resident streams."""

from __future__ import annotations

from .contracts import RawCallbackBlock
from .owner_finality import FinalityDurableProgress
from .owner_identity import (
    FinalityAckReceipt,
    OwnerInstanceId,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
)
from .ring import TimestampedBlockRing
from .timeline import StreamTimeline


class ResidentStreamIdentityMixin:
    def _identity_matches(
        self,
        owner: object | None,
        identity: OwnerInstanceId | None,
    ) -> bool:
        if owner is None or not isinstance(identity, OwnerInstanceId):
            return False
        return (
            identity.pool_epoch == self._pool_epoch
            and identity.hardware_id == self._resolved.hardware_id
            and identity.stream_generation == self._generation_number
            and getattr(owner, "run_id", None) == identity.run_id
            and getattr(owner, "commit_attempt_id", None)
            == identity.commit_attempt_id
            and getattr(owner, "owner_capability", None)
            == identity.owner_capability
            and getattr(owner, "pool_epoch", None) == identity.pool_epoch
            and getattr(owner, "stream_generation", None)
            == identity.stream_generation
        )

    def _find(self, identity: OwnerInstanceId | None):
        if not isinstance(identity, OwnerInstanceId):
            return None
        registration = self._registrations.get(identity)
        if registration is None or registration.identity != identity:
            return None
        return registration

    def _begin_owner_removal(self, identity: OwnerInstanceId):
        if not isinstance(identity, OwnerInstanceId):
            return OwnerRemovalReceipt(identity, OwnerRemovalOutcome.MISMATCH)
        with self._registration_guard:
            registration_present = self._find(identity) is not None
        if registration_present:
            return None
        return self._owner_finality.request_removal(
            identity, registration_present=False
        )

    def _remove_owner_receipt(self, identity: OwnerInstanceId):
        if not isinstance(identity, OwnerInstanceId):
            return OwnerRemovalReceipt(identity, OwnerRemovalOutcome.MISMATCH)
        with self._registration_guard:
            registration = self._find(identity)
            if registration is not None:
                self._registrations.pop(identity, None)
        return self._owner_finality.request_removal(
            identity, registration_present=registration is not None
        )

    def _retire_registration(self, registration):
        with self._registration_guard:
            current = self._registrations.get(registration.identity)
            present = current is registration
            if present:
                self._registrations.pop(registration.identity, None)
        return self._owner_finality.request_removal(
            registration.identity, registration_present=present
        )

    @staticmethod
    def _indeterminate_removal_receipt(identity: OwnerInstanceId):
        return OwnerRemovalReceipt(
            identity, OwnerRemovalOutcome.INDETERMINATE
        )

    def _dispatch_acknowledge_finality(
        self,
        identity: OwnerInstanceId,
        receipt: OwnerRemovalReceipt,
        durable_progress: FinalityDurableProgress,
    ):
        return self._owner_finality._acknowledge_entry(
            identity, receipt, durable_progress
        )

    def _dispatch_prune_finality(
        self,
        identity: OwnerInstanceId,
        ack_receipt: FinalityAckReceipt,
    ):
        return self._owner_finality._prune_entry(identity, ack_receipt)

    def _raw_identity_matches(self, raw: RawCallbackBlock) -> bool:
        return (
            raw.pool_epoch == self._pool_epoch
            and raw.hardware_id == self._resolved.hardware_id
            and raw.stream_generation == self._generation_number
        )

    def _require_timeline(self) -> StreamTimeline:
        if self._timeline is None:
            raise RuntimeError("stream timeline is unavailable")
        return self._timeline

    def _require_ring(self) -> TimestampedBlockRing:
        if self._ring is None:
            raise RuntimeError("stream ring is unavailable")
        return self._ring


__all__ = ["ResidentStreamIdentityMixin"]
