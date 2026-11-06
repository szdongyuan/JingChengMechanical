"""Owner of one spawned capture child and bounded metadata request transport."""
from dataclasses import dataclass, field
import multiprocessing
import queue
import re
import threading
import time
from uuid import uuid4

from app.service.analysis.resident_audio_mvp.contracts import ResidentRequestError

from .bootstrap import run
from .protocol import (PROTOCOL_VERSION, CaptureLimits, PreviewStreamConfig, WorkerConfig)
from .transport import PreviewRing
from .values import MAX_MESSAGE_BYTES, encode, pack, unpack


class CaptureProcessError(RuntimeError):
    pass


class CaptureBindingUnavailableError(ResidentRequestError):
    """The remote record was retired; the facade may have its local terminal proof."""


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    reply: object = None
    error: BaseException | None = None


class CaptureProcessClient:
    def __init__(self, config, endpoints, *, stream_specs, on_event, on_failure,
                 backend_factory=None, backend_options=None, startup_cancelled=None):
        if type(config) is not WorkerConfig:
            raise ValueError("config must be an exact WorkerConfig")
        config.__post_init__()
        if (backend_factory is not None and
                (type(backend_factory) is not str or len(backend_factory) > 256
                 or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", backend_factory) is None)):
            raise ValueError("backend_factory must name an explicit module-level factory")
        if backend_options is not None and type(backend_options) is not dict:
            raise ValueError("backend_options must contain value-only named options")
        options_bytes = pack(backend_options or {})
        self.config = config
        self.incarnation = uuid4().hex
        self.limits = CaptureLimits(config.max_capture_frames,
                                   tuple(item.input_channel_count for item in stream_specs),
                                   config.max_captures, config.max_retained_bytes)
        self._on_event, self._on_failure = on_event, on_failure
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._pipe_close_lock = threading.Lock()
        self._pipe_close_error = None
        self._pending = {}
        self._outgoing = queue.Queue(maxsize=64)
        self._startup = _Pending()
        self._failure = None
        self._stop = threading.Event()
        self._closing = False
        self.clean_close = None
        self._startup_complete = False
        self._startup_cancelled = startup_cancelled
        self._cleanup_errors = []
        self.preview = None
        self.connection = None
        self._child_connection = None
        self.process = None
        self._sender = None
        self._receiver = None
        self.context = multiprocessing.get_context("spawn")
        try:
            self._check_startup_cancelled()
            self.preview = PreviewRing.create(self.context, self.incarnation, tuple(
                PreviewStreamConfig(item.hardware_id, item.frames_per_buffer,
                                    item.input_channel_count, item.sample_rate_hz) for item in stream_specs))
            self._check_startup_cancelled()
            self.connection, self._child_connection = self.context.Pipe(duplex=True)
            self.process = self.context.Process(
                target=run, name="dedicated-audio-capture",
                args=(self._child_connection, config, encode(endpoints), self.preview.bootstrap,
                      backend_factory, options_bytes),
            )
            self._sender = threading.Thread(target=self._send_loop, name="capture-parent-control-writer", daemon=True)
            self._receiver = threading.Thread(target=self._receive_loop, name="capture-parent-control-reader", daemon=True)
            self._check_startup_cancelled()
            self.process.start()
            self._child_connection.close()
            self._sender.start()
            self._receiver.start()
            timeout = len(endpoints) * config.startup_stream_timeout_s + config.control_timeout_s
            deadline = time.monotonic() + timeout
            while not self._startup.event.wait(min(.05, max(0, deadline - time.monotonic()))):
                self._check_startup_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("capture child startup timed out")
            self._check_startup_cancelled()
            if self._startup.error:
                raise self._startup.error
            message = self._startup.reply
            self.startup = message["startup"]
            self.diagnostics = message["diagnostics"]
            self.owner_id = message["owner_id"]
            self._startup_complete = True
        except BaseException:
            try:
                self.close(config.shutdown_timeout_s)
            except BaseException as cleanup_error:
                # Retain the initial construction exception, even when a
                # cleanup primitive itself fails.
                self._record_cleanup_error("startup cleanup", cleanup_error)
            raise

    def _check_startup_cancelled(self):
        if self._startup_cancelled is not None and self._startup_cancelled.is_set():
            raise CaptureProcessError("capture child startup was cancelled by close")

    def request(self, op, *, timeout=None, **args):
        request_id = uuid4().hex
        pending = _Pending()
        message = pack(dict(version=PROTOCOL_VERSION, incarnation=self.incarnation,
                            request_id=request_id, op=op, args=args))
        with self._lock:
            if self._failure is not None:
                raise self._failure
            if len(self._pending) >= 64:
                raise CaptureProcessError("capture request capacity exhausted")
            self._pending[request_id] = pending
            try:
                self._outgoing.put_nowait(message)
            except queue.Full:
                self._pending.pop(request_id, None)
                raise CaptureProcessError("capture control sender capacity exhausted") from None
        if not pending.event.wait(self.config.control_timeout_s if timeout is None else timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            error = TimeoutError(f"capture {op} timed out; operation was not retried")
            # Receipt is idempotent. Every other ambiguous timeout fences this
            # incarnation, so neither a late reserve nor a start can be duplicated.
            if op != "receipt":
                self._fail(CaptureProcessError(str(error)))
            raise error
        if pending.error is not None:
            raise pending.error
        message = pending.reply
        if "error" in message:
            if message.get("error_kind") == "value":
                raise ValueError(message["error"].summary)
            if message.get("error_kind") == "binding_unavailable":
                raise CaptureBindingUnavailableError(message["error"])
            if message.get("error_kind") == "runtime":
                raise RuntimeError(message["error"].summary)
            raise ResidentRequestError(message["error"])
        return message.get("result")

    def _send_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    data = self._outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                self.connection.send_bytes(data)
        except (OSError, EOFError) as exc:
            self._fail(CaptureProcessError(f"capture child disconnected: {exc}"))

    def post(self, op, **args):
        """Nonblocking idempotent receipt from a control-reader callback."""
        if op != "terminal_receipt":
            raise ValueError("only terminal receipt can be posted")
        self._outgoing.put_nowait(pack(dict(version=PROTOCOL_VERSION, incarnation=self.incarnation,
                                           request_id=uuid4().hex, op=op, args=args)))

    def _receive_loop(self):
        try:
            while not self._stop.is_set():
                if not self.connection.poll(.05):
                    if not self.process.is_alive():
                        raise EOFError("capture child exited")
                    continue
                message = unpack(self.connection.recv_bytes(MAX_MESSAGE_BYTES))
                if (message.get("version") != PROTOCOL_VERSION
                        or message.get("incarnation") != self.incarnation):
                    raise ValueError("stale capture child message")
                kind = message["kind"]
                if kind == "startup":
                    self._startup.reply = message
                    self._startup.event.set()
                elif kind == "reply":
                    with self._lock:
                        pending = self._pending.pop(message["request_id"], None)
                    if pending is not None:
                        pending.reply = message
                        pending.event.set()
                elif kind == "fatal":
                    raise CaptureProcessError(message["error"])
                elif kind in ("readiness", "capture"):
                    self._on_event(message)
                else:
                    raise ValueError("unknown capture child message")
        except Exception as exc:
            self._fail(CaptureProcessError(f"capture child unavailable: {exc}"))

    def _fail(self, error):
        with self._lock:
            first_failure = self._failure is None
            if first_failure:
                self._failure = error
                pending = tuple(self._pending.values())
                self._pending.clear()
        if first_failure:
            self._startup.error = error
            self._startup.event.set()
            for item in pending:
                item.error = error
                item.event.set()
            self._stop.set()
            # Settlement must never depend on closing an already-invalid pipe.
            try:
                self._on_failure(error)
            finally:
                self._close_connection()
        else:
            # A prior close may have failed before releasing its handle. Retry
            # cleanup without publishing a second failure to canonical futures.
            self._close_connection()

    def _close_connection(self):
        # Connection.close's check/close/reset sequence is not atomic. Never
        # race two close callers, and do not block failure settlement behind one.
        if not self._pipe_close_lock.acquire(blocking=False):
            return False
        try:
            return self._close_endpoint(self.connection)
        finally:
            self._pipe_close_lock.release()

    def _record_cleanup_error(self, stage, error):
        if len(self._cleanup_errors) < 8:
            self._cleanup_errors.append(f"{stage}: {error}"[:256])

    def _close_endpoint(self, endpoint):
        if endpoint is None:
            return True
        try:
            if not endpoint.closed:
                endpoint.close()
        except Exception as exc:
            self._pipe_close_error = str(exc)[:256]
            return False
        return True

    def close(self, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        if not self._close_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            if self.clean_close is True:
                self._close_connection()
                self._close_endpoint(self._child_connection)
                return self.clean_close
            # A non-clean report stays non-clean, but retry unfinished owned
            # cleanup rather than caching leaked handles forever.
            return self._close_owned(deadline)
        finally:
            self._close_lock.release()

    def _close_owned(self, deadline):
        clean = self._failure is None and self._startup_complete
        self._closing = True
        self.close_results = None
        if not self._startup_complete:
            try:
                self._fail(CaptureProcessError("capture child startup did not complete"))
            except Exception as exc:
                self._record_cleanup_error("startup failure publication", exc)
        if not clean:
            # Retry an earlier failed pipe close before waiting for child exit;
            # otherwise an open parent handle prevents the child's EOF cleanup.
            self._close_connection()
        remaining = max(0, deadline - time.monotonic())
        process = self.process
        if process is not None and process.pid is not None and process.is_alive() and clean and remaining:
            try:
                self.close_results = self.request("close", timeout=max(.01, remaining * .7),
                                                  timeout_seconds=max(.01, remaining * .6))
            except Exception:
                clean = False
        clean = self._close_endpoint(self._child_connection) and clean
        if process is not None and process.pid is not None:
            try:
                process.join(max(0, deadline - time.monotonic()))
            except Exception as exc:
                self._record_cleanup_error("child join", exc)
                clean = False
            if process.is_alive():
                clean = False
                try:
                    process.terminate()
                except Exception as exc:
                    self._record_cleanup_error("child termination", exc)
                try:
                    process.join(.3)
                except Exception as exc:
                    self._record_cleanup_error("terminated child join", exc)
        try:
            self._fail(CaptureProcessError("capture child closed"))
        except Exception as exc:
            self._record_cleanup_error("close failure publication", exc)
            clean = False
        clean = self._close_connection() and clean and self._pipe_close_error is None
        for thread in (self._sender, self._receiver):
            if thread is not None and thread.ident is not None:
                if thread is not threading.current_thread():
                    try:
                        thread.join(max(0, deadline - time.monotonic()))
                    except Exception as exc:
                        self._record_cleanup_error("control thread join", exc)
                        clean = False
                clean = clean and not thread.is_alive()
        if self.preview is not None:
            try:
                clean = self.preview.close(max(0, deadline - time.monotonic())) and clean
            except Exception as exc:
                self._record_cleanup_error("preview close", exc)
                clean = False
        self.clean_close = clean and not self._cleanup_errors and process is not None and process.exitcode == 0
        return self.clean_close
