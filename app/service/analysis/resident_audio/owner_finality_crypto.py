"""Stream-local authentication for owner finality acknowledgement receipts."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from . import owner_finality_canonical as canonical
from .owner_identity import (
    FinalityAckReceipt,
    OwnerInstanceId,
    StreamInstanceId,
    _require_positive_ledger_version,
)
from .owner_finality_validation import (
    validate_stream_instance_id,
)


_ACK_DOMAIN = b"resident-audio-owner-finality-ack-v1\x00"
_MAX_ACK_TOKEN_LENGTH = 256


class OwnerFinalityAuthenticator:
    """Issues and statelessly verifies stream-bound acknowledgement values."""

    def __init__(self, stream_instance_id: StreamInstanceId) -> None:
        self._stream_instance_id = canonical.canonical_stream_instance_id(
            stream_instance_id
        )
        self._secret = secrets.token_bytes(32)

    def issue(
        self, instance_id: OwnerInstanceId, ledger_version: int
    ) -> FinalityAckReceipt:
        self._validate_self()
        instance_id = self._validate_token_inputs(
            instance_id, ledger_version
        )
        return FinalityAckReceipt(
            stream_instance_id=self._stream_instance_id,
            owner_instance_id=instance_id,
            ledger_version=ledger_version,
            ack_token=self._token(instance_id, ledger_version),
        )

    def validates(
        self,
        instance_id: OwnerInstanceId,
        receipt: FinalityAckReceipt | object,
        expected_version: int | None,
    ) -> bool:
        try:
            self._validate_self()
            instance_id = canonical.canonical_owner_instance_id(instance_id)
            receipt = canonical.canonical_finality_ack_receipt(receipt)
            if expected_version is not None:
                _require_positive_ledger_version(
                    expected_version,
                    owner=OwnerFinalityAuthenticator.__name__,
                )
        except (TypeError, ValueError):
            return False
        if (
            receipt.stream_instance_id != self._stream_instance_id
            or receipt.owner_instance_id != instance_id
            or (
                expected_version is not None
                and receipt.ledger_version != expected_version
            )
        ):
            return False
        expected_token = self._token(instance_id, receipt.ledger_version)
        return hmac.compare_digest(receipt.ack_token, expected_token)

    def _token(
        self, instance_id: OwnerInstanceId, ledger_version: int
    ) -> str:
        self._validate_self()
        instance_id = self._validate_token_inputs(
            instance_id, ledger_version
        )
        payload = bytearray(_ACK_DOMAIN)
        for value in (
            self._stream_instance_id.pool_epoch,
            self._stream_instance_id.hardware_id,
            self._stream_instance_id.stream_generation,
            self._stream_instance_id.instance_nonce,
            instance_id.pool_epoch,
            instance_id.hardware_id,
            instance_id.stream_generation,
            instance_id.run_id,
            instance_id.commit_attempt_id,
            instance_id.owner_capability,
            instance_id.instance_nonce,
            ledger_version,
        ):
            self._append_value(payload, value)
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_token_inputs(
        instance_id: OwnerInstanceId, ledger_version: int
    ) -> OwnerInstanceId:
        instance_id = canonical.canonical_owner_instance_id(instance_id)
        _require_positive_ledger_version(
            ledger_version, owner=OwnerFinalityAuthenticator.__name__
        )
        return instance_id

    def _validate_self(self) -> None:
        try:
            stream_instance_id = object.__getattribute__(
                self, "_stream_instance_id"
            )
            secret = object.__getattribute__(self, "_secret")
        except AttributeError:
            raise TypeError("authenticator is not initialized") from None
        validate_stream_instance_id(stream_instance_id)
        if type(secret) is not bytes or len(secret) != 32:
            raise TypeError("authenticator secret is malformed")

    @staticmethod
    def _append_value(payload: bytearray, value: object) -> None:
        if type(value) is int:
            discriminator = b"I"
        elif type(value) is str:
            discriminator = b"S"
        else:
            raise TypeError("HMAC identity values must be strings or integers")
        encoded = str(value).encode("utf-8")
        payload.extend(discriminator)
        payload.extend(len(encoded).to_bytes(8, "big"))
        payload.extend(encoded)


__all__ = ["OwnerFinalityAuthenticator"]
