"""Concrete execution tracking and shutdown resource snapshots."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .pool_types import ShutdownResourceSnapshot


@dataclass(frozen=True, slots=True)
class _ExecutionIdentity:
    hardware_id: str | None = None
    stream_generation: int | None = None
    run_id: str | None = None


@dataclass(slots=True)
class _ExecutionResource:
    kind: str
    worker: threading.Thread
    identities: tuple[_ExecutionIdentity, ...] = ()


class ResidentPoolResourceMixin:
    def _register_execution_locked(
        self, event, kind, worker, identities=()
    ) -> None:
        self._harvest_finished_workers_locked()
        self._execution_resources[event] = _ExecutionResource(
            kind, worker, tuple(identities)
        )

    def _unregister_execution_locked(self, event) -> None:
        self._execution_resources.pop(event, None)

    def _identify_current_execution_locked(
        self,
        *,
        kind,
        hardware_id=None,
        stream_generation=None,
        run_id=None,
    ) -> None:
        worker = threading.current_thread()
        for resource in self._execution_resources.values():
            if resource.worker is worker:
                resource.kind = kind
                resource.identities = (
                    _ExecutionIdentity(
                        hardware_id, stream_generation, run_id
                    ),
                )
                return

    def _register_control_worker_locked(
        self, worker, kind, identities=()
    ) -> None:
        self._control_worker_resources[worker] = _ExecutionResource(
            kind, worker, tuple(identities)
        )

    def _unregister_control_worker_locked(self, worker) -> None:
        self._control_worker_resources.pop(worker, None)

    def _execution_identities_for_run_ids_locked(self, run_ids):
        identities = []
        for run_id in run_ids:
            record = self._leases.get(run_id)
            identity = None if record is None else record.owner_instance_id
            identities.append(
                _ExecutionIdentity(
                    getattr(identity, "hardware_id", None),
                    None if record is None else record.stream_generation,
                    run_id,
                )
            )
        return tuple(identities)

    @staticmethod
    def _stream_resource_identity(stream):
        snapshot = stream.snapshot()
        return _ExecutionIdentity(
            snapshot.hardware_id,
            snapshot.stream_generation,
        )

    def _collect_live_shutdown_resources(self):
        with self._lock:
            streams = [
                tracked.stream
                for tracked in self._shutdown_close_resources.values()
            ]
            streams.extend(self._shutdown_pending_streams.values())
            streams.extend(
                tracked.stream
                for tracked in self._unpublished_closes.values()
            )
            streams.extend(
                tracked.record.stream
                for tracked in self._closing_streams.values()
            )
        snapshots = []
        for stream in {id(item): item for item in streams}.values():
            try:
                snapshot = stream.snapshot()
            except Exception:
                continue
            if snapshot.handle_open or snapshot.ingest_thread_alive:
                snapshots.append(
                    ShutdownResourceSnapshot(
                        hardware_id=snapshot.hardware_id,
                        stream_generation=snapshot.stream_generation,
                        thread_name=(
                            snapshot.ingest_thread_name
                            or "resident-audio-handle-control"
                        ),
                        thread_alive=snapshot.ingest_thread_alive,
                        handle_open=snapshot.handle_open,
                    )
                )
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (
                    item.hardware_id or "",
                    item.stream_generation or 0,
                    item.thread_name,
                ),
            )
        )

    @staticmethod
    def _execution_snapshots(resource):
        if not resource.worker.is_alive():
            return ()
        identities = resource.identities or (_ExecutionIdentity(),)
        return tuple(
            ShutdownResourceSnapshot(
                hardware_id=identity.hardware_id,
                stream_generation=identity.stream_generation,
                thread_name=resource.worker.name,
                thread_alive=True,
                handle_open=False,
                resource_kind=resource.kind,
                run_id=identity.run_id,
            )
            for identity in identities
        )

    def _collect_shutdown_resources(self):
        resources = list(self._collect_live_shutdown_resources())
        for work in (
            self._artifact_cleanup_work,
            self._force_terminate_work,
        ):
            if work is not None:
                incomplete = (
                    work.worker.is_alive()
                    or not work.settled.is_set()
                    or not work.harvested
                    or work.exception_type is not None
                    or (
                        work.kind == "backend-force-terminate"
                        and work.result is not True
                    )
                )
                if incomplete:
                    resources.append(
                        ShutdownResourceSnapshot(
                            hardware_id=None,
                            stream_generation=None,
                            thread_name=work.worker.name,
                            thread_alive=work.worker.is_alive(),
                            handle_open=False,
                            resource_kind=work.kind,
                            exception_type=work.exception_type,
                        )
                    )
        owner_resources = []
        with self._lock:
            executions = tuple(self._execution_resources.values())
            controls = tuple(self._control_worker_resources.values())
            for record in self._leases.values():
                worker = record.commit_worker
                if (
                    record.state.value == "COMMITTING"
                    and worker is not None
                    and worker.is_alive()
                ):
                    resources.append(
                        ShutdownResourceSnapshot(
                            hardware_id=getattr(
                                record.owner_instance_id,
                                "hardware_id",
                                None,
                            ),
                            stream_generation=record.stream_generation,
                            thread_name=worker.name,
                            thread_alive=True,
                            handle_open=False,
                            resource_kind="commit",
                            run_id=record.run_id,
                        )
                    )
                owner_resources.append(
                    (
                        record.owner,
                        getattr(
                            record.owner_instance_id, "hardware_id", None
                        ),
                        record.stream_generation,
                        record.run_id,
                    )
                )
        for execution in executions + controls:
            resources.extend(self._execution_snapshots(execution))
        for owner, hardware_id, stream_generation, run_id in owner_resources:
            try:
                snapshot = None if owner is None else owner.snapshot()
            except Exception:
                continue
            if snapshot is not None and snapshot.drain_thread_alive:
                resources.append(
                    ShutdownResourceSnapshot(
                        hardware_id=hardware_id,
                        stream_generation=stream_generation,
                        thread_name=snapshot.drain_thread_name,
                        thread_alive=True,
                        handle_open=False,
                        resource_kind="drain",
                        run_id=run_id,
                    )
                )
        return tuple(resources)


__all__ = ["ResidentPoolResourceMixin", "_ExecutionIdentity"]
