from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count

_STDERR_BUFFER_LIMIT = 64 * 1024
_EXCERPT_LIMIT = 2000
_SHUTDOWN_GRACE_SECONDS = 2.0
_POST_REQUEST_REUSE_FENCE_SECONDS = 0.25
_POST_REQUEST_REUSE_FENCE_POLL_SECONDS = 0.005
_STARTUP_STDERR_SETTLE_SECONDS = 0.15
_STARTUP_STDERR_SETTLE_QUIET_SECONDS = 0.02
_STARTUP_STDERR_SETTLE_POLL_SECONDS = 0.005
_STARTUP_STDERR_MIN_REQUEST_BUDGET_SECONDS = 0.05
_POST_RESPONSE_STDOUT_SETTLE_SECONDS = 0.02
_POST_RESPONSE_STDOUT_SETTLE_POLL_SECONDS = 0.005
_TZ8 = timezone(timedelta(hours=8))
_STDOUT_EOF = object()


class _RetryablePreSendInvalidationError(Exception):
    pass


class _PreSendInvalidationError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(_TZ8).isoformat()


def _split_entrypoint(entrypoint: str) -> list[str]:
    if os.name == "nt":
        if not entrypoint.strip():
            return []
        import ctypes
        from ctypes import wintypes

        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_int),
        ]
        command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
        argc = ctypes.c_int()
        argv_ptr = command_line_to_argv(entrypoint, ctypes.byref(argc))
        if not argv_ptr:
            raise ctypes.WinError()
        try:
            return [argv_ptr[index] for index in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv_ptr)
    return shlex.split(entrypoint)


def _slice_utf8_text(text: str, start_offset: int) -> str:
    if start_offset <= 0:
        return text
    encoded = text.encode("utf-8")
    if start_offset >= len(encoded):
        return ""
    return encoded[start_offset:].decode("utf-8", errors="ignore")


@dataclass
class _ResidentProcess:
    toolchain_id: str
    entrypoint: str
    process: subprocess.Popen[str]
    instance_id: int
    stdout_items: deque[object] = field(default_factory=deque)
    stdout_condition: threading.Condition = field(default_factory=threading.Condition)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_chunks: deque[tuple[int, str, int]] = field(default_factory=deque)
    stderr_total_bytes: int = 0
    stderr_buffer_byte_count: int = 0
    stderr_buffer_char_count: int = 0
    request_active: bool = False
    request_pending_send: bool = False
    request_send_started: bool = False
    request_write_flushed: bool = False
    idle_stdout_invalidated: bool = False
    idle_stderr_invalidated: bool = False
    active_stdout_line_count: int = 0
    active_stdout_protocol_error: str | None = None
    pending_stderr_start_position: int | None = None
    active_stderr_start_position: int | None = None
    active_stderr_excerpt: str = ""
    completed_request_count: int = 0
    post_request_reuse_fence_deadline: float | None = None

    def push_stdout_item(self, item: object) -> None:
        with self.stdout_condition:
            self.stdout_items.append(item)
            self.stdout_condition.notify()

    def wait_for_stdout_item(self, timeout_s: float) -> object:
        with self.stdout_condition:
            if not self.stdout_items:
                notified = self.stdout_condition.wait(timeout=timeout_s)
                if not notified and not self.stdout_items:
                    raise TimeoutError("Timed out waiting for resident stdout")
            if not self.stdout_items:
                raise TimeoutError("Timed out waiting for resident stdout")
            return self.stdout_items.popleft()

    def has_pending_stdout(self) -> bool:
        with self.stdout_condition:
            return bool(self.stdout_items)

    def begin_request(self, *, request_deadline: float | None = None) -> bool:
        if self._settle_idle_stderr_before_reuse(request_deadline=request_deadline):
            return False
        with self.state_lock:
            if self.request_active or self.request_pending_send:
                return False
            if self.idle_stdout_invalidated or self.idle_stderr_invalidated:
                return False
            self.request_pending_send = True
            self.request_send_started = False
            self.request_write_flushed = False
            self.request_active = False
            self.active_stdout_line_count = 0
            self.active_stdout_protocol_error = None
            self.post_request_reuse_fence_deadline = None
        with self.stderr_lock:
            self.pending_stderr_start_position = self.stderr_total_bytes
            self.active_stderr_start_position = None
            self.active_stderr_excerpt = ""
        return True

    def ready_to_send(self) -> bool:
        with self.state_lock:
            return (
                self.request_pending_send
                and not self.request_send_started
                and not self.request_write_flushed
                and not self.request_active
                and not self.idle_stdout_invalidated
                and not self.idle_stderr_invalidated
            )

    def mark_request_send_started(self) -> bool:
        with self.state_lock:
            if (
                not self.request_pending_send
                or self.request_active
                or self.idle_stdout_invalidated
                or self.idle_stderr_invalidated
            ):
                return False
            self.request_send_started = True
        return True

    def mark_request_flushed(self) -> None:
        with self.state_lock:
            self.request_write_flushed = self.request_pending_send

    def activate_request(self) -> bool:
        with self.state_lock:
            if (
                not self.request_pending_send
                or not self.request_send_started
                or self.request_active
                or self.idle_stdout_invalidated
                or self.idle_stderr_invalidated
            ):
                return False
            with self.stderr_lock:
                if self.pending_stderr_start_position is None:
                    self.active_stderr_start_position = self.stderr_total_bytes
                else:
                    self.active_stderr_start_position = (
                        self.pending_stderr_start_position
                    )
                self.pending_stderr_start_position = None
            self.request_pending_send = False
            self.request_send_started = False
            self.request_write_flushed = False
            self.request_active = True
        return True

    def _current_request_stderr_excerpt_locked(self) -> str:
        if self.active_stderr_excerpt:
            return self.active_stderr_excerpt

        start_position = self.active_stderr_start_position
        if start_position is None:
            start_position = self.pending_stderr_start_position
        if start_position is None:
            return ""

        parts: list[str] = []
        remaining = _EXCERPT_LIMIT
        for chunk_start, chunk_text, chunk_bytes in self.stderr_chunks:
            chunk_end = chunk_start + chunk_bytes
            if chunk_end <= start_position:
                continue
            if chunk_start < start_position:
                excerpt_chunk = _slice_utf8_text(
                    chunk_text,
                    start_position - chunk_start,
                )
            else:
                excerpt_chunk = chunk_text
            if not excerpt_chunk:
                continue
            parts.append(excerpt_chunk[:remaining])
            remaining -= len(parts[-1])
            if remaining <= 0:
                break
        return "".join(parts)

    def cancel_request(self, *, capture_stderr: bool = False) -> str:
        with self.state_lock:
            self.request_pending_send = False
            self.request_send_started = False
            self.request_write_flushed = False
            self.request_active = False
            self.active_stdout_line_count = 0
            self.active_stdout_protocol_error = None
        with self.stderr_lock:
            excerpt = (
                self._current_request_stderr_excerpt_locked()
                if capture_stderr
                else ""
            )
            self.pending_stderr_start_position = None
            self.active_stderr_start_position = None
            self.active_stderr_excerpt = ""
        return excerpt

    def finish_request(self) -> tuple[str | None, str]:
        with self.state_lock:
            self.request_pending_send = False
            self.request_send_started = False
            self.request_write_flushed = False
            self.request_active = False
            self.completed_request_count += 1
            self.post_request_reuse_fence_deadline = (
                time.monotonic() + _POST_REQUEST_REUSE_FENCE_SECONDS
            )
            violation = self.active_stdout_protocol_error
            self.active_stdout_line_count = 0
            self.active_stdout_protocol_error = None
        with self.stderr_lock:
            excerpt = self.active_stderr_excerpt
            self.pending_stderr_start_position = None
            self.active_stderr_start_position = None
            self.active_stderr_excerpt = ""
        return violation, excerpt

    def route_stdout_line(self, line: str) -> None:
        with self.state_lock:
            active = self.request_active or self.request_send_started
            if not active:
                self.idle_stdout_invalidated = True
                should_queue = False
            else:
                self.active_stdout_line_count += 1
                if self.active_stdout_line_count == 1:
                    should_queue = True
                else:
                    if self.active_stdout_protocol_error is None:
                        self.active_stdout_protocol_error = line
                    should_queue = False
        if should_queue:
            self.push_stdout_item(line)

    def needs_restart(self) -> bool:
        if self.process.poll() is not None:
            return True
        if self.has_pending_stdout():
            return True
        with self.state_lock:
            return (
                self.request_pending_send
                or self.request_send_started
                or self.idle_stdout_invalidated
                or self.idle_stderr_invalidated
            )

    def active_stdout_state(self) -> tuple[int, str | None]:
        with self.state_lock:
            return self.active_stdout_line_count, self.active_stdout_protocol_error

    def mark_idle_stderr_invalidated(self) -> None:
        with self.state_lock:
            self.idle_stderr_invalidated = True

    def _settle_idle_stderr_before_reuse(
        self,
        *,
        request_deadline: float | None = None,
    ) -> bool:
        with self.state_lock:
            if self.completed_request_count <= 0:
                return False
            if self.idle_stdout_invalidated or self.idle_stderr_invalidated:
                return True
            fence_deadline = self.post_request_reuse_fence_deadline
        if fence_deadline is None:
            return False
        while True:
            wait_deadline = fence_deadline
            if request_deadline is not None:
                wait_deadline = min(wait_deadline, request_deadline)
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                with self.state_lock:
                    if self.idle_stdout_invalidated or self.idle_stderr_invalidated:
                        return True
                    if self.post_request_reuse_fence_deadline != fence_deadline:
                        return False
                    if (
                        request_deadline is not None
                        and fence_deadline > request_deadline
                    ):
                        raise TimeoutError(
                            "Timed out waiting for resident reuse fence"
                        )
                    self.post_request_reuse_fence_deadline = None
                return False
            time.sleep(min(_POST_REQUEST_REUSE_FENCE_POLL_SECONDS, remaining))
            with self.state_lock:
                if self.idle_stdout_invalidated or self.idle_stderr_invalidated:
                    return True

    def stderr_position(self) -> int:
        with self.stderr_lock:
            return self.stderr_total_bytes

    def reserve_stderr_chunk(self, chunk: str) -> tuple[int, int]:
        chunk_bytes = len(chunk.encode("utf-8"))
        with self.stderr_lock:
            start = self.stderr_total_bytes
            self.stderr_total_bytes += chunk_bytes
            return start, chunk_bytes

    def append_stderr(self, chunk: str, *, start_position: int | None = None) -> None:
        if not chunk:
            return
        if start_position is None:
            start_position, chunk_bytes = self.reserve_stderr_chunk(chunk)
        else:
            chunk_bytes = len(chunk.encode("utf-8"))
        if chunk_bytes <= 0:
            return
        mark_idle_stderr = False
        with self.state_lock:
            request_pending_send = self.request_pending_send
            request_send_started = self.request_send_started
            request_write_flushed = self.request_write_flushed
            completed_request_count = self.completed_request_count
        with self.stderr_lock:
            self.stderr_chunks.append((start_position, chunk, chunk_bytes))
            self.stderr_buffer_byte_count += chunk_bytes
            self.stderr_buffer_char_count += len(chunk)
            active_start = self.active_stderr_start_position
            if active_start is None:
                if request_send_started or (
                    request_pending_send and request_write_flushed
                ):
                    active_start = self.pending_stderr_start_position
                elif request_pending_send or completed_request_count > 0:
                    mark_idle_stderr = True
            if (
                active_start is not None
                and len(self.active_stderr_excerpt) < _EXCERPT_LIMIT
                and (start_position + chunk_bytes) > active_start
            ):
                excerpt_chunk = chunk
                if start_position < active_start:
                    excerpt_chunk = _slice_utf8_text(
                        chunk,
                        active_start - start_position,
                    )
                remaining = _EXCERPT_LIMIT - len(self.active_stderr_excerpt)
                self.active_stderr_excerpt += excerpt_chunk[:remaining]
            while (
                self.stderr_buffer_byte_count > _STDERR_BUFFER_LIMIT
                and self.stderr_chunks
            ):
                _, chunk_text, buffered_bytes = self.stderr_chunks.popleft()
                self.stderr_buffer_byte_count -= buffered_bytes
                self.stderr_buffer_char_count -= len(chunk_text)
        if mark_idle_stderr:
            self.mark_idle_stderr_invalidated()

    def stderr_since(self, position: int) -> str:
        with self.stderr_lock:
            parts: list[str] = []
            for chunk_start, chunk_text, chunk_bytes in self.stderr_chunks:
                chunk_end = chunk_start + chunk_bytes
                if chunk_end <= position:
                    continue
                if chunk_start < position:
                    parts.append(_slice_utf8_text(chunk_text, position - chunk_start))
                    continue
                parts.append(chunk_text)
            return "".join(parts)

    def current_stderr_excerpt(self) -> str:
        with self.stderr_lock:
            return self.active_stderr_excerpt


class ResidentToolchainRuntime:
    def __init__(self) -> None:
        self._cache_lock = threading.Lock()
        self._processes: dict[str, _ResidentProcess] = {}
        self._leases: dict[str, threading.Lock] = {}
        self._instance_ids = count(1)

    def has_cached_process(self, toolchain_id: str) -> bool:
        with self._cache_lock:
            return toolchain_id in self._processes

    def analyze(
        self,
        *,
        toolchain_id: str,
        entrypoint: str,
        request_id: str,
        request_payload: dict,
        artifacts_dir: str,
        timeout_ms: int,
    ) -> dict:
        started_at = _now_iso()
        started_mono = time.monotonic()
        lease = self._try_acquire_lease(toolchain_id)
        if lease is None:
            return self._build_result(
                status="process_error",
                started_at=started_at,
                started_mono=started_mono,
                error_summary="Resident toolchain is busy",
                stderr_excerpt="",
            )

        cached_process: _ResidentProcess | None = None
        process_instance_id: int | None = None
        request_started = False
        request_finished = False
        try:
            deadline = started_mono + (timeout_ms / 1000.0)
            while True:
                try:
                    self._remaining_timeout(deadline)
                except TimeoutError:
                    if cached_process is not None:
                        self._discard_cached_process(
                            toolchain_id,
                            cached_process,
                            deadline=deadline,
                        )
                    return self._build_result(
                        status="timeout",
                        started_at=started_at,
                        started_mono=started_mono,
                        error_summary="Process timed out",
                        stderr_excerpt="",
                        process_instance_id=process_instance_id,
                    )
                try:
                    cached_process, _ = self._acquire_process(
                        toolchain_id,
                        entrypoint,
                        deadline=deadline,
                    )
                except TimeoutError:
                    return self._build_result(
                        status="timeout",
                        started_at=started_at,
                        started_mono=started_mono,
                        error_summary="Process timed out",
                        stderr_excerpt="",
                        process_instance_id=process_instance_id,
                    )
                process_instance_id = cached_process.instance_id
                try:
                    begin_request_started = cached_process.begin_request(
                        request_deadline=deadline
                    )
                except TimeoutError:
                    self._discard_cached_process(
                        toolchain_id,
                        cached_process,
                        deadline=deadline,
                    )
                    return self._build_result(
                        status="timeout",
                        started_at=started_at,
                        started_mono=started_mono,
                        error_summary="Process timed out",
                        stderr_excerpt="",
                        process_instance_id=process_instance_id,
                    )
                if begin_request_started:
                    try:
                        self._remaining_timeout(deadline)
                        self._send_request(
                            cached_process=cached_process,
                            request_id=request_id,
                            request_payload=request_payload,
                            artifacts_dir=artifacts_dir,
                        )
                        request_started = True
                        break
                    except (
                        _RetryablePreSendInvalidationError,
                        _PreSendInvalidationError,
                    ):
                        cached_process.cancel_request()
                        self._discard_cached_process(
                            toolchain_id,
                            cached_process,
                            deadline=deadline,
                        )
                        cached_process = None
                        process_instance_id = None
                        continue
                    except TimeoutError:
                        stderr_excerpt = cached_process.cancel_request(
                            capture_stderr=True
                        )
                        self._discard_cached_process(
                            toolchain_id,
                            cached_process,
                            deadline=deadline,
                        )
                        return self._build_result(
                            status="timeout",
                            started_at=started_at,
                            started_mono=started_mono,
                            error_summary="Process timed out",
                            stderr_excerpt=stderr_excerpt,
                            process_instance_id=process_instance_id,
                        )
                    except (BrokenPipeError, OSError, ValueError) as exc:
                        stderr_excerpt = cached_process.cancel_request(
                            capture_stderr=True
                        )
                        self._discard_cached_process(
                            toolchain_id,
                            cached_process,
                            deadline=deadline,
                        )
                        return self._build_result(
                            status="process_error",
                            started_at=started_at,
                            started_mono=started_mono,
                            error_summary=str(exc)[:_EXCERPT_LIMIT],
                            stderr_excerpt=stderr_excerpt,
                            process_instance_id=process_instance_id,
                        )
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline,
                )
                cached_process = None
            try:
                response_line = cached_process.wait_for_stdout_item(
                    self._remaining_timeout(deadline)
                )
            except TimeoutError:
                if request_started:
                    _, stderr_excerpt = self._complete_request(
                        cached_process,
                        deadline=deadline,
                    )
                    request_finished = True
                else:
                    cached_process.cancel_request()
                    stderr_excerpt = ""
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline,
                )
                return self._build_result(
                    status="timeout",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Process timed out",
                    stderr_excerpt=stderr_excerpt,
                    process_instance_id=cached_process.instance_id,
                )
            except (BrokenPipeError, OSError, ValueError) as exc:
                if request_started:
                    _, stderr_excerpt = self._complete_request(
                        cached_process,
                        deadline=deadline,
                    )
                    request_finished = True
                else:
                    cached_process.cancel_request()
                    stderr_excerpt = ""
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline,
                )
                return self._build_result(
                    status="process_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=str(exc)[:_EXCERPT_LIMIT],
                    stderr_excerpt=stderr_excerpt,
                    process_instance_id=cached_process.instance_id,
                )

            parsed = self._parse_response_line(response_line)
            discard_after_completion = False

            if parsed["status"] == "process_exit":
                discard_after_completion = True
                result = self._build_result(
                    status="process_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Resident process exited unexpectedly",
                    stderr_excerpt="",
                    process_instance_id=cached_process.instance_id,
                )
            elif parsed["status"] == "protocol_error":
                discard_after_completion = True
                result = self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=parsed["error"],
                    stderr_excerpt="",
                    process_instance_id=cached_process.instance_id,
                )
            else:
                envelope = parsed["envelope"]
                outer_request_id = envelope.get("request_id")
                if not isinstance(outer_request_id, str) or outer_request_id != request_id:
                    discard_after_completion = True
                    result = self._build_result(
                        status="protocol_error",
                        started_at=started_at,
                        started_mono=started_mono,
                        error_summary="Resident response request_id mismatch",
                        stderr_excerpt="",
                        process_instance_id=cached_process.instance_id,
                    )
                else:
                    message_type = envelope.get("message_type")
                    if message_type == "error":
                        error_summary = envelope.get("error")
                        if not isinstance(error_summary, str) or not error_summary:
                            error_summary = "Resident process reported an error"
                        discard_after_completion = True
                        result = self._build_result(
                            status="process_error",
                            started_at=started_at,
                            started_mono=started_mono,
                            error_summary=error_summary,
                            stderr_excerpt="",
                            process_instance_id=cached_process.instance_id,
                        )
                    elif message_type != "result":
                        discard_after_completion = True
                        result = self._build_result(
                            status="protocol_error",
                            started_at=started_at,
                            started_mono=started_mono,
                            error_summary="Resident response message_type is invalid",
                            stderr_excerpt="",
                            process_instance_id=cached_process.instance_id,
                        )
                    else:
                        response_payload = envelope.get("response")
                        if not isinstance(response_payload, dict):
                            discard_after_completion = True
                            result = self._build_result(
                                status="protocol_error",
                                started_at=started_at,
                                started_mono=started_mono,
                                error_summary="Resident response is missing response",
                                stderr_excerpt="",
                                process_instance_id=cached_process.instance_id,
                            )
                        else:
                            result = self._build_result(
                                status="success",
                                started_at=started_at,
                                started_mono=started_mono,
                                response_payload=response_payload,
                                stderr_excerpt="",
                                process_instance_id=cached_process.instance_id,
                            )

            active_stdout_error, stderr_excerpt = self._complete_request(
                cached_process,
                deadline=deadline,
            )
            request_finished = True
            result["stderr_excerpt"] = stderr_excerpt
            if active_stdout_error is not None:
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline,
                )
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=self._build_extra_stdout_error(active_stdout_error),
                    stderr_excerpt=stderr_excerpt,
                    process_instance_id=cached_process.instance_id,
                )
            if discard_after_completion:
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline,
                )
            result["finished_at"] = _now_iso()
            result["duration_ms"] = int((time.monotonic() - started_mono) * 1000)
            return result
        except Exception as exc:
            if cached_process is not None:
                if request_started and not request_finished:
                    _, stderr_excerpt = self._complete_request(
                        cached_process,
                        deadline=deadline if "deadline" in locals() else None,
                    )
                elif not request_started:
                    stderr_excerpt = cached_process.cancel_request(
                        capture_stderr=True
                    )
                else:
                    stderr_excerpt = ""
                self._discard_cached_process(
                    toolchain_id,
                    cached_process,
                    deadline=deadline if "deadline" in locals() else None,
                )
                process_instance_id = cached_process.instance_id
            else:
                stderr_excerpt = ""
                process_instance_id = None
            return self._build_result(
                status="process_error",
                started_at=started_at,
                started_mono=started_mono,
                error_summary=str(exc)[:_EXCERPT_LIMIT],
                stderr_excerpt=stderr_excerpt,
                process_instance_id=process_instance_id,
            )
        finally:
            lease.release()

    def invalidate(self, toolchain_id: str) -> None:
        cached_process = self._pop_cached_process(toolchain_id)
        if cached_process is not None:
            self._stop_process(cached_process)

    @contextmanager
    def hold_mutation_lease(self, toolchain_id: str):
        lease = self._try_acquire_lease(toolchain_id)
        if lease is None:
            raise ValueError("Resident toolchain is busy")
        try:
            yield
        finally:
            lease.release()

    def ensure_mutation_allowed(self, toolchain_id: str) -> None:
        with self.hold_mutation_lease(toolchain_id):
            return

    def shutdown_all(self) -> None:
        with self._cache_lock:
            cached_processes = list(self._processes.values())
            self._processes.clear()
        for cached_process in cached_processes:
            self._stop_process(cached_process)

    def _lease_for(self, toolchain_id: str) -> threading.Lock:
        with self._cache_lock:
            lease = self._leases.get(toolchain_id)
            if lease is None:
                lease = threading.Lock()
                self._leases[toolchain_id] = lease
            return lease

    def _try_acquire_lease(self, toolchain_id: str) -> threading.Lock | None:
        lease = self._lease_for(toolchain_id)
        if not lease.acquire(blocking=False):
            return None
        return lease

    def _acquire_process(
        self,
        toolchain_id: str,
        entrypoint: str,
        *,
        deadline: float | None = None,
    ) -> tuple[_ResidentProcess, bool]:
        stale_process = None
        with self._cache_lock:
            cached_process = self._processes.get(toolchain_id)
            if cached_process is not None and (
                cached_process.entrypoint != entrypoint or cached_process.needs_restart()
            ):
                stale_process = self._processes.pop(toolchain_id)
                cached_process = None
            if cached_process is not None:
                return cached_process, False

        if stale_process is not None:
            self._stop_process(stale_process, deadline=deadline)
            if deadline is not None:
                self._remaining_timeout(deadline)

        created = self._start_process(toolchain_id, entrypoint)
        try:
            self._settle_startup_stderr(created, deadline=deadline)
        except Exception:
            self._stop_process(created, deadline=deadline)
            raise
        with self._cache_lock:
            self._processes[toolchain_id] = created
        return created, True

    def _start_process(self, toolchain_id: str, entrypoint: str) -> _ResidentProcess:
        cmd = _split_entrypoint(entrypoint)
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("Resident process stdio pipes are unavailable")

        cached_process = _ResidentProcess(
            toolchain_id=toolchain_id,
            entrypoint=entrypoint,
            process=process,
            instance_id=next(self._instance_ids),
        )
        threading.Thread(
            target=self._drain_stdout,
            args=(cached_process,),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._drain_stderr,
            args=(cached_process,),
            daemon=True,
        ).start()
        return cached_process

    def _drain_stdout(self, cached_process: _ResidentProcess) -> None:
        try:
            assert cached_process.process.stdout is not None
            for raw_line in cached_process.process.stdout:
                cached_process.route_stdout_line(raw_line.rstrip("\r\n"))
        finally:
            cached_process.push_stdout_item(_STDOUT_EOF)

    def _drain_stderr(self, cached_process: _ResidentProcess) -> None:
        try:
            assert cached_process.process.stderr is not None
            while True:
                chunk = cached_process.process.stderr.read(1)
                if not chunk:
                    break
                start_position, _ = cached_process.reserve_stderr_chunk(chunk)
                cached_process.append_stderr(chunk, start_position=start_position)
        except Exception:
            return

    def _send_request(
        self,
        *,
        cached_process: _ResidentProcess,
        request_id: str,
        request_payload: dict,
        artifacts_dir: str,
    ) -> None:
        stdin = cached_process.process.stdin
        if stdin is None:
            raise BrokenPipeError("Resident process stdin is unavailable")
        if not cached_process.ready_to_send():
            raise _RetryablePreSendInvalidationError()
        if not cached_process.mark_request_send_started():
            raise _RetryablePreSendInvalidationError()
        payload = {
            "message_type": "analyze",
            "request_id": request_id,
            "request": request_payload,
            "artifacts_dir": artifacts_dir,
        }
        stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdin.flush()
        cached_process.mark_request_flushed()
        if not cached_process.activate_request():
            raise _PreSendInvalidationError()

    @staticmethod
    def _parse_response_line(line: object) -> dict:
        if line is _STDOUT_EOF:
            return {"status": "process_exit"}
        if not isinstance(line, str):
            return {"status": "protocol_error", "error": "Resident stdout is invalid"}
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            return {
                "status": "protocol_error",
                "error": "Resident stdout line is not valid JSON",
            }
        if not isinstance(envelope, dict):
            return {
                "status": "protocol_error",
                "error": "Resident stdout envelope must be a JSON object",
            }
        return {"status": "ok", "envelope": envelope}

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for resident response")
        return remaining

    def _capture_stderr_excerpt(
        self,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None = None,
    ) -> str:
        last_seen = cached_process.stderr_position()
        saw_post_response_growth = False
        settle_timeout = self._bounded_wait_timeout(0.1, deadline)
        if settle_timeout is None:
            return cached_process.current_stderr_excerpt()
        settle_deadline = time.monotonic() + settle_timeout
        while True:
            remaining = settle_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
            current = cached_process.stderr_position()
            if current == last_seen:
                break
            saw_post_response_growth = True
            last_seen = current
        if saw_post_response_growth:
            cached_process.mark_idle_stderr_invalidated()
        return cached_process.current_stderr_excerpt()

    def _settle_startup_stderr(
        self,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None,
    ) -> None:
        now = time.monotonic()
        if deadline is not None and deadline <= now:
            raise TimeoutError("Timed out waiting for resident response")
        wait_for_growth_seconds = _STARTUP_STDERR_SETTLE_SECONDS
        if deadline is not None:
            remaining = deadline - now
            wait_for_growth_seconds = min(
                wait_for_growth_seconds,
                max(0.0, remaining - _STARTUP_STDERR_MIN_REQUEST_BUDGET_SECONDS),
            )
        if wait_for_growth_seconds <= 0:
            return
        wait_for_growth_deadline = now + wait_for_growth_seconds

        last_seen = cached_process.stderr_position()
        observed_growth = False
        quiet_deadline = now + _STARTUP_STDERR_SETTLE_QUIET_SECONDS
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise TimeoutError("Timed out waiting for resident response")
            if observed_growth:
                if now >= quiet_deadline:
                    return
                next_deadline = quiet_deadline
            else:
                if now >= wait_for_growth_deadline:
                    return
                next_deadline = wait_for_growth_deadline
            if deadline is not None:
                next_deadline = min(next_deadline, deadline)
            if next_deadline <= now:
                return
            time.sleep(
                min(
                    _STARTUP_STDERR_SETTLE_POLL_SECONDS,
                    next_deadline - now,
                )
            )
            current = cached_process.stderr_position()
            if current != last_seen:
                observed_growth = True
                last_seen = current
                quiet_deadline = (
                    time.monotonic() + _STARTUP_STDERR_SETTLE_QUIET_SECONDS
                )

    def _capture_active_stdout_violation(
        self,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None = None,
    ) -> None:
        _, violation = cached_process.active_stdout_state()
        if violation is not None:
            return
        settle_timeout = self._bounded_wait_timeout(
            _POST_RESPONSE_STDOUT_SETTLE_SECONDS,
            deadline,
        )
        if settle_timeout is None:
            return
        settle_deadline = time.monotonic() + settle_timeout
        while True:
            remaining = settle_deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_POST_RESPONSE_STDOUT_SETTLE_POLL_SECONDS, remaining))
            _, violation = cached_process.active_stdout_state()
            if violation is not None:
                return

    def _complete_request(
        self,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None = None,
    ) -> tuple[str | None, str]:
        self._capture_stderr_excerpt(cached_process, deadline=deadline)
        self._capture_active_stdout_violation(cached_process, deadline=deadline)
        return cached_process.finish_request()

    @staticmethod
    def _build_extra_stdout_error(extra_line: str) -> str:
        suffix = extra_line[:_EXCERPT_LIMIT]
        if suffix:
            return f"Resident emitted unexpected extra stdout during active request: {suffix}"
        return "Resident emitted unexpected extra stdout during active request"

    def _discard_cached_process(
        self,
        toolchain_id: str,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None = None,
    ) -> None:
        with self._cache_lock:
            current = self._processes.get(toolchain_id)
            if current is cached_process:
                self._processes.pop(toolchain_id, None)
        self._stop_process(cached_process, deadline=deadline)

    def _pop_cached_process(self, toolchain_id: str) -> _ResidentProcess | None:
        with self._cache_lock:
            return self._processes.pop(toolchain_id, None)

    @staticmethod
    def _bounded_wait_timeout(limit_s: float, deadline: float | None) -> float | None:
        if deadline is None:
            return limit_s
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return min(limit_s, remaining)

    @classmethod
    def _wait_for_process_exit(
        cls,
        process: subprocess.Popen[str],
        *,
        limit_s: float,
        deadline: float | None,
    ) -> bool:
        if process.poll() is not None:
            return True
        wait_timeout = cls._bounded_wait_timeout(limit_s, deadline)
        if wait_timeout is None:
            return False
        try:
            process.wait(timeout=wait_timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _stop_process(
        self,
        cached_process: _ResidentProcess,
        *,
        deadline: float | None = None,
    ) -> None:
        process = cached_process.process
        stdin = process.stdin
        if process.poll() is None and stdin is not None and not stdin.closed:
            try:
                stdin.write(json.dumps({"message_type": "shutdown"}) + "\n")
                stdin.flush()
            except Exception:
                pass

        if not self._wait_for_process_exit(
            process,
            limit_s=_SHUTDOWN_GRACE_SECONDS,
            deadline=deadline,
        ):
            try:
                process.terminate()
            except Exception:
                pass
            if not self._wait_for_process_exit(
                process,
                limit_s=1.0,
                deadline=deadline,
            ):
                try:
                    process.kill()
                except Exception:
                    pass
                self._wait_for_process_exit(
                    process,
                    limit_s=1.0,
                    deadline=deadline,
                )

        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                continue

    @staticmethod
    def _build_result(
        *,
        status: str,
        started_at: str,
        started_mono: float,
        error_summary: str | None = None,
        response_payload: dict | None = None,
        stderr_excerpt: str = "",
        process_instance_id: int | None = None,
    ) -> dict:
        return {
            "module_status": status,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "duration_ms": int((time.monotonic() - started_mono) * 1000),
            "error_summary": error_summary,
            "response_payload": response_payload,
            "stdout_excerpt": None,
            "stderr_excerpt": stderr_excerpt,
            "process_instance_id": process_instance_id,
        }
