from __future__ import annotations

from typing import final

from app.service.managed_input_contracts import (
    BackendDiscoveryResult,
    BackendInputScan,
    BackendScanId,
    InputEndpointObservation,
    ManagedInputError,
    ManagedInputErrorCode,
    RawBackendInputScan,
    RawManagedInputBackendPort,
    StableUidCapability,
    StableUidContract,
)


@final
class ManagedInputDiscoveryAdapter:
    """Stateless validation boundary around one raw backend."""

    def __init__(self, backend: RawManagedInputBackendPort) -> None:
        self._backend = backend

    @property
    def refresh_requires_reinitialize(self) -> bool:
        return self._backend.refresh_requires_reinitialize

    @staticmethod
    def _failure(
        code: ManagedInputErrorCode,
        summary: str,
    ) -> BackendDiscoveryResult:
        return BackendDiscoveryResult(
            None,
            ManagedInputError(code, "backend_scan", summary),
        )

    def discover(self, scan_id: BackendScanId) -> BackendDiscoveryResult:
        try:
            raw = self._backend.query_raw_input_endpoints(scan_id)
        except Exception:
            raw = None
        if type(raw) is not RawBackendInputScan:
            return self._failure(
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "The raw backend did not return a complete input scan.",
            )
        if raw.scan_id != scan_id or not raw.complete:
            return self._failure(
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "The raw backend input scan is incomplete or has the wrong identity.",
            )

        if (
            raw.stable_uid_capability is StableUidCapability.NONE
            and raw.stable_uid_contract is not StableUidContract.ABSENT
        ) or (
            raw.stable_uid_capability is StableUidCapability.SUPPORTED
            and raw.stable_uid_contract
            is not StableUidContract.REVIEWED_OPAQUE_UID
        ):
            return self._failure(
                ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                "The backend stable UID capability and contract conflict.",
            )

        hostapi_by_index = {
            hostapi.hostapi_index: hostapi.hostapi_name
            for hostapi in raw.hostapis
        }
        if len(hostapi_by_index) != len(raw.hostapis):
            return self._failure(
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "The raw backend returned duplicate Host API identities.",
            )

        observation_ids: set[str] = set()
        device_indexes: set[int] = set()
        stable_uids: set[str] = set()
        for item in raw.observations:
            if (
                item.observation_id in observation_ids
                or item.device_index in device_indexes
                or item.hostapi_index not in hostapi_by_index
                or hostapi_by_index[item.hostapi_index]
                != item.descriptor.hostapi_name
            ):
                return self._failure(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The raw backend returned conflicting endpoint identities.",
                )
            observation_ids.add(item.observation_id)
            device_indexes.add(item.device_index)

            uid = item.stable_backend_uid
            if (
                raw.stable_uid_capability is StableUidCapability.SUPPORTED
                and uid is None
            ) or (
                raw.stable_uid_capability is StableUidCapability.NONE
                and uid is not None
            ):
                return self._failure(
                    ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                    "The backend scan conflicts with its stable UID capability.",
                )
            if uid is not None:
                if uid in stable_uids:
                    return self._failure(
                        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                        "The backend scan contains a duplicate stable UID.",
                    )
                stable_uids.add(uid)

        return BackendDiscoveryResult(
            BackendInputScan(
                scan_id,
                raw.stable_uid_capability,
                raw.observations,
            ),
            None,
        )

    def requery_validated_input_endpoint(
        self,
        scan_id: BackendScanId,
        device_index: int,
    ) -> InputEndpointObservation | None:
        try:
            observation = self._backend.requery_raw_input_endpoint(
                scan_id,
                device_index,
            )
        except Exception:
            return None
        if (
            type(observation) is not InputEndpointObservation
            or observation.scan_id != scan_id
            or observation.device_index != device_index
        ):
            return None
        return observation
