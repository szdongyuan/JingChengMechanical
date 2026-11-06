"""Value-only authoritative facts for resident artifact settlement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import ArtifactState


class ArtifactDisposition(str, Enum):
    INCOMPLETE = "INCOMPLETE"
    DELETED = "DELETED"
    CLAIMED = "CLAIMED"
    TRANSFERRED = "TRANSFERRED"
    RELEASED = "RELEASED"


class ArtifactQuotaOutcome(str, Enum):
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"
    RETAINED = "RETAINED"


@dataclass(frozen=True, slots=True)
class ArtifactSettlementReceipt:
    artifact_id: str
    publication_resolved: bool
    disposition: ArtifactDisposition
    quota_outcome: ArtifactQuotaOutcome

    @property
    def disposition_confirmed(self) -> bool:
        return self.disposition is not ArtifactDisposition.INCOMPLETE

    @property
    def quota_outcome_confirmed(self) -> bool:
        return self.quota_outcome is not ArtifactQuotaOutcome.UNKNOWN

    @property
    def complete(self) -> bool:
        return (
            self.publication_resolved
            and self.disposition_confirmed
            and self.quota_outcome_confirmed
        )


@dataclass(frozen=True, slots=True)
class CompletionSettlementReceipt:
    artifact: ArtifactSettlementReceipt
    terminal_state: str
    error_code: str | None = None
    failure_reason: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionInvocationResult:
    handler_returned: bool
    resolution: str | None
    exception_type: str | None


class ArtifactSettlementFacade:
    """Inspect and advance only through public artifact-store operations."""

    def __init__(self, store) -> None:
        self._store = store

    @staticmethod
    def unavailable_completion_result() -> CompletionInvocationResult:
        return CompletionInvocationResult(
            handler_returned=False,
            resolution=None,
            exception_type="RuntimeError",
        )

    def inspect(self, artifact_id: str) -> ArtifactSettlementReceipt:
        state = self._store.state(artifact_id)
        pending = self._store.publication_pending(artifact_id)
        if state is ArtifactState.CLAIMED and pending is False:
            return self._receipt(
                artifact_id,
                ArtifactDisposition.CLAIMED,
                ArtifactQuotaOutcome.RETAINED,
            )
        if state is ArtifactState.TRANSFERRED and pending is False:
            return self._receipt(
                artifact_id,
                ArtifactDisposition.TRANSFERRED,
                ArtifactQuotaOutcome.RETAINED,
            )
        if state is ArtifactState.DELETED and pending is False:
            return self._receipt(
                artifact_id,
                ArtifactDisposition.DELETED,
                ArtifactQuotaOutcome.RELEASED,
            )
        if state is ArtifactState.RELEASED and pending is False:
            return self._receipt(
                artifact_id,
                ArtifactDisposition.RELEASED,
                ArtifactQuotaOutcome.RELEASED,
            )
        if state is ArtifactState.DELETE_PENDING and pending is False:
            return ArtifactSettlementReceipt(
                artifact_id=artifact_id,
                publication_resolved=True,
                disposition=ArtifactDisposition.INCOMPLETE,
                quota_outcome=ArtifactQuotaOutcome.UNKNOWN,
            )
        return ArtifactSettlementReceipt(
            artifact_id=artifact_id,
            publication_resolved=False,
            disposition=ArtifactDisposition.INCOMPLETE,
            quota_outcome=ArtifactQuotaOutcome.UNKNOWN,
        )

    def decline(
        self,
        artifact_id: str,
        publication_token: str,
        reason: str,
    ) -> ArtifactSettlementReceipt:
        self._store.decline_publication(
            artifact_id, publication_token, reason
        )
        return self._inspect_after_delete_retry(artifact_id)

    def discard(self, artifact_id: str) -> ArtifactSettlementReceipt:
        self._store.discard_partial(artifact_id)
        return self._inspect_after_delete_retry(artifact_id)

    def settle_non_success(
        self,
        artifact_id: str,
        publication_token: str | None,
    ) -> ArtifactSettlementReceipt:
        state = self._store.state(artifact_id)
        pending = self._store.publication_pending(artifact_id)
        if pending:
            if not publication_token:
                return self.inspect(artifact_id)
            return self.decline(
                artifact_id,
                publication_token,
                "resident settlement declined publication",
            )
        if state is ArtifactState.PARTIAL:
            return self.discard(artifact_id)
        return self._inspect_after_delete_retry(artifact_id)

    @staticmethod
    def invoke_completion_handler(
        handler, completion
    ) -> CompletionInvocationResult:
        exception_type = None
        handler_returned = False
        try:
            if handler is None:
                raise RuntimeError("completion handler is unavailable")
            handler(completion)
            handler_returned = True
        except Exception as error:
            exception_type = type(error).__name__[:256]
        finally:
            error = None
            handler = None
        return CompletionInvocationResult(
            handler_returned=handler_returned,
            resolution=completion.resolution,
            exception_type=exception_type,
        )

    def classify_completion(
        self,
        artifact,
        *,
        handler_returned: bool,
        exception_type: str | None,
    ) -> CompletionSettlementReceipt:
        receipt = self.inspect(artifact.artifact_id)
        if receipt.publication_resolved:
            return CompletionSettlementReceipt(
                artifact=receipt,
                terminal_state="RELEASED",
                error_code=None,
                failure_reason=(
                    "completion_handler_raised_after_resolution"
                    if exception_type is not None
                    else None
                ),
                exception_type=exception_type,
            )
        return CompletionSettlementReceipt(
            artifact=receipt,
            terminal_state="FAILED",
            error_code="CAPTURE_ARTIFACT_IO_FAILED",
            failure_reason=(
                "completion_handler_left_publication_pending"
                if handler_returned
                else "completion_handler_raised"
            ),
            exception_type=exception_type,
        )

    def settle_completion(
        self,
        artifact,
        *,
        prior_terminal_state: str | None = None,
        prior_error_code: str | None = None,
        prior_failure_reason: str | None = None,
        prior_exception_type: str | None = None,
    ) -> CompletionSettlementReceipt:
        exception_type = prior_exception_type
        receipt = self._inspect_after_delete_retry(
            artifact.artifact_id
        )
        if receipt.publication_resolved:
            return CompletionSettlementReceipt(
                artifact=receipt,
                terminal_state=prior_terminal_state or "RELEASED",
                error_code=prior_error_code,
                failure_reason=(
                    prior_failure_reason
                    or (
                        "completion_handler_raised_after_resolution"
                        if exception_type is not None
                        else None
                    )
                ),
                exception_type=exception_type,
            )
        reason = (
            prior_failure_reason
            or (
                "completion_handler_raised"
                if exception_type is not None
                else "completion_handler_left_publication_pending"
            )
        )
        receipt = self.decline(
            artifact.artifact_id,
            artifact.publication_token,
            reason,
        )
        return CompletionSettlementReceipt(
            artifact=receipt,
            terminal_state=prior_terminal_state or "FAILED",
            error_code=prior_error_code or "CAPTURE_ARTIFACT_IO_FAILED",
            failure_reason=reason,
            exception_type=exception_type,
        )

    def _inspect_after_delete_retry(
        self, artifact_id: str
    ) -> ArtifactSettlementReceipt:
        receipt = self.inspect(artifact_id)
        if self._store.state(artifact_id) is ArtifactState.DELETE_PENDING:
            retry = getattr(
                self._store, "retry_delete_pending_artifact", None
            )
            if callable(retry):
                retry(artifact_id)
            receipt = self.inspect(artifact_id)
        return receipt

    @staticmethod
    def _receipt(
        artifact_id: str,
        disposition: ArtifactDisposition,
        quota: ArtifactQuotaOutcome,
    ) -> ArtifactSettlementReceipt:
        return ArtifactSettlementReceipt(
            artifact_id=artifact_id,
            publication_resolved=True,
            disposition=disposition,
            quota_outcome=quota,
        )


__all__ = [
    "ArtifactDisposition",
    "ArtifactQuotaOutcome",
    "ArtifactSettlementFacade",
    "ArtifactSettlementReceipt",
    "CompletionSettlementReceipt",
    "CompletionInvocationResult",
]
