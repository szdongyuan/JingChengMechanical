"""Narrow raw sounddevice discovery for managed production inputs."""

from __future__ import annotations

import math
import os
import threading
import time

from app.service.analysis.resident_audio_mvp.contracts import (
    AudioInputCallback,
    AudioInputFaultCallback,
    BackendCallbackDirective,
    BackendCallbackStatus,
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
CALLBACK_TIMESTAMP_JITTER_TOLERANCE_NS = 20_000_000
ADC_CONTINUITY_WINDOW_NS = 10_000_000_000


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
        self._last_callback_audio_end_ns: int | None = None
        self._audio_origin_end_ns: int | None = None
        self._audio_frames_since_origin = 0
        self._raw_adc_anchor_end_ns: int | None = None
        self._raw_adc_frames_since_anchor = 0
        self._native_input_latency = 0.0

    def _create_stream(self, **kwargs):
        if os.name != 'nt' or getattr(self._sounddevice, '__name__', None) != 'sounddevice':
            return super()._create_stream(**kwargs)
        from app.service.native_capture_stream import NativeCaptureStream
        return NativeCaptureStream(self._sounddevice, self._spec,
            self._sounddevice_callback, self._report_fault,
            getattr(self._callback, 'native_ready', lambda: True),
            self._set_native_latency)

    def _set_native_latency(self, latency):
        if not math.isfinite(latency) or latency < 0:
            raise ValueError('Native input latency must be finite and nonnegative')
        self._native_input_latency = latency

    def _open_stream(self, native):
        from app.service.native_capture_stream import NativeCaptureStream
        if isinstance(native, NativeCaptureStream):
            native.open_native()

    def native_producer_snapshot(self):
        native = self._native
        snapshot = getattr(native, 'producer_snapshot', None)
        return snapshot() if snapshot is not None else None

    def _now_ns(self) -> int:
        value = self._monotonic_authority.monotonic_ns()
        if type(value) is not int or value < 0:
            raise ValueError("monotonic authority returned an invalid value")
        return value

    def _uses_native_frame_timeline(self, time_info: object) -> bool:
        # These hosts report estimated ADC times, not sample-loss evidence.
        hostapi = self._spec.hostapi_name.lower()
        return hasattr(time_info, 'native_entry_ns') and (
            'wasapi' in hostapi or 'directsound' in hostapi
        )

    def _native_callback(
        self,
        samples,
        frame_count: int,
        time_info: object,
        native_status: object,
    ) -> BackendCallbackDirective:
        if not self._callback_priority_applied and not hasattr(time_info, 'native_entry_ns'):
            from app.service.analysis.thread_priority import (
                THREAD_PRIORITY_HIGHEST,
                set_current_thread_priority,
            )

            set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
            self._callback_priority_applied = True
        entry_ns = getattr(time_info, 'native_entry_ns', None)
        if entry_ns is None:
            entry_ns = self._now_ns()
        elif not self._uses_native_frame_timeline(time_info):
            entry_ns = max(0, entry_ns - time_info.clock_uncertainty_ns)
        try:
            callback_audio_end_ns = self._audio_block_end_ns(
                time_info, frame_count, entry_ns,
            )
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"audio timing hardware={self._spec.hardware_id} "
                f"entry_ns={entry_ns} frames={frame_count} "
                f"{_resident._safe_message(exc)}",
            )
            return BackendCallbackDirective.STOP
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
        """Validate raw capture time, then map frames onto one monotonic timeline.

        Native WASAPI/DirectSound use entry, reported latency and exact frames;
        their estimated ADC time cannot establish loss. For other paths,
        ADC/currentTime establish the first origin only. Missing driver timing
        uses frame continuity and overflow protection: it cannot prove hardware
        losslessness. In particular it never switches back to delivery time.
        The raw anchor spans ten seconds to catch accumulating skips while
        allowing small long-term clock skew; no individual error may exceed
        max(20 ms, two current blocks). Neither path inserts or drops samples.
        """
        block_duration_ns = round(frame_count * 1_000_000_000 / self._spec.sample_rate_hz)
        if self._uses_native_frame_timeline(time_info):
            if self._audio_origin_end_ns is None:
                uncertainty = time_info.clock_uncertainty_ns
                self._audio_origin_end_ns = max(0, min(entry_ns,
                    entry_ns - round(self._native_input_latency * 1_000_000_000)
                    + block_duration_ns) - uncertainty)
                candidate_end_ns = self._audio_origin_end_ns
            else:
                self._audio_frames_since_origin += frame_count
                candidate_end_ns = self._audio_origin_end_ns + round(
                    self._audio_frames_since_origin * 1_000_000_000 / self._spec.sample_rate_hz)
                # Keep the fixed clock-domain margin even after correction.
                ceiling = entry_ns - time_info.clock_uncertainty_ns
                if candidate_end_ns > ceiling:
                    corrected = max(self._last_callback_audio_end_ns, ceiling)
                    self._audio_origin_end_ns += corrected - candidate_end_ns
                    candidate_end_ns = corrected
            self._last_callback_audio_end_ns = candidate_end_ns
            return candidate_end_ns
        tolerance_ns = max(CALLBACK_TIMESTAMP_JITTER_TOLERANCE_NS, 2 * block_duration_ns)
        raw_adc_end_ns = None
        refresh_mapping = False
        try:
            adc_start = float(getattr(time_info, "inputBufferAdcTime"))
            if math.isfinite(adc_start) and adc_start > 0:
                raw_adc_end_ns = round(adc_start * 1_000_000_000) + block_duration_ns
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass

        if self._raw_adc_anchor_end_ns is not None:
            self._raw_adc_frames_since_anchor += frame_count
            elapsed_ns = round(self._raw_adc_frames_since_anchor * 1_000_000_000
                               / self._spec.sample_rate_hz)
            expected_raw_end_ns = self._raw_adc_anchor_end_ns + elapsed_ns
            if raw_adc_end_ns is not None:
                error_ns = raw_adc_end_ns - expected_raw_end_ns
                if abs(error_ns) > tolerance_ns:
                    raise ValueError(
                        f"ADC discontinuity raw_end_ns={raw_adc_end_ns} "
                        f"expected_end_ns={expected_raw_end_ns} delta_ns={error_ns} "
                        f"tolerance_ns={tolerance_ns}"
                    )
                if elapsed_ns >= ADC_CONTINUITY_WINDOW_NS:
                    self._raw_adc_anchor_end_ns = raw_adc_end_ns
                    self._raw_adc_frames_since_anchor = 0
                    refresh_mapping = True
        elif raw_adc_end_ns is not None:
            self._raw_adc_anchor_end_ns = raw_adc_end_ns
            self._raw_adc_frames_since_anchor = 0

        if self._audio_origin_end_ns is None:
            candidate_end_ns = entry_ns
            if raw_adc_end_ns is not None:
                try:
                    current_time = float(getattr(time_info, "currentTime"))
                    if math.isfinite(current_time) and current_time > 0:
                        self._portaudio_clock_offset_ns = entry_ns - round(current_time * 1_000_000_000)
                        candidate_end_ns = min(entry_ns, max(0, raw_adc_end_ns + self._portaudio_clock_offset_ns))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    pass
            self._audio_origin_end_ns = candidate_end_ns
        else:
            self._audio_frames_since_origin += frame_count
            candidate_end_ns = self._audio_origin_end_ns + round(
                self._audio_frames_since_origin * 1_000_000_000 / self._spec.sample_rate_hz
            )
            if refresh_mapping and self._portaudio_clock_offset_ns is not None:
                # Correct only validated clock skew, keeping delivery jitter out
                # of the mapping and never moving a block end backwards.
                corrected_end_ns = max(
                    self._last_callback_audio_end_ns + 1,
                    raw_adc_end_ns + self._portaudio_clock_offset_ns,
                )
                self._audio_origin_end_ns += corrected_end_ns - candidate_end_ns
                candidate_end_ns = corrected_end_ns
            if candidate_end_ns > entry_ns:
                # The Resident ingress rejects future timestamps. A driver
                # running slightly faster than its nominal sample rate can
                # deliver a contiguous block just before its predicted end.
                self._audio_origin_end_ns += entry_ns - candidate_end_ns
                candidate_end_ns = entry_ns
        self._last_callback_audio_end_ns = candidate_end_ns
        return candidate_end_ns

    def _before_native_restart(self) -> None:
        self._portaudio_clock_offset_ns = None
        self._last_callback_audio_end_ns = None
        self._audio_origin_end_ns = None
        self._audio_frames_since_origin = 0
        self._raw_adc_anchor_end_ns = None
        self._raw_adc_frames_since_anchor = 0


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
        self._scan_lock = threading.Lock()
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
        with self._scan_lock:
            return self._query_raw_input_endpoints_locked(scan_id)

    def _query_raw_input_endpoints_locked(
        self,
        scan_id: BackendScanId,
    ) -> RawBackendInputScan:
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
        with self._scan_lock:
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
