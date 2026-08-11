"""Run completed-recording analysis outside the capture process."""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import queue
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.golden_reference import FrozenGoldenArtifact
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisJob,
    AnalysisStage,
    FrozenResultObject,
)
from app.service.analysis.thread_priority import (
    IDLE_PRIORITY_CLASS,
    THREAD_PRIORITY_BELOW_NORMAL,
    set_current_process_priority,
    set_current_thread_priority,
)

_CANONICAL_TRANSPORT = "canonical-json-zlib-base64/v1"
_TRANSPORT_CHUNK_SIZE = 60_000
_TRANSPORT_ZLIB_LEVEL = 3
_DEFAULT_PROCESS_COUNT = 2


@dataclass(slots=True)
class _ProcessAudio:
    samples: np.ndarray
    sample_rate_hz: int
    channel_count: int
    frame_count: int

    def close(self) -> None:
        return None


class _ProcessCancellationToken:
    is_cancelled = False
    reason = None

    @staticmethod
    def wait(timeout_seconds=None) -> bool:
        del timeout_seconds
        return False


@dataclass(slots=True)
class _AnalysisProcessSlot:
    connection: object
    process: object
    ready: bool = False


def _process_error(summary: str) -> AnalysisExecutionError:
    return AnalysisExecutionError(
        AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            stage=AnalysisStage.EXECUTE,
            summary=summary[:1024],
        )
    )


def _plain_frozen_value(value: object) -> object:
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str):
        return value
    if value_type is tuple:
        return [_plain_frozen_value(item) for item in value]
    if value_type is FrozenResultObject:
        return {
            key: _plain_frozen_value(item)
            for key, item in value.items
        }
    raise TypeError(f"unsupported frozen result value {value_type.__name__}")


def _pack_result_for_transport(
    result: AnalysisExecutionResult,
) -> AnalysisExecutionResult:
    """Collapse the large result tree before it crosses into the UI process."""

    if result.status is not AnalysisExecutionStatus.SUCCEEDED:
        return result
    root = _plain_frozen_value(result.result_payload)
    if type(root) is not dict:
        raise TypeError("analysis result root is not an object")
    judgment = root.get("judgment")
    if judgment not in {"PASS", "FAIL"}:
        raise ValueError("analysis result judgment is invalid")
    canonical = json.dumps(
        root,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.b64encode(
        zlib.compress(canonical, level=_TRANSPORT_ZLIB_LEVEL)
    ).decode("ascii")
    chunks = tuple(
        encoded[offset : offset + _TRANSPORT_CHUNK_SIZE]
        for offset in range(0, len(encoded), _TRANSPORT_CHUNK_SIZE)
    )
    envelope = FrozenResultObject(
        (
            ("__transport__", _CANONICAL_TRANSPORT),
            ("judgment", judgment),
            ("chunks", chunks),
        )
    )
    return AnalysisExecutionResult(
        AnalysisExecutionStatus.SUCCEEDED,
        envelope,
    )


def _analysis_process_main(
    connection,
    golden_artifacts: Mapping[tuple[int, int], FrozenGoldenArtifact],
) -> None:
    """Own the heavy DSP runtime for the lifetime of the production graph."""

    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"
    set_current_process_priority(IDLE_PRIORITY_CLASS)
    set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)

    try:
        executor = ProductionAnalysisExecutor(
            frozen_golden_artifacts=golden_artifacts,
        )
        try:
            dsp.preload_loudness_backend()
        except Exception:
            # The actual analysis reports a backend error only when that
            # calculation is required; warm-up must not prevent other DSP.
            pass
        connection.send(("ready", None))
        while True:
            message = connection.recv()
            if not isinstance(message, tuple) or not message:
                continue
            if message[0] == "stop":
                return
            if message[0] != "execute" or len(message) != 6:
                continue

            _, job, samples, sample_rate_hz, channel_count, frame_count = message
            audio = _ProcessAudio(
                samples=np.asarray(samples),
                sample_rate_hz=int(sample_rate_hz),
                channel_count=int(channel_count),
                frame_count=int(frame_count),
            )
            try:
                result = executor.execute(
                    job,
                    audio,
                    _ProcessCancellationToken(),
                )
                result = _pack_result_for_transport(result)
            except AnalysisExecutionError as exc:
                connection.send(("analysis_error", exc.error))
            except BaseException as exc:
                detail = str(exc).strip()
                connection.send(
                    (
                        "unexpected_error",
                        (
                            f"analysis process failed: {type(exc).__name__}"
                            + (f": {detail}" if detail else "")
                        )[:1024],
                    )
                )
            else:
                connection.send(("result", result))
    except EOFError:
        return
    except BaseException as exc:
        try:
            detail = str(exc).strip()
            connection.send(
                (
                    "startup_error",
                    (
                        f"analysis process startup failed: {type(exc).__name__}"
                        + (f": {detail}" if detail else "")
                    )[:1024],
                )
            )
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


class ProcessIsolatedAnalysisExecutor:
    """Resident process pool used by the parallel analysis scheduler."""

    validates_result_out_of_process = True

    def __init__(
        self,
        *,
        frozen_golden_artifacts: Mapping[
            tuple[int, int], FrozenGoldenArtifact
        ] | None = None,
        capture_active: Callable[[], bool] | None = None,
        process_count: int = _DEFAULT_PROCESS_COUNT,
    ) -> None:
        if type(process_count) is not int:
            raise TypeError("process_count must be an exact int")
        if not 1 <= process_count <= 6:
            raise ValueError("process_count must be between 1 and 6")
        self._golden = dict(frozen_golden_artifacts or {})
        self._capture_active = capture_active
        self._process_count = process_count
        self._state_lock = threading.Lock()
        self._available: queue.Queue[_AnalysisProcessSlot] = queue.Queue(
            maxsize=process_count
        )
        self._slots: tuple[_AnalysisProcessSlot, ...] = ()
        self._closed = False

    def start(self) -> None:
        """Start warming the child without waiting on the UI or capture path."""

        with self._state_lock:
            if self._closed or self._slots:
                return
            context = multiprocessing.get_context("spawn")
            created: list[_AnalysisProcessSlot] = []
            try:
                for index in range(self._process_count):
                    parent_connection, child_connection = context.Pipe(
                        duplex=True
                    )
                    process = context.Process(
                        target=_analysis_process_main,
                        args=(child_connection, self._golden),
                        name=f"production-analysis-process-{index + 1}",
                        daemon=True,
                    )
                    try:
                        process.start()
                    except BaseException:
                        parent_connection.close()
                        raise
                    finally:
                        child_connection.close()
                    created.append(
                        _AnalysisProcessSlot(parent_connection, process)
                    )
            except BaseException:
                self._stop_slots(tuple(created))
                raise
            self._slots = tuple(created)
            for slot in self._slots:
                self._available.put_nowait(slot)

    def execute(
        self,
        job: AnalysisJob,
        audio,
        cancel_token,
    ) -> AnalysisExecutionResult:
        del cancel_token
        self.start()
        self._wait_for_capture_quiet()
        slot = self._acquire_slot()
        try:
            if self._closed:
                raise _process_error("analysis process is closed")
            connection = slot.connection
            process = slot.process

            self._wait_until_ready(slot)
            try:
                connection.send(
                    (
                        "execute",
                        job,
                        np.asarray(audio.samples),
                        int(audio.sample_rate_hz),
                        int(audio.channel_count),
                        int(audio.frame_count),
                    )
                )
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise _process_error("analysis process is unavailable") from exc

            while True:
                if connection.poll(0.05):
                    # The child may finish while the next device is recording.
                    # Delay result deserialization and UI projection until the
                    # capture group is quiet as well.
                    self._wait_for_capture_quiet()
                    try:
                        response = connection.recv()
                    except (EOFError, OSError) as exc:
                        raise _process_error(
                            "analysis process ended before returning a result"
                        ) from exc
                    return self._decode_response(response)
                if not process.is_alive():
                    raise _process_error(
                        "analysis process ended before returning a result"
                    )
        finally:
            with self._state_lock:
                if not self._closed and any(
                    candidate is slot for candidate in self._slots
                ):
                    self._available.put_nowait(slot)

    def _acquire_slot(self) -> _AnalysisProcessSlot:
        while True:
            with self._state_lock:
                if self._closed:
                    raise _process_error("analysis process is closed")
                started = bool(self._slots)
            if not started:
                raise _process_error("analysis process did not start")
            try:
                return self._available.get(timeout=0.05)
            except queue.Empty:
                continue

    def _wait_for_capture_quiet(self) -> None:
        """Keep analysis transfer and DSP behind the current recording group."""

        capture_active = self._capture_active
        if capture_active is None:
            return
        quiet_since = None
        while True:
            if capture_active():
                quiet_since = None
            else:
                now = time.monotonic()
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= 0.25:
                    return
            time.sleep(0.02)

    def _wait_until_ready(self, slot: _AnalysisProcessSlot) -> None:
        if slot.ready:
            return
        connection = slot.connection
        process = slot.process
        while True:
            if connection.poll(0.05):
                try:
                    response = connection.recv()
                except (EOFError, OSError) as exc:
                    raise _process_error(
                        "analysis process failed during startup"
                    ) from exc
                if (
                    isinstance(response, tuple)
                    and len(response) == 2
                    and response[0] == "ready"
                ):
                    slot.ready = True
                    return
                self._decode_response(response)
            if not process.is_alive():
                raise _process_error("analysis process failed during startup")

    @staticmethod
    def _decode_response(response) -> AnalysisExecutionResult:
        if not isinstance(response, tuple) or len(response) != 2:
            raise _process_error("analysis process returned an invalid response")
        kind, payload = response
        if kind == "result" and isinstance(payload, AnalysisExecutionResult):
            return payload
        if kind == "analysis_error" and isinstance(payload, AnalysisError):
            raise AnalysisExecutionError(payload)
        if kind in {"startup_error", "unexpected_error"}:
            raise _process_error(str(payload))
        raise _process_error("analysis process returned an invalid response")

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            slots = self._slots
            self._slots = ()
            while True:
                try:
                    self._available.get_nowait()
                except queue.Empty:
                    break
        self._stop_slots(slots)

    @staticmethod
    def _stop_slots(slots: tuple[_AnalysisProcessSlot, ...]) -> None:
        for slot in slots:
            connection = slot.connection
            if connection is not None:
                try:
                    connection.send(("stop",))
                except Exception:
                    pass
        graceful_deadline = time.monotonic() + 5.0
        for slot in slots:
            process = slot.process
            if process is not None:
                process.join(max(0.0, graceful_deadline - time.monotonic()))
        alive = [
            slot.process
            for slot in slots
            if slot.process is not None and slot.process.is_alive()
        ]
        for process in alive:
            process.terminate()
        terminate_deadline = time.monotonic() + 2.0
        for process in alive:
            process.join(max(0.0, terminate_deadline - time.monotonic()))
        for slot in slots:
            connection = slot.connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


__all__ = ["ProcessIsolatedAnalysisExecutor"]
