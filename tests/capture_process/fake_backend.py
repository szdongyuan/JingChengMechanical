"""Explicit spawn-safe synthetic backend; never opens physical audio hardware."""
import threading
import time


def endpoint(number=1):
    from app.service.managed_input_contracts import (
        BackendScanId, HardwareEndpointIdentity, ManagedHardwareId,
    )
    return HardwareEndpointIdentity(
        ManagedHardwareId(number), BackendScanId("capture-process-test", 1),
        number, 0, f"fake-{number}", f"mic-{number}", "fake-api", 1000.0, 2,
    )


def create_backend(endpoints, options):
    if options.get("factory_failure"):
        raise RuntimeError("explicit synthetic startup failure")
    if options.get("delay_start_reply_s"):
        from app.service.capture_process.worker import _Worker
        original = _Worker.dispatch
        def delayed(self, op, args):
            result = original(self, op, args)
            if op == "start":
                time.sleep(options["delay_start_reply_s"])
            return result
        _Worker.dispatch = delayed
    if options.get("delay_publish_s"):
        from app.service.capture_process.transport import CompletedAudioStore
        original_publish = CompletedAudioStore.publish
        def delayed_publish(self, reservation, pcm):
            time.sleep(options["delay_publish_s"])
            return original_publish(self, reservation, pcm)
        CompletedAudioStore.publish = delayed_publish
    if options.get("preview_failure"):
        from app.service.capture_process.transport import PreviewRing
        def broken_preview(*args, **kwargs):
            raise RuntimeError("synthetic preview failure")
        PreviewRing.offer = broken_preview
    if options.get("audit_pcm_reclamation"):
        import gc
        import weakref
        from app.service.capture_process.transport import CompletedAudioStore
        from app.service.capture_process.worker import _Worker
        references = []
        publish = CompletedAudioStore.publish
        dispatch = _Worker.dispatch
        def audited_publish(self, reservation, pcm):
            references.append(weakref.ref(pcm))
            return publish(self, reservation, pcm)
        def collect_pcm(self, op, args):
            if op == "test_collect_pcm":
                gc.collect()
                return {"observed": len(references), "alive": sum(ref() is not None for ref in references)}
            return dispatch(self, op, args)
        CompletedAudioStore.publish = audited_publish
        _Worker.dispatch = collect_pcm
    backend = SyntheticBackend(endpoints, options)
    if options.get("resolver"):
        from app.service.managed_input_endpoint_resolver import ManagedInputEndpointResolver
        return backend, ManagedInputEndpointResolver(backend)
    return backend


class SyntheticBackend:
    def __init__(self, endpoints, options):
        self.endpoints = endpoints
        self.options = options
        self.created = {}

    def create_input(self, spec, callback, on_fault):
        if spec.device_index in self.options.get("fail_devices", ()):
            raise RuntimeError("synthetic open failure")
        if self.options.get("require_resolved") and spec.device_index < 10:
            raise RuntimeError("child did not resolve physical input coordinates")
        number = self.created.get(spec.hardware_id, 0)
        self.created[spec.hardware_id] = number + 1
        fault_after = self.options.get("fault_after_frames") if number == 0 else None
        return SyntheticStream(spec, callback, on_fault, fault_after, self.options.get("start_return_delay", 0),
                               self.options.get("in_range_waveform", False))

    def query_raw_input_endpoints(self, scan_id):
        from app.service.managed_input_contracts import (
            RawBackendInputScan, RawHostApiIdentity, InputEndpointObservation,
            InputEndpointDescriptor, StableUidCapability, StableUidContract,
        )
        return RawBackendInputScan(scan_id, True, StableUidCapability.SUPPORTED,
            StableUidContract.REVIEWED_OPAQUE_UID, (RawHostApiIdentity(0, "fake-api"),),
            tuple(InputEndpointObservation(scan_id, f"fake-{item.hardware_id.value}",
                item.device_index + 10, item.hostapi_index, item.stable_backend_uid,
                InputEndpointDescriptor(item.device_name, item.hostapi_name,
                                        item.sample_rate_hz, item.input_channels)) for item in self.endpoints))


class SyntheticStream:
    def __init__(self, spec, callback, on_fault, fault_after=None, start_return_delay=0,
                 in_range_waveform=False):
        self.spec, self.callback, self.on_fault = spec, callback, on_fault
        self.stop = threading.Event()
        self.fault_after = fault_after
        self.start_return_delay = start_return_delay
        self.in_range_waveform = in_range_waveform

    def start(self, deadline_monotonic_ns):
        self.thread = threading.Thread(target=self.produce, daemon=True)
        self.thread.start()
        if self.start_return_delay:
            time.sleep(self.start_return_delay)

    def produce(self):
        import numpy as np
        from app.service.analysis.resident_audio_mvp.contracts import (
            BackendCallbackStatus, BackendCallbackDirective,
        )
        first = 0
        origin = time.monotonic_ns()
        size = self.spec.frames_per_buffer
        while not self.stop.is_set():
            end = origin + round((first + size) * 1e9 / self.spec.sample_rate_hz)
            while time.monotonic_ns() < end:
                if self.stop.wait(max(0, (end - time.monotonic_ns()) / 1e9)):
                    return
            pcm = (np.arange(first, first + size, dtype=np.float32)[:, None]
                   + np.arange(self.spec.input_channel_count, dtype=np.float32)[None, :] * 1000)
            if self.in_range_waveform:
                assert self.spec.input_channel_count == 2
                indices = np.arange(first, first + size, dtype=np.int64)
                phase = (indices // 96000) % 4
                pcm = np.column_stack(((indices % 2048 - 1024) / 4096 + phase / 16,
                                       (indices % 1024 - 512) / 2048 - phase / 16)).astype(np.float32)
            directive = self.callback(pcm, size, end, BackendCallbackStatus.OK)
            if directive is BackendCallbackDirective.STOP:
                break
            first += size
            if self.fault_after is not None and first >= self.fault_after:
                from app.service.analysis.resident_audio_mvp.contracts import BackendFault
                self.on_fault(BackendFault(BackendCallbackStatus.DEVICE_FAULT, "synthetic device fault"))
                return

    def request_stop(self):
        self.stop.set()

    def close(self, deadline_monotonic_ns):
        from app.service.analysis.resident_audio_mvp.contracts import (
            BackendCloseResult, BackendCloseStatus,
        )
        self.stop.set()
        if hasattr(self, "thread"):
            self.thread.join(max(0, (deadline_monotonic_ns - time.monotonic_ns()) / 1e9))
        return BackendCloseResult(BackendCloseStatus.CLOSED)
