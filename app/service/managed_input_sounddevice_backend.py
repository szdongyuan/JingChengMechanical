"""Narrow raw sounddevice discovery for managed production inputs."""

from __future__ import annotations

import threading
import time

from app.service.analysis.resident_audio_mvp.contracts import (
    AudioInputCallback,
    AudioInputFaultCallback,
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    ResidentStreamSpec,
)
from app.service.analysis.resident_audio_mvp import sounddevice_backend as _resident
from app.service.managed_input_contracts import (
    BackendScanId,
    InputEndpointDescriptor,
    InputEndpointObservation,
    RawBackendInputScan,
    RawHostApiIdentity,
    StableUidCapability,
    StableUidContract,
)


PYTHON_SYSTEM_MONOTONIC_DOMAIN = "python/system-monotonic"


class PythonSystemMonotonicAuthority:
    """Access the process-local Python/system monotonic clock domain."""

    monotonic_domain = PYTHON_SYSTEM_MONOTONIC_DOMAIN

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()


def validate_python_system_monotonic_authority(authority: object) -> None:
    if (
        getattr(authority, "monotonic_domain", None)
        != PYTHON_SYSTEM_MONOTONIC_DOMAIN
    ):
        raise ValueError(
            "monotonic authority must use the Python/system clock domain"
        )
    if not callable(getattr(authority, "monotonic", None)) or not callable(
        getattr(authority, "monotonic_ns", None)
    ):
        raise TypeError(
            "monotonic authority must provide monotonic() and monotonic_ns()"
        )


class _MonotonicAuthorityInputHandle(_resident._SoundDeviceInputHandle):
    def __init__(
        self,
        sounddevice_module: object,
        spec: ResidentStreamSpec,
        callback: AudioInputCallback,
        on_fault: AudioInputFaultCallback,
        monotonic_authority: object,
    ) -> None:
        super().__init__(sounddevice_module, spec, callback, on_fault)
        self._monotonic_authority = monotonic_authority
        self._portaudio_clock_offset_ns: int | None = None

    def _now_ns(self) -> int:
        value = self._monotonic_authority.monotonic_ns()
        if type(value) is not int or value < 0:
            raise ValueError("monotonic authority returned an invalid value")
        return value

    def start(self, deadline_monotonic_ns: int) -> None:
        if (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= self._now_ns()
        ):
            raise ValueError("start deadline must be a future integer")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("sounddevice input handle is single-use")
            thread = threading.Thread(
                name=f"resident-sounddevice-{self._spec.hardware_id}",
                target=self._control,
                daemon=True,
            )
            self._thread = thread
            thread.start()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - self._now_ns()) / 1_000_000_000,
        )
        if not self._start_done.wait(remaining):
            with self._lock:
                self._stop_requested = True
                self._close_requested = True
                self._wake.set()
            raise TimeoutError(
                f"sounddevice start deadline expired for {self._spec.hardware_id}"
            )
        with self._lock:
            error = self._start_error
        if error is not None:
            raise error

    def close(self, deadline_monotonic_ns: int) -> BackendCloseResult:
        if type(deadline_monotonic_ns) is not int:
            raise ValueError("close deadline must be an integer")
        with self._lock:
            if self._close_result is not None:
                return self._close_result
            if self._thread is None:
                self._close_result = BackendCloseResult(
                    BackendCloseStatus.CLOSED
                )
                return self._close_result
            self._stop_requested = True
            self._close_requested = True
            self._wake.set()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - self._now_ns()) / 1_000_000_000,
        )
        if not self._control_done.wait(remaining):
            return _resident._not_confirmed(
                f"sounddevice close {self._spec.hardware_id} not confirmed"
            )
        with self._lock:
            return self._close_result or _resident._not_confirmed(
                f"sounddevice close {self._spec.hardware_id} missing result"
            )

    def _native_callback(
        self,
        samples,
        frame_count: int,
        time_info: object,
        native_status: object,
    ) -> BackendCallbackDirective:
        if not self._callback_priority_applied:
            from app.service.analysis.thread_priority import (
                THREAD_PRIORITY_HIGHEST,
                set_current_thread_priority,
            )

            set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
            self._callback_priority_applied = True
        entry_ns = self._now_ns()
        callback_audio_end_ns = self._audio_block_end_ns(
            time_info,
            frame_count,
            entry_ns,
        )
        try:
            status = self._map_status(native_status)
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice status {_resident._safe_message(exc)}",
            )
            return BackendCallbackDirective.STOP
        try:
            directive = self._callback(
                samples,
                frame_count,
                callback_audio_end_ns,
                status,
            )
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"resident callback {_resident._safe_message(exc)}",
            )
            return BackendCallbackDirective.STOP
        if status is not BackendCallbackStatus.OK:
            try:
                status_summary = str(native_status)
            except Exception as exc:
                status_summary = f"status {_resident._safe_message(exc)}"
            self._report_fault(status, status_summary)
            return BackendCallbackDirective.STOP
        if type(directive) is not BackendCallbackDirective:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                "resident callback returned an invalid directive",
            )
            return BackendCallbackDirective.STOP
        return directive

    def _audio_block_end_ns(
        self,
        time_info: object,
        frame_count: int,
        entry_ns: int,
    ) -> int:
        """Map PortAudio's ADC clock to the application's monotonic clock."""

        try:
            current_time = float(getattr(time_info, "currentTime"))
            adc_start = float(getattr(time_info, "inputBufferAdcTime"))
            if self._portaudio_clock_offset_ns is None:
                self._portaudio_clock_offset_ns = entry_ns - round(
                    current_time * 1_000_000_000
                )
            adc_end_ns = (
                round(
                    (
                        adc_start
                        + frame_count / float(self._spec.sample_rate_hz)
                    )
                    * 1_000_000_000
                )
                + self._portaudio_clock_offset_ns
            )
            return min(entry_ns, max(0, adc_end_ns))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return entry_ns

class MonotonicSoundDeviceAudioBackend(_resident.SoundDeviceAudioBackend):
    """Existing stream lifecycle in the Python/system monotonic domain."""

    def __init__(
        self,
        sounddevice_module: object | None,
        monotonic_authority: object,
    ) -> None:
        validate_python_system_monotonic_authority(monotonic_authority)
        super().__init__(sounddevice_module)
        self._monotonic_authority = monotonic_authority

    @property
    def monotonic_domain(self) -> str:
        return PYTHON_SYSTEM_MONOTONIC_DOMAIN

    def create_input(
        self,
        spec: ResidentStreamSpec,
        callback: AudioInputCallback,
        on_fault: AudioInputFaultCallback,
    ) -> _MonotonicAuthorityInputHandle:
        if type(spec) is not ResidentStreamSpec:
            raise TypeError("spec must be an exact ResidentStreamSpec")
        if not callable(callback) or not callable(on_fault):
            raise TypeError("callback and on_fault must be callable")
        return _MonotonicAuthorityInputHandle(
            self._sounddevice,
            spec,
            callback,
            on_fault,
            self._monotonic_authority,
        )


class ManagedInputSoundDeviceBackend:
    """Convert one sounddevice enumeration into immutable raw contracts."""

    def __init__(self, sounddevice_module: object | None = None) -> None:
        if sounddevice_module is None:
            import sounddevice as sounddevice_module
        if not callable(getattr(sounddevice_module, "query_devices", None)):
            raise TypeError("sounddevice module must provide query_devices")
        if not callable(getattr(sounddevice_module, "query_hostapis", None)):
            raise TypeError("sounddevice module must provide query_hostapis")
        self._sounddevice = sounddevice_module
        self._last_scan: RawBackendInputScan | None = None

    @property
    def refresh_requires_reinitialize(self) -> bool:
        return False

    def query_raw_input_endpoints(
        self,
        scan_id: BackendScanId,
    ) -> RawBackendInputScan:
        if type(scan_id) is not BackendScanId:
            raise TypeError("scan_id must be an exact BackendScanId")
        devices = tuple(self._sounddevice.query_devices())
        hostapis_raw = tuple(self._sounddevice.query_hostapis())
        hostapis = tuple(
            RawHostApiIdentity(index, str(item["name"]))
            for index, item in enumerate(hostapis_raw)
        )
        hostapi_names = {
            item.hostapi_index: item.hostapi_name for item in hostapis
        }
        observations: list[InputEndpointObservation] = []
        for device_index, item in enumerate(devices):
            input_channels = int(item["max_input_channels"])
            if input_channels <= 0:
                continue
            hostapi_index = int(item["hostapi"])
            hostapi_name = hostapi_names[hostapi_index]
            descriptor = InputEndpointDescriptor(
                str(item["name"]),
                hostapi_name,
                float(item["default_samplerate"]),
                input_channels,
            )
            observations.append(
                InputEndpointObservation(
                    scan_id=scan_id,
                    observation_id=(
                        f"{scan_id.process_instance_id}:"
                        f"{scan_id.sequence}:{device_index}"
                    ),
                    device_index=device_index,
                    hostapi_index=hostapi_index,
                    stable_backend_uid=None,
                    descriptor=descriptor,
                )
            )
        scan = RawBackendInputScan(
            scan_id=scan_id,
            complete=True,
            stable_uid_capability=StableUidCapability.NONE,
            stable_uid_contract=StableUidContract.ABSENT,
            hostapis=hostapis,
            observations=tuple(observations),
        )
        self._last_scan = scan
        return scan

    def requery_raw_input_endpoint(
        self,
        scan_id: BackendScanId,
        device_index: int,
    ) -> InputEndpointObservation | None:
        scan = self._last_scan
        if scan is None or scan.scan_id != scan_id:
            return None
        matches = tuple(
            item
            for item in scan.observations
            if item.device_index == device_index
        )
        return matches[0] if len(matches) == 1 else None


__all__ = [
    "ManagedInputSoundDeviceBackend",
    "MonotonicSoundDeviceAudioBackend",
    "PYTHON_SYSTEM_MONOTONIC_DOMAIN",
    "PythonSystemMonotonicAuthority",
    "validate_python_system_monotonic_authority",
]
