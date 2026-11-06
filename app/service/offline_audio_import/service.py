"""One background job; UI observers never own its lifetime."""
import threading
import time
from uuid import uuid4
from .contracts import ImportPhase, ImportRequest, ImportStatus, ImportSubmission


class OfflineAudioImportService:
    def __init__(
        self, *, freeze, prepare, runtime_factory,
        production_busy=lambda: False, status_sink=None,
    ):
        self._freeze, self._prepare, self._runtime_factory = freeze, prepare, runtime_factory
        self._busy, self._sink = production_busy, status_sink
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._worker = None
        self._runtime = None
        self._status = None
        self._generation = 0

    @property
    def status(self) -> ImportStatus | None:
        with self._condition:
            return self._status

    def submit(self, request: ImportRequest) -> ImportSubmission:
        with self._condition:
            if self._stop.is_set():
                return ImportSubmission(False, error='import service is closed')
            if self._worker is not None and self._worker.is_alive():
                return ImportSubmission(False, error='one import is already unfinished')
            try:
                snapshot = self._freeze(request)
            except Exception as exc:
                return ImportSubmission(False, error=str(exc))
            run_id = str(uuid4())
            self._emit(run_id, ImportPhase.ACCEPTED)
            self._worker = threading.Thread(
                target=self._run, args=(run_id, snapshot),
                name='offline-audio-import', daemon=True,
            )
            self._worker.start()
            return ImportSubmission(True, run_id)

    def _emit(self, run_id, phase, **kwargs):
        with self._condition:
            self._generation += 1
            previous = self._status
            if previous is not None and previous.run_id == run_id:
                for name in ('result_ready', 'charts_ready', 'warnings'):
                    kwargs.setdefault(name, getattr(previous, name))
            status = ImportStatus(run_id, self._generation, phase, **kwargs)
            self._status = status
            self._condition.notify_all()
            if self._sink is not None:
                try:
                    self._sink(status)
                except Exception:
                    pass  # A closed observer cannot retire the application job.

    def _quiet(self, run_id):
        waiting = False
        while self._busy() and not self._stop.is_set():
            if not waiting:
                self._emit(run_id, ImportPhase.WAITING)
                waiting = True
            self._stop.wait(.05)
        if self._stop.is_set():
            raise InterruptedError('application shutdown')

    def _run(self, run_id, snapshot):
        try:
            if self._runtime is None:
                self._runtime = self._runtime_factory(self._busy)
            begin = getattr(self._runtime, 'begin_job', None)
            if begin is not None:
                begin(run_id)
            self._quiet(run_id)
            self._emit(run_id, ImportPhase.PREPARING)
            prepared = self._prepare(snapshot)
            self._quiet(run_id)
            self._runtime.run(
                run_id, prepared, self._stop, lambda: self._quiet(run_id),
                lambda phase, **kwargs: self._emit(
                    run_id, phase, warnings=prepared.warnings, **kwargs
                ),
            )
        except Exception as exc:
            phase = self.status.phase.value
            detail = f'{phase}: {exc}'
            readiness = {}
            try:
                if self._runtime is None:
                    self._runtime = self._runtime_factory(self._busy)
                    begin = getattr(self._runtime, 'begin_job', None)
                    if begin is not None:
                        begin(run_id)
                retire = getattr(self._runtime, 'record_failure', None)
                if retire is not None:
                    if retire(run_id, snapshot, exc) is False:
                        readiness = {'result_ready': False, 'charts_ready': False}
            except Exception as retirement_error:
                detail += f'; failure persistence: {retirement_error}'
            self._emit(
                run_id,
                ImportPhase.CANCELED if isinstance(exc, InterruptedError) else ImportPhase.FAILED,
                error=detail, **readiness,
            )

    def wait_for_phase(self, phase: ImportPhase, timeout=10) -> ImportStatus:
        with self._condition:
            if not self._condition.wait_for(
                lambda: self._status is not None and self._status.phase is phase,
                timeout,
            ):
                raise TimeoutError(f'expected {phase}, observed {self._status}')
            return self._status

    def close(self, timeout_seconds=5.) -> bool:
        self._stop.set()
        deadline = time.monotonic() + timeout_seconds
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0, deadline - time.monotonic()))
        if worker is not None and worker.is_alive():
            return False
        if self._runtime is not None:
            return self._runtime.close(max(.001, deadline - time.monotonic()))
        return True


class ImportShutdownOwner:
    """Adapt bounded service cleanup to the application's None-on-clean contract."""
    def __init__(self, service):
        self.service = service

    def close(self):
        if not self.service.close():
            raise TimeoutError('offline import worker did not stop')
