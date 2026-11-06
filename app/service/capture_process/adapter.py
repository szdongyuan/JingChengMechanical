"""The only bridge that constructs and publishes parent-local Resident tokens."""
import queue
import threading
import time
from uuid import uuid4

import numpy as np

from app.service.analysis.resident_audio_mvp import contracts as c
from app.service.analysis.resident_audio_mvp.diagnostics import (
    DiagnosticStage, DiagnosticReason, ResidentDiagnostic,
)
from app.service.analysis.resident_audio_mvp.pool import _CaptureCompletionPublisher, _BindingTerminalProof
from app.service.managed_input_contracts import (
    ManagedInputCloseResult, ManagedInputCloseStatus, ManagedInputError,
    ManagedInputErrorCode, ManagedHardwareId, ResidentStreamGenerationStamp, validate_shutdown_timeout,
    ResidentOpenFailure, ResidentStartupOpenResult, OfflineMaintenanceBlockerSnapshot,
)
from app.service.managed_input_resident_adapter import (
    ResidentCaptureTarget, ResidentManagedInputAdapter, resident_stream_spec_for_endpoint,
)
from .client import CaptureProcessClient, CaptureProcessError, CaptureBindingUnavailableError
from .protocol import CompletedAudioDescriptor, WorkerConfig
from .transport import copy_completed_audio


def _error(code, text):
    return c.ResidentRequestError(c.ResidentError(code, str(text)[:256] or code.value))


class _PoolProxy:
    def __init__(self, adapter):
        self.adapter = adapter
        self.nonce = object()
        self.lock = threading.RLock()
        self.bindings = {}
        self.tickets = {}
        self.failed = None

    def _binding(self, binding):
        c._validate_capture_binding(binding)
        if binding._owner_nonce is not self.nonce:
            raise _error(c.ResidentErrorCode.INVALID, "binding belongs to another capture owner")
        record = self.bindings.get(binding.binding_id)
        if record is None or record[0] is not binding:
            completion = binding._completion
            proof = completion._terminal_proof
            outcome = completion.result_if_ready()
            if (type(proof) is not _BindingTerminalProof or proof.binding is not binding
                    or proof.completion is not completion or outcome is None
                    or proof.outcome is not outcome or outcome.binding is not binding):
                raise _error(c.ResidentErrorCode.INVALID, "binding is unknown or stale")
        return binding

    def reserve_capture(self, run_id, hardware_id, *, generation_id=None):
        c._bounded_text(run_id, "run_id")
        c._bounded_text(hardware_id, "hardware_id")
        binding_id = uuid4().hex
        completion = c.CaptureCompletion()
        binding = object.__new__(c.CaptureBinding)
        for name, value in dict(binding_id=binding_id, run_id=run_id, hardware_id=hardware_id,
                                _owner_nonce=self.nonce, _completion=completion).items():
            object.__setattr__(binding, name, value)
        publisher = _CaptureCompletionPublisher(binding, completion)
        with self.lock:
            if self.failed:
                raise self.failed
            self.bindings[binding_id] = (binding, publisher)
        try:
            self.adapter.client.request("reserve", capture_id=binding_id, run_id=run_id,
                                        hardware_id=hardware_id, generation_id=generation_id)
        except BaseException:
            with self.lock:
                self.bindings.pop(binding_id, None)
            raise
        return binding

    def capture_completion(self, binding):
        with self.lock:
            return self._binding(binding)._completion

    def _terminal_if_owned(self, binding):
        with self.lock:
            try:
                token = self._binding(binding)
            except c.ResidentRequestError:
                return None
            return token._completion.result_if_ready()

    def start_capture(self, binding, *, trigger_monotonic_ns, target_frame_count):
        with self.lock:
            binding = self._binding(binding)
            c._exact_int(trigger_monotonic_ns, "trigger_monotonic_ns", minimum=1, maximum=(1 << 63) - 1)
            c._exact_int(target_frame_count, "target_frame_count", minimum=1,
                         maximum=self.adapter.config.max_capture_frames)
            if binding._completion.result_if_ready() is not None:
                return binding._completion
        try:
            self.adapter.client.request("start", capture_id=binding.binding_id,
                                        trigger_monotonic_ns=trigger_monotonic_ns,
                                        target_frame_count=target_frame_count)
        except CaptureBindingUnavailableError:
            # A request can already be queued when the materializer publishes
            # the canonical outcome and retires the remote binding.
            if self._terminal_if_owned(binding) is None:
                raise
        except (CaptureProcessError, TimeoutError):
            outcome = self._terminal_if_owned(binding)
            if outcome is None or outcome.status is c.CaptureOutcomeStatus.FAILED:
                raise
        return binding._completion

    def cancel_capture(self, binding):
        try:
            with self.lock:
                binding = self._binding(binding)
                outcome = binding._completion.result_if_ready()
            if outcome is not None:
                return c.CancelResult(c.CancelStatus.ALREADY_TERMINAL, outcome)
            result = self.adapter.client.request("cancel", capture_id=binding.binding_id)
            if result["outcome_status"] != c.CaptureOutcomeStatus.COMPLETED.value:
                outcome = c.CaptureOutcome(c.CaptureOutcomeStatus(result["outcome_status"]), binding, None, result["error"])
                self.settle(binding.binding_id, outcome)
                self.adapter.client.post("terminal_receipt", capture_id=binding.binding_id)
            outcome = binding._completion.wait(self.adapter.config.control_timeout_s)
            if outcome is None:
                raise CaptureProcessError("capture cancellation outcome has not arrived")
            return c.CancelResult(c.CancelStatus(result["status"]), outcome)
        except CaptureBindingUnavailableError as exc:
            outcome = self._terminal_if_owned(binding)
            if outcome is not None:
                return c.CancelResult(c.CancelStatus.ALREADY_TERMINAL, outcome)
            return c.CancelResult(c.CancelStatus.INVALID, error=exc.error)
        except c.ResidentRequestError as exc:
            if exc.error.code is c.ResidentErrorCode.INVALID:
                return c.CancelResult(c.CancelStatus.INVALID, error=exc.error)
            raise
        except (CaptureProcessError, TimeoutError):
            outcome = self._terminal_if_owned(binding)
            if outcome is not None and outcome.status is not c.CaptureOutcomeStatus.FAILED:
                return c.CancelResult(c.CancelStatus.ALREADY_TERMINAL, outcome)
            raise

    def begin_readiness(self, hardware_id, deadline_monotonic_ns):
        future, publisher = c._new_lifecycle_future()
        ticket_id = uuid4().hex
        ticket = c._issue_readiness_ticket(ticket_id=ticket_id, hardware_id=hardware_id,
                                           future=future, owner_nonce=self.nonce)
        with self.lock:
            if self.failed:
                raise self.failed
            self.tickets[ticket_id] = publisher
        try:
            self.adapter.client.request("readiness", ticket_id=ticket_id, hardware_id=hardware_id,
                                        deadline_monotonic_ns=deadline_monotonic_ns)
        except BaseException:
            with self.lock:
                self.tickets.pop(ticket_id, None)
            raise
        return ticket

    def settle(self, capture_id, outcome):
        with self.lock:
            record = self.bindings.pop(capture_id, None)
            if record is not None:
                record[1].prepare(outcome)
                record[1].publish()

    def fail(self, error):
        with self.lock:
            self.failed = error
            for binding, publisher in self.bindings.values():
                publisher.prepare(c.CaptureOutcome(c.CaptureOutcomeStatus.FAILED, binding, None,
                                                    c.ResidentError(c.ResidentErrorCode.FAULT, str(error)[:256])))
                publisher.publish()
            self.bindings.clear()
            for publisher in self.tickets.values():
                publisher.settle(c.LifecycleOutcome(c.LifecycleStatus.RECOVERY_FAILED,
                    diagnostic=ResidentDiagnostic(DiagnosticStage.INTERNAL,
                        DiagnosticReason.INVARIANT_VIOLATION, str(error)[:256])))
            self.tickets.clear()


class CaptureProcessManagedInputAdapter:
    def __init__(self, *, config, backend_factory=None, backend_options=None):
        self.config, self.backend_factory = config, backend_factory
        self.backend_options = backend_options
        self.client = None
        self._endpoints = None
        self._startup_result = None
        self._unsupported_ids = frozenset()
        self._stream_specs = {}
        self._close_result = None
        self._lifecycle = threading.Condition()
        self._startup_in_progress = False
        self._close_requested = False
        self._startup_cleanup_failed = False
        self._pool = _PoolProxy(self)
        self._events = queue.Queue(maxsize=12)
        self._stop = threading.Event()
        self._materializer = threading.Thread(target=self._materialize_loop, name="capture-parent-audio-copy", daemon=True)

    def open_startup_streams(self, endpoints):
        endpoints = ResidentManagedInputAdapter._validate_endpoint_tuple(endpoints)
        if type(self.config) is not WorkerConfig:
            raise ValueError("config must be an exact WorkerConfig")
        self.config.__post_init__()
        deadline = time.monotonic() + len(endpoints) * self.config.startup_stream_timeout_s + self.config.control_timeout_s
        with self._lifecycle:
            while self._startup_in_progress and not self._close_requested:
                if endpoints != self._endpoints:
                    raise ValueError("startup membership is already frozen")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("capture startup is still in progress")
                self._lifecycle.wait(remaining)
            if self._close_requested:
                raise ValueError("capture adapter is closed")
            if self._startup_result is not None:
                if endpoints != self._endpoints:
                    raise ValueError("startup membership is already frozen")
                return self._startup_result
            self._startup_in_progress = True
            self._endpoints = endpoints
        try:
            return self._open_startup(endpoints)
        except BaseException:
            with self._lifecycle:
                self._close_requested = True
                self._stop.set()
                self._lifecycle.notify_all()
            self._cleanup_cancelled_startup()
            raise
        finally:
            with self._lifecycle:
                self._startup_in_progress = False
                self._lifecycle.notify_all()

    def _open_startup(self, endpoints):
        stream_specs, failures = [], []
        for endpoint in sorted(endpoints, key=lambda item: item.hardware_id.value):
            spec = resident_stream_spec_for_endpoint(
                endpoint, f"managed-input-{endpoint.hardware_id.value}",
                frames_per_buffer=self.config.frames_per_buffer,
                max_capture_frames=self.config.max_capture_frames,
            )
            if type(spec) is str:
                failures.append(ResidentOpenFailure(endpoint.hardware_id, spec))
            else:
                stream_specs.append(spec)
        with self._lifecycle:
            if self._close_requested:
                raise ValueError("capture adapter was closed during startup")
            self._stream_specs = {spec.hardware_id: spec for spec in stream_specs}
            self._unsupported_ids = frozenset(item.hardware_id for item in failures)
            if not stream_specs:
                self._startup_result = ResidentStartupOpenResult((), tuple(failures))
                return self._startup_result
        client = CaptureProcessClient(self.config, endpoints, on_event=self._on_event,
                                      on_failure=self._pool.fail,
                                      stream_specs=tuple(stream_specs),
                                      backend_factory=self.backend_factory,
                                      backend_options=self.backend_options,
                                      startup_cancelled=self._stop)
        with self._lifecycle:
            # Publish ownership even if close overtook construction, so both
            # owners can observe and reap it. Readiness is a separate commit.
            self.client = client
            self._lifecycle.notify_all()
            if self._close_requested:
                raise ValueError("capture adapter was closed during startup")
        self._materializer.start()
        with self._lifecycle:
            if self._close_requested:
                raise ValueError("capture adapter was closed during startup")
            self._startup_result = client.startup
            return self._startup_result

    def _cleanup_cancelled_startup(self):
        deadline = time.monotonic() + self.config.shutdown_timeout_s
        client = self.client
        if client is not None:
            try:
                clean = client.close(max(0, deadline - time.monotonic()))
                self._startup_cleanup_failed = self._startup_cleanup_failed or not clean
            except Exception:
                self._startup_cleanup_failed = True
        if self._materializer.ident is not None and self._materializer is not threading.current_thread():
            try:
                self._materializer.join(max(0, deadline - time.monotonic()))
                self._startup_cleanup_failed = self._startup_cleanup_failed or self._materializer.is_alive()
            except Exception:
                self._startup_cleanup_failed = True

    def capture_target(self, hardware_id, generation):
        with self._lifecycle:
            if self.client is None or self._close_requested or self._startup_in_progress:
                raise ValueError("resident capture target is not ready")
        result = self.client.request("target", hardware_id=hardware_id, generation=generation)
        return ResidentCaptureTarget(self._pool, result["hardware_id"], result["generation"])

    def snapshot(self, hardware_id):
        if self.client is None:
            return OfflineMaintenanceBlockerSnapshot(0, False)
        return self.client.request("snapshot", hardware_id=hardware_id)

    def _on_event(self, message):
        if message["kind"] == "readiness":
            with self._pool.lock:
                publisher = self._pool.tickets.pop(message["ticket_id"], None)
            if publisher is not None:
                publisher.settle(message["outcome"])
        elif message["status"] != c.CaptureOutcomeStatus.COMPLETED.value:
            with self._pool.lock:
                record = self._pool.bindings.get(message["capture_id"])
            if record is not None:
                outcome = c.CaptureOutcome(c.CaptureOutcomeStatus(message["status"]), record[0], None, message["error"])
                self._pool.settle(message["capture_id"], outcome)
            self.client.post("terminal_receipt", capture_id=message["capture_id"])
        else:
            self._events.put_nowait(message)

    def _materialize_loop(self):
        while not self._stop.is_set():
            try:
                message = self._events.get(timeout=.05)
            except queue.Empty:
                continue
            self._materialize_one(message)

    def _materialize_one(self, message):
        # Keep every PCM/Resident reference in this short-lived frame. The
        # idle queue consumer must retain only bounded metadata between items.
        capture_id = message["capture_id"]
        with self._pool.lock:
            record = self._pool.bindings.get(capture_id)
        if record is None:
            return
        binding = record[0]
        try:
            descriptor = CompletedAudioDescriptor.from_wire(message["descriptor"])
            if descriptor.capture_id != capture_id:
                raise ValueError("completed audio belongs to a different capture")
            self._validate_completed_metadata(binding, descriptor, message["audio"])
            pcm = copy_completed_audio(descriptor, incarnation=self.client.incarnation,
                                       limits=self.client.limits, owner_id=self.client.owner_id)
            # CapturedAudio requires owned read-only input, then hardens it
            # into its own immutable bytes-backed representation.
            owned = np.array(pcm, copy=True, order="C")
            owned.setflags(write=False)
            audio = c.CapturedAudio(binding=binding, run_id=binding.run_id,
                                    hardware_id=binding.hardware_id, samples=owned, **message["audio"])
            outcome = c.CaptureOutcome(c.CaptureOutcomeStatus.COMPLETED, binding, audio, message["error"])
        except Exception as exc:
            outcome = c.CaptureOutcome(c.CaptureOutcomeStatus.FAILED, binding, None,
                                       c.ResidentError(c.ResidentErrorCode.FAULT, str(exc)[:256] or type(exc).__name__))
            self._pool.settle(capture_id, outcome)
            try:
                # Authenticate with the locally issued binding and child owner;
                # never trust a malformed descriptor for disposal or send a
                # success receipt for PCM that was not copied and constructed.
                self.client.request("discard", capture_id=capture_id, owner_id=self.client.owner_id)
            except Exception as disposal_error:
                self._fence_transfer(disposal_error)
            return
        # Local terminal visibility precedes remote retirement. No proxy lock
        # spans bulk copying or receipt RPC, and an ACK error cannot overwrite
        # an already-owned valid audio outcome with a false recording failure.
        self._pool.settle(capture_id, outcome)
        try:
            self._receipt(descriptor)
        except Exception as receipt_error:
            self._fence_transfer(receipt_error)

    def _validate_completed_metadata(self, binding, descriptor, metadata):
        spec = self._stream_specs.get(binding.hardware_id)
        if spec is None or type(metadata) is not dict:
            raise ValueError("completed audio has no matching stream layout")
        indices = metadata.get("channel_indices")
        if (
            type(metadata.get("frame_count")) is not int
            or metadata["frame_count"] != descriptor.frames
            or type(metadata.get("sample_rate_hz")) is not int
            or metadata["sample_rate_hz"] != descriptor.sample_rate_hz
            or descriptor.sample_rate_hz != spec.sample_rate_hz
            or type(indices) is not tuple
            or any(type(index) is not int for index in indices)
            or indices != spec.capture_channel_indices
            or descriptor.channels != len(spec.capture_channel_indices)
        ):
            raise ValueError("completed audio metadata conflicts with its descriptor or stream layout")

    def _fence_transfer(self, error):
        self._stop.set()
        self.client._fail(CaptureProcessError(f"capture transfer cleanup failed: {error}"[:256]))
        self.client.close(self.config.shutdown_timeout_s)

    def _receipt(self, descriptor):
        for attempt in range(3):
            try:
                self.client.request("receipt", descriptor=descriptor.to_wire())
                return
            except TimeoutError:
                if attempt == 2:
                    raise

    def measure_calibration(self, hardware_id, generation, channel_index, duration_frames):
        if (type(hardware_id) is not ManagedHardwareId
                or type(generation) is not ResidentStreamGenerationStamp
                or type(channel_index) is not int or type(duration_frames) is not int):
            raise ValueError("resident calibration arguments must have their exact value types")
        endpoint = next((item for item in self._endpoints or () if item.hardware_id == hardware_id), None)
        if endpoint is None or self.client is None:
            raise ValueError("resident calibration target is not ready")
        return self.client.request("calibration", hardware_id=hardware_id, generation=generation,
                                   channel_index=channel_index, duration_frames=duration_frames,
                                   timeout=max(0, duration_frames) / endpoint.sample_rate_hz
                                           + self.config.calibration_wait_margin_s + self.config.control_timeout_s)

    def reap_owned_capture_process(self, timeout_seconds):
        """Reap even after the upstream deadline exhausted the graceful budget."""
        if type(timeout_seconds) is not float or timeout_seconds != 0.0:
            validate_shutdown_timeout(timeout_seconds)
        results = self._close_owned(timeout_seconds)
        return all(item.status in (ManagedInputCloseStatus.CLOSED, ManagedInputCloseStatus.NOT_OPEN)
                   for item in results)

    def close_all(self, timeout_seconds):
        validate_shutdown_timeout(timeout_seconds)
        return self._close_owned(timeout_seconds)

    def _close_owned(self, timeout_seconds):
        # A zero graceful budget still reaches client.close, which fences
        # requests and gives only this owned child its bounded termination join.
        deadline = time.monotonic() + timeout_seconds
        with self._lifecycle:
            self._close_requested = True
            self._stop.set()
            self._lifecycle.notify_all()
        clean, attempted_client = True, None
        while True:
            with self._lifecycle:
                client = self.client
                pending_startup = self._startup_in_progress
                if client is not None and client is not attempted_client:
                    pass
                elif pending_startup and time.monotonic() < deadline:
                    self._lifecycle.wait(deadline - time.monotonic())
                    continue
                else:
                    break
            clean = client.close(max(0, deadline - time.monotonic())) and clean
            attempted_client = client
        if self._materializer.ident is not None:
            self._materializer.join(max(0, deadline - time.monotonic()))
            clean = clean and not self._materializer.is_alive()
        clean = clean and not pending_startup and not self._startup_cleanup_failed
        error = ManagedInputError(ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
                                  "resident_close", "Capture child did not close cleanly; stop and restart the process.")
        results = None if self.client is None else getattr(self.client, "close_results", None)
        if not clean or results is None:
            results = []
            for endpoint in sorted(self._endpoints or (), key=lambda item: item.hardware_id.value):
                never_opened = (self.client is None and not pending_startup) or endpoint.hardware_id in self._unsupported_ids
                results.append(ManagedInputCloseResult(
                    endpoint.hardware_id,
                    ManagedInputCloseStatus.NOT_OPEN if never_opened else
                    ManagedInputCloseStatus.TIMED_OUT if pending_startup else ManagedInputCloseStatus.FAILED,
                    None if never_opened else error,
                ))
            results = tuple(results)
        with self._lifecycle:
            if self._close_result is None:
                self._close_result = results
            return self._close_result
