from __future__ import annotations

import pytest

from app.service.analysis.resident_audio_mvp import DiagnosticReason
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    InputEndpointDescriptor,
    InputEndpointObservation,
    ManagedHardwareId,
    RawBackendInputScan,
    RawHostApiIdentity,
    StableUidCapability,
    StableUidContract,
)
from app.service.managed_input_endpoint_resolver import (
    EndpointResolutionError,
    ManagedInputEndpointResolver,
)


def identity(*, uid: str | None = None) -> HardwareEndpointIdentity:
    return HardwareEndpointIdentity(
        hardware_id=ManagedHardwareId(1),
        scan_id=BackendScanId("resolver-test", 4),
        device_index=2,
        hostapi_index=0,
        stable_backend_uid=uid,
        device_name="Exact Microphone",
        hostapi_name="WASAPI",
        sample_rate_hz=48_000.0,
        input_channels=2,
    )


def observation(
    scan_id: BackendScanId,
    *,
    index: int,
    uid: str | None = None,
    name: str = "Exact Microphone",
    hostapi: str = "WASAPI",
    rate: float = 48_000.0,
    channels: int = 2,
) -> InputEndpointObservation:
    return InputEndpointObservation(
        scan_id=scan_id,
        observation_id=f"{scan_id.process_instance_id}:{scan_id.sequence}:{index}",
        device_index=index,
        hostapi_index=0,
        stable_backend_uid=uid,
        descriptor=InputEndpointDescriptor(name, hostapi, rate, channels),
    )


class Scans:
    def __init__(self, rows: list[tuple[dict[str, object], ...]]) -> None:
        self.rows = rows
        self.calls: list[BackendScanId] = []

    def query_raw_input_endpoints(self, scan_id: BackendScanId) -> RawBackendInputScan:
        self.calls.append(scan_id)
        source = self.rows.pop(0)
        observations = tuple(observation(scan_id, **row) for row in source)
        has_uid = any(item.stable_backend_uid is not None for item in observations)
        return RawBackendInputScan(
            scan_id=scan_id,
            complete=True,
            stable_uid_capability=(
                StableUidCapability.SUPPORTED if has_uid else StableUidCapability.NONE
            ),
            stable_uid_contract=(
                StableUidContract.REVIEWED_OPAQUE_UID
                if has_uid
                else StableUidContract.ABSENT
            ),
            hostapis=(RawHostApiIdentity(0, "WASAPI"),),
            observations=observations,
        )


def test_resolver_reenumerates_and_uses_fresh_index_without_mutating_identity() -> None:
    frozen = identity()
    scans = Scans([({"index": 9},), ({"index": 11},)])
    resolver = ManagedInputEndpointResolver(scans)

    first = resolver.resolve(frozen)
    second = resolver.resolve(frozen)

    assert first.device_index == 9
    assert second.device_index == 11
    assert frozen.device_index == 2
    assert scans.calls == [
        BackendScanId("resolver-test", 5),
        BackendScanId("resolver-test", 6),
    ]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ((), DiagnosticReason.ENDPOINT_MISSING),
        (
            ({"index": 3}, {"index": 4}),
            DiagnosticReason.ENDPOINT_AMBIGUOUS,
        ),
        (
            ({"index": 3, "name": "Similar Microphone"},),
            DiagnosticReason.ENDPOINT_MISSING,
        ),
    ],
)
def test_tuple_resolution_rejects_missing_ambiguous_and_name_only_matches(
    rows: tuple[dict[str, object], ...], reason: DiagnosticReason
) -> None:
    resolver = ManagedInputEndpointResolver(Scans([rows]))

    with pytest.raises(EndpointResolutionError) as caught:
        resolver.resolve(identity())

    assert caught.value.reason_code is reason


def test_uid_resolution_requires_unique_uid_and_compatible_descriptor() -> None:
    frozen = identity(uid="stable-1")
    mismatch = ManagedInputEndpointResolver(
        Scans([({"index": 8, "uid": "stable-1", "rate": 44_100.0},)])
    )
    with pytest.raises(EndpointResolutionError) as caught:
        mismatch.resolve(frozen)
    assert caught.value.reason_code is DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH

    exact = ManagedInputEndpointResolver(
        Scans([({"index": 12, "uid": "stable-1"},)])
    ).resolve(frozen)
    assert exact.device_index == 12
    assert exact.stable_backend_uid == "stable-1"
