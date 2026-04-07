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
_TZ8 = timezone(timedelta(hours=8))
_STDOUT_EOF = object()


def _now_iso() -> str:
    return datetime.now(_TZ8).isoformat()


def _split_entrypoint(entrypoint: str) -> list[str]:
    if os.name == "nt":
        parts = shlex.split(entrypoint, posix=False)
        cleaned: list[str] = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}:
                cleaned.append(part[1:-1])
            else:
                cleaned.append(part)
        return cleaned
    return shlex.split(entrypoint)


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
    idle_stdout_invalidated: bool = False
    active_stdout_line_count: int = 0
    active_stdout_protocol_error: str | None = None
    active_stderr_excerpt: str = ""
    active_stderr_capture: bool = False

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

    def begin_request(self) -> None:
        with self.state_lock:
            self.request_active = True
            self.active_stdout_line_count = 0
            self.active_stdout_protocol_error = None
        with self.stderr_lock:
            self.active_stderr_excerpt = ""
            self.active_stderr_capture = True

    def finish_request(self) -> str | None:
        with self.state_lock:
            self.request_active = False
            violation = self.active_stdout_protocol_error
            self.active_stdout_line_count = 0
            self.active_stdout_protocol_error = None
        return violation

    def route_stdout_line(self, line: str) -> None:
        with self.state_lock:
            active = self.request_active
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
            return self.idle_stdout_invalidated

    def stderr_position(self) -> int:
        with self.stderr_lock:
            return self.stderr_total_bytes

    def append_stderr(self, chunk: str) -> None:
        if not chunk:
            return
        chunk_bytes = len(chunk.encode("utf-8"))
        if chunk_bytes <= 0:
            return
        with self.stderr_lock:
            start = self.stderr_total_bytes
            self.stderr_chunks.append((start, chunk, chunk_bytes))
            self.stderr_total_bytes += chunk_bytes
            self.stderr_buffer_byte_count += chunk_bytes
            self.stderr_buffer_char_count += len(chunk)
            if self.active_stderr_capture and len(self.active_stderr_excerpt) < _EXCERPT_LIMIT:
                remaining = _EXCERPT_LIMIT - len(self.active_stderr_excerpt)
                self.active_stderr_excerpt += chunk[:remaining]
            while (
                self.stderr_buffer_byte_count > _STDERR_BUFFER_LIMIT
                and self.stderr_chunks
            ):
                _, chunk_text, buffered_bytes = self.stderr_chunks.popleft()
                self.stderr_buffer_byte_count -= buffered_bytes
                self.stderr_buffer_char_count -= len(chunk_text)

    def stderr_since(self, position: int) -> str:
        with self.stderr_lock:
            parts: list[str] = []
            for chunk_start, chunk_text, chunk_bytes in self.stderr_chunks:
                chunk_end = chunk_start + chunk_bytes
                if chunk_end <= position:
                    continue
                parts.append(chunk_text)
            return "".join(parts)

    def capture_stderr_excerpt(self) -> str:
        with self.stderr_lock:
            excerpt = self.active_stderr_excerpt
            self.active_stderr_capture = False
            self.active_stderr_excerpt = ""
            return excerpt


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
        try:
            deadline = started_mono + (timeout_ms / 1000.0)
            cached_process, _ = self._acquire_process(toolchain_id, entrypoint)
            cached_process.begin_request()
            try:
                self._send_request(
                    cached_process=cached_process,
                    request_id=request_id,
                    request_payload=request_payload,
                    artifacts_dir=artifacts_dir,
                )
                response_line = cached_process.wait_for_stdout_item(
                    self._remaining_timeout(deadline)
                )
            except TimeoutError:
                cached_process.finish_request()
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="timeout",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Process timed out",
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )
            except (BrokenPipeError, OSError, ValueError) as exc:
                cached_process.finish_request()
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="process_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=str(exc)[:_EXCERPT_LIMIT],
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            parsed = self._parse_response_line(response_line)
            active_stdout_error = cached_process.finish_request()
            if active_stdout_error is not None:
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=self._build_extra_stdout_error(active_stdout_error),
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )
            if parsed["status"] == "process_exit":
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="process_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Resident process exited unexpectedly",
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            if parsed["status"] == "protocol_error":
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=parsed["error"],
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            envelope = parsed["envelope"]
            outer_request_id = envelope.get("request_id")
            if not isinstance(outer_request_id, str) or outer_request_id != request_id:
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Resident response request_id mismatch",
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            message_type = envelope.get("message_type")
            if message_type == "error":
                error_summary = envelope.get("error")
                if not isinstance(error_summary, str) or not error_summary:
                    error_summary = "Resident process reported an error"
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="process_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary=error_summary,
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            if message_type != "result":
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Resident response message_type is invalid",
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            response_payload = envelope.get("response")
            if not isinstance(response_payload, dict):
                self._discard_cached_process(toolchain_id, cached_process)
                return self._build_result(
                    status="protocol_error",
                    started_at=started_at,
                    started_mono=started_mono,
                    error_summary="Resident response is missing response",
                    stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                    process_instance_id=cached_process.instance_id,
                )

            return self._build_result(
                status="success",
                started_at=started_at,
                started_mono=started_mono,
                response_payload=response_payload,
                stderr_excerpt=self._capture_stderr_excerpt(cached_process),
                process_instance_id=cached_process.instance_id,
            )
        except Exception as exc:
            if cached_process is not None:
                cached_process.finish_request()
                self._discard_cached_process(toolchain_id, cached_process)
                stderr_excerpt = self._capture_stderr_excerpt(cached_process)
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
        self, toolchain_id: str, entrypoint: str
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
            self._stop_process(stale_process)

        created = self._start_process(toolchain_id, entrypoint)
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
                cached_process.append_stderr(chunk)
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
        payload = {
            "message_type": "analyze",
            "request_id": request_id,
            "request": request_payload,
            "artifacts_dir": artifacts_dir,
        }
        stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdin.flush()

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

    def _capture_stderr_excerpt(self, cached_process: _ResidentProcess) -> str:
        last_seen = cached_process.stderr_position()
        for _ in range(10):
            time.sleep(0.01)
            current = cached_process.stderr_position()
            if current == last_seen:
                break
            last_seen = current
        return cached_process.capture_stderr_excerpt()

    @staticmethod
    def _build_extra_stdout_error(extra_line: str) -> str:
        suffix = extra_line[:_EXCERPT_LIMIT]
        if suffix:
            return f"Resident emitted unexpected extra stdout during active request: {suffix}"
        return "Resident emitted unexpected extra stdout during active request"

    def _discard_cached_process(
        self, toolchain_id: str, cached_process: _ResidentProcess
    ) -> None:
        with self._cache_lock:
            current = self._processes.get(toolchain_id)
            if current is cached_process:
                self._processes.pop(toolchain_id, None)
        self._stop_process(cached_process)

    def _pop_cached_process(self, toolchain_id: str) -> _ResidentProcess | None:
        with self._cache_lock:
            return self._processes.pop(toolchain_id, None)

    def _stop_process(self, cached_process: _ResidentProcess) -> None:
        process = cached_process.process
        stdin = process.stdin
        if process.poll() is None and stdin is not None and not stdin.closed:
            try:
                stdin.write(json.dumps({"message_type": "shutdown"}) + "\n")
                stdin.flush()
            except Exception:
                pass

        try:
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass

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
