from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass

from .contracts import (
    AudioSaveOutcome,
    AudioSaveRequest,
    AudioSaveStatus,
    CapturedAudioSaveRequest,
    ImportedAudioSaveRequest,
    SaveCancelResult,
    SaveCancelStatus,
    SaveCancelToken,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    SaveSubmission,
    SaveSubmitStatus,
    TerminalErrorCategory,
)
from .ports import CapturedAudioSaver, ImportedAudioSaver, SaveOutcomeSink


@dataclass(frozen=True, slots=True)
class _SaveJob:
    request: AudioSaveRequest
    sink: SaveOutcomeSink
    cancel_token: SaveCancelToken


_SHUTDOWN_SENTINEL = object()


class BoundedAudioSaveDispatcher:
    def __init__(
        self,
        captured_saver: CapturedAudioSaver,
        imported_saver: ImportedAudioSaver,
        worker_count: int = 2,
        outstanding_capacity: int = 6,
    ) -> None:
        if (
            not isinstance(worker_count, int)
            or isinstance(worker_count, bool)
            or not 1 <= worker_count <= 6
        ):
            raise ValueError("worker_count must be an integer from 1 to 6")
        if (
            not isinstance(outstanding_capacity, int)
            or isinstance(outstanding_capacity, bool)
            or not worker_count <= outstanding_capacity <= 6
        ):
            raise ValueError(
                "outstanding_capacity must be between worker_count and 6"
            )

        self._captured_saver = captured_saver
        self._imported_saver = imported_saver
        self._lock = threading.Lock()
        self._outstanding_semaphore = threading.BoundedSemaphore(
            outstanding_capacity
        )
        self._queue: queue.Queue[_SaveJob | object] = queue.Queue()
        self._outstanding: dict[str, _SaveJob] = {}
        self._completed: dict[str, str] = {}
        self._errors: list[str] = []
        self._admission_closed = False
        self._shutdown_started = False
        self._accepted_count = 0
        self._canceled_count = 0
        self._completed_count = 0
        self._workers = tuple(
            threading.Thread(
                target=self._worker_main,
                name=f"audio-save-worker-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        )
        for worker in self._workers:
            worker.start()

    def try_submit(
        self,
        request: AudioSaveRequest,
        sink: SaveOutcomeSink,
    ) -> SaveSubmission:
        with self._lock:
            if self._admission_closed:
                return self._submission(
                    request,
                    SaveSubmitStatus.CLOSED,
                    "audio save dispatcher is closed",
                )
        if not self._outstanding_semaphore.acquire(blocking=False):
            return self._submission(
                request,
                SaveSubmitStatus.SATURATED,
                "audio save capacity is full",
            )

        job = _SaveJob(
            request=request,
            sink=sink,
            cancel_token=SaveCancelToken(),
        )
        with self._lock:
            if self._admission_closed:
                self._outstanding_semaphore.release()
                return self._submission(
                    request,
                    SaveSubmitStatus.CLOSED,
                    "audio save dispatcher is closed",
                )
            if (
                request.save_job_id in self._outstanding
                or request.save_job_id in self._completed
            ):
                self._outstanding_semaphore.release()
                return self._submission(
                    request,
                    SaveSubmitStatus.INVALID,
                    "duplicate save job ID",
                )
            self._outstanding[request.save_job_id] = job
            self._accepted_count += 1
            self._queue.put(job)
        return self._submission(request, SaveSubmitStatus.ACCEPTED, "")

    def cancel(
        self,
        run_id: str,
        save_job_id: str,
    ) -> SaveCancelResult:
        with self._lock:
            job = self._outstanding.get(save_job_id)
            if job is not None and job.request.run_id == run_id:
                if not job.cancel_token.is_cancel_requested():
                    job.cancel_token._request_cancel()
                    self._canceled_count += 1
                status = SaveCancelStatus.CANCEL_REQUESTED
            elif self._completed.get(save_job_id) == run_id:
                status = SaveCancelStatus.ALREADY_TERMINAL
            else:
                status = SaveCancelStatus.NOT_FOUND
        return SaveCancelResult(
            status=status,
            run_id=run_id,
            save_job_id=save_job_id,
            reason="",
        )

    def shutdown(
        self,
        deadline_monotonic: float,
    ) -> SaveDispatcherShutdownReport:
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise ValueError("deadline_monotonic must be a finite number")

        with self._lock:
            if not self._shutdown_started:
                self._admission_closed = True
                for job in self._outstanding.values():
                    if not job.cancel_token.is_cancel_requested():
                        job.cancel_token._request_cancel()
                        self._canceled_count += 1
                for _worker in self._workers:
                    self._queue.put(_SHUTDOWN_SENTINEL)
                self._shutdown_started = True

        current_worker = threading.current_thread()
        for worker in self._workers:
            if worker is current_worker:
                continue
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            worker.join(min(remaining, threading.TIMEOUT_MAX))

        with self._lock:
            remaining_job_ids = tuple(self._outstanding)
            live_worker_ids = tuple(
                worker.name for worker in self._workers if worker.is_alive()
            )
            status = (
                SaveDispatcherShutdownStatus.CLOSED
                if not remaining_job_ids and not live_worker_ids
                else SaveDispatcherShutdownStatus.INCOMPLETE
            )
            return SaveDispatcherShutdownReport(
                status=status,
                accepted_count=self._accepted_count,
                canceled_count=self._canceled_count,
                completed_count=self._completed_count,
                remaining_save_job_ids=remaining_job_ids,
                live_worker_ids=live_worker_ids,
                errors=tuple(self._errors),
            )

    @staticmethod
    def _submission(
        request: AudioSaveRequest,
        status: SaveSubmitStatus,
        reason: str,
    ) -> SaveSubmission:
        return SaveSubmission(
            status=status,
            run_id=request.run_id,
            save_job_id=request.save_job_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            reason=reason,
        )

    def _worker_main(self) -> None:
        while True:
            job = self._queue.get()
            if job is _SHUTDOWN_SENTINEL:
                self._queue.task_done()
                return
            assert isinstance(job, _SaveJob)
            try:
                try:
                    outcome = self._route_to_exact_saver(job)
                except Exception as exc:
                    reason = self._bounded_exception_message(
                        "save_saver_exception",
                        exc,
                    )
                    self._record_error("save_saver_exception", exc)
                    outcome = self._failed_outcome(
                        job.request,
                        reason,
                    )
                outcome = self._validate_or_replace_outcome(
                    job.request,
                    outcome,
                )
                try:
                    job.sink(outcome)
                except Exception as exc:
                    self._record_error("save_sink_exception", exc)
            finally:
                with self._lock:
                    self._outstanding.pop(job.request.save_job_id, None)
                    self._completed[job.request.save_job_id] = job.request.run_id
                    self._completed_count += 1
                self._outstanding_semaphore.release()
                self._queue.task_done()

    def _route_to_exact_saver(self, job: _SaveJob) -> AudioSaveOutcome:
        if isinstance(job.request, CapturedAudioSaveRequest):
            return self._captured_saver.save_captured(
                job.request,
                job.cancel_token,
            )
        if isinstance(job.request, ImportedAudioSaveRequest):
            return self._imported_saver.save_imported(
                job.request,
                job.cancel_token,
            )
        raise TypeError("unsupported audio save request")

    def _validate_or_replace_outcome(
        self,
        request: AudioSaveRequest,
        outcome: object,
    ) -> AudioSaveOutcome:
        mismatch = not isinstance(outcome, AudioSaveOutcome)
        if not mismatch:
            mismatch = (
                outcome.save_job_id != request.save_job_id
                or outcome.run_id != request.run_id
                or outcome.source_kind is not request.source_kind
                or outcome.source_identity != request.source_identity
            )
        if (
            not mismatch
            and outcome.status is AudioSaveStatus.SAVED
        ):
            persisted = outcome.persisted_audio
            mismatch = (
                persisted is None
                or persisted.final_path != request.final_path_request.final_path
                or persisted.sample_rate_hz
                != request.metadata.expected_sample_rate_hz
                or persisted.channel_count
                != request.metadata.expected_channel_count
                or persisted.frame_count
                != request.metadata.expected_frame_count
                or persisted.format is not request.metadata.format
            )
        if mismatch:
            message = "saver outcome identity mismatch"
            self._record_error(
                "save_outcome_mismatch",
                ValueError(message),
            )
            return self._failed_outcome(request, message)
        return outcome

    @staticmethod
    def _failed_outcome(
        request: AudioSaveRequest,
        reason: str,
    ) -> AudioSaveOutcome:
        return AudioSaveOutcome(
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            status=AudioSaveStatus.FAILED,
            persisted_audio=None,
            error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
            reason=reason,
        )

    @staticmethod
    def _bounded_exception_message(stage: str, exc: Exception) -> str:
        fallback = "save_exception_format_failure: Exception"
        try:
            try:
                detail = str(exc)
            except Exception:
                detail = "<unprintable exception>"
            message = f"{stage}: {type(exc).__name__}: {detail}"
            message = message.replace("\x00", "\ufffd")
            encoded = message.encode("utf-8", errors="replace")
            if len(encoded) > 512:
                encoded = encoded[:512]
            return encoded.decode("utf-8", errors="ignore")
        except Exception:
            return fallback

    def _record_error(self, stage: str, exc: Exception) -> None:
        try:
            message = self._bounded_exception_message(stage, exc)
            with self._lock:
                if len(self._errors) < 32:
                    self._errors.append(message)
        except Exception:
            return
