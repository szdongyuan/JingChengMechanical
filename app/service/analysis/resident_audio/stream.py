"""One resident input stream and its generation-isolated ingest worker."""

from __future__ import annotations

import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .backend import AudioInputBackend, AudioInputHandle
from .contracts import (
    CaptureErrorCode,
    CaptureLayerError,
    CaptureOwnerState,
    ResolvedInputDevice,
    StreamReadyToken,
    StreamState,
)
from .ingress import (
    GenerationLatch,
    PortAudioCallbackAdapter,
    SpscBlockIngress,
    captured_block_from_raw,
)
from .owner_types import OwnerControlEvent, OwnerControlKind
from .owner_finality import OwnerFinalityLedger
from .owner_identity import OwnerInstanceId, StreamInstanceId
from .ring import TimestampedBlockRing
from .stream_control import (
    ResidentStreamControlMixin,
    StreamCommand,
    StreamCommandIndeterminate,
)
from .stream_admission import CommandAdmission
from .stream_identity import ResidentStreamIdentityMixin
from .stream_shutdown import ResidentStreamShutdownMixin
from .stream_startup import ResidentStreamStartupMixin, StartupHandoff
from .stream_types import (
    RETRY_DELAYS_SECONDS,
    ResidentStreamSnapshot,
    RetryBackoff,
    StreamFailureEvent,
)
from .timeline import StreamTimeline, TimelineState


INPUT_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True)
class _Registration:
    owner: object
    identity: OwnerInstanceId
    active: bool = False


class ResidentStream(
    ResidentStreamStartupMixin,
    ResidentStreamShutdownMixin,
    ResidentStreamControlMixin,
    ResidentStreamIdentityMixin,
):
    """Own one PortAudio handle, ingress, and sole ingest-state writer."""

    def __init__(
        self,
        *,
        resolved_device: ResolvedInputDevice,
        backend: AudioInputBackend,
        pool_epoch: str,
        stream_generation: int,
        monotonic_clock: Callable[[], float] = time.monotonic,
        ready_sink: Callable[[StreamReadyToken], None] | None = None,
        failure_sink: Callable[[StreamFailureEvent], None] | None = None,
        retry_backoff: RetryBackoff | None = None,
        owner_finality_limit: int = 64,
        physical_close_gate=None,
    ) -> None:
        if not isinstance(resolved_device, ResolvedInputDevice):
            raise TypeError("resolved_device must be a ResolvedInputDevice")
        if not isinstance(pool_epoch, str) or not pool_epoch:
            raise ValueError("pool_epoch must be a non-empty string")
        if (
            isinstance(stream_generation, bool)
            or not isinstance(stream_generation, int)
            or stream_generation < 1
        ):
            raise ValueError("stream_generation must be a positive integer")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self._resolved = resolved_device
        self._backend = backend
        self._pool_epoch = pool_epoch
        self._generation_number = stream_generation
        self._clock = monotonic_clock
        self._ready_sink = ready_sink or (lambda _token: None)
        self._failure_sink = failure_sink or (lambda _event: None)
        self._retry = retry_backoff or RetryBackoff()

        self._generation = GenerationLatch()
        self._wake = threading.Event()
        self._ingress = SpscBlockIngress(self._generation, wake_event=self._wake)
        self._callback = PortAudioCallbackAdapter(
            pool_epoch=pool_epoch,
            hardware_id=resolved_device.hardware_id,
            stream_generation=stream_generation,
            generation_latch=self._generation,
            ingress=self._ingress,
            monotonic_clock=monotonic_clock,
        )
        self._commands: queue.Queue[StreamCommand] = queue.Queue()
        self._command_admission = CommandAdmission()
        self._state = StreamState.CLOSED
        self._state_lock = threading.Lock()
        self._handle_lock = threading.Lock()
        self._handle_close_lock = threading.Lock()
        self._handle: AudioInputHandle | None = None
        self._handle_closed = False
        self._handle_close_attempted = False
        self._handle_abort_succeeded: bool | None = None
        self._handle_stop_succeeded: bool | None = None
        self._tracked_cleanup_handle: AudioInputHandle | None = None
        self._physical_close_gate = physical_close_gate
        self._timeline: StreamTimeline | None = None
        self._ring: TimestampedBlockRing | None = None
        self._stream_instance_id = StreamInstanceId(
            pool_epoch=pool_epoch,
            hardware_id=resolved_device.hardware_id,
            stream_generation=stream_generation,
            instance_nonce=uuid.uuid4().hex,
        )
        self._owner_finality = OwnerFinalityLedger(
            self._stream_instance_id, limit=owner_finality_limit
        )
        self._registrations: dict[OwnerInstanceId, _Registration] = {}
        self._pool_reserved_prepares: set[OwnerInstanceId] = set()
        self._registration_guard = threading.Lock()
        self._ready_token: StreamReadyToken | None = None
        self._failure_code: CaptureErrorCode | None = None
        self._start_consumed = False
        self._startup = StartupHandoff()
        self._shutdown_begin_lock = threading.Lock()
        self._shutdown_begin_result = None
        self._shutdown_control_close_required = False
        self._shutdown_control_close_authorized = False
        self._startup_cleanup_clean: bool | None = None
        self._opened_at: float | None = None
        self._last_ingested_at: float | None = None
        self._running = False
        self._pause_raw = False
        self._worker: threading.Thread | None = None
        self._overflow_count = 0
        self._recovery_count = 0
        self._backlog_count = 0

    @property
    def resolved_device(self) -> ResolvedInputDevice:
        return self._resolved

    @property
    def generation_latch(self) -> GenerationLatch:
        return self._generation

    @property
    def current_ready_token(self) -> StreamReadyToken | None:
        with self._state_lock:
            if self._state is not StreamState.READY:
                return None
            return self._ready_token

    def _ingest_loop(self) -> None:
        try:
            while self._running:
                self._wake.wait(0.05)
                self._drain_commands()
                if not self._running:
                    break
                if not self._generation.alive:
                    self._consume_latched_failure()
                if self._running and not self._pause_raw:
                    self._drain_raw()
                if self._running:
                    self._check_deadlines(float(self._clock()))
                if self._commands.empty() and self._ingress.size == 0:
                    self._wake.clear()
        except Exception as error:
            self._fail_generation(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                f"resident ingest worker failed: {error}",
            )
        finally:
            self._running = False
            self._finalize_command_admission()

    def _finalize_command_admission(self) -> None:
        self._command_admission.close_and_drain(self._commands)

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if not command.claim():
                continue
            try:
                result = self._execute(command)
            except Exception:
                result = False
            finally:
                command.complete(result)

    def _execute(self, command: StreamCommand) -> bool:
        if command.kind == "barrier":
            if not self._pause_raw:
                self._drain_raw()
            return True
        if command.kind == "pause":
            self._pause_raw = True
            return True
        if command.kind == "resume":
            self._pause_raw = False
            self._drain_raw()
            return True
        if command.kind == "prepare":
            assert isinstance(command.identity, OwnerInstanceId)
            activated = False
            try:
                claim = self._owner_finality.claim_prepare(command.identity)
                if not claim.activation_allowed:
                    return False
                activated = self._prepare(command.owner, command.identity)
                return activated
            finally:
                self._owner_finality.resolve_prepare(
                    command.identity, activated=activated
                )
        if command.kind == "activate":
            return self._activate(
                command.owner, command.identity, float(command.value)
            )
        if command.kind == "remove":
            return self._remove(command.identity)
        if command.kind == "ack-finality":
            receipt, durable = command.value
            return self._dispatch_acknowledge_finality(
                command.identity, receipt, durable
            )
        if command.kind == "prune-finality":
            return self._dispatch_prune_finality(
                command.identity, command.value
            )
        if command.kind == "switch":
            return self._switch(command.owner, command.identity)
        if command.kind == "owner-control":
            if (
                not isinstance(command.value, OwnerControlEvent)
                or command.owner is not command.value.owner
            ):
                return False
            return self._handle_owner_control(command.value)
        if command.kind == "deadline":
            self._check_deadlines(float(command.value))
            return True
        if command.kind == "backend-failure":
            error, disconnected = command.value
            prefix = "input device disconnected" if disconnected else "backend failure"
            self._fail_generation(
                CaptureErrorCode.STREAM_OPEN_FAILED,
                f"{prefix}: {error}",
                exception_type=type(error).__name__,
            )
            return True
        if command.kind == "close":
            self._begin_close()
            return True
        return False

    def _prepare(
        self, owner: object | None, identity: OwnerInstanceId | None
    ) -> bool:
        if not self._identity_matches(owner, identity):
            return False
        with self._state_lock:
            state = self._state
        if state not in (StreamState.READY, StreamState.TIMING_RECOVERY):
            return False
        assert identity is not None
        with self._registration_guard:
            if not self._owner_finality.authorize_prepare_publication(identity):
                return False
            existing = self._registrations.get(identity)
            if existing is not None:
                return existing.owner is owner and existing.identity == identity
            self._registrations[identity] = _Registration(owner, identity)
            return True

    def _activate(
        self,
        owner: object | None,
        identity: OwnerInstanceId | None,
        commit_monotonic: float,
    ) -> bool:
        registration = self._find(identity)
        if registration is not None and registration.owner is not owner:
            registration = None
        if registration is None or registration.active:
            return registration is not None and registration.active
        snapshot = owner.snapshot()
        if not snapshot.commit_open:
            return False
        state = owner.activate(
            self._require_timeline(), commit_monotonic=commit_monotonic
        )
        if state in (
            CaptureOwnerState.FAILED,
            CaptureOwnerState.CANCELED,
            CaptureOwnerState.COMPLETED,
        ):
            self._retire_registration(registration)
            return False
        registration.active = True
        return True

    def _remove(self, identity: OwnerInstanceId | None):
        return self._remove_owner_receipt(identity)

    def _switch(
        self, owner: object | None, identity: OwnerInstanceId | None
    ) -> bool:
        registration = self._find(identity)
        if registration is not None and registration.owner is not owner:
            registration = None
        if registration is None or not registration.active:
            return False
        return bool(owner.switch_to_live(self._require_timeline().cursor))

    def _handle_owner_control(self, value: object) -> bool:
        if not isinstance(value, OwnerControlEvent):
            return False
        registration = next(
            (
                candidate
                for candidate in self._registrations.values()
                if candidate.identity.run_id == value.run_id
            ),
            None,
        )
        if registration is None:
            return False
        identity = registration.identity
        if (
            registration.owner is not value.owner
            or value.pool_epoch != identity.pool_epoch
            or value.stream_generation != identity.stream_generation
            or value.commit_attempt_id != identity.commit_attempt_id
            or value.owner_capability != identity.owner_capability
        ):
            return False
        if value.kind is OwnerControlKind.DETACH:
            self._retire_registration(registration)
            return True
        if value.kind is OwnerControlKind.REQUEST_LIVE_SWITCH:
            return bool(
                registration.owner.switch_to_live(
                    self._require_timeline().cursor
                )
            )
        return False

    def _drain_raw(self) -> None:
        while self._generation.alive:
            raw = self._ingress.try_pop()
            if raw is None:
                return
            if not self._raw_identity_matches(raw):
                continue
            try:
                timeline = self._require_timeline()
                update = timeline.ingest(raw)
                captured = captured_block_from_raw(
                    raw, update, sample_rate=self._resolved.sample_rate
                )
                ring = self._require_ring()
                ring.append(captured)
                self._last_ingested_at = raw.callback_monotonic
                self._apply_timeline_state(captured)
                self._advance_owners(captured)
            except CaptureLayerError as error:
                self._fail_generation(error.code, error.message)
                return
            except Exception as error:
                self._fail_generation(
                    CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                    f"input ingest failed: {error}",
                )
                return

    def _apply_timeline_state(self, captured) -> None:
        timeline = self._require_timeline()
        with self._state_lock:
            previous = self._state
            if timeline.state is TimelineState.TIMING_RECOVERY:
                if previous is not StreamState.OPENING:
                    self._state = StreamState.TIMING_RECOVERY
                    if previous is not StreamState.TIMING_RECOVERY:
                        self._recovery_count += 1
                return
            if timeline.state is not TimelineState.READY:
                return
        if previous is StreamState.OPENING:
            token = StreamReadyToken(
                hardware_id=self._resolved.hardware_id,
                pool_epoch=self._pool_epoch,
                stream_generation=self._generation_number,
                sample_rate=self._resolved.sample_rate,
                channels=self._resolved.channels,
                earliest_mappable_monotonic=captured.monotonic_start,
                latest_mappable_monotonic=captured.monotonic_end,
                earliest_sample=captured.sample_start,
                latest_sample=captured.sample_end,
            )
            if not self._generation.try_claim_readiness():
                return
            with self._state_lock:
                self._ready_token = token
                self._state = StreamState.READY
            self._retry.reset()
            try:
                self._ready_sink(token)
            except Exception:
                pass
        else:
            with self._state_lock:
                self._state = StreamState.READY

    def _advance_owners(self, captured) -> None:
        timeline = self._require_timeline()
        for _instance_id, registration in tuple(self._registrations.items()):
            if not registration.active:
                continue
            owner = registration.owner
            try:
                snapshot = owner.snapshot()
                if snapshot.state is CaptureOwnerState.WAITING_FOR_MAPPING:
                    owner.advance_pending_mapping(
                        timeline,
                        now_monotonic=captured.monotonic_end,
                    )
                    snapshot = owner.snapshot()
                if snapshot.state is CaptureOwnerState.CATCHING_UP:
                    owner.notify_ring_extended()
                elif snapshot.state is CaptureOwnerState.LIVE:
                    owner.offer_live(captured)
            except Exception as error:
                fail = getattr(owner, "fail", None)
                if callable(fail):
                    fail(
                        CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                        f"owner fan-out failed: {error}",
                    )
                self._retire_registration(registration)

    def _check_deadlines(self, now: float) -> None:
        if not math.isfinite(now):
            return
        with self._state_lock:
            state = self._state
            opened_at = self._opened_at
        if (
            state is StreamState.OPENING
            and opened_at is not None
            and now - opened_at >= INPUT_TIMEOUT_SECONDS
        ):
            self._fail_generation(
                CaptureErrorCode.FIRST_VALID_BLOCK_TIMEOUT,
                "no valid timestamped input block arrived within two seconds",
            )
        elif (
            state in (StreamState.READY, StreamState.TIMING_RECOVERY)
            and self._last_ingested_at is not None
            and now - self._last_ingested_at >= INPUT_TIMEOUT_SECONDS
        ):
            self._fail_generation(
                CaptureErrorCode.INPUT_DATA_TIMEOUT,
                "no input block arrived for two seconds",
            )

    def _consume_latched_failure(self) -> None:
        code = self._generation.failure_code
        if code is not None:
            self._fail_generation(
                code,
                self._generation.failure_message
                or "callback generation failure",
            )
        # Raw blocks already queued for a failed generation are stale by
        # definition. Discard them so the callback wake event can become idle.
        while self._ingress.try_pop() is not None:
            pass

    def _fail_generation(
        self,
        code: CaptureErrorCode,
        message: str,
        *,
        exception_type: str | None = None,
    ) -> bool:
        with self._state_lock:
            if self._state in (
                StreamState.FAILED,
                StreamState.CLOSING,
                StreamState.CLOSED,
            ):
                return False
        # This must precede owner/state publication so callbacks are inert first.
        self._generation.fail(code, message)
        with self._state_lock:
            self._state = StreamState.FAILED
            self._failure_code = code
            if code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP:
                self._overflow_count += 1
            if code in (
                CaptureErrorCode.INPUT_INGRESS_BACKLOG,
                CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
            ):
                self._backlog_count += 1
        event = StreamFailureEvent(
            hardware_id=self._resolved.hardware_id,
            pool_epoch=self._pool_epoch,
            stream_generation=self._generation_number,
            error_code=code,
            message=message,
            retry_delay=self._retry.next_delay(),
            exception_type=exception_type,
        )
        try:
            self._failure_sink(event)
        except Exception:
            pass
        for registration in tuple(self._registrations.values()):
            try:
                registration.owner.stream_failed(code, message)
            except Exception:
                pass
        for registration in tuple(self._registrations.values()):
            self._retire_registration(registration)
        return True

    def _begin_close(self) -> None:
        if self._generation.alive:
            self._generation.deactivate()
        with self._state_lock:
            self._state = StreamState.CLOSING
        for registration in tuple(self._registrations.values()):
            try:
                registration.owner.stream_failed(
                    CaptureErrorCode.STREAM_GENERATION_CHANGED,
                    "resident stream closed or replaced",
                )
            except Exception:
                pass
        for registration in tuple(self._registrations.values()):
            self._retire_registration(registration)
        self._running = False


__all__ = [
    "INPUT_TIMEOUT_SECONDS",
    "OwnerInstanceId",
    "RETRY_DELAYS_SECONDS",
    "ResidentStream",
    "ResidentStreamSnapshot",
    "RetryBackoff",
    "StreamFailureEvent",
    "StreamCommandIndeterminate",
]
