"""Child-owned Resident lifecycle. No drain callback performs control I/O."""
from dataclasses import dataclass, replace
import importlib
import queue
import threading
import time
from uuid import uuid4

import numpy as np

from app.service.analysis.resident_audio_mvp import contracts as c
from app.service.managed_input_resident_adapter import ResidentManagedInputAdapter
from .bootstrap import runtime_diagnostics
from .protocol import (PROTOCOL_VERSION, CaptureLimits, CompletedAudioDescriptor,
                       TransportCapacityError, bounded_text)
from .transport import CompletedAudioStore, PreviewRing
from .values import MAX_MESSAGE_BYTES, pack, unpack, decode


@dataclass
class _Capture:
    binding: object
    reservation: object
    started: bool = False
    publishing: bool = False
    descriptor: object = None


class _BindingUnavailable(c.ResidentRequestError):
    def __init__(self):
        super().__init__(c.ResidentError(c.ResidentErrorCode.INVALID, "capture binding is unknown or retired"))


class _StartupResolvedBackend:
    """Initial open uses the same local identity resolver as Resident recovery."""
    def __init__(self, backend, resolver, endpoints, timeout_s):
        self.backend, self.resolver, self.timeout_s = backend, resolver, timeout_s
        self.frozen = {f"managed-input-{item.hardware_id.value}": item for item in endpoints}
        self.initial = set()

    def create_input(self, spec, callback, on_fault):
        if spec.hardware_id not in self.initial:
            self.initial.add(spec.hardware_id)
            resolved = self.resolver.resolve(self.frozen[spec.hardware_id],
                deadline_monotonic_ns=time.monotonic_ns() + round(self.timeout_s * 1e9))
            spec = replace(spec, device_index=resolved.device_index, hostapi_index=resolved.hostapi_index)
        return self.backend.create_input(spec, callback, on_fault)


def _production_backend(endpoints, options):
    from app.service.managed_input_sounddevice_backend import (
        MonotonicSoundDeviceAudioBackend, PythonSystemMonotonicAuthority,
        ManagedInputSoundDeviceBackend,
    )
    from app.service.managed_input_endpoint_resolver import ManagedInputEndpointResolver
    backend = MonotonicSoundDeviceAudioBackend(None, PythonSystemMonotonicAuthority())
    return backend, ManagedInputEndpointResolver(ManagedInputSoundDeviceBackend())


def _level_meter(samples, rate):
    return float(20.0 * np.log10(max(float(np.sqrt(np.mean(np.square(samples)))), 1e-12)))


class _Worker:
    def __init__(self, connection, config, endpoints, preview, backend_factory, backend_options):
        self.connection, self.config = connection, config
        self.incarnation = preview.incarnation
        self.endpoints = decode(endpoints)
        self.limits = CaptureLimits(config.max_capture_frames,
                                   tuple(item.config.channels for item in preview.streams),
                                   config.max_captures, config.max_retained_bytes)
        self.store = CompletedAudioStore(self.incarnation, self.limits)
        self.preview = PreviewRing.attach(preview, incarnation=self.incarnation)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.outgoing = queue.Queue(maxsize=64)
        self.captures = {}
        self.tickets = {}
        self.calibration = None
        self.targets = {}
        self.sequences = {}
        self.adapter = None
        self.backend_factory = backend_factory
        self.backend_options = unpack(backend_options)
        self.sender = threading.Thread(target=self.send_loop, name="capture-control-writer", daemon=True)
        self.terminal = threading.Thread(target=self.terminal_loop, name="capture-terminal-owner", daemon=True)
        self.lifecycle = threading.Thread(target=self.lifecycle_loop, name="capture-readiness-owner", daemon=True)

    def emit(self, kind, **values):
        message = dict(version=PROTOCOL_VERSION, incarnation=self.incarnation, kind=kind, **values)
        try:
            self.outgoing.put_nowait(pack(message))
        except queue.Full:
            self.stop.set()
            raise RuntimeError("capture control delivery capacity exhausted")

    def reply_error(self, request_id, exc):
        error = (exc.error if isinstance(exc, c.ResidentRequestError) else
                 c.ResidentError(c.ResidentErrorCode.CAPACITY if isinstance(exc, TransportCapacityError)
                                 else c.ResidentErrorCode.INVALID, str(exc)[:256] or type(exc).__name__))
        error_kind = "value" if isinstance(exc, ValueError) else "resident"
        if isinstance(exc, _BindingUnavailable):
            error_kind = "binding_unavailable"
        elif isinstance(exc, RuntimeError) and not isinstance(exc, TransportCapacityError):
            error_kind = "runtime"
        self.emit("reply", request_id=request_id, error=error, error_kind=error_kind)

    def send_loop(self):
        try:
            while not self.stop.is_set() or not self.outgoing.empty():
                try:
                    message = self.outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    self.connection.send_bytes(message)
                finally:
                    self.outgoing.task_done()
        except (OSError, EOFError):
            self.stop.set()

    def observe(self, hardware_id, pcm, frames, block_end_ns, rate):
        # Each stream has one drain owner. Advance even when offer drops a block.
        sequence, first, generation = self.sequences.get(
            hardware_id, (0, 0, f"{hardware_id}:generation-1"))
        pool = None if self.adapter is None else self.adapter._pool
        current = None if pool is None else pool._generation_ids.get(hardware_id)
        if pool is not None:
            slot = pool._slots.get(hardware_id)
            if current is None or slot is None:
                # An unpublished recovery candidate has no authoritative cursor
                # or generation yet. Its preview is optional; its PCM stays local.
                self.sequences[hardware_id] = sequence + 1, first + frames, generation
                return
            first = slot._ring.write_cursor - frames
        current = current or generation
        self.sequences[hardware_id] = sequence + 1, first + frames, current
        self.preview.offer(hardware_id, pcm, sequence=sequence, first_sample=first,
                           monotonic_ns=max(0, block_end_ns - round(frames * 1e9 / rate)),
                           generation_id=current)

    def open(self):
        factory = _production_backend
        if self.backend_factory is not None:
            module, name = self.backend_factory.split(":")
            factory = getattr(importlib.import_module(module), name)
        built = factory(self.endpoints, self.backend_options)
        backend, resolver = built if type(built) is tuple else (built, None)
        if resolver is not None:
            backend = _StartupResolvedBackend(backend, resolver, self.endpoints,
                                               self.config.startup_stream_timeout_s)
        self.adapter = ResidentManagedInputAdapter(
            backend=backend, endpoint_resolver=resolver, level_meter=_level_meter,
            frames_per_buffer=self.config.frames_per_buffer,
            max_capture_frames=self.config.max_capture_frames,
            startup_stream_timeout_s=self.config.startup_stream_timeout_s,
            fault_close_timeout_s=self.config.fault_close_timeout_s,
            shutdown_timeout_s=self.config.shutdown_timeout_s,
            calibration_wait_margin_s=float(self.config.calibration_wait_margin_s),
            drain_observer=self.observe,
        )
        startup = self.adapter.open_startup_streams(self.endpoints)
        for ready in startup.ready_streams:
            target = self.adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
            self.targets[target.resident_hardware_id] = target
        return startup

    def target(self, hardware_id):
        bounded_text(hardware_id, "hardware_id")
        if hardware_id not in self.targets:
            raise ValueError("unknown capture hardware")
        return self.targets[hardware_id]

    def dispatch(self, op, args):
        if op == "target":
            target = self.adapter.capture_target(args["hardware_id"], args["generation"])
            return {"hardware_id": target.resident_hardware_id, "generation": target.generation}
        if op == "snapshot":
            return self.adapter.snapshot(args["hardware_id"])
        if op == "reserve":
            target = self.target(args["hardware_id"])
            spec = self.adapter._spec_by_managed[target.generation.hardware_id]
            capture_id = bounded_text(args["capture_id"], "capture_id")
            with self.lock:
                reservation = self.store.reserve(capture_id, frames=self.limits.max_capture_frames,
                                                 channels=spec.input_channel_count, sample_rate_hz=spec.sample_rate_hz)
                try:
                    binding = target.pool.reserve_capture(args["run_id"], target.resident_hardware_id,
                                                          generation_id=args.get("generation_id"))
                except BaseException:
                    self.store.cancel(reservation)
                    raise
                self.captures[capture_id] = _Capture(binding, reservation)
            return {"capture_id": capture_id}
        if op == "readiness":
            target = self.target(args["hardware_id"])
            with self.lock:
                if len(self.tickets) >= 6:
                    raise TransportCapacityError("readiness ticket capacity exhausted")
                ticket_id = bounded_text(args["ticket_id"], "ticket_id")
                ticket = target.pool.begin_readiness(target.resident_hardware_id, args["deadline_monotonic_ns"])
                self.tickets[ticket_id] = ticket
            return {"ticket_id": ticket_id}
        if op == "start":
            with self.lock:
                record = self.captures.get(args["capture_id"])
                if record is None:
                    raise _BindingUnavailable()
                pool = self.target(record.binding.hardware_id).pool
                pool.start_capture(record.binding, trigger_monotonic_ns=args["trigger_monotonic_ns"],
                                   target_frame_count=args["target_frame_count"])
                if not record.started:
                    record.reservation = self.store.shrink(record.reservation, frames=args["target_frame_count"])
                    record.started = True
            return None
        if op == "cancel":
            with self.lock:
                record = self.captures.get(args["capture_id"])
                if record is None:
                    raise _BindingUnavailable()
                result = self.target(record.binding.hardware_id).pool.cancel_capture(record.binding)
            return {"status": result.status.value, "outcome_status": result.outcome.status.value,
                    "error": result.outcome.error}
        if op == "terminal_receipt":
            with self.lock:
                record = self.captures.get(args["capture_id"])
                if record is not None:
                    outcome = record.binding._completion.result_if_ready()
                    if outcome is None or outcome.status is c.CaptureOutcomeStatus.COMPLETED:
                        raise ValueError("terminal receipt requires a non-audio terminal result")
                    self.store.cancel(record.reservation)
                    del self.captures[args["capture_id"]]
            return None
        if op == "receipt":
            descriptor = CompletedAudioDescriptor.from_wire(args["descriptor"])
            with self.lock:
                released = self.store.receipt(descriptor)
                record = self.captures.get(descriptor.capture_id)
                if record is not None and record.descriptor == descriptor:
                    del self.captures[descriptor.capture_id]
            return released
        if op == "discard":
            if args["owner_id"] != self.store.owner_id:
                raise ValueError("discard belongs to another capture owner")
            capture_id = bounded_text(args["capture_id"], "capture_id")
            with self.lock:
                record = self.captures.get(capture_id)
                if record is None:
                    return False
                outcome = record.binding._completion.result_if_ready()
                if (outcome is None or
                        (outcome.status is c.CaptureOutcomeStatus.COMPLETED and record.descriptor is None)):
                    raise ValueError("discard requires a published terminal capture")
                self.store.cancel(record.reservation)
                del self.captures[capture_id]
            return True
        if op == "stats":
            return {"pending_count": self.store.pending_count, "reserved_bytes": self.store.reserved_bytes,
                    "captures": len(self.captures)}
        if op == "close":
            return self.adapter.close_all(args["timeout_seconds"])
        raise ValueError("unknown capture control operation")

    def begin_calibration(self, request_id, args):
        try:
            target = self.adapter.capture_target(args["hardware_id"], args["generation"])
        except ValueError:
            raise ValueError("resident calibration target is not ready") from None
        if self.calibration is not None and self.calibration.is_alive():
            raise TransportCapacityError("calibration is already in progress")
        spec = self.adapter._spec_by_managed[target.generation.hardware_id]
        reservation = self.store.reserve(uuid4().hex, frames=self.limits.max_capture_frames,
                                         channels=spec.input_channel_count, sample_rate_hz=spec.sample_rate_hz)
        def measure():
            try:
                result = self.adapter.measure_calibration(**args)
            except Exception as exc:
                self.reply_error(request_id, exc)
            else:
                self.emit("reply", request_id=request_id, result=result)
            finally:
                self.store.cancel(reservation)
        self.calibration = threading.Thread(target=measure, name="capture-calibration", daemon=True)
        try:
            self.calibration.start()
        except BaseException:
            self.store.cancel(reservation)
            raise

    def lifecycle_loop(self):
        try:
            while not self.stop.wait(.005):
                with self.lock:
                    tickets = tuple(self.tickets.items())
                for ticket_id, ticket in tickets:
                    outcome = ticket.future.result_if_ready()
                    if outcome is not None:
                        self.emit("readiness", ticket_id=ticket_id, outcome=outcome)
                        with self.lock:
                            self.tickets.pop(ticket_id, None)
        except Exception as exc:
            try:
                self.emit("fatal", error=str(exc)[:256] or type(exc).__name__)
            finally:
                self.stop.set()

    def terminal_loop(self):
        try:
            while not self.stop.wait(.005):
                with self.lock:
                    capture_ids = tuple(self.captures)
                for capture_id in capture_ids:
                    self._publish_terminal(capture_id)
        except Exception as exc:
            try:
                self.emit("fatal", error=str(exc)[:256] or type(exc).__name__)
            finally:
                self.stop.set()

    def _publish_terminal(self, capture_id):
        # This helper releases all capture/audio references before the polling
        # thread sleeps, including the last completed result after its receipt.
        with self.lock:
            record = self.captures.get(capture_id)
            if record is None or record.publishing:
                return
            outcome = record.binding._completion.result_if_ready()
            if outcome is None:
                return
            record.publishing = True
        values = {"capture_id": capture_id, "status": outcome.status.value, "error": outcome.error}
        if outcome.audio is not None:
            audio = outcome.audio
            descriptor = self.store.publish(record.reservation, audio.samples)
            with self.lock:
                record.descriptor = descriptor
            values.update(descriptor=descriptor.to_wire(), audio={
                name: getattr(audio, name) for name in (
                    "trigger_monotonic_ns", "start_frame_cursor", "frame_count",
                    "sample_rate_hz", "channel_indices", "completion_monotonic_ns")})
        self.emit("capture", **values)

    def run(self):
        clean = True
        self.sender.start()
        try:
            runtime_diagnostics()
            startup = self.open()
            diagnostics = runtime_diagnostics()
            self.emit("startup", startup=startup, diagnostics=diagnostics, owner_id=self.store.owner_id)
            self.terminal.start()
            self.lifecycle.start()
            while not self.stop.is_set():
                if not self.connection.poll(.05):
                    continue
                message = unpack(self.connection.recv_bytes(MAX_MESSAGE_BYTES))
                if (message.get("version") != PROTOCOL_VERSION
                        or message.get("incarnation") != self.incarnation):
                    raise ValueError("stale capture-process request")
                request_id = message["request_id"]
                try:
                    if message["op"] == "calibration":
                        self.begin_calibration(request_id, message["args"])
                        continue
                    result = self.dispatch(message["op"], message["args"])
                except Exception as exc:
                    self.reply_error(request_id, exc)
                else:
                    self.emit("reply", request_id=request_id, result=result)
                if message["op"] == "close":
                    break
        except (EOFError, OSError):
            pass
        except Exception as exc:
            clean = False
            self.emit("fatal", error=str(exc)[:256] or type(exc).__name__)
        finally:
            self.stop.set()
            deadline = time.monotonic() + self.config.shutdown_timeout_s
            if self.adapter is not None:
                results = self.adapter.close_all(self.config.shutdown_timeout_s)
                clean = clean and all(result.status.value in ("closed", "not_open") for result in results)
            for thread in (self.terminal, self.lifecycle, self.calibration):
                if thread is not None and thread.ident is not None:
                    thread.join(max(0, deadline - time.monotonic()))
                    clean = not thread.is_alive() and clean
            clean = self.store.close(max(0, deadline - time.monotonic())) and clean
            clean = self.preview.close(max(0, deadline - time.monotonic())) and clean
            self.sender.join(max(0, deadline - time.monotonic()))
            clean = not self.sender.is_alive() and clean
            self.connection.close()
        return clean


def run_worker(*args):
    raise SystemExit(0 if _Worker(*args).run() else 1)
