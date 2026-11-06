"""Hardware-free composition of the production persistence and process engine."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisScheduler, AnalysisSchedulerConfig, AiToolchainSnapshot,
    canonical_toolchain_lane_key, AnalysisCancelOrigin,
)
from app.service.run_persistence import (
    RunRepository, AudioRepository, AttemptRepository, AudioPublication,
    AudioPersistenceService, ProductionRunAudioAdapter, ImportedAudioSaveRequest,
    AnalysisPersistenceBoundary, RunPersistenceSchedulerBridge,
    InitialAnalysisRequest, RunInsert, IngressKind, TriggerSource, TransactionState,
)
from app.service.run_persistence.run_repository import RunTerminal
from app.service.run_persistence.contracts import RunState
from app.service.run_persistence.run_persistence_scheduler_bridge import BridgeCancelRequest
from .contracts import ImportPhase


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def checked(result):
    if result.state is not TransactionState.COMMITTED or result.value is not True:
        raise RuntimeError(result.reason or 'persistence operation did not commit')


class PlaybackShutdownOwner:
    def __init__(self, playback):
        self.playback = playback

    def close(self):
        report = self.playback.shutdown(5.)
        if report.status.value != 'CLEAN':
            raise RuntimeError(f'offline playback shutdown: {report.code}')


class OfflineImportRuntime:
    def __init__(self, *, database, artifact_store, output_directory, production_busy):
        self.database = database
        self.artifacts = artifact_store
        self.output_directory = Path(output_directory)
        self.busy = production_busy
        self.runs = RunRepository(database)
        self.audio = AudioRepository(database)
        self.attempts = AttemptRepository(database, result_artifact_store=artifact_store)
        self.audio_authority = ProductionRunAudioAdapter(
            AudioPersistenceService(AudioPublication(), self.audio, self.runs)
        )
        self.executor = self.bridge = None
        self._lane_key = None
        self._enriched = threading.Event()
        self._enrichment_error = None
        self._enrichment_discarded = False
        self._run_id = None
        self._persistence_run_id = None
        self._run_inserted = False

    def begin_job(self, run_id):
        """Track insertion authority for only the current application job."""
        self._persistence_run_id = run_id
        self._run_inserted = False

    def _enrichment_committed(self, event):
        if event.identity.run_id == self._run_id:
            self._enriched.set()

    def _deliver_enrichment(self, report):
        event = self.boundary.accept_enrichment(report)
        if event is None and report.identity.run_id == self._run_id:
            # A deleted/superseded attempt has no committed-event callback.
            self._enrichment_discarded = True
            self._enriched.set()
        return event

    def record_failure(self, run_id, snapshot, error) -> bool:
        """Retire this job, returning whether its saved record still exists."""
        if run_id != self._persistence_run_id:
            return False  # A late handler has no authority to create a past job.
        run = self.runs.get(run_id)
        if run is None:
            if self._run_inserted:
                return False  # User deletion is terminal for this run identity.
            # Preparation failed before insertion: preserve its durable failure.
            self._insert_run(run_id, snapshot)
            run = self.runs.get(run_id)
        if run is None:
            return False
        if run.state == 'ACTIVE':
            canceled = isinstance(error, InterruptedError)
            checked(self.runs.finish_active(run_id, RunTerminal(
                RunState.CANCELED if canceled else RunState.FAILED,
                'IMPORT_CANCELED' if canceled else 'IMPORT_FAILED',
                str(error), utc_now(),
            )))
        return True

    def _insert_run(self, run_id, snapshot):
        metadata = snapshot.metadata
        envelope = json.loads(snapshot.inputs.config_snapshot_json)
        checked(self.runs.insert_active(RunInsert(
            run_id=run_id, ingress_kind=IngressKind.IMPORTED,
            trigger_source=TriggerSource.IMPORT,
            config_id=envelope['config']['config_id'], hardware_id=None,
            snapshots=snapshot.inputs, accepted_at=utc_now(),
            imported_source_identity=snapshot.source_identity or f'import:{run_id}',
            imported_source_path=str(metadata.path),
            imported_expected_frame_count=metadata.frame_count,
        )))
        self._run_inserted = True

    def _enrichment_failed(self, identity, reason):
        self.boundary.retire_enrichment(identity, reason)
        if identity.run_id == self._run_id:
            self._enrichment_error = reason
            self._enriched.set()

    def _ensure_lane(self, snapshot):
        envelope = json.loads(snapshot.inputs.config_snapshot_json)
        raw = envelope['analysis']['toolchain']
        # Reuse resident models only for identical frozen software inputs.
        key = (
            snapshot.inputs.config_snapshot_json,
            tuple(repr(item) for item in snapshot.golden_artifacts),
        )
        if self.bridge is not None and key == self._lane_key:
            return
        if not self.close():
            raise RuntimeError('previous import analysis lane did not stop')
        toolchains = ()
        if raw is not None:
            toolchains = (AiToolchainSnapshot(
                toolchain_id=raw['toolchain_id'],
                runtime_lane_key=canonical_toolchain_lane_key(raw['toolchain_id']),
                execution_revision=raw['execution_revision'],
                execution_mode=raw['execution_mode'], entrypoint=raw['entrypoint'],
                package_dir=raw['package_dir'], params_json=json.dumps(raw['params']),
                timeout_seconds=raw['timeout_seconds'],
            ),)
        self.boundary = AnalysisPersistenceBoundary(
            runtime=self.database, attempt_repository=self.attempts,
            now_utc=utc_now, enrichment_sink=self._enrichment_committed,
        )
        self.executor = ProcessIsolatedAnalysisExecutor(
            process_count=1, capture_active=self.busy,
            frozen_golden_artifacts={
                (a.config_id, a.channel_index): a for a in snapshot.golden_artifacts
            },
            selected_toolchains=toolchains,
            enrichment_sink=self._deliver_enrichment,
            enrichment_failure_sink=self._enrichment_failed,
        )
        self.boundary.attach_staged_executor(self.executor)
        self.executor.start()
        scheduler = AnalysisScheduler(
            config=AnalysisSchedulerConfig(worker_count=1, pending_capacity=1),
            audio_loader=self.audio_authority, analysis_executor=self.executor,
            start_gate=self.boundary, outcome_sink=self.boundary, clock=time,
        )
        self.bridge = RunPersistenceSchedulerBridge(
            runtime=self.database, run_repository=self.runs,
            audio_repository=self.audio, attempt_repository=self.attempts,
            boundary=self.boundary, scheduler=scheduler, now_utc=utc_now,
        )
        self._lane_key = key

    def run(self, run_id, snapshot, stop, quiet, emit):
        self._run_id = run_id
        self._enriched.clear()
        self._enrichment_error = None
        self._enrichment_discarded = False
        metadata = snapshot.metadata
        self._insert_run(run_id, snapshot)
        try:
            quiet()
            emit(ImportPhase.SAVING)
            self.output_directory.mkdir(parents=True, exist_ok=True)
            result = self.audio_authority.persist_imported(ImportedAudioSaveRequest(
                save_job_id=str(uuid4()),
                run_id=run_id, source_identity=snapshot.source_identity, content_revision=1,
                source_path=str(metadata.path),
                final_path=str((self.output_directory / f'{run_id}.wav').resolve()),
                expected_sample_rate_hz=metadata.sample_rate_hz,
                expected_channel_count=metadata.channel_count,
                expected_frame_count=metadata.frame_count, now=utc_now(),
            ))
            if result.reference is None:
                raise RuntimeError(result.reason or result.code)
            quiet()
            emit(ImportPhase.PREPARING)
            self._ensure_lane(snapshot)
            quiet()
            emit(ImportPhase.ANALYZING)
            terminal = threading.Event()
            reports = []

            def on_terminal(report):
                reports.append(report)
                terminal.set()
            receipt = self.bridge.submit_initial(
                InitialAnalysisRequest(run_id, result.reference), on_terminal
            )
            if not receipt.scheduler_accepted and not reports:
                raise RuntimeError(receipt.reason or receipt.code)
            canceled = False
            while not terminal.wait(.05):
                if stop.is_set() and not canceled:
                    self.bridge.cancel(BridgeCancelRequest(
                        run_id, receipt.analysis_attempt_id,
                        AnalysisCancelOrigin.SCHEDULER_SHUTDOWN, 'application shutdown',
                    ))
                    canceled = True
            report = reports[0]
            if report.judgment is None:
                if stop.is_set():
                    raise InterruptedError('application shutdown')
                raise RuntimeError(report.summary or report.code)
            emit(ImportPhase.COMPLETING_CHARTS, result_ready=True)
            while not self._enriched.wait(.05):
                if stop.is_set():
                    raise InterruptedError('application shutdown during chart completion')
            if self._enrichment_error:
                raise RuntimeError(f'chart completion: {self._enrichment_error}')
            if self._enrichment_discarded:
                emit(
                    ImportPhase.CANCELED,
                    error='Chart enrichment discarded: saved attempt is no longer available.',
                    result_ready=self.runs.get(run_id) is not None,
                    charts_ready=False,
                )
                return  # Discard is terminal; do not recreate a deleted run as failed.
            emit(ImportPhase.COMPLETED, result_ready=True, charts_ready=True)
        except BaseException as exc:
            self.record_failure(run_id, snapshot, exc)
            raise

    def close(self, timeout_seconds=5.):
        if self.bridge is not None:
            report = self.bridge.stop_and_join_scheduler(timeout_seconds)
            if report.status.value != 'SCHEDULER_CLOSED':
                return False
            self.bridge = None
        if self.executor is not None:
            self.executor.close()
            self.executor = None
        self._lane_key = None
        return True
