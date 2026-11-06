"""Explicit value-only codec. Resident tokens, futures and PCM cannot enter RPC."""
from dataclasses import fields
from enum import Enum
import json

MAX_MESSAGE_BYTES = 65536


def _types():
    from app.service import managed_input_contracts as m
    from app.service.analysis.resident_audio_mvp import contracts as c
    from app.service.analysis.resident_audio_mvp import diagnostics as d
    return {t.__name__: t for t in (
        m.ManagedHardwareId, m.BackendScanId, m.HardwareEndpointIdentity,
        m.ResidentStreamGenerationStamp, m.ResidentReadyStream, m.ResidentOpenFailure,
        m.ResidentStartupOpenResult, m.ResidentCalibrationMeasurement,
        m.OfflineMaintenanceBlockerSnapshot, m.ManagedInputCloseResult,
        m.ManagedInputCloseStatus, m.ManagedInputError, m.ManagedInputErrorCode,
        c.ResidentError, c.ResidentErrorCode, c.LifecycleOutcome, c.LifecycleStatus,
        d.ResidentDiagnostic, d.DiagnosticStage, d.DiagnosticReason,
    )}


def encode(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) in (tuple, list):
        return [encode(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("wire keys must be strings")
        return {key: encode(item) for key, item in value.items()}
    allowed = _types()
    if allowed.get(type(value).__name__) is not type(value):
        raise TypeError("not an allowed capture-process value")
    if isinstance(value, Enum):
        return {"$type": type(value).__name__, "value": value.value}
    return {"$type": type(value).__name__,
            "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}


def decode(value):
    if type(value) is list:
        return tuple(decode(item) for item in value)
    if type(value) is not dict:
        return value
    if "$type" not in value:
        return {key: decode(item) for key, item in value.items()}
    cls = _types()[value["$type"]]
    if issubclass(cls, Enum):
        return cls(value["value"])
    return cls(**{key: decode(item) for key, item in value["fields"].items()})


def pack(value):
    data = json.dumps(encode(value), allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("capture control message exceeds bounds")
    return data


def unpack(data):
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("capture control message exceeds bounds")
    return decode(json.loads(data))
