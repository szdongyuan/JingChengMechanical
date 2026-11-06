"""Spawn-only C producer fixture; substitutes the device, never native capture."""
import ctypes as ct
import json
import os
from pathlib import Path


def create_backend(endpoints, options):
    import sounddevice as sd
    from app.service.capture_process.worker import _Worker
    from app.service.capture_process.transport import PreviewRing
    from app.service.managed_input_sounddevice_backend import (
        MonotonicSoundDeviceAudioBackend, PythonSystemMonotonicAuthority,
    )
    from app.service.native_capture_buffer import _load_library

    production = _load_library()  # Resolve the compiled driver's DLL import first.
    driver = ct.CDLL(str(Path(__file__).with_name("capture_test_driver.dll")))
    driver._production = production
    for name, args, result in (
        ("driver_start", [ct.c_void_p] + [ct.c_uint32] * 5, ct.c_void_p),
        ("driver_wait", [ct.c_void_p, ct.c_uint32], ct.c_int),
        ("driver_entry", [ct.c_void_p, ct.c_uint32], ct.c_uint64),
        ("driver_priority", [ct.c_void_p], ct.c_int),
        ("driver_result", [ct.c_void_p], ct.c_int),
        ("driver_destroy", [ct.c_void_p], None),
    ):
        fn = getattr(driver, name)
        fn.argtypes, fn.restype = args, result
    streams, handles, closed = [], [], []
    dropped = {}
    offer = PreviewRing.offer

    def counted_offer(self, hardware_id, *args, **kwargs):
        accepted = offer(self, hardware_id, *args, **kwargs)
        if not accepted:
            dropped[hardware_id] = dropped.get(hardware_id, 0) + 1
        return accepted

    PreviewRing.offer = counted_offer

    class ProducingStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.latency, self.active, self.closed = .1, False, False
            self.driver = None
            streams.append(self)

        def start(self):
            assert isinstance(self.kwargs["callback"], sd._ffi.CData)
            assert self.kwargs["wrap_callback"] is None
            assert self.kwargs["samplerate"] == 48000
            assert self.kwargs["channels"] == 2
            self.driver = driver.driver_start(
                int(sd._ffi.cast("uintptr_t", self.kwargs["userdata"])),
                10000, 512, 2, 12, 0,
            )
            assert self.driver
            self.active = True

        def stop(self, ignore_errors=True):
            # Production NativeCaptureStream requests callback stop first.
            assert driver.driver_wait(self.driver, 1000)
            self.active = False

        def close(self, ignore_errors=True):
            assert not self.active
            assert driver.driver_wait(self.driver, 0)
            result = driver.driver_result(self.driver)
            assert result in (0, 2)  # 2 is the owned stop, not a device fault.
            closed.append({"device": self.kwargs["device"], "driver_result": result})
            driver.driver_destroy(self.driver)
            self.driver = None
            self.closed = True

    def forbidden_python_stream(**kwargs):
        raise AssertionError("native proof must not use a Python sounddevice callback")

    sd._StreamBase = ProducingStream
    sd.InputStream = forbidden_python_stream

    class Backend(MonotonicSoundDeviceAudioBackend):
        def create_input(self, spec, callback, on_fault):
            handle = super().create_input(spec, callback, on_fault)
            handles.append(handle)
            return handle

    dispatch = _Worker.dispatch

    def inspected_dispatch(self, op, args):
        if op == "test_native_snapshot":
            pool = self.adapter._pool
            snapshots = {}
            with pool._lock:
                for hardware, slot in pool._slots.items():
                    with slot._lock:
                        metadata = tuple(slot._ring.block_metadata)
                        first = metadata[0]
                        origin = first.estimated_start_ns - first.start_cursor * 1_000_000_000 // 48000
                        native = slot._handle._native
                        stats = native.producer_snapshot()
                        stream = native._stream
                        snapshots[hardware] = {
                            "origin_ns": origin,
                            "oldest": slot._ring.oldest_cursor,
                            "cursor": slot._ring.write_cursor,
                            "generation": pool._generation_ids[hardware],
                            "fault": str(slot._fault) if slot._fault else None,
                            "overflow": slot._ingress.overflow_count,
                            "sequence": stats.sequence, "consumed": stats.consumed,
                            "error": stats.error, "priority_applied": stats.priority_applied,
                            "driver_priority": driver.driver_priority(stream.driver),
                            "driver_result": driver.driver_result(stream.driver),
                            "first_entry": driver.driver_entry(stream.driver, 0),
                            "dropped_preview": dropped.get(hardware, 0),
                        }
            with self.lock:
                captures = [{"status": record.binding._completion.result_if_ready().status.name
                             if record.binding._completion.result_if_ready() else "ACTIVE",
                             "shm_name": record.descriptor.shm_name if record.descriptor else None}
                            for record in self.captures.values()]
            return {"pid": os.getpid(), "streams": snapshots, "captures": captures,
                    "pending_count": self.store.pending_count}
        result = dispatch(self, op, args)
        if op == "close":
            Path(options["cleanup_path"]).write_text(json.dumps({
                "closed": closed, "streams_closed": all(s.closed for s in streams),
                "native_closed": all(h._native.closed for h in handles),
            }), encoding="utf-8")
        return result

    _Worker.dispatch = inspected_dispatch
    return Backend(sd, PythonSystemMonotonicAuthority())
